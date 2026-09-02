"""Authenticated WebSocket fan-out for web reader updates."""

import asyncio
import base64
import queue
import time
from contextlib import suppress
from typing import Any

from web.auth import is_authorized
from web.bridge import get_push_queue

#: Heartbeat cadence: the SPA watchdog (app.js) treats the WebSocket as a
#: zombie when no traffic arrives for 10s; the server therefore fans out a
#: lightweight ``{"type": "heartbeat"}`` frame every ``HEARTBEAT_INTERVAL_S``
#: even when the push queue is empty, so healthy connections are never
#: mistaken for dead.
HEARTBEAT_INTERVAL_S = 5.0


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
    last_send = 0.0
    while True:
        push_queue = get_push_queue()
        if push_queue is None:
            await asyncio.sleep(0.5)
            continue
        try:
            event = await asyncio.to_thread(push_queue.get, True, 0.5)
        except queue.Empty:
            event = None
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


def install_websocket(app: Any, token: str) -> None:
    """Mount the authenticated socket and its single queue-draining task."""
    from fastapi import WebSocket, WebSocketDisconnect

    async def websocket_endpoint(websocket: WebSocket) -> None:
        authorization, subprotocol = _browser_authorization(websocket)
        if not is_authorized(authorization, token):
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

    async def start_broadcaster() -> None:
        app.state.websocket_broadcaster = asyncio.create_task(_broadcast(app))

    async def stop_broadcaster() -> None:
        task = app.state.websocket_broadcaster
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    app.add_api_websocket_route("/ws", websocket_endpoint)
    app.router.add_event_handler("startup", start_broadcaster)
    app.router.add_event_handler("shutdown", stop_broadcaster)
