"""
Signal TUI Client — Textual interface integrated with signal-cli via JSON-RPC.

Thin entry point: single-instance lock, crash logging, and process startup.
The application itself lives in the ``tui`` package (see ``tui.app.SignalTUI``).
"""

import logging
import os
import sys
import traceback


if __name__ == "__main__":
    # When launched as a script (`python signal_tui.py`), also register this
    # module under its canonical name so that `import signal_tui` from within
    # the `tui` package resolves to THIS module instead of re-executing the
    # script (which would cause a circular import through `tui.app`).
    sys.modules["signal_tui"] = sys.modules["__main__"]


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
    except Exception:
        # If anything goes wrong, allow the app to start anyway
        return True

def _release_lock():
    """Remove the lock file if it belongs to us."""
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE) as f:
                old_pid = int(f.read().strip())
            if old_pid == os.getpid():
                os.remove(LOCK_FILE)
    except Exception:
        pass

# Global exception handler: salva le eccezioni non gestite su file
# per debug, senza interferire con stderr usato da Textual per la TUI.
def _global_exception_handler(exc_type, exc_value, exc_traceback):
    try:
        with open("/tmp/signal-crash.log", "w") as f:
            traceback.print_exception(exc_type, exc_value, exc_traceback, file=f)
    except Exception:
        pass  # non vogliamo causare altri errori
    # Chiama comunque l'handler predefinito per vedere l'errore anche in console
    sys.__excepthook__(exc_type, exc_value, exc_traceback)

sys.excepthook = _global_exception_handler

from emoji_picker import replace_emoji_aliases  # noqa: F401 (re-exported for test patching)

from tui.app import SignalTUI


logger = logging.getLogger("signal_tui")
# Ensure LINK-* logs are written to a file (Textual may suppress stderr)
_link_fh = logging.FileHandler("/tmp/signal-link.log", mode="w")
_link_fh.setLevel(logging.DEBUG)
_link_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_link_fh)
logger.setLevel(logging.DEBUG)


if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler("/tmp/signal-tui.log", mode="w")],
    )
    import signal as signal_module

    if not _acquire_lock():
        print("❌ Signal TUI is already running (lock file /tmp/signal-tui.lock).", file=sys.stderr)
        print("   If you're sure it's not running, delete the lock file and try again.", file=sys.stderr)
        sys.exit(1)

    app = SignalTUI()

    def _handle_sigint(sig, frame):
        """Handle Ctrl+C: stop polling and exit cleanly."""
        app._polling_active = False
        app.exit()

    signal_module.signal(signal_module.SIGINT, _handle_sigint)
    app.run()
    _release_lock()
