"""
Regression tests for signal_tui.py — lock file mechanism.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Import the lock functions directly from signal_tui
# We need to mock LOCK_FILE to avoid touching /tmp
from signal_tui import _acquire_lock, _release_lock


class TestLockFile:
    """🔒 Meccanismo lock file per istanza singola."""

    def test_acquire_lock_success(self, tmp_path):
        """Lock acquisito quando nessun altro processo è in esecuzione."""
        lock_file = tmp_path / "test.lock"
        with patch("signal_tui.LOCK_FILE", str(lock_file)):
            result = _acquire_lock()
        assert result is True
        assert lock_file.exists()
        assert lock_file.read_text().strip() == str(os.getpid())

    def test_acquire_lock_alive_process(self, tmp_path):
        """Lock rifiutato quando un altro processo è in esecuzione."""
        lock_file = tmp_path / "test.lock"
        # Simula un altro processo in esecuzione (PID 999999999)
        lock_file.write_text("999999999")

        with (
            patch("signal_tui.LOCK_FILE", str(lock_file)),
            patch("os.kill") as mock_kill,
        ):
            # os.kill(pid, 0) con processo vivo → non solleva eccezioni
            mock_kill.return_value = None
            result = _acquire_lock()

        assert result is False

    def test_acquire_lock_dead_process(self, tmp_path):
        """Lock acquisito quando il vecchio processo è morto."""
        lock_file = tmp_path / "test.lock"
        lock_file.write_text("999999999")

        with (
            patch("signal_tui.LOCK_FILE", str(lock_file)),
            patch("os.kill") as mock_kill,
        ):
            # os.kill(pid, 0) con processo morto → solleva OSError
            mock_kill.side_effect = OSError("No such process")
            result = _acquire_lock()

        assert result is True
        # Il lock file deve essere stato sovrascritto con il nostro PID
        assert lock_file.read_text().strip() == str(os.getpid())

    def test_release_lock(self, tmp_path):
        """Lock rilasciato correttamente."""
        lock_file = tmp_path / "test.lock"
        lock_file.write_text(str(os.getpid()))

        with patch("signal_tui.LOCK_FILE", str(lock_file)):
            _release_lock()

        assert not lock_file.exists()

    def test_release_lock_not_ours(self, tmp_path):
        """Lock non nostro → non viene rimosso."""
        lock_file = tmp_path / "test.lock"
        lock_file.write_text("999999999")

        with patch("signal_tui.LOCK_FILE", str(lock_file)):
            _release_lock()

        # Il file deve ancora esistere (non è il nostro lock)
        assert lock_file.exists()

    def test_acquire_lock_exception_safe(self, tmp_path):
        """Eccezione durante lock → restituisce True (fail-safe)."""
        lock_file = tmp_path / "test.lock"

        with (
            patch("signal_tui.LOCK_FILE", str(lock_file)),
            patch("os.path.exists") as mock_exists,
        ):
            mock_exists.side_effect = Exception("Unexpected error")
            result = _acquire_lock()

        assert result is True  # fail-safe
