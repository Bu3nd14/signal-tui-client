"""Unix-socket adapter: the server API over newline-delimited JSON-RPC 2.0.

The wire contract (framing, methods, error mapping, timeouts, attachment
policy) is specified in ``docs/DESIGN_CLIENT_SERVER_SPLIT.md`` §4.2; this
module only translates.  Every registered operation is a method, plus
``rpc.hello``, ``server.ping``, ``events.subscribe`` and ``events.unsubscribe``.

Threading: :func:`start` runs an asyncio loop on a dedicated thread (like
``web.server``).  Sync operations run in a bounded thread pool, async ones
(``messages.send``) on the loop; their internal ``to_thread`` calls use a
second bounded pool, so neither slow calls nor many connections can block the
loop, other connections or a connection's event notifications.  Bus events
are pumped by one thread per subscribed connection, which hands each event to
the loop and waits until it is written, so a slow reader backs up into its
bounded bus subscription (-> ``gap``) instead of into unbounded memory.

This module imports only ``server.*`` (enforced in
``tests/test_server_import_rules.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import socket
import stat
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from server.api import ApiError, ChatAPI, Operation
from server.bus import EventBus, Subscription
from server.models_api import AttachmentIn
from server.paths import (  # noqa: F401 - re-exported for callers of the adapter
    SOCKET_ENV,
    default_socket_path,
    runtime_file_path,
)
from server.uploads import (
    MAX_STAGED_TOTAL_BYTES,
    UploadValidationError,
    prune_upload_directory,
    stage_local_file,
)
from server.version import API_VERSION, SERVER_VERSION

logger = logging.getLogger(__name__)

#: Longest accepted request line (bytes, newline included).
MAX_LINE_BYTES = 16 * 1024 * 1024
#: Server-side time limit of a call without a ``timeout`` member / the largest
#: ``timeout`` a client may ask for (seconds).
DEFAULT_TIMEOUT_S = 120.0
MAX_TIMEOUT_S = 600.0
MAX_CONNECTIONS = 32
MAX_INFLIGHT_PER_CONNECTION = 64
#: A connection that cannot be written to for this long is dropped.
WRITE_TIMEOUT_S = 30.0
#: Idle ``heartbeat`` notification cadence on a subscribed connection.
HEARTBEAT_INTERVAL_S = 5.0
#: Thread pools: sync operations / everything async operations offload.
SYNC_WORKERS = 16
AUX_WORKERS = 16
#: Lines above this size are parsed off the loop.
_INLINE_PARSE_BYTES = 256 * 1024
#: After a fatal protocol error the connection is half-closed and the peer's
#: pending input discarded (at most this long / this many bytes) so the error
#: line is not lost to an ECONNRESET.
_DRAIN_ON_CLOSE_S = 1.0
_DRAIN_ON_CLOSE_BYTES = MAX_LINE_BYTES
#: ``sun_path`` is 108 bytes on Linux, 104 on BSD/macOS (NUL included).
_MAX_SOCKET_PATH_BYTES = 103

# -- JSON-RPC error codes -------------------------------------------------------

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TIMEOUT = -32020
TOO_MANY_REQUESTS = -32030
TOO_MANY_CONNECTIONS = -32031

#: ``ApiError.code`` -> JSON-RPC error code.
RPC_CODES: dict[str, int] = {
    "invalid_request": INVALID_PARAMS,
    "not_found": -32001,
    "unauthorized": -32002,
    "forbidden": -32003,
    "too_large": -32004,
    "conflict": -32005,
    "unprocessable": -32006,
    "not_supported": -32007,
    "backend_down": -32008,
    "backend_error": -32009,
    "internal": INTERNAL_ERROR,
}

#: Methods the adapter answers itself (an operation may not shadow them).
BUILTIN_METHODS = ("rpc.hello", "server.ping", "events.subscribe", "events.unsubscribe")

#: Operations whose ``attachments`` are client-named local files that must be
#: copied into the upload directory before the operation sees them (mc-7kp).
STAGING_OPERATIONS = frozenset({"messages.send"})
#: Operations with an ``attachments`` field that ignores the files (a pre-check):
#: their attachment list is emptied, no path of the client reaches them.
IGNORED_ATTACHMENT_OPERATIONS = frozenset({"messages.send.check"})


class SocketAdapterError(RuntimeError):
    """The socket adapter could not start."""


# -- encoding ---------------------------------------------------------------------


def _dumps(obj: Any) -> str:
    """Compact JSON on one line (ASCII only, so no stray line separators)."""
    return json.dumps(obj, separators=(",", ":"), allow_nan=False)


def _jsonable(value: Any) -> Any:
    """*value* reduced to plain JSON data (anything exotic becomes ``str``)."""
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return str(value)


def _line(text: str) -> bytes:
    return text.encode("ascii") + b"\n"


def _error_line(req_id: Any, code: int, message: str, data: Any = None) -> bytes:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return _line(_dumps({"jsonrpc": "2.0", "id": req_id, "error": error}))


def _result_line(req_id: Any, result_json: str) -> bytes:
    return _line(f'{{"jsonrpc":"2.0","id":{_dumps(req_id)},"result":{result_json}}}')


class _RpcError(Exception):
    """An error response in the making."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _from_api_error(exc: ApiError) -> _RpcError:
    details = exc.details
    if exc.code == "invalid_request" and isinstance(details, list):
        # pydantic error dicts carry the offending input and exceptions: keep
        # only what a client can act on.
        details = [
            {k: e.get(k) for k in ("type", "loc", "msg")}
            for e in details
            if isinstance(e, dict)
        ]
    return _RpcError(
        RPC_CODES.get(exc.code, INTERNAL_ERROR),
        exc.message,
        {"code": exc.code, "message": exc.message, "details": _jsonable(details)},
    )


def _call_and_dump(api: ChatAPI, name: str, params: dict[str, Any]) -> str:
    """Worker-thread body of a sync operation: call and encode the result."""
    return _dumps(api.call_json(name, params))


# -- peer credentials -------------------------------------------------------------


def _peer_uid(sock: Any) -> int | None:
    """uid of the connected peer (Linux ``SO_PEERCRED``); ``None`` when the
    platform cannot say (permissions alone then protect the socket); ``-1``
    when it should have but failed (refuse)."""
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        return None
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
    except (OSError, struct.error):
        return -1
    return uid


# -- the adapter ------------------------------------------------------------------


def _attachment_field_ok(op: Operation) -> bool:
    """Every field typed with :class:`AttachmentIn` must be called ``attachments``
    (the one the staging code knows); fail closed otherwise."""
    for name, info in op.request.model_fields.items():
        if "AttachmentIn" in repr(info.annotation) and name != "attachments":
            return False
    return True


def attachment_operations(api: ChatAPI) -> set[str]:
    """Names of the operations whose request has an ``attachments`` field."""
    found = set()
    for op in api.operations():
        if "AttachmentIn" in repr(op.request.model_fields.get("attachments")):
            found.add(op.name)
    return found


class _Adapter:
    """Everything one running socket server shares (lives on the adapter loop)."""

    def __init__(self, api: ChatAPI, bus: EventBus | None) -> None:
        self.api = api
        self.bus = bus
        self.operations = {op.name: op for op in api.operations()}
        shadowed = set(self.operations) & set(BUILTIN_METHODS)
        if shadowed:
            raise SocketAdapterError(f"operations shadow builtin methods: {shadowed}")
        for op in self.operations.values():
            if not _attachment_field_ok(op):
                raise SocketAdapterError(
                    f"{op.name}: AttachmentIn outside 'attachments' is not staged"
                )
        self.attachment_ops = attachment_operations(api)
        unhandled = (
            self.attachment_ops - STAGING_OPERATIONS - IGNORED_ATTACHMENT_OPERATIONS
        )
        if unhandled:
            raise SocketAdapterError(f"no attachment policy for {sorted(unhandled)}")
        self.sync_pool = ThreadPoolExecutor(SYNC_WORKERS, "megachat-sock-op")
        self.aux_pool = ThreadPoolExecutor(AUX_WORKERS, "megachat-sock-aux")
        self.connections: set[_Connection] = set()
        api.client_counters["socket"] = lambda: len(self.connections)
        self.tasks: set[asyncio.Task[Any]] = set()
        self.stopping = False

    def spawn(self, coro: Any) -> asyncio.Task[Any]:
        """Run *coro* detached from any connection (operations outlive them)."""
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)

        def done(finished: asyncio.Task[Any]) -> None:
            self.tasks.discard(finished)
            if not finished.cancelled():
                finished.exception()  # mark retrieved: callers may have left

        task.add_done_callback(done)
        return task

    async def on_connect(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        conn = _Connection(self, reader, writer)
        try:
            if self.stopping:
                return
            sock = writer.get_extra_info("socket")
            uid = _peer_uid(sock) if sock is not None else None
            if uid is not None and uid != os.geteuid():
                logger.warning("Socket connection from uid %s refused", uid)
                return
            if len(self.connections) >= MAX_CONNECTIONS:
                writer.write(
                    _error_line(None, TOO_MANY_CONNECTIONS, "Too many connections")
                )
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(writer.drain(), 1)
                return
            self.connections.add(conn)
            await conn.run()
            await conn.aclose()
        except Exception:
            logger.exception("Socket connection handler failed")
        finally:
            self.connections.discard(conn)
            conn.close()

    # -- calls ----------------------------------------------------------------

    async def run_operation(self, op: Operation, params: dict[str, Any]) -> str:
        """Run *op* (staging attachments first); returns the result as JSON text."""
        staged: list[Path] = []
        try:
            if op.name in self.attachment_ops:
                params = await self._prepare_attachments(op, params, staged)
            loop = asyncio.get_running_loop()
            if op.is_async:
                result = await self.api.acall_json(op.name, params)
                return await loop.run_in_executor(self.aux_pool, _dumps, result)
            return await loop.run_in_executor(
                self.sync_pool, _call_and_dump, self.api, op.name, params
            )
        finally:
            for path in staged:
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)

    async def _prepare_attachments(
        self, op: Operation, params: dict[str, Any], staged: list[Path]
    ) -> dict[str, Any]:
        """The attachment policy of the socket adapter (mc-7kp): copy, never trust.

        Each client-named path is copied into the upload directory (see
        :func:`server.uploads.stage_local_file`) and the operation receives the
        copies; *staged* collects them so the caller deletes them when the call
        ends, whatever the outcome.
        """
        raw = params.get("attachments")
        if op.name in IGNORED_ATTACHMENT_OPERATIONS:
            return {**params, "attachments": []} if raw else params
        if not raw:
            return params
        try:
            slots = TypeAdapter(list[AttachmentIn]).validate_python(raw)
        except ValidationError as exc:
            raise _from_api_error(
                ApiError("invalid_request", "Invalid request", exc.errors())
            ) from None
        loop = asyncio.get_running_loop()
        # Validate the payload before spending any I/O on files (mc-mbm.10).
        check = {k: v for k, v in params.items() if k != "attachments"}
        check["attachment_count"] = len(slots)
        check["slots_ok"] = all(s.path or s.rejected is not None for s in slots)
        await loop.run_in_executor(
            self.sync_pool, self.api.call_json, "messages.send.check", check
        )
        budget = MAX_STAGED_TOTAL_BYTES
        out: list[dict[str, Any]] = []
        for slot in slots:
            if not slot.path:
                out.append(slot.model_dump())  # rejected / empty: the op decides
                continue
            filename = slot.filename or os.path.basename(slot.path)
            try:
                copy = await loop.run_in_executor(
                    self.sync_pool,
                    lambda s=slot, n=filename, b=budget: stage_local_file(
                        s.path, filename=n, content_type=s.content_type, max_bytes=b
                    ),
                )
            except UploadValidationError as exc:
                if exc.status_code == 413:
                    raise _from_api_error(
                        ApiError("too_large", "Upload too large")
                    ) from None
                raise _from_api_error(
                    ApiError("invalid_request", "Invalid request")
                ) from None
            except OSError:
                raise _from_api_error(
                    ApiError("invalid_request", "Invalid request")
                ) from None
            staged.append(copy)
            budget -= copy.stat().st_size
            out.append({**slot.model_dump(), "path": str(copy), "filename": filename})
        return {**params, "attachments": out}


class _Connection:
    """One client connection: reads requests, answers them concurrently."""

    def __init__(
        self,
        adapter: _Adapter,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.adapter = adapter
        self.reader = reader
        self.writer = writer
        self.closed = False
        self.inflight: set[asyncio.Task[Any]] = set()
        self.subscription: Subscription | None = None

    # -- output ---------------------------------------------------------------

    async def send(self, data: bytes) -> None:
        if self.closed:
            return
        try:
            self.writer.write(data)
            await asyncio.wait_for(self.writer.drain(), WRITE_TIMEOUT_S)
        except (TimeoutError, OSError):  # a stuck or vanished peer
            logger.info("Socket client dropped (write failed or timed out)")
            self.close()

    async def flush(self, timeout: float) -> None:
        """Wait (bounded) until the output written so far left the transport."""
        if self.closed:
            return
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.writer.drain(), timeout)

    def close(self) -> None:
        """Drop the connection now (unsent output is lost)."""
        if self.closed:
            return
        self.closed = True
        self.unsubscribe()
        with contextlib.suppress(Exception):
            self.writer.transport.abort()

    async def _drain_before_close(self) -> None:
        """Half-close, then discard the peer's pending input before closing.

        Closing a Unix stream socket with unread input makes the peer's next
        read fail with ECONNRESET, which can discard the error line we just
        sent.  Bounded in time and bytes so a client that keeps writing
        cannot hold the connection open.
        """
        with contextlib.suppress(Exception):
            await self.writer.drain()
            if self.writer.can_write_eof():
                self.writer.write_eof()
        deadline = asyncio.get_running_loop().time() + _DRAIN_ON_CLOSE_S
        discarded = 0
        while discarded < _DRAIN_ON_CLOSE_BYTES:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            try:
                chunk = await asyncio.wait_for(self.reader.read(65536), remaining)
            except (TimeoutError, OSError, ValueError):
                return
            if not chunk:
                return
            discarded += len(chunk)

    async def aclose(self) -> None:
        """Close after flushing what was already written (the transport buffer
        may still hold the tail of a large response)."""
        if self.closed:
            return
        self.closed = True
        self.unsubscribe()
        with contextlib.suppress(Exception):
            self.writer.close()
            await asyncio.wait_for(self.writer.wait_closed(), 5)
        with contextlib.suppress(Exception):
            self.writer.transport.abort()  # no-op once closed; else give up

    # -- events ---------------------------------------------------------------

    def unsubscribe(self) -> bool:
        sub, self.subscription = self.subscription, None
        if sub is None:
            return False
        sub.close()
        return True

    def start_pump(self, sub: Subscription) -> None:
        threading.Thread(
            target=self._pump,
            args=(sub, asyncio.get_running_loop()),
            name="megachat-sock-events",
            daemon=True,
        ).start()

    def _pump(self, sub: Subscription, loop: asyncio.AbstractEventLoop) -> None:
        """Thread body: bus events -> notifications, written in order."""
        last_sent = time.monotonic()
        while not sub.closed and not self.closed:
            event = sub.get(0.5)
            if event is None:
                if time.monotonic() - last_sent < HEARTBEAT_INTERVAL_S:
                    continue
                frame: dict[str, Any] = {
                    "jsonrpc": "2.0",
                    "method": "heartbeat",
                    "params": {"ts": time.time()},
                }
            else:
                frame = {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": {
                        "seq": event.seq,
                        "type": event.type,
                        "payload": event.payload,
                        "ts": event.ts,
                    },
                }
            try:
                data = _line(_dumps(frame))
            except (TypeError, ValueError):
                logger.warning("Socket: dropped non-JSON event %s", frame["method"])
                continue
            try:
                future = asyncio.run_coroutine_threadsafe(self.send(data), loop)
            except RuntimeError:  # the loop is gone
                return
            while True:
                try:
                    future.result(0.5)
                    break
                except TimeoutError:
                    if sub.closed or self.closed:
                        future.cancel()
                        return
                except (Exception, asyncio.CancelledError):  # noqa: BLE001 - shutdown
                    return
            last_sent = time.monotonic()

    # -- input ----------------------------------------------------------------

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while not self.closed:
            try:
                raw = await self.reader.readuntil(b"\n")
            except asyncio.IncompleteReadError:
                # EOF (a partial last line is dropped).  The peer may only have
                # half-closed: let the requests it already sent be answered.
                if self.inflight:
                    await asyncio.wait(set(self.inflight))
                return
            except asyncio.LimitOverrunError:
                await self.send(
                    _error_line(
                        None,
                        RPC_CODES["too_large"],
                        f"Request line exceeds {MAX_LINE_BYTES} bytes",
                        {"code": "too_large", "message": "Request line too large"},
                    )
                )
                await self._drain_before_close()
                return  # the stream cannot be resynchronised
            except OSError:
                return
            raw = raw.strip()
            if not raw:
                continue
            try:
                if len(raw) > _INLINE_PARSE_BYTES:
                    message = await loop.run_in_executor(
                        self.adapter.aux_pool, json.loads, raw
                    )
                else:
                    message = json.loads(raw)
            except (ValueError, RecursionError):  # includes UnicodeDecodeError
                await self.send(_error_line(None, PARSE_ERROR, "Parse error"))
                continue
            if len(self.inflight) >= MAX_INFLIGHT_PER_CONNECTION:
                req_id = message.get("id") if isinstance(message, dict) else None
                await self.send(
                    _error_line(
                        req_id if isinstance(req_id, str | int) else None,
                        TOO_MANY_REQUESTS,
                        "Too many requests in flight",
                    )
                )
                continue
            task = loop.create_task(self.handle(message))
            self.inflight.add(task)
            task.add_done_callback(self.inflight.discard)

    async def handle(self, message: Any) -> None:
        """Validate one request envelope, run it, write the response."""
        req_id: Any = None
        has_id = False
        try:
            if isinstance(message, list):
                raise _RpcError(INVALID_REQUEST, "Batch requests are not supported")
            if not isinstance(message, dict):
                raise _RpcError(INVALID_REQUEST, "Invalid request")
            has_id = "id" in message
            req_id = message.get("id")
            if has_id and (
                isinstance(req_id, bool) or not isinstance(req_id, str | int)
            ):
                req_id, has_id = None, True
                raise _RpcError(INVALID_REQUEST, "id must be a string or an integer")
            if message.get("jsonrpc") != "2.0":
                raise _RpcError(INVALID_REQUEST, "jsonrpc must be '2.0'")
            method = message.get("method")
            if not isinstance(method, str):
                raise _RpcError(INVALID_REQUEST, "method must be a string")
            params = message.get("params")
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise _RpcError(INVALID_PARAMS, "params must be an object")
            timeout = _timeout_of(message)
            result_json, after = await self._execute(method, params, timeout)
            if has_id:
                await self.send(_result_line(req_id, result_json))
            if after is not None:
                after()
        except _RpcError as exc:
            if has_id or exc.code in (INVALID_REQUEST, PARSE_ERROR):
                await self.send(_error_line(req_id, exc.code, exc.message, exc.data))
        except Exception:
            logger.exception("Socket request failed")
            if has_id:
                await self.send(
                    _error_line(
                        req_id,
                        INTERNAL_ERROR,
                        "Internal error",
                        {"code": "internal", "message": "Internal error"},
                    )
                )

    async def _execute(
        self, method: str, params: dict[str, Any], timeout: float
    ) -> tuple[str, Any]:
        """``(result JSON text, callback to run after the response is written)``."""
        adapter = self.adapter
        if method == "rpc.hello":
            return _dumps(
                {
                    "api_version": API_VERSION,
                    "server_version": SERVER_VERSION,
                    "operations": sorted(adapter.operations),
                }
            ), None
        if method == "server.ping":
            return _dumps({"pong": True, "ts": time.time()}), None
        if method == "events.unsubscribe":
            self.unsubscribe()
            return _dumps({"subscribed": False}), None
        if method == "events.subscribe":
            return self._subscribe(params)
        op = adapter.operations.get(method)
        if op is None:
            raise _RpcError(
                METHOD_NOT_FOUND,
                f"Unknown method {method!r}",
                {"code": "method_not_found", "message": f"Unknown method {method!r}"},
            )
        # The operation outlives this request (and its connection): a timeout
        # only stops *waiting*, see the design doc.
        task = adapter.spawn(adapter.run_operation(op, params))
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            task.add_done_callback(_log_abandoned)
            raise _RpcError(
                TIMEOUT,
                f"{method} timed out after {timeout:g}s",
                {"code": "timeout", "message": f"{method} timed out"},
            )
        try:
            return task.result(), None
        except ApiError as exc:
            raise _from_api_error(exc) from None

    def _subscribe(self, params: dict[str, Any]) -> tuple[str, Any]:
        bus = self.adapter.bus
        if bus is None:
            raise _from_api_error(
                ApiError("not_supported", "This server has no event bus")
            )
        since = params.get("since")
        if since is not None and (
            isinstance(since, bool) or not isinstance(since, int) or since < 0
        ):
            raise _RpcError(INVALID_PARAMS, "since must be an integer >= 0")
        self.unsubscribe()
        sub = bus.subscribe(since)
        self.subscription = sub
        result = _dumps({"subscribed": True, "last_seq": bus.last_seq})
        # Notifications start only after the response is written.
        return result, lambda: self.start_pump(sub)


def _timeout_of(message: dict[str, Any]) -> float:
    value = message.get("timeout")
    if value is None:
        return DEFAULT_TIMEOUT_S
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not 0 < value <= MAX_TIMEOUT_S
    ):
        raise _RpcError(
            INVALID_REQUEST, f"timeout must be a number in (0, {MAX_TIMEOUT_S:g}]"
        )
    return float(value)


def _log_abandoned(task: asyncio.Task[Any]) -> None:
    """An operation whose caller stopped waiting finished: its outcome is lost,
    but a crash must still show in the log."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None and not isinstance(exc, ApiError | _RpcError):
        logger.error("Abandoned socket call failed", exc_info=exc)


# -- lifecycle --------------------------------------------------------------------


@dataclass
class SocketHandle:
    """State owned by one socket-adapter thread."""

    path: Path
    runtime_path: Path
    thread: threading.Thread
    status: str = "starting"
    error: BaseException | None = None
    loop: asyncio.AbstractEventLoop | None = None
    adapter: _Adapter | None = None
    identity: tuple[int, int] | None = None  # (st_dev, st_ino) of our socket
    stop_event: asyncio.Event | None = None
    ready: threading.Event = field(default_factory=threading.Event)


def _prepare_directory(path: Path) -> None:
    parent = path.parent
    created = not parent.exists()
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = parent.stat()
    except OSError as exc:
        raise SocketAdapterError(f"cannot prepare {parent}: {exc}") from exc
    if info.st_uid != os.geteuid():
        raise SocketAdapterError(f"{parent} is owned by another user")
    if info.st_mode & 0o077:
        if created or parent.name == "megachat":
            parent.chmod(0o700)
        else:
            logger.warning(
                "Socket directory %s is accessible by others; the socket itself "
                "is 0600",
                parent,
            )


def _probe_live(path: Path) -> bool:
    """True when something accepts connections on the socket at *path*."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError):
        return False
    except TimeoutError:
        return True  # accept backlog full: somebody is there
    except OSError as exc:
        raise SocketAdapterError(f"cannot probe {path}: {exc}") from exc
    finally:
        probe.close()
    return True


def _clear_stale(path: Path, runtime: Path) -> None:
    """Remove the leftovers of a crashed server; refuse a live one."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        info = None
    if info is not None:
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
            raise SocketAdapterError(f"{path} exists and is not our socket")
        if _probe_live(path):
            raise SocketAdapterError(f"another server is already listening on {path}")
        logger.info("Removing stale socket %s", path)
        path.unlink(missing_ok=True)
    runtime.unlink(missing_ok=True)


def _write_runtime(handle: SocketHandle, started_at: float) -> None:
    data = {
        "pid": os.getpid(),
        "socket": str(handle.path),
        "version": SERVER_VERSION,
        "api_version": API_VERSION,
        "started_at": started_at,
    }
    tmp = handle.runtime_path.with_name(
        f".{handle.runtime_path.name}.{os.getpid()}.tmp"
    )
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(data, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, handle.runtime_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _remove_files(handle: SocketHandle) -> None:
    """Delete our socket and runtime file (only if they are still ours)."""
    with contextlib.suppress(OSError):
        info = os.lstat(handle.path)
        if (info.st_dev, info.st_ino) == handle.identity:
            handle.path.unlink()
    with contextlib.suppress(OSError, ValueError):
        recorded = json.loads(handle.runtime_path.read_text(encoding="utf-8"))
        if recorded.get("pid") == os.getpid():
            handle.runtime_path.unlink()


def start(
    api: ChatAPI, bus: EventBus | None = None, path: Path | str | None = None
) -> SocketHandle:
    """Start the adapter on a dedicated thread; returns once it is listening.

    *path* defaults to :func:`default_socket_path`.  Raises
    :class:`SocketAdapterError` when it cannot start (path unusable, a live
    server already listening there).
    """
    sock_path = Path(path) if path is not None else default_socket_path()
    sock_path = Path(os.path.abspath(sock_path))
    if len(os.fsencode(sock_path)) > _MAX_SOCKET_PATH_BYTES:
        raise SocketAdapterError(f"socket path too long: {sock_path}")
    adapter = _Adapter(api, bus)  # validates the registry before touching disk
    _prepare_directory(sock_path)
    runtime = runtime_file_path(sock_path)
    _clear_stale(sock_path, runtime)
    prune_upload_directory()

    handle = SocketHandle(
        path=sock_path,
        runtime_path=runtime,
        thread=threading.Thread(),  # replaced below
        adapter=adapter,
    )

    async def serve() -> None:
        loop = asyncio.get_running_loop()
        handle.loop = loop
        loop.set_default_executor(adapter.aux_pool)
        handle.stop_event = asyncio.Event()
        # Bind by hand so the umask only covers the bind itself: the socket is
        # born 0600 and no other thread's files are affected meanwhile.
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)
        try:
            listener.bind(str(sock_path))
        except BaseException:
            listener.close()
            raise
        finally:
            os.umask(old_umask)
        info = os.lstat(sock_path)
        handle.identity = (info.st_dev, info.st_ino)  # from now on it is ours
        try:
            os.chmod(sock_path, 0o600)
            server = await asyncio.start_unix_server(
                adapter.on_connect, sock=listener, limit=MAX_LINE_BYTES
            )
        except BaseException:
            listener.close()
            raise
        _write_runtime(handle, time.time())
        handle.status = "up"
        handle.ready.set()
        try:
            await handle.stop_event.wait()
        finally:
            adapter.stopping = True
            _remove_files(handle)  # clients stop finding us before we close
            server.close()
            # Let answers already written (``server.shutdown``'s) leave the
            # transport buffers before the connections are aborted.
            await asyncio.gather(
                *(c.flush(1.0) for c in list(adapter.connections)),
                return_exceptions=True,
            )
            for conn in list(adapter.connections):
                conn.close()
            pending = [t for t in adapter.tasks if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=2)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), 2)

    def run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(serve())
        except BaseException as exc:
            handle.error = exc
            if handle.status != "up":
                logger.error("Socket adapter failed to start: %s", exc)
            else:
                logger.exception("Socket adapter thread failed")
        finally:
            handle.status = "down"
            _remove_files(handle)
            adapter.api.client_counters.pop("socket", None)
            handle.ready.set()
            adapter.sync_pool.shutdown(wait=False, cancel_futures=True)
            adapter.aux_pool.shutdown(wait=False, cancel_futures=True)
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            handle.loop = None

    handle.thread = threading.Thread(target=run, name="megachat-socket", daemon=True)
    handle.thread.start()
    if not handle.ready.wait(10) or handle.status != "up":
        stop(handle)
        raise SocketAdapterError(
            f"socket adapter failed to start on {sock_path}: {handle.error or 'timeout'}"
        )
    logger.info("Socket adapter listening on %s", sock_path)
    return handle


def stop(handle: SocketHandle | None) -> None:
    """Stop accepting, drop connections, remove the socket and runtime file."""
    if handle is None:
        return
    loop, event = handle.loop, handle.stop_event
    if loop is not None and event is not None and handle.thread.is_alive():
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(event.set)
    handle.thread.join(8)
    if handle.thread.is_alive():
        logger.warning("Socket adapter did not stop within eight seconds")
    else:
        handle.status = "down"
