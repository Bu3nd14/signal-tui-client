"""
Regression tests for Fix 2 — SQLite persistence moved off the UI thread.

Before the fix, ``on_input_submitted`` performed the SQLite write
(``_add_message_to_cache``, ~37ms) synchronously on the UI thread.  The fix
splits ingestion into a synchronous, persistence-free cache seed
(``ingest_message(..., persist=False)``) plus a worker-side
``_persist_message`` that runs BEFORE the network send.

These tests verify the fix is behavior-preserving:
  T2a: ``on_input_submitted`` does NOT write to SQLite on the UI thread but
       still mounts the bubble and seeds ``self._cache``.
  T2b: the worker persists the row with the correct protocol/msg_id/quote.
  T2c: the optimistic row is ALREADY in SQLite when ``send_message_sync`` runs.
  T2d: optimistic send + echo within the dedup window → no duplicate.
  T2e: two identical sends within 5s → both bubbles shown, only ONE row.
  T2f: protocol attribution (whatsapp vs signal).
  T2g: reply data → ``quote_text`` persisted and quote params forwarded.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import backend as backend_mod
from backends import BackendManager, SignalBackend, WhatsAppBackend
from models import PROTOCOL_SIGNAL, PROTOCOL_WHATSAPP, ChatContact, ChatEvent
from signal_tui import SignalTUI


def _db_rows(db_file: Path) -> list[dict]:
    """Read all rows from the temp SQLite DB (oldest first)."""
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM messages ORDER BY id")]
    conn.close()
    return rows


def _signal_app() -> SignalTUI:
    """App with a real manager + Signal backend (no real backends/network)."""
    app = SignalTUI()
    app.manager = BackendManager()
    app.signal_backend = SignalBackend()
    app.manager.register(app.signal_backend)
    return app


def _prepare_send(app: SignalTUI) -> None:
    """Stub the DOM-touching / async parts of ``on_input_submitted``."""
    app._is_completion_visible = MagicMock(return_value=False)
    app.query_one = MagicMock(side_effect=Exception("no DOM in test"))
    app._add_message = MagicMock()
    app._cancel_reply = MagicMock()
    app.run_worker = MagicMock()
    app._reply_to = None
    app._cache = {}


def _send_text(app: SignalTUI, text: str) -> None:
    """Submit *text* through ``on_input_submitted`` (emoji aliases identity)."""
    import signal_tui as stui

    event = MagicMock()
    event.value = text
    with patch.object(stui, "replace_emoji_aliases", side_effect=lambda x: x):
        app.on_input_submitted(event)


def _run_workers(app: SignalTUI) -> None:
    """Execute every worker scheduled via ``run_worker`` inline (sync)."""
    for call in app.run_worker.call_args_list:
        call.args[0]()


@pytest.fixture
def tmp_db(tmp_path: Path):
    """Point the backend at a temp DB/CACHE_DIR for the duration of a test."""
    db_file = tmp_path / "messages.db"
    with (
        patch.object(backend_mod, "DB_FILE", db_file),
        patch.object(backend_mod, "CACHE_DIR", tmp_path),
    ):
        yield db_file


class TestSendPersistOffthread:
    """📤 T2a-g — persistenza fuori dal thread UI, behavior-preserving."""

    # ── T2a: no SQLite write on the UI thread ─────────────────────────────

    def test_submit_does_not_persist_on_ui_thread(self, tmp_db):
        """(a) ``on_input_submitted`` non scrive in SQLite ma monta subito."""
        app = _signal_app()
        contact = ChatContact(
            id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        app.selected_contact = contact
        _prepare_send(app)

        with patch("backends.signal._add_message_to_cache") as spy_persist:
            _send_text(app, "ciao")

        # The (slow) SQLite write was NOT performed on the UI thread.
        spy_persist.assert_not_called()
        # The bubble was mounted immediately and the UI cache was seeded.
        assert app._add_message.call_count == 1
        assert len(app._cache[contact.cache_key]) == 1
        # The persistence work was deferred to a worker.
        assert app.run_worker.call_count == 1

    # ── T2b: worker persists the correct row ─────────────────────────────

    def test_worker_persists_row_with_correct_fields(self, tmp_db):
        """(b) la persistenza nel worker scrive protocol/msg_id/quote_text corretti."""
        app = _signal_app()
        contact = ChatContact(
            id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        app.selected_contact = contact
        _prepare_send(app)
        app._reply_to = {"text": "domanda", "timestamp": 1234}
        app.signal_backend.send_message_sync = MagicMock(return_value="ts-1")

        _send_text(app, "risposta")
        _run_workers(app)

        rows = _db_rows(tmp_db)
        assert len(rows) == 1
        row = rows[0]
        assert row["protocol"] == "signal"
        assert row["msg_id"] is None  # Signal optimistic rows carry no id
        assert row["text"] == "risposta"
        assert row["quote_text"] == "domanda"
        assert row["is_mine"] == 1
        assert row["status"] == "sent"

    # ── T2c: persist happens BEFORE the network send ─────────────────────

    def test_persist_happens_before_send(self, tmp_db):
        """(c) quando ``send_message_sync`` gira, la riga è GIÀ in SQLite."""
        app = _signal_app()
        contact = ChatContact(
            id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        app.selected_contact = contact
        _prepare_send(app)

        rows_at_send: dict[str, int] = {}

        def send_spy(contact_id, text, **kwargs):
            rows_at_send["n"] = len(_db_rows(tmp_db))
            return "ts-1"

        app.signal_backend.send_message_sync = MagicMock(side_effect=send_spy)

        _send_text(app, "ciao")
        _run_workers(app)

        assert rows_at_send.get("n") == 1

    # ── T2d: echo dedup unchanged ────────────────────────────────────────

    def test_optimistic_send_then_echo_not_duplicated(self, tmp_db):
        """(d) invio ottimistico + echo stesso testo entro finestra → 1 sola riga."""
        app = _signal_app()
        contact = ChatContact(
            id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        app.selected_contact = contact
        _prepare_send(app)
        app.signal_backend.send_message_sync = MagicMock(return_value="ts-1")
        app.call_from_thread = MagicMock(side_effect=lambda fn, *a, **k: fn(*a, **k))

        _send_text(app, "ciao")
        _run_workers(app)

        ts = app._cache[contact.cache_key][0]["timestamp"]
        echo_payload = {
            "text": "ciao",
            "is_mine": True,
            "sender": "You",
            "timestamp": ts,
            "quote_text": None,
            "msg_type": "text",
            "attachment_info": None,
            "attachment_id": None,
            "contact": contact,
        }
        app._handle_message_event(
            ChatEvent(
                type="message",
                protocol=PROTOCOL_SIGNAL,
                contact_id=contact.id,
                payload=echo_payload,
            )
        )

        # No duplicate bubble and no duplicate DB row.
        assert app._add_message.call_count == 1
        assert len(_db_rows(tmp_db)) == 1
        assert len(app.signal_backend.cache[contact.id]) == 1

    # ── T2e: double identical send within 5s ─────────────────────────────

    def test_double_same_text_not_persisted(self, tmp_db):
        """(e) stesso testo 2 volte in <5s: seconda bolla mostrata, NON persistita."""
        app = _signal_app()
        contact = ChatContact(
            id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        app.selected_contact = contact
        _prepare_send(app)
        app.signal_backend.send_message_sync = MagicMock(return_value="ts-1")

        _send_text(app, "ciao")
        time.sleep(0.002)
        _send_text(app, "ciao")
        _run_workers(app)

        # Both bubbles are shown (UI mirrors both sends) ...
        assert app._add_message.call_count == 2
        assert len(app._cache[contact.cache_key]) == 2
        # ... but only ONE row is persisted (dedup preserved across the split).
        assert len(_db_rows(tmp_db)) == 1

    # ── T2f: protocol attribution ────────────────────────────────────────

    def test_whatsapp_send_persists_whatsapp_protocol(self, tmp_db):
        """(f) invio WhatsApp → riga ``protocol='whatsapp'``."""
        app = SignalTUI()
        app.manager = BackendManager()
        wa = WhatsAppBackend()
        wa.send_message_sync = MagicMock(return_value="wa-ts")
        app.manager.register(wa)
        app.signal_backend = SignalBackend()
        app.manager.register(app.signal_backend)

        contact = ChatContact(
            id="16660245291231@lid", display_name="Pix", protocol=PROTOCOL_WHATSAPP
        )
        app.selected_contact = contact
        _prepare_send(app)

        _send_text(app, "ciao wa")
        _run_workers(app)

        rows = _db_rows(tmp_db)
        assert len(rows) == 1
        assert rows[0]["protocol"] == "whatsapp"

    def test_signal_send_persists_signal_protocol(self, tmp_db):
        """(f) invio Signal → riga ``protocol='signal'``."""
        app = _signal_app()
        contact = ChatContact(
            id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        app.selected_contact = contact
        _prepare_send(app)
        app.signal_backend.send_message_sync = MagicMock(return_value="ts-1")

        _send_text(app, "ciao signal")
        _run_workers(app)

        rows = _db_rows(tmp_db)
        assert len(rows) == 1
        assert rows[0]["protocol"] == "signal"

    # ── T2g: reply/quote data ────────────────────────────────────────────

    def test_reply_persists_quote_and_forwards_quote_params(self, tmp_db):
        """(g) con ``_reply_to``: quote_text persistito e quote params a send."""
        app = _signal_app()
        contact = ChatContact(
            id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        app.selected_contact = contact
        _prepare_send(app)
        app._reply_to = {"text": "domanda", "timestamp": 1234}
        app.signal_backend.send_message_sync = MagicMock(return_value="ts-1")

        _send_text(app, "risposta")
        _run_workers(app)

        rows = _db_rows(tmp_db)
        assert len(rows) == 1
        assert rows[0]["quote_text"] == "domanda"

        kwargs = app.signal_backend.send_message_sync.call_args.kwargs
        assert kwargs["quote_timestamp"] == 1234
        assert kwargs["quote_author"] == contact.id
        assert kwargs["quote_message"] == "domanda"
