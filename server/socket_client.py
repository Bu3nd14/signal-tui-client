"""Unix-socket :class:`~server.client.ChatClient` (the TUI's remote transport).

Speaks the wire contract of ``docs/DESIGN_CLIENT_SERVER_SPLIT.md`` §4.2 to
:mod:`server.socket_adapter`.  Use :meth:`SocketClient.connect`::

    client = SocketClient.connect()          # $MEGACHAT_SOCKET, XDG, ~/.local
    client.call("server.status")
    sub = client.subscribe()                 # same consumer API as in-process
    event = sub.get(timeout=0.1)

Threading.  One socket, one *supervisor* thread that reads it (and reconnects),
any number of caller threads.  ``call`` is thread safe: each request gets a
fresh id, responses are matched by id and may arrive in any order.  Events are
routed to subscriptions by the reader thread, in wire order; a response and
the events around it are therefore processed in the order the server wrote
them.

Errors.  Everything raised is an :class:`~server.api.ApiError`:

- the server's own errors come back as ``ApiError(code, message, details)``
  with the same code the in-process client would raise;
- :class:`ServerUnavailable` (``code == "server_unavailable"``, client side
  only, not an operation failure): the connection is down, refused, reset,
  silent for too long, or the client is closed.  A call that was in flight
  when the connection dropped raises it too, and its outcome is **unknown**
  (the server may have executed it);
- :class:`OperationTimeout` (``code == "timeout"``): the server (or the
  client, if the server stayed silent) gave up waiting.  The operation is
  **not cancelled** and may still complete: treat a timed-out mutation as
  unknown and re-sync;
- :class:`IncompatibleServer` (``code == "incompatible_server"``): the
  server speaks another wire ``api_version``; reconnecting cannot help.

Reconnect and resume.  When the connection is lost (EOF, error, or no traffic
for ``heartbeat_timeout`` seconds) every pending call fails with
:class:`ServerUnavailable`, subscribers get a ``client_connection`` event
(``lost``) and the supervisor reconnects in the background with a bounded,
jittered backoff.  New calls fail immediately while disconnected.  On success
the client re-checks the version, re-subscribes with ``since=<last seq
seen>`` and emits ``client_connection`` (``restored``); events published in
the meantime are replayed.  If the server cannot replay them (``gap`` event)
or it restarted (runtime-file pid/``started_at`` changed or its sequence
numbers went backwards) the subscribers receive a ``gap`` event instead
(reason ``restart`` for a restart): they must resync.

Synthetic event ``client_connection`` (never sent by the server): payload
``{"state": "lost" | "restored", "reason": str}``; its ``seq`` is the last
server seq seen (it does not advance the stream).  Consumers that do not know
it ignore it, like any unknown type.

Attachments.  ``messages.send`` takes absolute local file paths in
``attachments``; they are passed as they are and the server copies them
(staging); nothing is read client-side.

This module must not import Textual, FastAPI, ``tui``, ``web`` or
``protocols``.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import itertools
import json
import logging
import os
import random
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from server.api import ERROR_CODES, ApiError
from server.bus import GAP, Event
from server.client import ChatClient, ClientSubscription
from server.paths import default_socket_path, runtime_file_path
from server.version import API_VERSION

logger = logging.getLogger(__name__)

__all__ = [
    "CONNECTION_EVENT",
    "IncompatibleServer",
    "OperationTimeout",
    "ServerUnavailable",
    "SocketClient",
]

#: Type of the synthetic connection-state event.
CONNECTION_EVENT = "client_connection"

#: Wire constants of the adapter (``tests/test_socket_client.py`` pins them to
#: the adapter's, which this module does not import).
_RPC_TIMEOUT = -32020
_RPC_TOO_MANY_REQUESTS = -32030
_RPC_TOO_MANY_CONNECTIONS = -32031
_RPC_PROTOCOL_CODES = (-32700, -32600, -32602)
_MAX_TIMEOUT_S = 600.0
HEARTBEAT_INTERVAL_S = 5.0

#: Socket poll period of the reader: bounds how late liveness and shutdown
#: are noticed.
_TICK_S = 0.25
#: Extra seconds the client waits for an answer after the server-side timeout.
_RESPONSE_GRACE_S = 5.0
#: A stalled write drops the connection after this long (as the server does).
_WRITE_TIMEOUT_S = 30.0
_PING_WRITE_TIMEOUT_S = 2.0
#: Longest response line the client accepts.
_MAX_LINE_BYTES = 256 * 1024 * 1024
#: Per-subscription queue bound (overflow -> ``gap``), and replay ring size.
_SUB_QUEUE = 1000
_RING = 2000
_HANDSHAKE_TIMEOUT_S = 10.0
#: A connection that lived this long (seconds) counts as stable.
_STABLE_S = 2.0


# -- errors -----------------------------------------------------------------------


class _ClientError(ApiError):
    """Client-side error: an :class:`ApiError` whose code is not one the server
    sends (so it bypasses the registry's code check)."""

    CODE: ClassVar[str]

    def __init__(self, message: str, details: Any = None) -> None:
        Exception.__init__(self, message)
        self.code = self.CODE
        self.message = message
        self.details = details

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.message!r})"


class ServerUnavailable(_ClientError):
    """No usable connection to the server (see the module docstring)."""

    CODE = "server_unavailable"


class OperationTimeout(_ClientError):
    """The answer did not arrive in time; the operation may still complete."""

    CODE = "timeout"


class IncompatibleServer(_ClientError):
    """The server's ``api_version`` differs from this client's."""

    CODE = "incompatible_server"


# -- plumbing ---------------------------------------------------------------------


class _Pending:
    """One in-flight request: a one-shot result that can be waited on from a
    thread or an event loop."""

    def __init__(
        self,
        method: str,
        conn: _Conn | None,
        req_id: int,
        hook: Callable[[Any], None] | None = None,
    ) -> None:
        self.method = method
        self.conn = conn
        self.id = req_id
        self.hook = hook
        self.event = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[], None]] = []

    @property
    def done(self) -> bool:
        return self.event.is_set()

    def resolve(self, result: Any = None, error: BaseException | None = None) -> None:
        with self._lock:
            if self.event.is_set():
                return
            self.result, self.error = result, error
            self.event.set()
            callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            with contextlib.suppress(Exception):
                callback()

    def add_callback(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if not self.event.is_set():
                self._callbacks.append(callback)
                return
        callback()

    def value(self) -> Any:
        if self.error is not None:
            raise self.error
        return self.result


class _Conn:
    """One connected, handshaken socket and what belongs to it."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.write_lock = threading.Lock()
        self.buf = bytearray()
        self.scan = 0
        self.pending: dict[int, _Pending] = {}
        self.dead = False
        #: Server-side event stream is on for this connection.
        self.streaming = False
        self.last_rx = time.monotonic()
        self.last_tx = time.monotonic()
        self.ping: _Pending | None = None
        self.published = time.monotonic()
        #: Filled by the handshake (``_open``): the ``rpc.hello`` result, the
        #: server identity (runtime file), whether it differs from the previous
        #: server, and what the resumed ``events.subscribe`` asked/answered.
        self.hello: dict[str, Any] = {}
        self.identity: tuple[Any, Any] | None = None
        self.restarted = False
        self.resumed = False
        self.stream_since: int | None = None
        self.stream_last = 0

    def take_line(self) -> bytes | None:
        """Pop one complete line from the buffer (``None`` if there is none)."""
        index = self.buf.find(b"\n", self.scan)
        if index < 0:
            self.scan = len(self.buf)
            return None
        line = bytes(self.buf[:index])
        del self.buf[: index + 1]
        self.scan = 0
        return line

    def fill(self) -> bool:
        """One ``recv``; ``False`` on a poll timeout, raises on EOF/error."""
        try:
            chunk = self.sock.recv(262144)
        except TimeoutError:
            return False
        if not chunk:
            raise ConnectionError("server closed the connection")
        self.last_rx = time.monotonic()
        self.buf += chunk
        if len(self.buf) - self.scan > _MAX_LINE_BYTES:
            raise ConnectionError("response line too large")
        return True

    def shutdown(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self.sock.close()


class _SocketSubscription(ClientSubscription):
    """Bounded event queue fed by the reader thread (mirrors the bus's)."""

    def __init__(self) -> None:
        self._queue: collections.deque[Event] = collections.deque()
        self._cond = threading.Condition()
        self._closed = False

    def _push(self, event: Event) -> None:
        with self._cond:
            if self._closed:
                return
            if len(self._queue) >= _SUB_QUEUE:
                self._queue.clear()
                self._queue.append(
                    Event(event.seq, GAP, {"reason": "overflow"}, event.ts)
                )
            else:
                self._queue.append(event)
            self._cond.notify()

    def get(self, timeout: float | None = None) -> Event | None:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while not self._queue and not self._closed:
                if deadline is None:
                    self._cond.wait()
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._cond.wait(remaining)
            if self._closed:
                return None
            return self._queue.popleft()

    def drain(self) -> list[Event]:
        with self._cond:
            if self._closed:
                return []
            events = list(self._queue)
            self._queue.clear()
            return events

    async def aget(self, timeout: float | None = None) -> Event | None:
        return await asyncio.to_thread(self.get, timeout)

    def close(self) -> None:
        # Owned and cleaned up by the client (``_subs`` is weak about closed
        # entries: they are skipped on the next push and pruned on subscribe).
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._queue.clear()
            self._cond.notify_all()

    @property
    def closed(self) -> bool:
        with self._cond:
            return self._closed


def _error_from(err: Any, method: str) -> BaseException:
    """The exception for a JSON-RPC ``error`` object."""
    if not isinstance(err, dict):
        return ApiError("internal", "Malformed error response")
    code = err.get("code")
    message = str(err.get("message") or "Error")
    data = err.get("data")
    data = data if isinstance(data, dict) else {}
    if code == _RPC_TIMEOUT:
        return OperationTimeout(
            f"{method}: the server stopped waiting; the operation may still complete"
        )
    api_code = data.get("code")
    if api_code == "method_not_found":
        return ApiError("not_found", f"Unknown operation {method!r}")
    if api_code in ERROR_CODES:
        return ApiError(
            api_code, str(data.get("message") or message), data.get("details")
        )
    if code in _RPC_PROTOCOL_CODES:
        return ApiError("invalid_request", message)
    if code == _RPC_TOO_MANY_REQUESTS:
        return ApiError("backend_error", "Server busy: too many requests in flight")
    return ApiError("internal", message)


def _encode(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, separators=(",", ":"), allow_nan=False).encode() + b"\n"


# -- the client -------------------------------------------------------------------


class SocketClient(ChatClient):
    """:class:`ChatClient` over the server's Unix socket.

    Build with :meth:`connect`.  Options (keyword only):

    ``heartbeat_timeout``
        Seconds without any bytes from the server after which the connection
        is declared dead (default 15 = 3 heartbeats).  Idle connections
        without a subscription are kept talking by a client ``server.ping``
        every ``ping_interval`` seconds.
    ``reconnect``
        Reconnect in the background after a loss (default ``True``); when
        ``False`` a loss is final: calls raise :class:`ServerUnavailable` and
        subscriptions are closed.
    ``reconnect_timeout``
        Give up (finally) after this many seconds of failed attempts
        (default: never).  ``backoff`` is ``(first, maximum)`` seconds
        between attempts, doubled each time with jitter.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        heartbeat_timeout: float = 3 * HEARTBEAT_INTERVAL_S,
        ping_interval: float = HEARTBEAT_INTERVAL_S,
        reconnect: bool = True,
        reconnect_timeout: float | None = None,
        backoff: tuple[float, float] = (0.1, 5.0),
    ) -> None:
        self.path = Path(path).expanduser() if path else default_socket_path()
        self._heartbeat_timeout = heartbeat_timeout
        self._ping_interval = ping_interval
        self._reconnect = reconnect
        self._reconnect_timeout = reconnect_timeout
        self._backoff = backoff
        self._lock = threading.Lock()
        self._subscribe_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._stop = threading.Event()
        self._conn: _Conn | None = None
        self._closed = False
        self._fatal: ApiError | None = None
        self._thread: threading.Thread | None = None
        self._flaps = 0
        self._subs: list[_SocketSubscription] = []
        #: Event stream state.  ``_stream_known``: a server-side subscription
        #: was established (its baseline is ``_stream_last``); it stays true
        #: across outages so that the stream is resumed, not restarted.
        self._stream_known = False
        self._stream_last: int | None = None
        self._ring: collections.deque[Event] = collections.deque(maxlen=_RING)
        self._pending_since: int | None = None
        self._identity: tuple[Any, Any] | None = None
        #: ``rpc.hello`` result of the current/last connection.
        self.server_info: dict[str, Any] = {}

    # -- construction ---------------------------------------------------------

    @classmethod
    def connect(
        cls,
        path: str | Path | None = None,
        *,
        timeout: float = 5.0,
        **options: Any,
    ) -> SocketClient:
        """Connect to the server at *path* (default: :func:`default_socket_path`)
        and return a running client.

        A refused/missing socket is retried until *timeout* (so a caller that
        just spawned the server can wait for it); raises
        :class:`ServerUnavailable` then, :class:`IncompatibleServer` at once
        for a version mismatch.
        """
        client = cls(path, **options)
        deadline = time.monotonic() + max(0.0, timeout)
        delay = 0.05
        while True:
            try:
                conn = client._open(deadline)
                break
            except ServerUnavailable:
                if time.monotonic() + delay >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.5)
        client._publish(conn, restored=False)
        client._thread = threading.Thread(
            target=client._run, args=(conn,), name="megachat-sock-client", daemon=True
        )
        client._thread.start()
        return client

    # -- state ----------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """``True`` while a handshaken connection is up."""
        with self._lock:
            return self._conn is not None and not self._closed

    @property
    def state(self) -> str:
        """``"connected"``, ``"reconnecting"`` or ``"closed"`` (final)."""
        with self._lock:
            if self._closed or self._fatal is not None:
                return "closed"
            return "connected" if self._conn is not None else "reconnecting"

    # -- calls ----------------------------------------------------------------

    def call(
        self,
        name: str,
        data: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        pending = self._submit(name, data, timeout)
        if not pending.event.wait(self._local_deadline(timeout)):
            self._abandon(pending)
            raise self._local_timeout(name)
        return pending.value()

    async def acall(
        self,
        name: str,
        data: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        # Sending may block on a full socket buffer: keep it off the loop.
        pending = await asyncio.to_thread(self._submit, name, data, timeout)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()

        def wake() -> None:
            def finish() -> None:
                if not future.done():
                    future.set_result(None)

            with contextlib.suppress(RuntimeError):  # the loop is closed
                loop.call_soon_threadsafe(finish)

        pending.add_callback(wake)
        try:
            await asyncio.wait_for(future, self._local_deadline(timeout))
        except TimeoutError:
            self._abandon(pending)
            raise self._local_timeout(name) from None
        except asyncio.CancelledError:
            self._abandon(pending)  # the operation itself keeps running
            raise
        return pending.value()

    @staticmethod
    def _local_deadline(timeout: float | None) -> float | None:
        return (
            None
            if timeout is None
            else min(timeout, _MAX_TIMEOUT_S) + _RESPONSE_GRACE_S
        )

    @staticmethod
    def _local_timeout(name: str) -> OperationTimeout:
        return OperationTimeout(
            f"{name}: no answer from the server; the operation may still complete"
        )

    def _abandon(self, pending: _Pending) -> None:
        """Stop waiting for *pending*; a late answer is ignored."""
        conn = pending.conn
        if conn is not None:
            with self._lock:
                conn.pending.pop(pending.id, None)

    def _submit(
        self,
        name: str,
        data: dict[str, Any] | None,
        timeout: float | None,
        hook: Callable[[Any], None] | None = None,
    ) -> _Pending:
        """Register and send one request on the current connection."""
        if timeout is not None and not timeout > 0:
            raise ValueError("timeout must be > 0")
        params = {} if data is None else data
        with self._lock:
            self._raise_if_unusable()
            conn = self._conn
            if conn is None:
                raise ServerUnavailable("Not connected to the server")
            pending = _Pending(name, conn, next(self._ids), hook)
            conn.pending[pending.id] = pending
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": pending.id,
            "method": name,
            "params": params,
        }
        if timeout is not None:
            message["timeout"] = min(float(timeout), _MAX_TIMEOUT_S)
        try:
            payload = _encode(message)
        except (TypeError, ValueError) as exc:
            self._abandon(pending)
            raise ApiError(
                "invalid_request", f"Request is not JSON serializable: {exc}"
            ) from None
        self._send(conn, payload, _WRITE_TIMEOUT_S)
        return pending

    def _raise_if_unusable(self) -> None:
        if self._closed:
            raise ServerUnavailable("Client is closed")
        if self._fatal is not None:
            raise self._fatal

    def _send(self, conn: _Conn, payload: bytes, write_timeout: float) -> None:
        """Write *payload*; a failed or stalled write kills the connection (the
        request's pending then fails with :class:`ServerUnavailable`)."""
        deadline = time.monotonic() + write_timeout
        view = memoryview(payload)
        try:
            with conn.write_lock:
                while view:
                    try:
                        sent = conn.sock.send(view)
                    except TimeoutError:
                        if time.monotonic() > deadline:
                            raise
                        continue
                    view = view[sent:]
            conn.last_tx = time.monotonic()
        except OSError as exc:
            self._fail(conn, f"write failed: {exc}")

    # -- events ---------------------------------------------------------------

    def subscribe(self, since: int | None = None) -> _SocketSubscription:
        """Start receiving events (see :class:`ClientSubscription`).

        All subscriptions share the connection's one server-side stream.  The
        first subscription sets it up with *since*; later ones are served from
        a local ring of the last 2000 events (a *since* older than that gets a
        ``gap``).  Subscribing while disconnected succeeds: the stream starts
        when the connection returns.
        """
        if since is not None and (isinstance(since, bool) or since < 0):
            raise ValueError("since must be an integer >= 0")
        sub = _SocketSubscription()
        with self._subscribe_lock:
            with self._lock:
                self._raise_if_unusable()
                self._subs = [s for s in self._subs if not s.closed]
                self._subs.append(sub)
                if self._stream_known:
                    self._replay_locked(sub, since)
                    return sub
                connected = self._conn is not None
                if not connected:
                    if since is not None:
                        self._pending_since = (
                            since
                            if self._pending_since is None
                            else min(self._pending_since, since)
                        )
                    return sub
            try:
                pending = self._submit(
                    "events.subscribe",
                    {} if since is None else {"since": since},
                    _HANDSHAKE_TIMEOUT_S,
                    hook=lambda result: self._activate(result, since),
                )
                if not pending.event.wait(_HANDSHAKE_TIMEOUT_S + _RESPONSE_GRACE_S):
                    self._abandon(pending)
                    raise self._local_timeout("events.subscribe")
                pending.value()
            except ServerUnavailable:
                # The connection dropped meanwhile: the stream starts on
                # reconnect, from what was asked for.
                with self._lock:
                    if since is not None:
                        self._pending_since = since
            except BaseException:
                sub.close()
                raise
        return sub

    def _replay_locked(self, sub: _SocketSubscription, since: int | None) -> None:
        """Serve a late subscription from the ring (client lock held)."""
        last = self._stream_last
        if since is None or last is None:
            return
        since = min(since, last)
        events = [e for e in self._ring if e.seq > since]
        oldest = events[0].seq if events else last + 1
        if oldest - 1 > since:
            sub._push(
                Event(last, GAP, {"reason": "buffer", "since": since}, time.time())
            )
        for event in events:
            sub._push(event)

    def _activate(self, result: Any, since: int | None) -> None:
        """Hook run by the reader on the ``events.subscribe`` answer, before
        the notifications that follow it are processed."""
        last = result.get("last_seq") if isinstance(result, dict) else None
        if not isinstance(last, int):
            last = 0
        with self._lock:
            conn = self._conn
            if conn is not None:
                conn.streaming = True
            self._stream_known = True
            baseline = last if since is None else min(since, last)
            if self._stream_last is None:
                self._stream_last = baseline
            self._pending_since = None

    def _route(self, params: Any) -> None:
        """Deliver one ``event`` notification (reader thread)."""
        if not isinstance(params, dict):
            return
        seq, type_, payload, ts = (
            params.get("seq"),
            params.get("type"),
            params.get("payload"),
            params.get("ts"),
        )
        if (
            not isinstance(seq, int)
            or not isinstance(type_, str)
            or not isinstance(payload, dict)
            or not isinstance(ts, int | float)
        ):
            logger.debug("Dropped malformed event notification")
            return
        event = Event(seq, type_, payload, float(ts))
        with self._lock:
            if type_ == GAP:
                self._ring.clear()
                if payload.get("reason") == "overflow":
                    self._stream_last = seq
            else:
                self._ring.append(event)
                self._stream_last = seq
            for sub in self._subs:
                sub._push(event)

    def _broadcast_locked(self, type_: str, payload: dict[str, Any]) -> None:
        event = Event(self._stream_last or 0, type_, payload, time.time())
        for sub in self._subs:
            sub._push(event)

    # -- connection lifecycle ---------------------------------------------------

    def _read_identity(self) -> tuple[Any, Any] | None:
        """``(pid, started_at)`` of the server per its runtime file."""
        try:
            info = json.loads(runtime_file_path(self.path).read_text(encoding="utf-8"))
            return (info.get("pid"), info.get("started_at"))
        except (OSError, ValueError, AttributeError):
            return None

    def _sync_request(
        self, conn: _Conn, method: str, params: dict[str, Any], deadline: float
    ) -> Any:
        """Handshake-phase request/answer on a not yet published connection."""
        req_id = next(self._ids)
        self._send_raw(
            conn,
            _encode(
                {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
            ),
            deadline,
        )
        while True:
            line = conn.take_line()
            if line is None:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{method}: no answer")
                conn.fill()
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("id") is None and "error" in message:
                err = message["error"]
                if (
                    isinstance(err, dict)
                    and err.get("code") == _RPC_TOO_MANY_CONNECTIONS
                ):
                    raise ConnectionError("server has too many connections")
                continue
            if message.get("id") != req_id:
                continue
            if "error" in message:
                raise _error_from(message["error"], method)
            return message.get("result")

    @staticmethod
    def _send_raw(conn: _Conn, payload: bytes, deadline: float) -> None:
        view = memoryview(payload)
        while view:
            try:
                sent = conn.sock.send(view)
            except TimeoutError:
                if time.monotonic() > deadline:
                    raise
                continue
            view = view[sent:]

    def _open(self, deadline: float) -> _Conn:
        """Connect, say hello, check the version, resume the event stream.

        Returns a handshaken connection that is not yet published.  Raises
        :class:`ServerUnavailable` (retryable) or :class:`IncompatibleServer`.
        """
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn = _Conn(sock)
        try:
            sock.settimeout(_TICK_S)
            try:
                sock.connect(os.fspath(self.path))
            except OSError as exc:
                raise ServerUnavailable(
                    f"Cannot connect to the server at {self.path}: {exc.strerror or exc}"
                ) from None
            hello_deadline = min(deadline, time.monotonic() + _HANDSHAKE_TIMEOUT_S)
            hello_deadline = max(hello_deadline, time.monotonic() + 1.0)
            try:
                hello = self._sync_request(conn, "rpc.hello", {}, hello_deadline)
            except ApiError as exc:
                raise ServerUnavailable(
                    f"Server handshake failed: {exc.message}"
                ) from None
            version = hello.get("api_version") if isinstance(hello, dict) else None
            if version != API_VERSION:
                raise IncompatibleServer(
                    f"Server speaks socket API version {version!r}, "
                    f"this client {API_VERSION}",
                    {"server": version, "client": API_VERSION},
                )
            conn.hello = hello
            try:
                self._resume_stream(conn, hello_deadline)
            except ApiError as exc:  # e.g. a server without an event bus
                logger.warning("Cannot resume the event stream: %s", exc.message)
        except (OSError, TimeoutError) as exc:
            conn.shutdown()
            raise ServerUnavailable(f"Server connection failed: {exc}") from None
        except BaseException:
            conn.shutdown()
            raise
        return conn

    def _resume_stream(self, conn: _Conn, deadline: float) -> None:
        """Re-subscribe on a fresh connection from where the stream stopped."""
        identity = self._read_identity()
        with self._lock:
            known = self._stream_known
            wanted = known or bool(self._subs) or self._pending_since is not None
            last = self._stream_last
            pending_since = self._pending_since
            previous = self._identity
        restarted = (
            known
            and previous is not None
            and identity is not None
            and identity != previous
        )
        conn.identity = identity
        conn.restarted = restarted
        if not wanted:
            return
        if restarted:
            since = None
        elif last is not None:
            since = last
        else:
            since = pending_since
        result = self._sync_request(
            conn,
            "events.subscribe",
            {} if since is None else {"since": since},
            deadline,
        )
        server_last = result.get("last_seq") if isinstance(result, dict) else None
        if not isinstance(server_last, int):
            server_last = 0
        if known and not restarted and last is not None and server_last < last:
            conn.restarted = True
        conn.stream_since = since
        conn.stream_last = server_last
        conn.resumed = True

    def _publish(self, conn: _Conn, *, restored: bool) -> None:
        """Make *conn* the live connection and announce it."""
        restarted = conn.restarted
        with self._lock:
            if self._closed:
                conn.dead = True
                conn.shutdown()
                return
            self.server_info = conn.hello
            identity = conn.identity
            if identity is not None or self._identity is None:
                self._identity = identity
            if conn.resumed:
                conn.streaming = True
                if restarted:
                    # A different server: its sequence numbers mean nothing here.
                    self._ring.clear()
                    self._stream_last = conn.stream_last
                    self._stream_known = True
                else:
                    self._stream_known = True
                    since = conn.stream_since
                    if self._stream_last is None:
                        self._stream_last = (
                            conn.stream_last
                            if since is None
                            else min(since, conn.stream_last)
                        )
                self._pending_since = None
            self._conn = conn
            conn.published = time.monotonic()
            late_since = self._pending_since
            need_stream = (
                not conn.resumed
                and not self._stream_known
                and (bool(self._subs) or late_since is not None)
            )
            if restored:
                self._broadcast_locked(
                    CONNECTION_EVENT, {"state": "restored", "reason": "reconnected"}
                )
                if restarted:
                    self._broadcast_locked(GAP, {"reason": "restart"})
        if need_stream:
            # A subscription appeared while the handshake was running: start the
            # stream now (the answer is handled by the reader that follows).
            try:
                self._submit(
                    "events.subscribe",
                    {} if late_since is None else {"since": late_since},
                    _HANDSHAKE_TIMEOUT_S,
                    hook=lambda result: self._activate(result, late_since),
                )
            except ApiError:
                logger.debug("Late event subscription failed", exc_info=True)

    def _fail(self, conn: _Conn, reason: str) -> None:
        """Declare *conn* dead (idempotent): fail its calls, announce the loss."""
        with self._lock:
            if conn.dead:
                return
            conn.dead = True
            current = self._conn is conn
            if current:
                self._conn = None
            orphans = list(conn.pending.values())
            conn.pending.clear()
            announce = current and not self._closed
            if announce:
                # Same critical section that makes ``connected`` False: exactly
                # the subscribers that existed at the loss get "lost"; one
                # subscribing afterwards sees only the later "restored".
                self._broadcast_locked(
                    CONNECTION_EVENT, {"state": "lost", "reason": reason}
                )
        conn.shutdown()
        error = ServerUnavailable(f"Connection to the server lost: {reason}")
        for pending in orphans:
            pending.resolve(error=error)
        if announce:
            logger.info("Server connection lost: %s", reason)

    def _run(self, conn: _Conn | None) -> None:
        """Supervisor thread: read the connection, reconnect when it drops."""
        try:
            while conn is not None and not self._stop.is_set():
                self._read_loop(conn)
                if self._stop.is_set():
                    break
                if not self._reconnect:
                    self._set_fatal(ServerUnavailable("Connection to the server lost"))
                    break
                self._flap_pause(conn)
                conn = self._reconnect_loop()
        except Exception:  # pragma: no cover - a bug must not leak silently
            logger.exception("Socket client supervisor crashed")
            self._set_fatal(ServerUnavailable("Socket client failed"))
        finally:
            with self._lock:
                subs = list(self._subs)
                self._subs = []
                conn = self._conn
                self._conn = None
            if conn is not None:
                self._fail_detached(conn)
            for sub in subs:
                sub.close()

    def _fail_detached(self, conn: _Conn) -> None:
        with self._lock:
            conn.dead = True
            orphans = list(conn.pending.values())
            conn.pending.clear()
        conn.shutdown()
        for pending in orphans:
            pending.resolve(error=ServerUnavailable("Client is closed"))

    def _set_fatal(self, error: ApiError) -> None:
        with self._lock:
            if self._fatal is None and not self._closed:
                self._fatal = error

    def _flap_pause(self, lost: _Conn) -> None:
        """Back off when connections keep dying right after they are made (a
        server that accepts and then drops us must not be hammered)."""
        if time.monotonic() - lost.published >= _STABLE_S:
            self._flaps = 0
            return
        first, longest = self._backoff
        self._stop.wait(min(longest, first * 2**self._flaps) * (0.5 + random.random()))
        self._flaps += 1

    def _reconnect_loop(self) -> _Conn | None:
        first, longest = self._backoff
        delay = first
        started = time.monotonic()
        while not self._stop.is_set():
            try:
                conn = self._open(time.monotonic() + _HANDSHAKE_TIMEOUT_S)
                self._publish(conn, restored=True)
                return conn
            except IncompatibleServer as exc:
                self._set_fatal(exc)
                return None
            except ServerUnavailable:
                pass
            if (
                self._reconnect_timeout is not None
                and time.monotonic() - started > self._reconnect_timeout
            ):
                self._set_fatal(
                    ServerUnavailable(
                        f"Server did not come back within {self._reconnect_timeout:g}s"
                    )
                )
                return None
            self._stop.wait(delay * (0.5 + random.random()))
            delay = min(delay * 2, longest)
        return None

    # -- reader ---------------------------------------------------------------

    def _read_loop(self, conn: _Conn) -> None:
        """Dispatch everything the server sends until the connection ends."""
        while not self._stop.is_set() and not conn.dead:
            try:
                line = conn.take_line()
                if line is None:
                    if not conn.fill():
                        self._tick(conn)
                    continue
            except OSError as exc:
                self._fail(conn, str(exc) or type(exc).__name__)
                return
            self._dispatch(conn, line)
        self._fail(conn, "closed")

    def _tick(self, conn: _Conn) -> None:
        """Liveness bookkeeping on every idle poll of the reader."""
        now = time.monotonic()
        if now - conn.last_rx > self._heartbeat_timeout:
            self._fail(
                conn, f"no data from the server for {self._heartbeat_timeout:g}s"
            )
            return
        if conn.streaming:
            return  # heartbeats keep the connection talking
        if conn.ping is not None and not conn.ping.done:
            return
        if now - max(conn.last_rx, conn.last_tx) < self._ping_interval:
            return
        with self._lock:
            if conn.dead:
                return
            ping = _Pending("server.ping", conn, next(self._ids))
            conn.pending[ping.id] = ping
        conn.ping = ping
        self._send(
            conn,
            _encode({"jsonrpc": "2.0", "id": ping.id, "method": "server.ping"}),
            _PING_WRITE_TIMEOUT_S,
        )

    def _dispatch(self, conn: _Conn, line: bytes) -> None:
        if not line.strip():
            return
        try:
            message = json.loads(line)
        except ValueError:
            logger.debug("Dropped a malformed line from the server")
            return
        if not isinstance(message, dict):
            return
        method = message.get("method")
        if isinstance(method, str):
            if method == "event":
                self._route(message.get("params"))
            return  # heartbeat (liveness only) and unknown notifications
        req_id = message.get("id")
        if req_id is None:
            logger.debug("Server error without id: %s", message.get("error"))
            return
        with self._lock:
            pending = (
                conn.pending.pop(req_id, None) if isinstance(req_id, int) else None
            )
        if pending is None:
            return  # answered after the caller gave up
        if "error" in message:
            pending.resolve(error=_error_from(message["error"], pending.method))
            return
        result = message.get("result")
        if pending.hook is not None:
            try:
                pending.hook(result)
            except Exception:
                logger.exception("Response hook failed")
        pending.resolve(result)

    # -- closing --------------------------------------------------------------

    def close(self) -> None:
        """Close the connection and all subscriptions; stops reconnecting.

        Idempotent; calls in flight fail with :class:`ServerUnavailable`.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            conn = self._conn
            self._conn = None
            subs = list(self._subs)
            self._subs = []
        self._stop.set()
        if conn is not None:
            self._fail_detached(conn)
        for sub in subs:
            sub.close()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(3)
