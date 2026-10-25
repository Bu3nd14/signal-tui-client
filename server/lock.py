"""Single-instance lock of the server (the process that owns the backends).

The lock file holds the PID of the running owner.  Since Phase 2 the owner is
the headless server (``megachat serve``, spawned on demand by the TUI) or, for
``megachat --in-process``, the TUI itself.  A TUI that attaches to a server as
a client never takes it, so several TUIs may share one server.

The location is ``/tmp/signal-tui.lock`` unless ``$MEGACHAT_LOCK_FILE`` is set
(tests and isolated setups; a child process inherits it).  Check-and-write is
serialised with an ``flock`` on ``<lock>.guard``, so two processes started at
the same moment cannot both win.
"""

import contextlib
import logging
import os

try:  # POSIX only; without it the lock degrades to the unguarded check
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger("signal_tui")

LOCK_ENV = "MEGACHAT_LOCK_FILE"
LOCK_FILE = os.environ.get(LOCK_ENV) or "/tmp/signal-tui.lock"


@contextlib.contextmanager
def _guard():
    """Exclusive advisory lock around the check-and-write of the lock file."""
    fd = None
    if fcntl is not None:
        try:
            fd = os.open(LOCK_FILE + ".guard", os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            logger.debug("Lock guard unavailable", exc_info=True)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)  # closing releases the flock


def lock_holder() -> int | None:
    """PID in the lock file if that process is alive, else ``None``."""
    try:
        with open(LOCK_FILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    return pid


def _acquire_lock() -> bool:
    """Try to acquire a lock file to prevent multiple instances.

    Returns True if the lock was acquired (or no other instance is running),
    False if another instance is already running.
    """
    try:
        with _guard():
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
