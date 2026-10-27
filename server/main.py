"""Headless server mode: the chat core plus its adapters, no TUI.

``megachat serve`` replaces the "run the TUI inside tmux to keep the web UI
alive" workaround.  It starts the Unix-socket adapter (the transport of the
TUI client, design §4.2; ``--no-socket`` / ``--socket PATH``) and the web
adapter (``--no-web``).  The server holds the single-instance lock
(``server/lock.py``); TUIs attach to it as clients (``megachat``) and never
take it, except ``megachat --in-process``, which runs its own core and is
mutually exclusive with a server.

This module must never import Textual or ``tui``.  ``web`` is imported lazily
(only when the web adapter is enabled).
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from server import socket_adapter
from server.bus import EventBus
from server.core import ChatCore
from server.lock import LOCK_FILE, _acquire_lock, _release_lock

logger = logging.getLogger("signal_tui")

LOG_FILE = "/tmp/signal-tui.log"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="megachat serve",
        description="Run the chat core headless (web UI, no TUI)",
    )
    parser.add_argument("--debug", action="store_true", help="enable debug logging")
    parser.add_argument("--web-port", type=int, help="web UI listen port")
    parser.add_argument(
        "--web-host",
        type=str,
        help=(
            "web UI bind host (default 127.0.0.1); comma-separate several to "
            "listen on each, e.g. 127.0.0.1,192.168.1.10"
        ),
    )
    parser.add_argument(
        "--web-no-auth",
        action="store_true",
        help=(
            "disable the web UI Bearer token requirement (INSECURE: anyone who "
            "can reach --web-host/--web-port gets full read/send access with no "
            "credential — only use on a trusted network or behind your own auth)"
        ),
    )
    parser.add_argument(
        "--no-web",
        action="store_true",
        help="do not start the web adapter (clients can still use the socket)",
    )
    parser.add_argument(
        "--no-socket",
        action="store_true",
        help="do not start the Unix-socket adapter",
    )
    parser.add_argument(
        "--socket",
        type=str,
        metavar="PATH",
        help=(
            "Unix socket path (default: $MEGACHAT_SOCKET, else "
            "$XDG_RUNTIME_DIR/megachat/server.sock, else "
            "~/.local/state/megachat/server.sock)"
        ),
    )
    args = parser.parse_args(argv)
    if args.web_port is not None and not 1 <= args.web_port <= 65535:
        parser.error("--web-port must be between 1 and 65535")
    if args.no_socket and args.socket:
        parser.error("--socket and --no-socket are mutually exclusive")
    return args


def _configure_logging(debug: bool) -> None:
    """Log to the same file as the TUI (append), INFO unless ``--debug``."""
    # basicConfig is a no-op once the root logger has handlers: only open the
    # file when it will actually be used (else the handler leaks its file).
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.DEBUG if debug else logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
            handlers=[logging.FileHandler(LOG_FILE, mode="a")],
        )
    logger.setLevel(logging.DEBUG if debug else logging.INFO)


def _shutdown(core: ChatCore, web_handle: Any, socket_handle: Any = None) -> None:
    """Stop the core, then the adapters (socket first, then web), then
    disconnect and prune.

    The order lives in :meth:`ChatCore.shutdown`, shared with the TUI exit
    paths; this only supplies the adapter stops.
    """
    stop_adapters = None
    if web_handle is not None or socket_handle is not None:

        def stop_adapters() -> None:
            try:
                socket_adapter.stop(socket_handle)
            finally:
                if web_handle is not None:
                    from web.server import stop_web_server

                    stop_web_server(web_handle)

    core.shutdown(stop_adapters)


def serve(
    argv: list[str] | None = None, *, stop_event: threading.Event | None = None
) -> int:
    """Run the headless server until SIGINT/SIGTERM; return the exit code.

    *stop_event* lets callers (tests) end the wait without a real signal; the
    signal handlers set the same event.
    """
    args = _parse_args(argv)
    _configure_logging(args.debug)

    if not _acquire_lock():
        print(
            f"❌ Megachat is already running (lock file {LOCK_FILE}).",
            file=sys.stderr,
        )
        print(
            "   A server (or a --in-process TUI) is already running; "
            "delete the lock file only if you are sure nothing is.",
            file=sys.stderr,
        )
        return 1

    stop = stop_event if stop_event is not None else threading.Event()
    core: ChatCore | None = None
    web_handle: Any = None
    socket_handle: Any = None
    previous_handlers: dict[int, Any] = {}
    exit_code = 0
    try:
        bus = EventBus()
        core = ChatCore(bus)
        # ``server.shutdown`` ends the serve loop like SIGINT does.
        core.api.shutdown_hook = stop.set
        core.start()

        if not args.no_socket:
            try:
                socket_handle = socket_adapter.start(
                    core.api,
                    bus,
                    Path(args.socket).expanduser() if args.socket else None,
                )
            except socket_adapter.SocketAdapterError as exc:
                logger.error("Socket adapter failed to start: %s", exc)
                print(f"❌ Socket adapter failed to start: {exc}", file=sys.stderr)
                exit_code = 1
            else:
                line = f"🔌 Socket: {socket_handle.path}"
                logger.info(line)
                print(line, flush=True)

        if exit_code == 0 and args.no_web:
            if socket_handle is None:
                logger.warning(
                    "Serving without the web and socket adapters: no client can connect"
                )
                print(
                    "⚠️  Running without the web and socket adapters: no client "
                    "can connect.",
                    file=sys.stderr,
                )
            else:
                logger.info("Serving without the web adapter (--no-web)")
                print(
                    "ℹ️  Running without the web adapter (--no-web): clients "
                    f"connect through the socket {socket_handle.path}.",
                    file=sys.stderr,
                )
        elif exit_code == 0:
            from protocols.config import web_host, web_port, web_token
            from web.server import format_hosts, parse_hosts, start_web_server

            host = args.web_host if args.web_host is not None else web_host()
            port = args.web_port if args.web_port is not None else web_port()
            token = web_token()
            urls = format_hosts(parse_hosts(host), port)
            web_handle = start_web_server(
                core.api,
                port,
                token,
                host=host,
                require_auth=not args.web_no_auth,
                bus=bus,
            )
            if web_handle is None or web_handle.status == "down":
                logger.error("Web adapter failed to start (see log); exiting")
                print(
                    f"❌ Web adapter failed to start on {urls} (see {LOG_FILE}).",
                    file=sys.stderr,
                )
                exit_code = 1
            else:
                handle = web_handle
                core.api.client_counters["web"] = lambda: (
                    len(handle.websocket_connections)
                    + len(handle.websocket_v2_connections)
                )
                if args.web_no_auth:
                    line = f"🌐 Web UI: {urls} — ⚠️ NO AUTH (--web-no-auth)"
                else:
                    line = f"🌐 Web UI: {urls} — token: {token}"
                logger.info(line)
                print(line, flush=True)

        if exit_code == 0:
            if threading.current_thread() is threading.main_thread():
                for sig in (signal.SIGINT, signal.SIGTERM):
                    previous_handlers[sig] = signal.signal(
                        sig, lambda _s, _f: stop.set()
                    )
            logger.info("Megachat server running (pid lock %s)", LOCK_FILE)
            # Event.wait() with no timeout is not reliably interruptible on
            # every platform; a short timeout keeps signals responsive.
            while not stop.wait(0.5):
                pass
            logger.info("Shutdown requested")
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        if core is not None:
            _shutdown(core, web_handle, socket_handle)
        _release_lock()
    return exit_code
