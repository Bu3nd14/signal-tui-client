"""Authenticated WebSocket fan-out for web reader updates.

Two sockets, both authenticated the same way:

* ``/ws`` - the legacy thin notification frames (what the SPA consumes), one
  shared fan-out over the core bus (:func:`_broadcast`).  Unchanged.
* ``/ws/v2`` - the core's typed wire events with resume (:func:`_stream_v2`),
  one bus subscription per connection.  Protocol::

      client -> server   {"type": "subscribe", "since": N}      (optional)
      server -> client   {"seq": 12, "type": "message", "payload": {...},
                          "ts": 1700000000.5}                   (every event)
      server -> client   {"type": "heartbeat"}                  (when idle)
      server -> client   {"type": "error", "payload": {"reason": "invalid_subscribe"}}

  ``since`` (``?since=N`` on the URL or the subscribe frame) replays the
  events with ``seq > N`` still in the bus ring buffer before going live; when
  ``N`` fell out of the buffer, or the connection's queue overflowed, a
  ``{"type": "gap", "payload": {"reason": "buffer"|"overflow"}}`` event tells
  the client to resync with the REST endpoints.  Without ``?since`` the server
  waits up to :data:`SUBSCRIBE_GRACE_S` for a subscribe frame (events keep
  buffering meanwhile, nothing is lost) and then streams live.  A later
  subscribe frame restarts the stream from its ``since``.
"""

import asyncio
import base64
import json
import logging
import time
from contextlib import suppress
from typing import Any

from web.auth import is_authorized
from web.legacy_events import to_legacy

#: Heartbeat cadence: the SPA watchdog (app.js) treats the WebSocket as a
#: zombie when no traffic arrives for 10s; the server therefore fans out a
#: lightweight ``{"type": "heartbeat"}`` frame every ``HEARTBEAT_INTERVAL_S``
#: even when the push queue is empty, so healthy connections are never
#: mistaken for dead.
HEARTBEAT_INTERVAL_S = 5.0

#: How long ``/ws/v2`` waits for a subscribe frame before it starts streaming
#: live (only when the URL carries no ``?since``).
SUBSCRIBE_GRACE_S = 0.5

logger = logging.getLogger(__name__)


def _browser_authorization(websocket: Any) -> tuple[str | None, str | None]:
    authorization = websocket.headers.get("authorization")
    if authorization:
        return authorization, None

    protocols = {
        protocol.strip()
        for protocol in websocket.headers.get("sec-websocket-protocol", "").split(",")
    }
    for protocol in protocols:
        prefix = "signal-tui-token."
        if not protocol.startswith(prefix):
            continue
        encoded = protocol.removeprefix(prefix)
        try:
            padding = "=" * (-len(encoded) % 4)
            supplied = base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None, None
        accepted = "signal-tui-bearer"
        return f"Bearer {supplied}", accepted if accepted in protocols else None
    return None, None


async def _broadcast(app: Any) -> None:
    """Fan the core bus out to every WebSocket (legacy frames + heartbeat)."""
    last_send = 0.0
    sub = None
    sub_bus = None
    try:
        while True:
            bus = getattr(app.state, "bus", None)
            if bus is None:
                if sub is not None:
                    sub.close()
                    sub = sub_bus = None
                await asyncio.sleep(0.5)
                continue
            if sub is None or sub_bus is not bus:
                if sub is not None:
                    sub.close()
                sub = bus.subscribe()
                sub_bus = bus
            bus_event = await asyncio.to_thread(sub.get, 0.5)
            # Events without a legacy frame (backend_*, gap, ...) are skipped
            # like an idle tick, so the heartbeat cadence is unaffected.
            event = None if bus_event is None else to_legacy(bus_event)
            if event is None:
                now = time.monotonic()
                if now - last_send < HEARTBEAT_INTERVAL_S:
                    continue
                event = {"type": "heartbeat"}
                last_send = now
            else:
                last_send = time.monotonic()
            stale = []
            for websocket in tuple(app.state.websocket_connections):
                try:
                    await asyncio.wait_for(websocket.send_json(event), timeout=0.5)
                except Exception:  # noqa: BLE001
                    stale.append(websocket)
            for websocket in stale:
                app.state.websocket_connections.discard(websocket)
                with suppress(Exception):
                    await websocket.close()
    finally:
        if sub is not None:
            sub.close()


def _wire_frame(event: Any) -> dict[str, Any]:
    """``/ws/v2`` frame of a bus event."""
    return {
        "seq": event.seq,
        "type": event.type,
        "payload": event.payload,
        "ts": event.ts,
    }


def _parse_since(value: Any) -> int | None:
    """A valid ``since`` (int >= 0; bools are not ints here), else ``ValueError``."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("since")  # noqa: TRY004
    if isinstance(value, str):
        value = int(value)  # ValueError for anything but digits
    if not isinstance(value, int) or value < 0:
        raise ValueError("since")
    return value


def _subscribe_since(raw: Any) -> tuple[bool, int | None]:
    """``(is_subscribe, since)`` of a client frame; ``ValueError`` if malformed."""
    if raw is None:
        return False, None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return False, None
    if not isinstance(data, dict) or data.get("type") != "subscribe":
        return False, None
    return True, _parse_since(data.get("since"))


async def _stream_v2(websocket: Any, app: Any, since: int | None) -> None:
    """Stream bus events to one ``/ws/v2`` client until it goes away."""
    # Bounded: a client flooding frames cannot grow memory (extras are dropped).
    incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=16)
    closed = asyncio.Event()

    async def reader() -> None:
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                with suppress(asyncio.QueueFull):
                    incoming.put_nowait(message)
        except Exception:
            logger.debug("ws/v2 reader ended", exc_info=True)
        finally:
            closed.set()

    bus = getattr(app.state, "bus", None)
    sub = bus.subscribe(since) if bus is not None else None
    reader_task = asyncio.create_task(reader())
    last_send = 0.0

    async def resubscribe(message: dict[str, Any]) -> None:
        """Apply a client frame: a valid subscribe restarts the stream."""
        nonlocal sub
        try:
            is_subscribe, new_since = _subscribe_since(message.get("text"))
        except ValueError:
            await websocket.send_json(
                {"type": "error", "payload": {"reason": "invalid_subscribe"}}
            )
            return
        if is_subscribe and bus is not None:
            if sub is not None:
                sub.close()
            sub = bus.subscribe(new_since)

    try:
        if since is None:
            # Grace period for a subscribe frame; the live subscription above
            # keeps buffering, and is replaced if the frame asks for a replay.
            with suppress(asyncio.TimeoutError):
                first = await asyncio.wait_for(incoming.get(), SUBSCRIBE_GRACE_S)
                await resubscribe(first)
        poll = min(0.25, HEARTBEAT_INTERVAL_S)
        while not closed.is_set():
            while not incoming.empty():
                await resubscribe(incoming.get_nowait())
            event = None
            if sub is not None:
                event = await asyncio.to_thread(sub.get, poll)
            else:
                await asyncio.sleep(poll)
            if event is None:
                now = time.monotonic()
                if now - last_send < HEARTBEAT_INTERVAL_S:
                    continue
                frame: dict[str, Any] = {"type": "heartbeat"}
                last_send = now
            else:
                frame = _wire_frame(event)
                last_send = time.monotonic()
            try:
                await websocket.send_json(frame)
            except (TypeError, ValueError):
                logger.warning("ws/v2: dropped non-JSON event %s", frame.get("type"))
    except Exception:
        logger.debug("ws/v2 stream ended", exc_info=True)
    finally:
        if sub is not None:
            sub.close()
        reader_task.cancel()
        with suppress(asyncio.CancelledError):
            await reader_task


def install_websocket(app: Any, token: str, *, required: bool = True) -> None:
    """Mount the (optionally authenticated) socket and its fan-out task.

    When *required* is ``False`` (``--web-no-auth``), every connection is
    accepted regardless of what (if anything) it supplies.
    """
    from fastapi import WebSocket, WebSocketDisconnect

    if not hasattr(app.state, "websocket_v2_connections"):
        app.state.websocket_v2_connections = set()
    v2_connections = app.state.websocket_v2_connections

    async def websocket_endpoint(websocket: WebSocket) -> None:
        authorization, subprotocol = _browser_authorization(websocket)
        if required and not is_authorized(authorization, token):
            await websocket.close(code=1008)
            return
        await websocket.accept(subprotocol=subprotocol)
        app.state.websocket_connections.add(websocket)
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
        except (RuntimeError, WebSocketDisconnect):
            pass
        finally:
            app.state.websocket_connections.discard(websocket)

    async def websocket_v2_endpoint(websocket: WebSocket) -> None:
        authorization, subprotocol = _browser_authorization(websocket)
        if required and not is_authorized(authorization, token):
            await websocket.close(code=1008)
            return
        try:
            since = _parse_since(websocket.query_params.get("since"))
        except ValueError:
            await websocket.close(code=1008)
            return
        await websocket.accept(subprotocol=subprotocol)
        # A separate set: the legacy broadcaster writes to every socket in
        # ``websocket_connections``, v2 sockets must only get v2 frames.
        v2_connections.add(websocket)
        try:
            await _stream_v2(websocket, app, since)
        finally:
            v2_connections.discard(websocket)

    async def start_broadcaster() -> None:
        app.state.websocket_broadcaster = asyncio.create_task(_broadcast(app))

    async def stop_broadcaster() -> None:
        task = app.state.websocket_broadcaster
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    app.add_api_websocket_route("/ws", websocket_endpoint)
    app.add_api_websocket_route("/ws/v2", websocket_v2_endpoint)
    app.router.add_event_handler("startup", start_broadcaster)
    app.router.add_event_handler("shutdown", stop_broadcaster)
