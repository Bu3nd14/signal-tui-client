"""
Signal TUI Client — Textual interface integrated with signal-cli via JSON-RPC.

Thin entry point: single-instance lock, crash logging, and process startup.
The application itself lives in the ``tui`` package (see ``tui.app.SignalTUI``).

Composition root: this module runs before the core exists, so it is the one UI
module allowed to import ``protocols.config`` (startup settings: image protocol,
web options) and ``web`` (it hands the embedded web adapter to the TUI).
``tests/test_server_import_rules.py`` documents and enforces that exception.
"""

import argparse
import logging
import os
import shutil
import sys
import traceback

if __name__ == "__main__":
    # When launched as a script (`python signal_tui.py`), also register this
    # module under its canonical name so that `import signal_tui` from within
    # the `tui` package resolves to THIS module instead of re-executing the
    # script (which would cause a circular import through `tui.app`).
    sys.modules["signal_tui"] = sys.modules["__main__"]

logger = logging.getLogger("signal_tui")


# Global exception handler: salva le eccezioni non gestite su file
# per debug, senza interferire con stderr usato da Textual per la TUI.
def _global_exception_handler(exc_type, exc_value, exc_traceback):
    try:
        with open("/tmp/signal-crash.log", "w") as f:
            traceback.print_exception(exc_type, exc_value, exc_traceback, file=f)
    except Exception as _e:
        logger.debug(
            "Failed to write crash log", exc_info=True
        )  # non vogliamo causare altri errori
    # Chiama comunque l'handler predefinito per vedere l'errore anche in console
    sys.__excepthook__(exc_type, exc_value, exc_traceback)


sys.excepthook = _global_exception_handler

from emoji_picker import (
    replace_emoji_aliases,  # noqa: F401 (re-exported for test patching)
)
from protocols.config import image_protocol, web_enabled, web_host, web_port, web_token
from server.attach import AttachError, attach_or_spawn, serve_args_for_web
from server.lock import (
    LOCK_FILE,
    _acquire_lock,
    _release_lock,
)
from tui.app import SignalTUI
from tui.images.cellsize import get_cell_size
from tui.images.detect import ImageSupport, detect_image_support

logger = logging.getLogger("signal_tui")
# Ensure LINK-* logs are written to a file (Textual may suppress stderr)
_link_fh = logging.FileHandler("/tmp/signal-link.log", mode="w")
_link_fh.setLevel(logging.DEBUG)
_link_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_link_fh)
logger.setLevel(logging.DEBUG)


#: The TUI's own log when it is a client.  The server logs to
#: ``/tmp/signal-tui.log`` (``server.main.LOG_FILE``); ``--in-process`` keeps
#: logging there, since the TUI is then the core.
TUI_LOG_FILE = "/tmp/megachat-tui.log"
CORE_LOG_FILE = "/tmp/signal-tui.log"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line options for the TUI entry point."""
    parser = argparse.ArgumentParser(description="Signal TUI Client")
    parser.add_argument("--debug", action="store_true", help="enable debug logging")
    parser.add_argument(
        "--web",
        action="store_true",
        default=None,
        help=(
            "enable the optional read-only web UI (served by the server this "
            "TUI starts; ignored with a note when attaching to a running one)"
        ),
    )
    parser.add_argument("--web-port", type=int, help="web UI listen port")
    parser.add_argument(
        "--web-host", type=str, help="web UI bind host (default 127.0.0.1)"
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
        "--connect",
        metavar="PATH",
        help="attach to the server on this Unix socket; never start one",
    )
    parser.add_argument(
        "--in-process",
        action="store_true",
        help=(
            "debugging/tests: run the chat core inside the TUI (takes the "
            "instance lock, no server, no socket) instead of attaching to a "
            "server"
        ),
    )
    args = parser.parse_args(argv)
    if args.connect and args.in_process:
        parser.error("--connect and --in-process are mutually exclusive")
    if args.web_port is not None and not 1 <= args.web_port <= 65535:
        parser.error("--web-port must be between 1 and 65535")
    return args


def _web_wanted(args: argparse.Namespace) -> bool:
    """Whether the user wants the web UI: ``--web``, any ``--web-*`` option, or
    (without ``--web``) ``config.json`` enabling it."""
    return _web_flags_given(args) or web_enabled()


def _web_flags_given(args: argparse.Namespace) -> bool:
    return bool(
        args.web
        or args.web_port is not None
        or args.web_host is not None
        or args.web_no_auth
    )


def main(argv: list[str] | None = None) -> int:
    """Run the TUI (single-instance lock, logging, image detection, app)."""

    args = _parse_args(argv)

    # --debug: logging di livello DEBUG (default INFO) su file, senza
    # interferire con stderr usato da Textual per la TUI.  Il TUI client ha il
    # suo log; con --in-process (il TUI e' il core) resta quello storico.
    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(
                CORE_LOG_FILE if args.in_process else TUI_LOG_FILE, mode="a"
            )
        ],
    )
    import signal as signal_module

    client = None
    server_spawned: bool | None = None
    if args.in_process:
        # The TUI owns the backends: it takes the instance lock itself.
        if not _acquire_lock():
            print(
                f"❌ Megachat is already running (lock file {LOCK_FILE}).",
                file=sys.stderr,
            )
            print(
                "   If you're sure it's not running, delete the lock file and "
                "try again.",
                file=sys.stderr,
            )
            return 1
    else:
        # A client never takes the lock: it belongs to the server.
        web_wanted = _web_wanted(args)
        try:
            attached = attach_or_spawn(
                connect_path=args.connect,
                serve_args=serve_args_for_web(
                    web=web_wanted,
                    web_port=args.web_port,
                    web_host=args.web_host,
                    web_no_auth=args.web_no_auth,
                )
                + (["--debug"] if args.debug else []),
                notify=lambda msg: print(msg, file=sys.stderr, flush=True),
            )
        except AttachError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("Interrupted.", file=sys.stderr)
            return 130
        client = attached.client
        server_spawned = attached.spawned
        if _web_flags_given(args) and not attached.spawned:
            print(
                "ℹ️  Attached to a running server: the web options are "
                "ignored (the server decides what it serves).",
                file=sys.stderr,
            )
        logger.info(
            "TUI client: %s server at %s",
            "spawned" if attached.spawned else "attached to",
            attached.path,
        )

    # Detect the terminal image backend BEFORE ``app.run()``.  Any failure
    # degrades to CATIMG (current behaviour), never blocking startup.
    try:
        image_support = detect_image_support(
            isatty=sys.stdin.isatty() and sys.stdout.isatty(),
            env=os.environ,
            override=image_protocol(),
        )
    except Exception:
        logger.info(
            "Image support detection failed, falling back to CATIMG", exc_info=True
        )
        image_support = ImageSupport.CATIMG

    logger.info(
        "Image support: %s (TERM=%r, override=%r, isatty=%s, TMUX=%r, catimg=%s)",
        image_support.value,
        os.environ.get("TERM"),
        image_protocol(),
        sys.stdin.isatty() and sys.stdout.isatty(),
        os.environ.get("TMUX"),
        bool(shutil.which("catimg")),
    )

    # Diagnostic: when TERM claims kitty, also log the raw TGP probe result
    # with its timing (the probe above may have degraded to catimg even on a
    # real kitty if the reply is late/absent in the process context).
    if os.environ.get("TERM") == "xterm-kitty":
        import time as _time

        from tui.images.detect import query_kitty_ok

        _t0 = _time.monotonic()
        _ok = query_kitty_ok(sys.stdin.fileno(), sys.stdout.fileno())
        logger.info(
            "TGP probe (TERM=xterm-kitty): %s in %.2fs", _ok, _time.monotonic() - _t0
        )

    # P2: measure the cell size BEFORE ``app.run()``, in the same pre-run window
    # where the TGP query is already safe (no Textual key-thread yet).  The CSI
    # ``16 t`` fallback reads stdin, so it must never run inside the app.
    initial_cell_size = None
    if image_support is ImageSupport.KITTY:
        try:
            initial_cell_size = get_cell_size(sys.stdin.fileno())
        except Exception:
            logger.debug("Cell-size pre-run detection failed", exc_info=True)
        logger.info("Native image cell size: %r", initial_cell_size)

    # Only the in-process TUI embeds the web adapter; with a server, the server
    # serves the web UI.
    enable_web = args.in_process and (
        args.web if args.web is not None else web_enabled()
    )
    web_adapter = None
    if enable_web:
        # Composition root: the only place that wires the web adapter into the
        # TUI process (``tui`` itself must not import ``web``).
        from types import SimpleNamespace

        from web.server import start_web_server, stop_web_server

        web_adapter = SimpleNamespace(start=start_web_server, stop=stop_web_server)
    app = SignalTUI(
        image_support=image_support,
        initial_cell_size=initial_cell_size,
        web_enabled=enable_web,
        web_port=args.web_port if args.web_port is not None else web_port(),
        web_host=args.web_host if args.web_host is not None else web_host(),
        web_token=web_token(),
        web_no_auth=args.web_no_auth,
        web_adapter=web_adapter,
        client=client,
        server_spawned=server_spawned,
    )

    def _handle_sigint(sig, frame):
        """Handle Ctrl+C: shut the core down (prune included) and exit.

        Il prune va eseguito QUI e non solo in ``on_exit_app``: ``app.exit()``
        chiamato dal gestore del segnale non innesca in modo affidabile il
        ciclo di shutdown di Textual, quindi senza questo step lo stop via
        segnale (web-signal-tui-stop) non poterebbe mai lo storico.
        ``ChatCore.shutdown`` e' idempotente: ``on_exit_app`` non lo ripete.
        """
        app.shutdown_from_signal()

    # Pre-run window: a plain handler.  Once the loop runs, ``on_mount`` moves
    # it onto ``loop.add_signal_handler`` (see ``SignalTUI._sigint_callback``).
    signal_module.signal(signal_module.SIGINT, _handle_sigint)
    app._sigint_callback = lambda: _handle_sigint(signal_module.SIGINT, None)
    try:
        app.run()
    finally:
        if client is not None:
            # An attached or spawned TUI only disconnects here (Ctrl+C, or
            # Detach/Quit already done by the exit dialog, tui/exit_flow.py);
            # stopping the server is the dialog's Quit, never this.
            client.close()
    if args.in_process:
        _release_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main())
