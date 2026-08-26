"""Lifecycle for the optional web reader server."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class WebServerHandle:
    """State owned by one web-server thread."""

    server: Any
    thread: threading.Thread
    port: int
    status: str = "starting"
    loop: asyncio.AbstractEventLoop | None = None
    websocket_connections: set[Any] = field(default_factory=set)


_active_server: WebServerHandle | None = None


def start_web_server(
    manager: Any,
    port: int = 4242,
    token: str | None = None,
    *,
    host: str = "127.0.0.1",
) -> WebServerHandle | None:
    """Start uvicorn on a dedicated thread and event loop."""
    global _active_server

    token = token or os.environ.get("SIGNAL_TUI_WEB_TOKEN", "")
    if not token:
        logger.error(
            "Web UI requires a Bearer token; configure SIGNAL_TUI_WEB_TOKEN "
            "or web.token (web down)"
        )
        return None

    try:
        import uvicorn
        from fastapi import FastAPI
        from fastapi.staticfiles import StaticFiles
    except ImportError:
        logger.error(
            "Web UI is enabled but optional dependencies are missing; "
            "install requirements-web.txt (web down)"
        )
        return None

    if not 1 <= port <= 65535:
        logger.error("Invalid web server port %r (web down)", port)
        return None

    app = FastAPI()
    app.state.manager = manager
    app.state.token = token
    app.state.websocket_connections = set()

    from web.api import create_api_router
    from web.auth import install_auth
    from web.bridge import init_bridge
    from web.ws import install_websocket

    init_bridge()
    install_auth(app, token)
    app.include_router(create_api_router())
    install_websocket(app, token)

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "port": port}

    app.mount(
        "/",
        StaticFiles(directory=Path(__file__).with_name("static"), html=True),
        name="web-ui",
    )

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    ready = threading.Event()
    handle: WebServerHandle

    async def mark_started() -> None:
        while not server.started:
            await asyncio.sleep(0.01)
        handle.status = "up"
        logger.info("Web server listening on http://%s:%d", host, port)
        ready.set()

    def run() -> None:
        monitor: asyncio.Task | None = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            handle.loop = loop
            monitor = loop.create_task(mark_started())
            loop.run_until_complete(server.serve())
        except BaseException:
            handle.status = "down"
            ready.set()
            if server.started:
                logger.exception("Web server thread failed (web down)")
            else:
                logger.error(
                    "Web server failed to bind to %s:%d (web down; port may be in use)",
                    host,
                    port,
                )
        finally:
            handle.status = "down"
            if _active_server is handle:
                from web.bridge import close_bridge

                close_bridge()
            loop = handle.loop
            if loop is not None:
                try:
                    if monitor is not None and not monitor.done():
                        monitor.cancel()
                        loop.run_until_complete(
                            asyncio.gather(monitor, return_exceptions=True)
                        )
                    loop.run_until_complete(loop.shutdown_asyncgens())
                finally:
                    loop.close()
                    handle.loop = None

    thread = threading.Thread(target=run, name="signal-tui-web", daemon=True)
    handle = WebServerHandle(
        server=server,
        thread=thread,
        port=port,
        websocket_connections=app.state.websocket_connections,
    )
    _active_server = handle
    thread.start()
    ready.wait(1)
    return handle


def stop_web_server(handle: WebServerHandle | None = None) -> None:
    """Request server shutdown, close web sockets, and wait up to three seconds."""
    global _active_server

    handle = handle or _active_server
    if handle is None:
        return

    async def close_websockets() -> None:
        connections = tuple(handle.websocket_connections)
        if connections:
            await asyncio.gather(
                *(connection.close() for connection in connections),
                return_exceptions=True,
            )

    loop = handle.loop
    if loop is not None and loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(close_websockets(), loop)
        except RuntimeError:
            logger.debug("WebSocket shutdown raced with web loop exit", exc_info=True)
    handle.server.should_exit = True
    handle.thread.join(3)
    if handle.thread.is_alive():
        logger.warning("Web server did not stop within three seconds")
    else:
        handle.status = "down"
    if _active_server is handle:
        _active_server = None
        from web.bridge import close_bridge

        close_bridge()
