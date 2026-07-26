"""
Regression tests for backend.py — message cache (save, load, prune, receipts).
"""

from __future__ import annotations

import json
import time
import pytest
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# We'll test the cache functions by temporarily patching CACHE_FILE
from backend import (
    _load_cache,
    _save_cache,
    _prune_cache,
    _write_cache,
    _mark_as_read,
    _process_receipt,
    CACHE_RETENTION_DAYS,
)


class TestCacheSaveLoad:
    """💾 Salvataggio e caricamento della cache."""

    def test_save_and_load(self, tmp_cache_file, sample_messages):
        """Salva messaggi, ricarica, verifica contenuto."""
        with patch("backend.CACHE_FILE", tmp_cache_file):
            _save_cache(sample_messages)
            loaded = _load_cache()
        assert loaded == sample_messages

    def test_load_missing_file(self, tmp_cache_file):
        """File inesistente → dict vuoto."""
        with patch("backend.CACHE_FILE", tmp_cache_file):
            # Il file non esiste
            loaded = _load_cache()
        assert loaded == {}

    def test_load_corrupted_json(self, tmp_cache_file):
        """File JSON corrotto → dict vuoto."""
        tmp_cache_file.write_text("{corrupted json!")
        with patch("backend.CACHE_FILE", tmp_cache_file):
            loaded = _load_cache()
        assert loaded == {}

    def test_save_creates_directory(self, tmp_path):
        """Salva in una directory che non esiste → viene creata."""
        cache_dir = tmp_path / "nested" / "dir"
        cache_file = cache_dir / "messages.json"
        with patch("backend.CACHE_FILE", cache_file):
            with patch("backend.CACHE_DIR", cache_dir):
                _save_cache({"+39": []})
        assert cache_file.exists()


class TestCachePrune:
    """✂️ Potatura della cache (messaggi vecchi e limite 200)."""

    def test_prune_old_messages(self, tmp_cache_file):
        """Messaggi più vecchi di CACHE_RETENTION_DAYS vengono rimossi."""
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - CACHE_RETENTION_DAYS * 24 * 60 * 60 * 1000
        old_ts = cutoff - 1000  # un secondo prima del cutoff
        recent_ts = now_ms

        cache = {
            "+39": [
                {"text": "vecchio", "timestamp": old_ts},
                {"text": "nuovo", "timestamp": recent_ts},
            ]
        }
        with patch("backend.CACHE_FILE", tmp_cache_file):
            _save_cache(cache)
            _prune_cache()
            pruned = _load_cache()
        assert len(pruned["+39"]) == 1
        assert pruned["+39"][0]["text"] == "nuovo"

    def test_prune_max_200_messages(self, tmp_cache_file):
        """Limite 200 messaggi per contatto."""
        now_ms = int(time.time() * 1000)
        cache = {
            "+39": [
                {"text": f"msg-{i}", "timestamp": now_ms + i}
                for i in range(250)
            ]
        }
        with patch("backend.CACHE_FILE", tmp_cache_file):
            _save_cache(cache)
            _prune_cache()
            pruned = _load_cache()
        assert len(pruned["+39"]) == 200
        # Verifica che siano gli ultimi 200
        assert pruned["+39"][0]["text"] == "msg-50"

    def test_prune_empty_contact_removed(self, tmp_cache_file):
        """Contatto senza messaggi dopo prune → rimosso."""
        now_ms = int(time.time() * 1000)
        cache = {
            "+39": [
                {"text": "vecchio", "timestamp": 1},  # molto vecchio
            ]
        }
        with patch("backend.CACHE_FILE", tmp_cache_file):
            _save_cache(cache)
            _prune_cache()
            pruned = _load_cache()
        assert "+39" not in pruned

    def test_prune_no_modification(self, tmp_cache_file, sample_messages):
        """Se nessun messaggio va potato, il contenuto rimane invariato."""
        with patch("backend.CACHE_FILE", tmp_cache_file):
            _save_cache(sample_messages)
            content_before = tmp_cache_file.read_text()
            _prune_cache()
            content_after = tmp_cache_file.read_text()
        # Il contenuto del file deve rimanere invariato
        assert content_after == content_before


class TestCacheMarkAsRead:
    """✅ Marcatura messaggi come letti."""

    def test_mark_as_read(self, tmp_cache_file, sample_messages):
        """Messaggi non letti diventano letti."""
        with patch("backend.CACHE_FILE", tmp_cache_file):
            _save_cache(sample_messages)
            _mark_as_read("+391234567890")
            loaded = _load_cache()
        for msg in loaded["+391234567890"]:
            if not msg["is_mine"]:
                assert msg["read"] is True

    def test_mark_as_read_no_contact(self, tmp_cache_file):
        """Contatto inesistente → nessun errore."""
        with patch("backend.CACHE_FILE", tmp_cache_file):
            _mark_as_read("+999")  # non deve sollevare eccezioni


class TestProcessReceipt:
    """📬 Elaborazione receiptMessage (delivery e read)."""

    def test_receipt_delivery(self, tmp_cache_file, sample_messages):
        """Receipt di delivery → status 'delivered'."""
        # Prendi il timestamp del primo messaggio (non mine)
        ts = sample_messages["+391234567890"][0]["timestamp"]
        envelope = {
            "sourceNumber": "+391234567890",
            "receiptMessage": {
                "isDelivery": True,
                "isRead": False,
                "timestamps": [ts],
            },
        }
        updated = _process_receipt(envelope, sample_messages)
        assert len(updated) == 1
        assert updated[0]["status"] == "delivered"

    def test_receipt_read(self, tmp_cache_file, sample_messages):
        """Receipt di read → status 'read'."""
        # Prendi il timestamp del primo messaggio (non mine)
        ts = sample_messages["+391234567890"][0]["timestamp"]
        envelope = {
            "sourceNumber": "+391234567890",
            "receiptMessage": {
                "isDelivery": False,
                "isRead": True,
                "timestamps": [ts],
            },
        }
        updated = _process_receipt(envelope, sample_messages)
        assert len(updated) == 1
        assert updated[0]["status"] == "read"

    def test_receipt_no_match(self, sample_messages):
        """Timestamp che non matcha → lista vuota."""
        envelope = {
            "sourceNumber": "+391234567890",
            "receiptMessage": {
                "isDelivery": True,
                "isRead": False,
                "timestamps": [999999999],
            },
        }
        updated = _process_receipt(envelope, sample_messages)
        assert updated == []

    def test_receipt_no_timestamps(self, sample_messages):
        """Receipt senza timestamps → lista vuota."""
        envelope = {
            "sourceNumber": "+391234567890",
            "receiptMessage": {"isDelivery": True, "isRead": False},
        }
        updated = _process_receipt(envelope, sample_messages)
        assert updated == []

    def test_receipt_no_source(self, sample_messages):
        """Envelope senza source → lista vuota."""
        envelope = {
            "receiptMessage": {
                "isDelivery": True,
                "isRead": False,
                "timestamps": [1000001],
            },
        }
        updated = _process_receipt(envelope, sample_messages)
        assert updated == []

    def test_receipt_only_upgrades_status(self, sample_messages):
        """Non deve downgradare: sent → delivered OK, delivered → sent NO."""
        messages = {
            "+391234567890": [
                {"text": "test", "is_mine": True, "timestamp": 1000001,
                 "status": "delivered"},
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
