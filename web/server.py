"""Lifecycle for the optional web reader server."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from server.api import ChatAPI
from server.bus import EventBus

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
    websocket_v2_connections: set[Any] = field(default_factory=set)


_active_server: WebServerHandle | None = None


def start_web_server(
    api: ChatAPI,
    port: int = 4242,
    token: str | None = None,
    *,
    host: str = "127.0.0.1",
    require_auth: bool = True,
    bus: EventBus | None = None,
) -> WebServerHandle | None:
    """Start uvicorn on a dedicated thread and event loop.

    *api* is the server API every route calls (``core.api``): the adapter has
    no other access to the core.  *bus* is the event bus ``/ws`` and ``/ws/v2``
    stream from (default ``api.bus``).

    *require_auth* set to ``False`` (``--web-no-auth``) drops the Bearer
    token requirement entirely — every REST/media/WebSocket request is
    accepted with no credential. Explicit opt-in only; the token is still
    required by default.
    """
    global _active_server

    token = token or os.environ.get("SIGNAL_TUI_WEB_TOKEN", "")
    if require_auth and not token:
        logger.error(
            "Web UI requires a Bearer token; configure SIGNAL_TUI_WEB_TOKEN, "
            "web.token, or pass --web-no-auth (web down)"
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
    app.state.api = api
    app.state.token = token
    app.state.websocket_connections = set()
    app.state.websocket_v2_connections = set()

    from web.api import create_api_router
    from web.auth import install_auth
    from web.uploads import prepare_upload_directory
    from web.ws import install_websocket

    # The bus /ws and /ws/v2 stream from: the caller's, else the API's, else a
    # private one (an API stand-in without a bus still gets heartbeats).
    app.state.bus = bus or api.bus or EventBus()
    install_auth(app, token, required=require_auth)
    app.include_router(create_api_router())
    install_websocket(app, token, required=require_auth)

    @app.get("/health")
    async def health() -> dict[str, object]:
        # Unauthenticated by design: the SPA reads auth_required before it
        # decides whether to show the token login dialog.
        return {"status": "ok", "port": port, "auth_required": require_auth}

    app.mount(
        "/",
        StaticFiles(directory=Path(__file__).with_name("static"), html=True),
        name="web-ui",
    )

    @app.middleware("http")
    async def no_cache_html(request: Any, call_next: Any) -> Any:
        """L'HTML non va cachato: ogni reload deve vedere i nuovi ``?v=`` dei
        file statici (CSS/JS). CSS/JS e media restano cachati normalmente."""
        response = await call_next(request)
        if request.url.path in ("/", "/index.html"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        # ``log_config=None`` evita il ``dictConfig`` di uvicorn, che altrimenti
        # chiude i FileHandler dell'applicazione (root e modulari) e nessun log
        # arriva piu' a /tmp/signal-tui.log dopo l'avvio del server web.
        # Con ``access_log=False`` l'access log di uvicorn resta fuori dal file.
        log_config=None,
        access_log=False,
    )
    server = uvicorn.Server(config)
    ready = threading.Event()
    handle: WebServerHandle

    async def mark_started() -> None:
        while not server.started:
            await asyncio.sleep(0.01)
        handle.status = "up"
        # Only web-signal-tui-bg (the tmux alias) prints the Bearer token to
        # the console today; a manual `python3 signal_tui.py --web` launch
        # left the user with no way to find it short of reading config.json
        # by hand. Log it here too so it's always discoverable.
        if require_auth:
            logger.info(
                "Web server listening on http://%s:%d — Bearer token: %s",
                host,
                port,
                token,
            )
        else:
            logger.warning(
                "Web server listening on http://%s:%d — NO AUTH (--web-no-auth): "
                "anyone who can reach this address/port has full read/send access",
                host,
                port,
            )
        ready.set()

    def run() -> None:
        monitor: asyncio.Task | None = None
        try:
            prepare_upload_directory()
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
        websocket_v2_connections=app.state.websocket_v2_connections,
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
        connections = (
            *handle.websocket_connections,
            *handle.websocket_v2_connections,
        )
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
