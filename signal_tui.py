"""
Signal TUI Client — Textual interface integrated with signal-cli via JSON-RPC.

Thin entry point: single-instance lock, crash logging, and process startup.
The application itself lives in the ``tui`` package (see ``tui.app.SignalTUI``).
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

LOCK_FILE = "/tmp/signal-tui.lock"


def _acquire_lock() -> bool:
    """Try to acquire a lock file to prevent multiple instances.

    Returns True if the lock was acquired (or no other instance is running),
    False if another instance is already running.
    """
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE) as f:
                old_pid = int(f.read().strip())
            # Check if the process is still alive
            try:
                os.kill(old_pid, 0)
                # Process is alive → another instance is running
                return False
            except OSError:
                # Process is dead → we can take the lock
                pass
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
        return True
    except Exception as _e:
        # If anything goes wrong, allow the app to start anyway
        logger.debug("Lock acquisition failed, allowing startup", exc_info=True)
        return True


def _release_lock():
    """Remove the lock file if it belongs to us."""
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE) as f:
                old_pid = int(f.read().strip())
            if old_pid == os.getpid():
                os.remove(LOCK_FILE)
    except Exception as _e:
        logger.debug("Lock release failed", exc_info=True)


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

from backends.config import image_protocol, web_enabled, web_host, web_port, web_token
from emoji_picker import (
    replace_emoji_aliases,  # noqa: F401 (re-exported for test patching)
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


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line options for the TUI entry point."""
    parser = argparse.ArgumentParser(description="Signal TUI Client")
    parser.add_argument("--debug", action="store_true", help="enable debug logging")
    parser.add_argument(
        "--web",
        action="store_true",
        default=None,
        help="enable the optional read-only web UI",
    )
    parser.add_argument("--web-port", type=int, help="web UI listen port")
    parser.add_argument(
        "--web-host", type=str, help="web UI bind host (default 127.0.0.1)"
    )
    args = parser.parse_args(argv)
    if args.web_port is not None and not 1 <= args.web_port <= 65535:
        parser.error("--web-port must be between 1 and 65535")
    return args


if __name__ == "__main__":
    import logging

    args = _parse_args()

    # --debug: logging di livello DEBUG su /tmp/signal-tui.log (default INFO).
    # Utile per la strumentazione (es. timing di send_message_sync) senza
    # interferire con stderr usato da Textual per la TUI.
    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler("/tmp/signal-tui.log", mode="w")],
    )
    import signal as signal_module

    if not _acquire_lock():
        print(
            "❌ Signal TUI is already running (lock file /tmp/signal-tui.lock).",
            file=sys.stderr,
        )
        print(
            "   If you're sure it's not running, delete the lock file and try again.",
            file=sys.stderr,
        )
        sys.exit(1)

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

    enable_web = args.web if args.web is not None else web_enabled()
    app = SignalTUI(
        image_support=image_support,
        initial_cell_size=initial_cell_size,
        web_enabled=enable_web,
        web_port=args.web_port if args.web_port is not None else web_port(),
        web_host=args.web_host if args.web_host is not None else web_host(),
        web_token=web_token(),
    )

    def _handle_sigint(sig, frame):
        """Handle Ctrl+C: stop polling and exit cleanly."""
        app._polling_active = False
        app.exit()

    signal_module.signal(signal_module.SIGINT, _handle_sigint)
    app.run()
    _release_lock()
