"""
Regression tests for the cache debounce optimization.

Verifies that:
1. _maybe_flush_cache() only writes to disk every 5 messages (not every message).
2. _flush_cache() forces an immediate write + prune + reload.
3. _update_unread_badges(contact_number) only recomputes the given contact.
"""

from __future__ import annotations

import sys
import time
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


class TestFlushOnExit:
    """🚪 Verifica che on_exit() salvi le modifiche pendenti."""

    def _make_app(self) -> SignalTUI:
        app = SignalTUI()
        app._cache = {"+391234567890": []}
        return app

    def test_on_exit_flushes_pending_saves(self):
        """on_exit() chiama _flush_cache() quando ci sono modifiche pendenti."""
        app = self._make_app()
        app._pending_saves = 3

        with patch.object(app, "_flush_cache") as mock_flush:
            app.on_exit()
            mock_flush.assert_called_once()

    def test_on_exit_no_flush_when_no_pending(self):
        """on_exit() NON chiama _flush_cache() se non ci sono modifiche pendenti."""
        app = self._make_app()
        app._pending_saves = 0

        with patch.object(app, "_flush_cache") as mock_flush:
            app.on_exit()
            mock_flush.assert_not_called()


class TestSafetyTimer:
    """⏱️ Verifica il timer di sicurezza per il flush periodico."""

    def _make_app(self) -> SignalTUI:
        app = SignalTUI()
        app._cache = {"+391234567890": []}
        return app

    def test_flush_cache_updates_last_flush_time(self):
        """_flush_cache() aggiorna _last_flush_time."""
        app = self._make_app()
        app._last_flush_time = 0

        with patch("signal_tui._save_cache"), \
             patch("signal_tui._prune_cache"), \
             patch("signal_tui._load_cache", return_value={}):
            app._flush_cache()

            assert app._last_flush_time > 0

    def test_poll_worker_flushes_after_interval(self):
        """_poll_worker() chiama _flush_cache() quando _pending_saves > 0
        e sono passati più di _CACHE_FLUSH_INTERVAL secondi dall'ultimo flush."""
        app = self._make_app()
        app._pending_saves = 2
        app._last_flush_time = 0  # simulate a very old flush
        app._polling_active = True
        app._use_daemon = False  # avoid calling rpc.receive()

        with patch.object(app, "_flush_cache") as mock_flush, \
             patch("signal_tui.time.sleep", side_effect=lambda s: setattr(app, "_polling_active", False)):
            app._poll_worker()

            mock_flush.assert_called_once()

    def test_poll_worker_no_flush_within_interval(self):
        """_poll_worker() NON chiama _flush_cache() se l'ultimo flush è recente."""
        app = self._make_app()
        app._pending_saves = 2
        app._last_flush_time = time.time()  # recent flush
        app._polling_active = True
        app._use_daemon = False  # avoid calling rpc.receive()

        with patch.object(app, "_flush_cache") as mock_flush, \
             patch("signal_tui.time.sleep", side_effect=lambda s: setattr(app, "_polling_active", False)):
            app._poll_worker()

            mock_flush.assert_not_called()


class TestReceiptRaceCondition:
    """🔒 Verifica che _process_receipt_envelope non perda modifiche
    quando _flush_cache() viene chiamato concorrentemente."""

    def _make_app(self) -> SignalTUI:
        app = SignalTUI()
        app._cache = {
            "+391234567890": [
                {
                    "text": "Ciao!",
                    "is_mine": True,
                    "sender": "You",
                    "timestamp": 1000,
                    "quote_text": None,
                    "msg_type": "text",
                    "attachment_info": None,
                    "attachment_id": None,
                    "read": True,
                    "status": "sent",
                }
            ]
        }
        return app

    def test_receipt_updates_current_cache(self):
        """_process_receipt_envelope aggiorna self._cache direttamente
        (non un riferimento salvato che potrebbe diventare stale)."""
        app = self._make_app()
        app.selected_contact = Contact(number="+391234567890", name="Test")

        envelope = {
            "receiptMessage": {
                "isRead": True,
                "timestamps": [1000],
            },
            "sourceNumber": "+391234567890",
        }

        with patch("signal_tui._save_cache"), \
             patch("signal_tui._prune_cache"), \
             patch("signal_tui._load_cache", return_value={}), \
             patch.object(app, "call_from_thread") as mock_cft:
            result = app._process_receipt_envelope(envelope)

        assert result is True
        # The message in self._cache must have status "read"
        assert app._cache["+391234567890"][0]["status"] == "read"
        # call_from_thread should be called to update the UI
        mock_cft.assert_called_once()

    def test_receipt_survives_flush_cache(self):
        """Le modifiche del receipt non vengono perse quando _flush_cache()
        viene chiamato dopo _process_receipt_envelope."""
        app = self._make_app()

        envelope = {
            "receiptMessage": {
                "isRead": True,
                "timestamps": [1000],
            },
            "sourceNumber": "+391234567890",
        }

        # Simulate: receipt processed, then flush_cache called
        with patch("signal_tui._save_cache") as mock_save, \
             patch("signal_tui._prune_cache") as mock_prune, \
             patch("signal_tui._load_cache", return_value={
                 "+391234567890": [
                     {
                         "text": "Ciao!",
                         "is_mine": True,
                         "sender": "You",
                         "timestamp": 1000,
                         "quote_text": None,
                         "msg_type": "text",
                         "attachment_info": None,
                         "attachment_id": None,
                         "read": True,
                         "status": "read",  # already updated by receipt
                     }
                 ]
             }), \
             patch.object(app, "call_from_thread"):
            app._process_receipt_envelope(envelope)
            app._flush_cache()

            # The saved cache must have status "read"
            saved_cache = mock_save.call_args[0][0]
            assert saved_cache["+391234567890"][0]["status"] == "read"

    def test_receipt_no_match_returns_false(self):
        """Se il timestamp del receipt non matcha, non aggiorna nulla."""
        app = self._make_app()

        envelope = {
            "receiptMessage": {
                "isRead": True,
                "timestamps": [9999],  # no match
            },
            "sourceNumber": "+391234567890",
        }

        with patch("signal_tui._save_cache"), \
             patch("signal_tui._prune_cache"), \
             patch("signal_tui._load_cache", return_value={}), \
             patch.object(app, "call_from_thread") as mock_cft:
            result = app._process_receipt_envelope(envelope)

        assert result is False
        # Status unchanged
        assert app._cache["+391234567890"][0]["status"] == "sent"
        # call_from_thread should NOT be called
        mock_cft.assert_not_called()


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
