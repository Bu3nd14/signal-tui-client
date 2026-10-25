"""Single-instance lock shared by the TUI and the headless server.

The lock file holds the PID of the running instance.  Until the TUI becomes a
client of the server (Phase 2) both entry points run their own in-process
core, so they must be mutually exclusive: both take this lock.
"""

import logging
import os

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
