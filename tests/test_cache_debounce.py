"""
Regression tests for the SQLite cache migration.

The old JSON cache used a debounce mechanism (_maybe_flush_cache /
_flush_cache) to batch writes. With SQLite, writes are incremental, so
those methods no longer exist. These tests verify the new behavior:

1. _add_message_to_cache() persists each message immediately (incremental).
2. _mark_as_read() persists read state to SQLite.
3. _update_message_status() persists status changes to SQLite.
4. _process_receipt_envelope() persists receipt status changes to SQLite.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from signal_tui import SignalTUI
from backend import (
    Contact,
    _load_cache,
    _add_message_to_cache,
    _mark_as_read,
    _update_message_status,
)


@pytest.fixture
def tmp_db(tmp_path: Path):
    """Point backend at a temporary SQLite DB and reset it between tests."""
    db_file = tmp_path / "messages.db"
    with patch("backend.DB_FILE", db_file), \
         patch("backend.CACHE_DIR", tmp_path):
        yield db_file


class TestIncrementalWrites:
    """💾 Verifica che le scritture siano incrementali (niente debounce)."""

    def test_add_message_persists_immediately(self, tmp_db):
        """Ogni _add_message_to_cache() scrive subito su SQLite."""
        _add_message_to_cache("+391234567890", "Ciao!", False, "Mario", 1000)
        # Reload from DB: the message must already be there
        loaded = _load_cache()
        assert len(loaded["+391234567890"]) == 1
        assert loaded["+391234567890"][0]["text"] == "Ciao!"

    def test_multiple_adds_all_persisted(self, tmp_db):
        """Più messaggi vengono tutti persistiti (nessun batch)."""
        for i in range(5):
            _add_message_to_cache("+391234567890", f"msg-{i}", False, "Mario", 1000 + i)
        loaded = _load_cache()
        assert len(loaded["+391234567890"]) == 5

    def test_no_debounce_attributes(self):
        """Gli attributi di debounce non esistono più."""
        app = SignalTUI()
        assert not hasattr(app, "_pending_saves")
        assert not hasattr(app, "_CACHE_FLUSH_THRESHOLD")
        assert not hasattr(app, "_CACHE_FLUSH_INTERVAL")
        assert not hasattr(app, "_last_flush_time")
        assert not hasattr(app, "_flush_cache")
        assert not hasattr(app, "_maybe_flush_cache")


class TestMarkAsReadPersistence:
    """✅ Verifica che _mark_as_read() persista su SQLite."""

    def test_mark_as_read_persists(self, tmp_db):
        """_mark_as_read() aggiorna lo stato read nel DB."""
        _add_message_to_cache("+391234567890", "Ciao!", False, "Mario", 1000)
        _mark_as_read("+391234567890")
        loaded = _load_cache()
        assert loaded["+391234567890"][0]["read"] is True


class TestUpdateMessageStatusPersistence:
    """📝 Verifica che _update_message_status() persista su SQLite."""

    def test_update_status_persists(self, tmp_db):
        """_update_message_status() aggiorna lo status nel DB."""
        _add_message_to_cache("+391234567890", "Ciao!", True, "You", 1000)
        _update_message_status(1000, "delivered")
        loaded = _load_cache()
        assert loaded["+391234567890"][0]["status"] == "delivered"


class TestReceiptPersistence:
    """📬 Verifica che _process_receipt_envelope() persista su SQLite."""

    def _make_app(self, tmp_db) -> SignalTUI:
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

    def test_receipt_updates_cache_and_db(self, tmp_db):
        """_process_receipt_envelope aggiorna self._cache e il DB."""
        app = self._make_app(tmp_db)
        app.selected_contact = Contact(number="+391234567890", name="Test")

        envelope = {
            "receiptMessage": {
                "isRead": True,
                "timestamps": [1000],
            },
            "sourceNumber": "+391234567890",
        }

        with patch.object(app, "call_from_thread") as mock_cft:
            result = app._process_receipt_envelope(envelope)

        assert result is True
        # The message in self._cache must have status "read"
        assert app._cache["+391234567890"][0]["status"] == "read"
        # call_from_thread should be called to update the UI
        mock_cft.assert_called_once()

    def test_receipt_no_match_returns_false(self, tmp_db):
        """Se il timestamp del receipt non matcha, non aggiorna nulla."""
        app = self._make_app(tmp_db)

        envelope = {
            "receiptMessage": {
                "isRead": True,
                "timestamps": [9999],  # no match
            },
            "sourceNumber": "+391234567890",
        }

        with patch.object(app, "call_from_thread") as mock_cft:
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
