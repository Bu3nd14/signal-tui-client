"""
Regression tests for backend.py — message cache (SQLite: add, load, prune, receipts).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# We'll test the cache functions by temporarily patching DB_FILE / CACHE_DIR
from backend import (
    CACHE_RETENTION_DAYS,
    _add_message_to_cache,
    _load_cache,
    _mark_as_read,
    _process_receipt,
    _prune_cache,
    _update_message_status,
)


@pytest.fixture
def tmp_db(tmp_path: Path):
    """Point backend at a temporary SQLite DB and reset it between tests."""
    db_file = tmp_path / "messages.db"
    with patch("backend.DB_FILE", db_file), patch("backend.CACHE_DIR", tmp_path):
        yield db_file


class TestCacheAddLoad:
    """💾 Aggiunta e caricamento della cache SQLite."""

    def test_add_and_load(self, tmp_db):
        """Aggiunge messaggi, ricarica, verifica contenuto."""
        _add_message_to_cache(
            "+391234567890",
            "Ciao!",
            False,
            "Mario",
            1000,
        )
        _add_message_to_cache(
            "+391234567890",
            "Come stai?",
            True,
            "You",
            1001,
        )
        loaded = _load_cache()
        assert "+391234567890" in loaded
        msgs = loaded["+391234567890"]
        assert len(msgs) == 2
        assert msgs[0]["text"] == "Ciao!"
        assert msgs[0]["is_mine"] is False
        assert msgs[0]["status"] == "read"
        assert msgs[1]["text"] == "Come stai?"
        assert msgs[1]["is_mine"] is True
        assert msgs[1]["status"] == "sent"

    def test_load_empty_db(self, tmp_db):
        """DB vuoto → dict vuoto."""
        loaded = _load_cache()
        assert loaded == {}

    def test_add_creates_directory(self, tmp_path):
        """Aggiunge in una directory che non esiste → viene creata."""
        cache_dir = tmp_path / "nested" / "dir"
        db_file = cache_dir / "messages.db"
        with patch("backend.DB_FILE", db_file), patch("backend.CACHE_DIR", cache_dir):
            _add_message_to_cache("+39", "test", True, "You", 1)
        assert db_file.exists()

    def test_add_preserves_optional_fields(self, tmp_db):
        """I campi opzionali (quote, attachment, msg_type) vengono salvati."""
        _add_message_to_cache(
            "+391234567890",
            "img",
            False,
            "Mario",
            2000,
            quote_text="citazione",
            msg_type="image",
            attachment_info="photo.jpg",
            attachment_id="att-123",
        )
        loaded = _load_cache()
        msg = loaded["+391234567890"][0]
        assert msg["quote_text"] == "citazione"
        assert msg["msg_type"] == "image"
        assert msg["attachment_info"] == "photo.jpg"
        assert msg["attachment_id"] == "att-123"

    def test_quote_attachment_fields_survive_roundtrip(self, tmp_db):
        """I metadati della quote-media (id + content_type) sopravvivono al DB."""
        _add_message_to_cache(
            "42",
            "reply",
            False,
            "Mario",
            2000,
            quote_text="quoted",
            quote_attachment_id="tgref:42:12",
            quote_content_type="image/png",
        )
        loaded = _load_cache()
        msg = loaded["42"][0]
        assert msg["quote_attachment_id"] == "tgref:42:12"
        assert msg["quote_content_type"] == "image/png"

    def test_quote_attachment_path_survives_roundtrip(self, tmp_db):
        """Il path della thumbnail Signal (persistente) sopravvive al DB."""
        _add_message_to_cache(
            "42",
            "reply",
            False,
            "Mario",
            2000,
            quote_text="quoted",
            quote_attachment_path="/tmp/quote-thumbs/abc123.png",
        )
        loaded = _load_cache()
        msg = loaded["42"][0]
        assert msg["quote_attachment_path"] == "/tmp/quote-thumbs/abc123.png"


class TestCachePrune:
    """✂️ Potatura della cache (messaggi vecchi e limite 200)."""

    def test_prune_old_messages(self, tmp_db):
        """Messaggi più vecchi di CACHE_RETENTION_DAYS vengono rimossi."""
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - CACHE_RETENTION_DAYS * 24 * 60 * 60 * 1000
        old_ts = cutoff - 1000  # un secondo prima del cutoff
        recent_ts = now_ms

        _add_message_to_cache("+39", "vecchio", False, "Mario", old_ts)
        _add_message_to_cache("+39", "nuovo", False, "Mario", recent_ts)

        _prune_cache()
        pruned = _load_cache()
        assert len(pruned["+39"]) == 2  # time-based prune disabled, both kept
        assert pruned["+39"][0]["text"] == "vecchio"  # older msg now first

    def test_prune_max_200_messages(self, tmp_db):
        """Limite 200 messaggi per contatto."""
        now_ms = int(time.time() * 1000)
        for i in range(250):
            _add_message_to_cache("+39", f"msg-{i}", False, "Mario", now_ms + i)

        _prune_cache()
        pruned = _load_cache()
        assert len(pruned["+39"]) == 200
        # Verifica che siano gli ultimi 200
        assert pruned["+39"][0]["text"] == "msg-50"

    def test_prune_empty_contact_removed(self, tmp_db):
        """Contatto con solo messaggi vecchi → rimosso dopo prune."""
        _add_message_to_cache("+39", "vecchio", False, "Mario", 1)  # molto vecchio
        _prune_cache()
        pruned = _load_cache()
        assert "+39" in pruned  # time prune disabled

    def test_prune_no_modification(self, tmp_db):
        """Se nessun messaggio va potato, il contenuto rimane invariato."""
        now_ms = int(time.time() * 1000)
        _add_message_to_cache("+39", "nuovo", False, "Mario", now_ms)
        _prune_cache()
        pruned = _load_cache()
        assert len(pruned["+39"]) == 1
        assert pruned["+39"][0]["text"] == "nuovo"


class TestCacheMarkAsRead:
    """✅ Marcatura messaggi come letti."""

    def test_mark_as_read(self, tmp_db):
        """Messaggi non letti diventano letti."""
        _add_message_to_cache("+391234567890", "Ciao!", False, "Mario", 1000)
        _add_message_to_cache("+391234567890", "Mio", True, "You", 1001)

        _mark_as_read("+391234567890")
        loaded = _load_cache()
        for msg in loaded["+391234567890"]:
            assert msg["read"] is True

    def test_mark_as_read_no_contact(self, tmp_db):
        """Contatto inesistente → nessun errore."""
        _mark_as_read("+999")  # non deve sollevare eccezioni


class TestUpdateMessageStatus:
    """📝 Aggiornamento dello status di un messaggio."""

    def test_update_status(self, tmp_db):
        """Lo status di un messaggio viene aggiornato per timestamp."""
        _add_message_to_cache("+391234567890", "Ciao!", True, "You", 1000)
        _update_message_status(1000, "delivered", "signal", "+391234567890")
        loaded = _load_cache()
        assert loaded["+391234567890"][0]["status"] == "delivered"

    def test_update_status_no_match(self, tmp_db):
        """Timestamp inesistente → nessun errore, nessuna modifica."""
        _add_message_to_cache("+391234567890", "Ciao!", True, "You", 1000)
        _update_message_status(9999, "delivered", "signal", "+391234567890")
        loaded = _load_cache()
        assert loaded["+391234567890"][0]["status"] == "sent"

    def test_update_status_scoped_by_protocol(self, tmp_db):
        """Stesso timestamp su protocolli diversi → aggiorna solo quello giusto."""
        _add_message_to_cache(
            "+391234567890", "Ciao!", True, "You", 1000, protocol="signal"
        )
        _add_message_to_cache(
            "391234567890@c.us", "Ciao!", True, "You", 1000, protocol="whatsapp"
        )
        _update_message_status(1000, "delivered", "signal", "+391234567890")
        loaded = _load_cache()
        assert loaded["+391234567890"][0]["status"] == "delivered"
        assert loaded["391234567890@c.us"][0]["status"] == "sent"

    def test_update_status_scoped_by_contact(self, tmp_db):
        """Stesso timestamp su contatti diversi (stesso protocollo) → aggiorna solo quello giusto."""
        _add_message_to_cache(
            "+391234567890", "Ciao!", True, "You", 1000, protocol="signal"
        )
        _add_message_to_cache(
            "+399999999999", "Ciao!", True, "You", 1000, protocol="signal"
        )
        _update_message_status(1000, "delivered", "signal", "+391234567890")
        loaded = _load_cache()
        assert loaded["+391234567890"][0]["status"] == "delivered"
        assert loaded["+399999999999"][0]["status"] == "sent"

    def test_status_transition_is_conditional_and_never_regresses_receipts(
        self, tmp_db
    ):
        _add_message_to_cache("+39", "Ciao", True, "You", 1000, status="pending")
        assert _update_message_status(
            1000,
            "sent",
            "signal",
            "+39",
            text="Ciao",
            expected_statuses=("pending",),
        )
        assert not _update_message_status(1000, "failed", "signal", "+39", text="Ciao")
        _update_message_status(1000, "read", "signal", "+39", text="Ciao")
        assert not _update_message_status(
            1000, "delivered", "signal", "+39", text="Ciao"
        )
        assert _load_cache()["+39"][0]["status"] == "read"


class TestProcessReceipt:
    """📬 Elaborazione receiptMessage (delivery e read)."""

    def _make_cache(self):
        """Return an in-memory cache with a sent message."""
        return {
            "+391234567890": [
                {
                    "text": "Ciao!",
                    "is_mine": True,
                    "sender": "You",
                    "timestamp": 1000001,
                    "quote_text": None,
                    "msg_type": "text",
                    "attachment_info": None,
                    "attachment_id": None,
                    "read": True,
                    "status": "sent",
                },
            ]
        }

    def test_receipt_delivery(self, tmp_db):
        """Receipt di delivery → status 'delivered'."""
        cache = self._make_cache()
        envelope = {
            "sourceNumber": "+391234567890",
            "receiptMessage": {
                "isDelivery": True,
                "isRead": False,
                "timestamps": [1000001],
            },
        }
        updated = _process_receipt(envelope, cache)
        assert len(updated) == 1
        assert updated[0]["status"] == "delivered"

    def test_receipt_read(self, tmp_db):
        """Receipt di read → status 'read'."""
        cache = self._make_cache()
        envelope = {
            "sourceNumber": "+391234567890",
            "receiptMessage": {
                "isDelivery": False,
                "isRead": True,
                "timestamps": [1000001],
            },
        }
        updated = _process_receipt(envelope, cache)
        assert len(updated) == 1
        assert updated[0]["status"] == "read"

    def test_receipt_no_match(self, tmp_db):
        """Timestamp che non matcha → lista vuota."""
        cache = self._make_cache()
        envelope = {
            "sourceNumber": "+391234567890",
            "receiptMessage": {
                "isDelivery": True,
                "isRead": False,
                "timestamps": [999999999],
            },
        }
        updated = _process_receipt(envelope, cache)
        assert updated == []

    def test_receipt_no_timestamps(self, tmp_db):
        """Receipt senza timestamps → lista vuota."""
        cache = self._make_cache()
        envelope = {
            "sourceNumber": "+391234567890",
            "receiptMessage": {"isDelivery": True, "isRead": False},
        }
        updated = _process_receipt(envelope, cache)
        assert updated == []

    def test_receipt_no_source(self, tmp_db):
        """Envelope senza source → lista vuota."""
        cache = self._make_cache()
        envelope = {
            "receiptMessage": {
                "isDelivery": True,
                "isRead": False,
                "timestamps": [1000001],
            },
        }
        updated = _process_receipt(envelope, cache)
        assert updated == []

    def test_receipt_only_upgrades_status(self, tmp_db):
        """Non deve downgradare: sent → delivered OK, delivered → sent NO."""
        messages = {
            "+391234567890": [
                {
                    "text": "test",
                    "is_mine": True,
                    "timestamp": 1000001,
                    "status": "delivered",
                },
            ]
        }
        envelope = {
            "sourceNumber": "+391234567890",
            "receiptMessage": {
                "isDelivery": True,
                "isRead": False,
                "timestamps": [1000001],
            },
        }
        updated = _process_receipt(envelope, messages)
        # Già 'delivered', non deve tornare 'sent'
        assert len(updated) == 0
