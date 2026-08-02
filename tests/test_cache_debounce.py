"""
Regression tests for the cache debounce optimization.

Verifies that:
1. _maybe_flush_cache() only writes to disk every 5 messages (not every message).
2. _flush_cache() forces an immediate write + prune + reload.
3. _update_unread_badges(contact_number) only recomputes the given contact.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from signal_tui import SignalTUI
from backend import Contact


class TestCacheDebounce:
    """🔄 Verifica il debounce del salvataggio cache."""

    def _make_app(self) -> SignalTUI:
        app = SignalTUI()
        app._cache = {"+391234567890": []}
        return app

    def test_maybe_flush_does_not_write_before_threshold(self):
        """Prima di 5 messaggi, _maybe_flush_cache non scrive su disco."""
        app = self._make_app()

        with patch("signal_tui._save_cache") as mock_save, \
             patch("signal_tui._prune_cache") as mock_prune, \
             patch("signal_tui._load_cache", return_value={}) as mock_load:
            for _ in range(4):
                app._maybe_flush_cache()

            # No disk writes before the threshold
            mock_save.assert_not_called()
            mock_prune.assert_not_called()
            mock_load.assert_not_called()
            assert app._pending_saves == 4

    def test_maybe_flush_writes_at_threshold(self):
        """Al 5° messaggio, _maybe_flush_cache scrive su disco."""
        app = self._make_app()

        with patch("signal_tui._save_cache") as mock_save, \
             patch("signal_tui._prune_cache") as mock_prune, \
             patch("signal_tui._load_cache", return_value={}) as mock_load:
            for _ in range(5):
                app._maybe_flush_cache()

            # Exactly one write at the threshold
            mock_save.assert_called_once()
            mock_prune.assert_called_once()
            mock_load.assert_called_once()
            assert app._pending_saves == 0

    def test_flush_cache_resets_counter(self):
        """_flush_cache() forza la scrittura e resetta il contatore."""
        app = self._make_app()
        app._pending_saves = 3

        with patch("signal_tui._save_cache") as mock_save, \
             patch("signal_tui._prune_cache") as mock_prune, \
             patch("signal_tui._load_cache", return_value={}) as mock_load:
            app._flush_cache()

            mock_save.assert_called_once()
            mock_prune.assert_called_once()
            mock_load.assert_called_once()
            assert app._pending_saves == 0

    def test_maybe_flush_after_flush_restarts_counting(self):
        """Dopo un flush, il contatore riparte da zero."""
        app = self._make_app()

        with patch("signal_tui._save_cache"), \
             patch("signal_tui._prune_cache"), \
             patch("signal_tui._load_cache", return_value={}):
            # 5 messages → flush
            for _ in range(5):
                app._maybe_flush_cache()
            assert app._pending_saves == 0

            # 3 more messages → no flush yet
            for _ in range(3):
                app._maybe_flush_cache()
            assert app._pending_saves == 3


class TestIncrementalUnreadBadges:
    """🔄 Verifica l'aggiornamento incrementale dei badge unread."""

    def _make_app(self) -> SignalTUI:
        app = SignalTUI()
        c1 = Contact(number="+391", name="Alice", aci="a1")
        c2 = Contact(number="+392", name="Bob", aci="b2")
        app.contacts = [c1, c2]
        app._cache = {
            "+391": [
                {"text": "hi", "is_mine": False, "read": False, "timestamp": 1},
                {"text": "hello", "is_mine": False, "read": True, "timestamp": 2},
            ],
            "+392": [
                {"text": "yo", "is_mine": False, "read": False, "timestamp": 3},
            ],
        }
        app._unread_counts = {}
        return app

    def test_incremental_only_updates_given_contact(self):
        """Con contact_number, calcola solo per quel contatto."""
        app = self._make_app()

        # Patch _sort_contacts and query_one to avoid UI interaction
        with patch.object(app, "_sort_contacts") as mock_sort, \
             patch.object(app, "query_one") as mock_query:
            mock_list = type("FakeList", (), {"clear": lambda self: None, "append": lambda self, x: None, "index": 0})()
            mock_query.return_value = mock_list

            app._update_unread_badges("+391")

            # Only +391 should be in _unread_counts
            assert app._unread_counts == {"+391": 1}
            mock_sort.assert_called_once()

    def test_incremental_no_change_returns_early(self):
        """Se il conteggio non cambia, non ricostruisce la lista."""
        app = self._make_app()
        app._unread_counts = {"+391": 1, "+392": 1}

        with patch.object(app, "_sort_contacts") as mock_sort, \
             patch.object(app, "query_one") as mock_query:
            app._update_unread_badges("+391")

            # No change → no re-sort, no rebuild
            mock_sort.assert_not_called()
            mock_query.assert_not_called()

    def test_full_update_all_contacts(self):
        """Senza contact_number, calcola per tutti i contatti."""
        app = self._make_app()

        with patch.object(app, "_sort_contacts") as mock_sort, \
             patch.object(app, "query_one") as mock_query:
            mock_list = type("FakeList", (), {"clear": lambda self: None, "append": lambda self, x: None, "index": 0})()
            mock_query.return_value = mock_list

            app._update_unread_badges()

            assert app._unread_counts == {"+391": 1, "+392": 1}
            mock_sort.assert_called_once()
