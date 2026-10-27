"""Lifecycle for the optional web reader server."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from server.api import ChatAPI
from server.bus import EventBus

logger = logging.getLogger(__name__)

_SHARED_SESSION_CLOSE_TIMEOUT_S = 2.0


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


def parse_hosts(host: str | Sequence[str]) -> list[str]:
    """Split a bind host setting into the addresses to listen on.

    *host* is one address, a comma-separated list (``--web-host
    127.0.0.1,192.168.1.10``) or a sequence of either; duplicates are dropped
    and an empty setting means every interface (``0.0.0.0``).
    """
    items = [host] if isinstance(host, str) else list(host)
    hosts: list[str] = []
    for item in items:
        for part in str(item).split(","):
            part = part.strip()
            if part and part not in hosts:
                hosts.append(part)
    return hosts or ["0.0.0.0"]


def format_hosts(hosts: Sequence[str], port: int) -> str:
    """``http://HOST:PORT`` for every host, IPv6 literals bracketed."""
    return ", ".join(
        f"http://[{h}]:{port}" if ":" in h else f"http://{h}:{port}" for h in hosts
    )


def _bind_sockets(hosts: Sequence[str], port: int) -> list[socket.socket]:
    """Bind and listen on *port* for every host; close them all on failure."""
    sockets: list[socket.socket] = []
    try:
        for host in hosts:
            family, kind, proto, _, addr = socket.getaddrinfo(
                host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
            )[0]
            sock = socket.socket(family, kind, proto)
            sockets.append(sock)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                # ``::`` next to ``0.0.0.0`` must not also claim the IPv4 port.
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind(addr)
            sock.listen(2048)
            sock.set_inheritable(True)
    except BaseException:
        for sock in sockets:
            sock.close()
        raise
    return sockets


def start_web_server(
    api: ChatAPI,
    port: int = 4242,
    token: str | None = None,
    *,
    host: str | Sequence[str] = "127.0.0.1",
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

    *host* may name several addresses (comma-separated or a sequence, see
    :func:`parse_hosts`); the server listens on all of them.
    """
    global _active_server

    hosts = parse_hosts(host)
    urls = format_hosts(hosts, port)

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
    from web.gmail_api import create_gmail_image_router, create_gmail_router
    from web.uploads import prepare_upload_directory
    from web.ws import install_websocket

    # The bus /ws and /ws/v2 stream from: the caller's, else the API's, else a
    # private one (an API stand-in without a bus still gets heartbeats).
    app.state.bus = bus or api.bus or EventBus()
    install_auth(app, token, required=require_auth)
    app.include_router(create_api_router())
    app.include_router(create_gmail_router())
    app.include_router(create_gmail_image_router())
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
        host=hosts[0],
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
                "Web server listening on %s — Bearer token: %s",
                urls,
                token,
            )
        else:
            logger.warning(
                "Web server listening on %s — NO AUTH (--web-no-auth): "
                "anyone who can reach this address/port has full read/send access",
                urls,
            )
        ready.set()

    def run() -> None:
        monitor: asyncio.Task | None = None
        sockets: list[socket.socket] = []
        try:
            prepare_upload_directory()
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            handle.loop = loop
            # One host: uvicorn binds it itself. Several: bind them here and
            # hand uvicorn the listening sockets.
            if len(hosts) > 1:
                sockets = _bind_sockets(hosts, port)
            monitor = loop.create_task(mark_started())
            loop.run_until_complete(server.serve(sockets=sockets or None))
        except BaseException:
            handle.status = "down"
            ready.set()
            if server.started:
                logger.exception("Web server thread failed (web down)")
            else:
                logger.error(
                    "Web server failed to bind to %s (web down; port may be in use)",
                    urls,
                )
        finally:
            handle.status = "down"
            for sock in sockets:
                sock.close()
            loop = handle.loop
            if loop is not None:
                try:
                    if monitor is not None and not monitor.done():
                        monitor.cancel()
                        loop.run_until_complete(
                            asyncio.gather(monitor, return_exceptions=True)
                        )
                    # Loop-bound core resources (the shared HTTP session) must
                    # close on this loop; the IMAP pool is closed by the core
                    # (``ChatCore.shutdown``), not by a view of it.
                    loop.run_until_complete(
                        api.close_loop_resources(_SHARED_SESSION_CLOSE_TIMEOUT_S)
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
