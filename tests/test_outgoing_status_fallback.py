"""Regression tests: transizione pending→sent robusta al timestamp dell'echo.

Caso reale (chat "Tartufi Bolliti", 21/08/2026): l'echo di WAHA può sostituire
il timestamp ottimistico del client con quello del server PRIMA che il worker
esegua la transizione → il match per ``timestamp`` di ``_update_message_status``
fallisce e la bolla resta "grigia".  Il fallback per testo
(``_update_message_status_by_text`` + ``_transition_outgoing_status``) deve
aggiornare comunque la riga.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend import (
    _add_message_to_cache,
    _update_message_status,
    _update_message_status_by_text,
)
from models import PROTOCOL_WHATSAPP
from tui.send import SendMixin


@pytest.fixture
def tmp_db(tmp_path: Path):
    """Point backend at a temporary SQLite DB and reset it between tests."""
    db_file = tmp_path / "messages.db"
    with patch("backend.DB_FILE", db_file), patch("backend.CACHE_DIR", tmp_path):
        yield db_file


def _seed_outgoing(tmp_db, contact: str, text: str, ts: int, status: str = "pending"):
    _add_message_to_cache(
        contact,
        text,
        True,
        "You",
        ts,
        protocol=PROTOCOL_WHATSAPP,
        status=status,
    )


def _row_status(tmp_db, contact: str, text: str) -> str:
    conn = sqlite3.connect(tmp_db)
    try:
        cur = conn.execute(
            "SELECT status FROM messages WHERE contact_number = ? AND text = ?",
            (contact, text),
        )
        return cur.fetchone()[0]
    finally:
        conn.close()


class TestUpdateMessageStatusByText:
    """Fallback DB: aggiorna la riga outgoing più recente per testo."""

    def test_updates_row_when_timestamp_mismatch(self, tmp_db):
        contact = "123@g.us"
        text = "hai finito col calcolo?"
        _seed_outgoing(tmp_db, contact, text, 1787342685618)  # ts client persistito

        # L'echo ha già spostato la riga sul ts server (race send veloce):
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            "UPDATE messages SET timestamp = ? WHERE text = ?", (1787342685000, text)
        )
        conn.commit()
        conn.close()

        # Il match per timestamp client fallisce:
        assert not _update_message_status(
            1787342685618,
            "sent",
            PROTOCOL_WHATSAPP,
            contact,
            text=text,
            expected_statuses=("pending",),
        )
        # Il fallback per testo aggiorna la riga:
        assert _update_message_status_by_text(
            text, "sent", PROTOCOL_WHATSAPP, contact, expected_statuses=("pending",)
        )
        assert _row_status(tmp_db, contact, text) == "sent"

    def test_respects_expected_statuses(self, tmp_db):
        contact = "123@g.us"
        text = "ciao"
        _seed_outgoing(tmp_db, contact, text, 1000, status="sent")
        # Già `sent` → expected ("pending",) non matcha:
        assert not _update_message_status_by_text(
            text,
            "delivered",
            PROTOCOL_WHATSAPP,
            contact,
            expected_statuses=("pending",),
        )
        # Senza expected, il rank guard accetta delivered (sent→delivered):
        assert _update_message_status_by_text(
            text, "delivered", PROTOCOL_WHATSAPP, contact
        )

    def test_rank_guard_no_downgrade(self, tmp_db):
        contact = "123@g.us"
        text = "ciao"
        _seed_outgoing(tmp_db, contact, text, 1000, status="read")
        assert not _update_message_status_by_text(
            text, "sent", PROTOCOL_WHATSAPP, contact
        )
        assert _row_status(tmp_db, contact, text) == "read"

    def test_scoped_to_contact_and_outgoing_only(self, tmp_db):
        text = "stesso testo"
        _seed_outgoing(tmp_db, "123@g.us", text, 1000, status="pending")
        _seed_outgoing(tmp_db, "999@g.us", text, 1000, status="pending")
        assert _update_message_status_by_text(
            text, "sent", PROTOCOL_WHATSAPP, "123@g.us", expected_statuses=("pending",)
        )
        assert _row_status(tmp_db, "123@g.us", text) == "sent"
        # L'altro contatto resta invariato:
        assert _row_status(tmp_db, "999@g.us", text) == "pending"

    def test_updates_most_recent_row(self, tmp_db):
        contact = "123@g.us"
        text = "doppio"
        _seed_outgoing(tmp_db, contact, text, 1000, status="pending")
        _seed_outgoing(tmp_db, contact, text, 2000, status="pending")
        assert _update_message_status_by_text(
            text, "sent", PROTOCOL_WHATSAPP, contact, expected_statuses=("pending",)
        )
        # Solo la più recente viene aggiornata:
        conn = sqlite3.connect(tmp_db)
        try:
            rows = conn.execute(
                "SELECT timestamp, status FROM messages WHERE contact_number=? AND text=? ORDER BY timestamp",
                (contact, text),
            ).fetchall()
        finally:
            conn.close()
        assert dict(rows) == {1000: "pending", 2000: "sent"}


class TestTransitionOutgoingStatusFallback:
    """La transizione usa il fallback per testo quando il timestamp non combacia."""

    def _handler(self, backend):
        handler = SimpleNamespace()
        handler.manager = SimpleNamespace(get=MagicMock(return_value=backend))
        handler.signal_backend = SimpleNamespace(cache={})
        handler._cache = {}
        handler.call_from_thread = MagicMock(side_effect=lambda fn, *args: fn(*args))
        handler._update_message_widgets_status = MagicMock()
        return handler

    def test_fallback_updates_db_cache_and_widgets(self, tmp_db):
        contact = "123@g.us"
        text = "hai finito col calcolo?"
        ts_client = 1787342685618
        ts_server = 1787342685000
        _seed_outgoing(tmp_db, contact, text, ts_client, status="pending")

        # L'echo ha già spostato la riga sul ts server:
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            "UPDATE messages SET timestamp = ? WHERE text = ?", (ts_server, text)
        )
        conn.commit()
        conn.close()

        backend = SimpleNamespace(
            cache={
                contact: [
                    {
                        "is_mine": True,
                        "text": text,
                        "timestamp": ts_server,
                        "status": "pending",
                    }
                ]
            }
        )
        handler = self._handler(backend)

        ok = SendMixin._transition_outgoing_status(
            handler, PROTOCOL_WHATSAPP, contact, ts_client, text, "sent", ("pending",)
        )
        assert ok is True
        assert _row_status(tmp_db, contact, text) == "sent"
        assert backend.cache[contact][0]["status"] == "sent"
        handler._update_message_widgets_status.assert_called_once()

    def test_no_downgrade_when_echo_advanced_first(self, tmp_db):
        contact = "123@g.us"
        text = "già letto"
        _seed_outgoing(tmp_db, contact, text, 1000, status="read")
        backend = SimpleNamespace(
            cache={
                contact: [
                    {
                        "is_mine": True,
                        "text": text,
                        "timestamp": 2000,
                        "status": "read",
                    }
                ]
            }
        )
        handler = self._handler(backend)
        ok = SendMixin._transition_outgoing_status(
            handler, PROTOCOL_WHATSAPP, contact, 1000, text, "sent", ("pending",)
        )
        # La riga non è più pending: né il match per ts né il fallback procedono,
        # e il rank guard impedisce il downgrade → transizione rifiutata.
        assert ok is False
        assert _row_status(tmp_db, contact, text) == "read"
