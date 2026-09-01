"""Regression tests for bug #44 (data loss via multi-row ``_update_message_id``).

Covers the three design points of the approved fix:

1. ``_update_message_id`` targets a single id-less row within the echo window
   (closest timestamp, deterministic ``rowid`` tie-break) and returns ``bool``.
2. ``_dedup_messages_by_id`` never merges a partition whose timestamps diverge
   beyond the echo window (logs a warning instead).
3. WhatsApp ``ingest_message`` reuses a ``failed`` id-less DB row (not in the
   in-memory cache) instead of inserting a duplicate, mirroring it into cache.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import protocols.db as backend_mod
from protocols.db import (
    _ECHO_MATCH_WINDOW_MS,
    _add_message_to_cache,
    _dedup_messages_by_id,
    _update_message_id,
)
from protocols.whatsapp import WhatsAppBackend


@pytest.fixture
def tmp_db(tmp_path: Path):
    """Point backend at a temporary SQLite DB and reset it between tests."""
    db_file = tmp_path / "messages.db"
    with (
        patch("protocols.db.DB_FILE", db_file),
        patch("protocols.db.CACHE_DIR", tmp_path),
    ):
        yield db_file


def _rows(db_file: Path, contact: str, text: str) -> list[tuple]:
    conn = sqlite3.connect(db_file)
    try:
        return conn.execute(
            "SELECT msg_id, timestamp, status FROM messages "
            "WHERE contact_number = ? AND text = ? ORDER BY timestamp",
            (contact, text),
        ).fetchall()
    finally:
        conn.close()


class TestUpdateMessageIdWindow:
    """🎯 Punto 1: ``_update_message_id`` mirato a UNA riga entro la finestra."""

    def test_update_message_id_multi_row_same_text(self, tmp_db):
        """Due righe id-less con stesso testo: solo la più vicina riceve l'id."""
        contact = "123@g.us"
        _add_message_to_cache(contact, "ok", True, "You", 1000, protocol="whatsapp")
        _add_message_to_cache(contact, "ok", True, "You", 2000, protocol="whatsapp")

        assert (
            _update_message_id(
                contact, "ok", True, 1900, "real-id", protocol="whatsapp"
            )
            is True
        )

        rows = _rows(tmp_db, contact, "ok")
        # La riga a ts=2000 (la più vicina a 1900) ha preso l'id e il ts 1900;
        # l'altra (ts=1000) resta id-less e intatta.
        assert rows == [(None, 1000, "sent"), ("real-id", 1900, "sent")]

    def test_update_message_id_outside_window_returns_false(self, tmp_db):
        """Riga oltre la finestra → False, nessuna modifica."""
        contact = "123@g.us"
        ts = 1000
        _add_message_to_cache(contact, "ok", True, "You", ts, protocol="whatsapp")

        far_ts = ts + _ECHO_MATCH_WINDOW_MS + 1
        assert (
            _update_message_id(
                contact, "ok", True, far_ts, "real-id", protocol="whatsapp"
            )
            is False
        )

        assert _rows(tmp_db, contact, "ok") == [(None, ts, "sent")]


class TestDedupDefensiveGuard:
    """🛡️ Punto 2: dedup difensiva su partizioni con timestamp divergenti."""

    def test_dedup_skips_partition_with_divergent_timestamps(self, tmp_db):
        """Partizione con Δ > finestra → 0 rimozioni (e log)."""
        contact = "123@g.us"
        _add_message_to_cache(
            contact, "ok", True, "You", 1000, protocol="whatsapp", msg_id="shared"
        )
        _add_message_to_cache(
            contact,
            "ok",
            True,
            "You",
            1000 + _ECHO_MATCH_WINDOW_MS + 1,
            protocol="whatsapp",
            msg_id="shared",
        )

        with patch("protocols.db.logger.warning") as mock_warn:
            removed = _dedup_messages_by_id()

        assert removed == 0
        mock_warn.assert_called_once()
        assert len(_rows(tmp_db, contact, "ok")) == 2


class TestIngestFailedRowReuse:
    """♻️ Punto 3: fallback DB post-send per il retry di un messaggio failed."""

    def test_ingest_reuses_failed_db_row_without_cache_duplication(self, tmp_db):
        """Coerenza cache/DB: la riga failed del DB viene riusata, non duplicata."""
        contact = "123@g.us"
        ts = 1700000000
        _add_message_to_cache(
            contact, "ok", True, "You", ts, protocol="whatsapp", status="failed"
        )

        backend = WhatsAppBackend(api_url="http://api.test", media_dir="")
        backend.cache = {}

        added = backend.ingest_message(
            contact,
            {"id": "real-id", "text": "ok", "is_mine": True, "sender": "You"},
            ts,
        )

        assert added is False
        # Cache e DB restano coerenti: una sola riga, con id reale e status
        # avanzato a "sent" (l'echo è la prova che il messaggio è partito).
        assert len(backend.cache[contact]) == 1
        entry = backend.cache[contact][0]
        assert entry["id"] == "real-id"
        assert entry["text"] == "ok"
        assert entry["timestamp"] == ts
        assert entry["status"] == "sent"

        db_rows = _rows(tmp_db, contact, "ok")
        assert len(db_rows) == 1
        assert db_rows[0] == ("real-id", ts, "sent")

    def test_reuse_scoped_to_failed_row_when_sent_idless_is_closer(self, tmp_db):
        """Multi-riga id-less (failed + sent stesso testo): l'id va SOLO alla failed.

        Riproduce il bug del tester: con una riga ``failed`` (ts più lontano) e
        una ``sent`` (ts più vicino all'echo), il fallback deve agganciare l'id
        SOLO alla ``failed``, lasciando la ``sent`` id-less — senza split-brain.
        """
        contact = "123@g.us"
        ts_failed = 1700000000000
        ts_sent = 1700000100000
        echo_ts = 1700000105000  # più vicino alla riga sent
        _add_message_to_cache(
            contact, "ok", True, "You", ts_failed, protocol="whatsapp", status="failed"
        )
        _add_message_to_cache(
            contact, "ok", True, "You", ts_sent, protocol="whatsapp", status="sent"
        )

        backend = WhatsAppBackend(api_url="http://api.test", media_dir="")
        backend.cache = {}

        added = backend.ingest_message(
            contact,
            {"id": "real-id", "text": "ok", "is_mine": True, "sender": "You"},
            echo_ts,
        )

        assert added is False
        # Cache coerente col DB: una sola entry, specchio della riga failed.
        assert len(backend.cache[contact]) == 1
        entry = backend.cache[contact][0]
        assert entry["id"] == "real-id"
        assert entry["status"] == "sent"
        assert entry["timestamp"] == echo_ts

        # DB: la riga failed ha preso id+ts+status sent; la sent resta id-less.
        rows = _rows(tmp_db, contact, "ok")
        assert rows == [
            (None, ts_sent, "sent"),
            ("real-id", echo_ts, "sent"),
        ]

    def test_receipt_fallback_uniqueness_after_failed_row_reuse(self, tmp_db):
        """La riga riusata ha ora un id: il receipt matcha per id, non via fallback id-less."""
        contact = "1@c.us"
        ts = 1700000000
        _add_message_to_cache(
            contact, "ok", True, "You", ts, protocol="whatsapp", status="failed"
        )

        backend = WhatsAppBackend(api_url="http://api.test", media_dir="")
        backend.cache = {}
        assert (
            backend.ingest_message(
                contact,
                {"id": "real-id", "text": "ok", "is_mine": True, "sender": "You"},
                ts,
            )
            is False
        )

        with (
            patch.object(backend_mod, "_update_message_status_by_id") as mock_by_id,
            patch.object(backend_mod, "_update_message_status") as mock_by_ts,
        ):
            updated = backend.process_receipt(
                {"message_ids": ["real-id"], "is_read": False}
            )

        assert [u["id"] for u in updated] == ["real-id"]
        assert backend.cache[contact][0]["status"] == "delivered"
        mock_by_id.assert_called_once_with(
            "real-id", "delivered", protocol="whatsapp", contact_number=contact
        )
        mock_by_ts.assert_not_called()
