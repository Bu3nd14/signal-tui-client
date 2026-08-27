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
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import backend as backend_mod
from backends import BackendManager, SignalBackend, WhatsAppBackend
from models import (
    PROTOCOL_SIGNAL,
    PROTOCOL_TELEGRAM,
    PROTOCOL_WHATSAPP,
    ChatContact,
    ChatEvent,
)
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
    app._status = MagicMock()
    app._update_message_widgets_status = MagicMock()
    app.call_from_thread = MagicMock(
        side_effect=lambda callback, *args, **kwargs: callback(*args, **kwargs)
    )
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

    @pytest.mark.parametrize("message_id", [None, "not-a-message-id", "0"])
    def test_telegram_reply_without_valid_message_id_keeps_input_and_reply(
        self, message_id
    ):
        app = SignalTUI()
        contact = ChatContact(id="42", display_name="Ada", protocol=PROTOCOL_TELEGRAM)
        app.selected_contact = contact
        _prepare_send(app)
        app.manager = MagicMock()
        reply_data = {
            "text": "original message",
            "timestamp": 1234,
            "message_id": message_id,
        }
        app._reply_to = reply_data
        input_widget = SimpleNamespace(value="telegram reply")
        event = SimpleNamespace(value="telegram reply", input=input_widget)

        import signal_tui as stui

        with patch.object(stui, "replace_emoji_aliases", side_effect=lambda text: text):
            app.on_input_submitted(event)

        assert input_widget.value == "telegram reply"
        assert app._reply_to is reply_data
        assert app._cache == {}
        app._add_message.assert_not_called()
        app.run_worker.assert_not_called()
        app.manager.get.assert_not_called()

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
        assert row["msg_id"] == "ts-1"  # Signal now persists the real server id
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

    def test_persisted_send_pushes_web_event_once_across_echo(self, tmp_db):
        app = _signal_app()
        contact = ChatContact(
            id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        app.selected_contact = contact
        app._web_enabled = True
        _prepare_send(app)
        app.signal_backend.send_message_sync = MagicMock(return_value="ts-1")

        with patch("web.bridge.push_event") as push_event:
            _send_text(app, "ciao web")
            ts = app._cache[contact.cache_key][0]["timestamp"]
            push_event.assert_not_called()
            _run_workers(app)

            app._handle_message_event(
                ChatEvent(
                    type="message",
                    protocol=PROTOCOL_SIGNAL,
                    contact_id=contact.id,
                    payload={
                        "id": "ts-1",
                        "text": "ciao web",
                        "is_mine": True,
                        "sender": "You",
                        "timestamp": ts,
                        "quote_text": None,
                        "msg_type": "text",
                        "attachment_info": None,
                        "attachment_id": None,
                        "contact": contact,
                    },
                )
            )

        push_event.assert_called_once_with(
            {
                "type": "message",
                "payload": {
                    "id": None,
                    "protocol": PROTOCOL_SIGNAL,
                    "contact_id": contact.id,
                    "timestamp": ts,
                },
            }
        )

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

    def test_reply_persists_quote_attachment_metadata(self, tmp_db):
        """(g2) reply a una quote immagine → persiste quote_attachment_id/type."""
        app = _signal_app()
        contact = ChatContact(
            id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        app.selected_contact = contact
        _prepare_send(app)
        app._reply_to = {
            "text": "🖼️ Immagine",
            "timestamp": 1234,
            "quote_wire_body": None,
            "attachment_id": "att-1",
            "content_type": "image/png",
            "quote_attachment_id": "att-1",
            "quote_attachment_path": Path("/tmp/quote-thumbs/abc123.png"),
            "quote_content_type": "image/png",
        }
        app.signal_backend.send_message_sync = MagicMock(return_value="ts-1")

        _send_text(app, "risposta")
        _run_workers(app)

        rows = _db_rows(tmp_db)
        assert len(rows) == 1
        assert rows[0]["quote_attachment_id"] == "att-1"
        assert rows[0]["quote_attachment_path"] == "/tmp/quote-thumbs/abc123.png"
        assert rows[0]["quote_content_type"] == "image/png"

    def test_whatsapp_reply_persists_quote_attachment_id(self, tmp_db):
        """Uscita WhatsApp: l'id della quote immagine è propagato e persistito."""
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
        app._reply_to = {
            "text": "🖼️ Immagine",
            "timestamp": 1234,
            "message_id": "wa-msg-1",
            "quote_wire_body": None,
            "attachment_id": "https://waha.local/media/123",
            "content_type": "image/jpeg",
            "quote_attachment_id": "https://waha.local/media/123",
            "quote_attachment_path": None,
            "quote_content_type": "image/jpeg",
        }

        _send_text(app, "risposta wa")
        _run_workers(app)

        rows = _db_rows(tmp_db)
        assert len(rows) == 1
        assert rows[0]["protocol"] == "whatsapp"
        assert rows[0]["quote_attachment_id"] == "https://waha.local/media/123"
        assert rows[0]["quote_content_type"] == "image/jpeg"

    def test_live_event_propagates_quote_attachment_metadata(self, tmp_db):
        """L'evento live con quote immagine propaga i metadati a cache UI + bubble."""
        app = _signal_app()
        contact = ChatContact(
            id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        app.contacts = [contact]
        app.selected_contact = contact
        _prepare_send(app)

        payload = {
            "text": "testo",
            "is_mine": False,
            "sender": "Mario",
            "timestamp": 1000,
            "quote_text": "🖼️ Immagine",
            "msg_type": "text",
            "attachment_info": None,
            "attachment_id": None,
            "contact": contact,
            "quote_attachment_id": "att-1",
            "quote_attachment_path": "/tmp/quote-thumbs/abc.png",
            "quote_content_type": "image/png",
        }

        handled = app._handle_message_event(
            ChatEvent(
                type="message",
                protocol=PROTOCOL_SIGNAL,
                contact_id=contact.id,
                payload=payload,
            )
        )

        assert handled is True
        entry = app._cache[contact.cache_key][0]
        assert entry["quote_attachment_id"] == "att-1"
        assert entry["quote_attachment_path"] == "/tmp/quote-thumbs/abc.png"
        assert entry["quote_content_type"] == "image/png"
        # Live rendering (_add_message) received the metadata too.
        kwargs = app._add_message.call_args.kwargs
        assert kwargs["quote_attachment_id"] == "att-1"
        assert kwargs["quote_attachment_path"] == "/tmp/quote-thumbs/abc.png"
        assert kwargs["quote_content_type"] == "image/png"

    # ── T2h: worker submission target is immutable ───────────────────────

    def test_worker_uses_original_contact_after_same_protocol_switch(self, tmp_db):
        """Changing Signal chat before execution must not redirect the send."""
        app = _signal_app()
        original = ChatContact(
            id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        other = ChatContact(
            id="+399876543210", display_name="Luigi", protocol=PROTOCOL_SIGNAL
        )
        app.selected_contact = original
        _prepare_send(app)
        app._reply_to = {"text": "domanda", "timestamp": 1234}
        app.signal_backend.send_message_sync = MagicMock(return_value="ts-1")

        _send_text(app, "risposta")
        app.selected_contact = other
        _run_workers(app)

        args = app.signal_backend.send_message_sync.call_args
        assert args.args[:2] == (original.id, "risposta")
        assert args.kwargs["quote_author"] == original.id

    def test_worker_uses_original_protocol_after_protocol_switch(self, tmp_db):
        """Changing protocol before execution must not redirect the send."""
        app = _signal_app()
        whatsapp = WhatsAppBackend()
        whatsapp.send_message_sync = MagicMock(return_value="wa-ts")
        app.manager.register(whatsapp)
        signal_contact = ChatContact(
            id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        whatsapp_contact = ChatContact(
            id="16660245291231@lid",
            display_name="Pix",
            protocol=PROTOCOL_WHATSAPP,
        )
        app.selected_contact = signal_contact
        _prepare_send(app)
        app.signal_backend.send_message_sync = MagicMock(return_value="signal-ts")

        _send_text(app, "ciao")
        app.selected_contact = whatsapp_contact
        _run_workers(app)

        app.signal_backend.send_message_sync.assert_called_once_with(
            signal_contact.id,
            "ciao",
            quote_timestamp=None,
            quote_author=None,
            quote_message=None,
        )
        whatsapp.send_message_sync.assert_not_called()

    def test_failed_send_is_persisted_and_retry_reuses_the_same_message(self, tmp_db):
        app = _signal_app()
        contact = ChatContact(
            id="+391234567890", display_name="Mario", protocol="signal"
        )
        app.selected_contact = contact
        _prepare_send(app)
        app.call_from_thread = MagicMock(side_effect=lambda fn, *a, **k: fn(*a, **k))
        app.signal_backend.send_message_sync = MagicMock(
            side_effect=RuntimeError("offline")
        )

        _send_text(app, "ciao")
        _run_workers(app)

        assert _db_rows(tmp_db)[0]["status"] == "failed"
        assert app._cache[contact.cache_key][0]["status"] == "failed"
        assert app.signal_backend.cache[contact.id][0]["status"] == "failed"

        app.signal_backend.send_message_sync = MagicMock(return_value="ok")
        ts = app._cache[contact.cache_key][0]["timestamp"]
        app.run_worker.reset_mock()
        app._retry_failed_message(ts, "ciao")
        _run_workers(app)

        assert len(_db_rows(tmp_db)) == 1
        assert len(app._cache[contact.cache_key]) == 1
        assert app._cache[contact.cache_key][0]["status"] == "sent"


class TestMediaReplySend:
    """🖼️ Bug #37 — contratto display vs filo in uscita + guardie protocollo + retry."""

    def test_send_media_reply_without_caption_sends_empty_quote_message(self, tmp_db):
        """Media senza caption → ``quote_message == ""`` (mai il segnaposto)."""
        app = _signal_app()
        contact = ChatContact(
            id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        app.selected_contact = contact
        _prepare_send(app)
        app._reply_to = {
            "text": "🖼️ Immagine",
            "timestamp": 1234,
            "message_id": "sig-1234",
            "quote_wire_body": None,
        }
        app.signal_backend.send_message_sync = MagicMock(return_value="ts-1")

        _send_text(app, "risposta")
        _run_workers(app)

        kwargs = app.signal_backend.send_message_sync.call_args.kwargs
        assert kwargs["quote_timestamp"] == 1234
        assert kwargs["quote_author"] == contact.id
        assert kwargs["quote_message"] == ""
        assert kwargs["reply_to_message_id"] == "sig-1234"

        # Il display (segnaposto) resta nel DB, non sul filo.
        assert _db_rows(tmp_db)[0]["quote_text"] == "🖼️ Immagine"

    def test_send_media_reply_with_caption_sends_caption(self, tmp_db):
        """Media con caption → ``quote_message == caption``."""
        app = _signal_app()
        contact = ChatContact(
            id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        app.selected_contact = contact
        _prepare_send(app)
        app._reply_to = {
            "text": "Che bella!",
            "timestamp": 1234,
            "message_id": "sig-1234",
            "quote_wire_body": "Che bella!",
        }
        app.signal_backend.send_message_sync = MagicMock(return_value="ts-1")

        _send_text(app, "risposta")
        _run_workers(app)

        kwargs = app.signal_backend.send_message_sync.call_args.kwargs
        assert kwargs["quote_message"] == "Che bella!"

    def test_send_text_reply_is_unchanged(self, tmp_db):
        """Reply a testo (chiave assente) → ``quote_message == text`` invariato."""
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

        kwargs = app.signal_backend.send_message_sync.call_args.kwargs
        assert kwargs["quote_message"] == "domanda"

    def test_media_reply_signal_builds_quote_attachments_with_preview(self, tmp_db):
        """(B) media Signal senza caption → ``quote_attachments == [f"{ct}:{name}:{path}"]``."""
        app = _signal_app()
        contact = ChatContact(
            id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        app.selected_contact = contact
        _prepare_send(app)
        app._reply_to = {
            "text": "🖼️ Immagine",
            "timestamp": 1234,
            "message_id": "sig-1234",
            "quote_wire_body": None,
            "content_type": "image/png",
            "attachment_id": "att-1",
        }
        app.signal_backend.send_message_sync = MagicMock(return_value="ts-1")
        preview = Path("/tmp/att-1.png")

        with patch("tui.send.get_attachment_path", return_value=preview):
            _send_text(app, "risposta")
            _run_workers(app)

        kwargs = app.signal_backend.send_message_sync.call_args.kwargs
        assert kwargs["quote_attachments"] == ["image/png:att-1.png:/tmp/att-1.png"]
        assert kwargs["quote_message"] == ""

    def test_media_reply_signal_with_caption_builds_quote_attachments(self, tmp_db):
        """(B) media Signal con caption → ``quote_attachments`` presente + caption."""
        app = _signal_app()
        contact = ChatContact(
            id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        app.selected_contact = contact
        _prepare_send(app)
        app._reply_to = {
            "text": "Che bella!",
            "timestamp": 1234,
            "message_id": "sig-1234",
            "quote_wire_body": "Che bella!",
            "content_type": "image/jpeg",
            "attachment_id": "att-1",
        }
        app.signal_backend.send_message_sync = MagicMock(return_value="ts-1")

        with patch("tui.send.get_attachment_path", return_value=Path("/tmp/a.jpg")):
            _send_text(app, "risposta")
            _run_workers(app)

        kwargs = app.signal_backend.send_message_sync.call_args.kwargs
        assert kwargs["quote_message"] == "Che bella!"
        assert kwargs["quote_attachments"] == ["image/jpeg:a.jpg:/tmp/a.jpg"]

    def test_text_reply_omits_quote_attachments(self, tmp_db):
        """(B) reply a testo → ``quote_attachments`` ASSENTE (chiave non passata)."""
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

        kwargs = app.signal_backend.send_message_sync.call_args.kwargs
        assert "quote_attachments" not in kwargs

    def test_media_reply_signal_missing_preview_falls_back_to_content_type(
        self, tmp_db
    ):
        """(B) previewFile mancante → ``quote_attachments == [content_type]``."""
        app = _signal_app()
        contact = ChatContact(
            id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        app.selected_contact = contact
        _prepare_send(app)
        app._reply_to = {
            "text": "🖼️ Immagine",
            "timestamp": 1234,
            "message_id": "sig-1234",
            "quote_wire_body": None,
            "content_type": "image/png",
            "attachment_id": "att-missing",
        }
        app.signal_backend.send_message_sync = MagicMock(return_value="ts-1")

        with patch("tui.send.get_attachment_path", return_value=None):
            _send_text(app, "risposta")
            _run_workers(app)

        kwargs = app.signal_backend.send_message_sync.call_args.kwargs
        assert kwargs["quote_attachments"] == ["image/png"]

    def test_retry_media_reply_reconstructs_quote_attachments(self, tmp_db):
        """(D) retry media post-reload → ricostruisce ``quote_attachments`` via DB."""
        app = _signal_app()
        contact = ChatContact(
            id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        app.selected_contact = contact
        _prepare_send(app)
        app.call_from_thread = MagicMock(side_effect=lambda fn, *a, **k: fn(*a, **k))
        app._reply_to = {
            "text": "🖼️ Immagine",
            "timestamp": 1234,
            "message_id": "sig-1234",
            "quote_wire_body": None,
            "content_type": "image/png",
            "attachment_id": "att-1",
        }
        app.signal_backend.send_message_sync = MagicMock(
            side_effect=RuntimeError("offline")
        )

        with patch("tui.send.get_attachment_path", return_value=Path("/tmp/att-1.png")):
            _send_text(app, "risposta")
            _run_workers(app)

        assert app._cache[contact.cache_key][0]["status"] == "failed"
        row = _db_rows(tmp_db)[0]
        assert row["content_type"] == "image/png"
        assert row["attachment_id"] == "att-1"

        app.signal_backend.send_message_sync = MagicMock(return_value="ok")
        ts = app._cache[contact.cache_key][0]["timestamp"]
        app.run_worker.reset_mock()
        with patch("tui.send.get_attachment_path", return_value=Path("/tmp/att-1.png")):
            app._retry_failed_message(ts, "risposta")
            _run_workers(app)

        kwargs = app.signal_backend.send_message_sync.call_args.kwargs
        assert kwargs["quote_message"] == ""
        assert kwargs["quote_attachments"] == ["image/png:att-1.png:/tmp/att-1.png"]

    def test_retry_media_reply_with_caption_reconstructs_quote_attachments(
        self, tmp_db
    ):
        """(Bug #1) retry media CON caption → ricostruisce ``quote_attachments``.

        Il ``quote_text`` persistito è la caption reale (non il segnaposto),
        quindi la ricostruzione deve essere gated sulla presenza dei metadati
        media (``content_type``/``attachment_id``), non sul predicato
        ``is_media_quote_placeholder``.
        """
        app = _signal_app()
        contact = ChatContact(
            id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        app.selected_contact = contact
        _prepare_send(app)
        app.call_from_thread = MagicMock(side_effect=lambda fn, *a, **k: fn(*a, **k))
        app._reply_to = {
            "text": "Che bella!",
            "timestamp": 1234,
            "message_id": "sig-1234",
            "quote_wire_body": "Che bella!",
            "content_type": "image/jpeg",
            "attachment_id": "att-1",
        }
        app.signal_backend.send_message_sync = MagicMock(
            side_effect=RuntimeError("offline")
        )

        with patch("tui.send.get_attachment_path", return_value=Path("/tmp/att-1.jpg")):
            _send_text(app, "risposta")
            _run_workers(app)

        assert app._cache[contact.cache_key][0]["status"] == "failed"
        row = _db_rows(tmp_db)[0]
        assert row["quote_text"] == "Che bella!"
        assert row["content_type"] == "image/jpeg"
        assert row["attachment_id"] == "att-1"

        app.signal_backend.send_message_sync = MagicMock(return_value="ok")
        ts = app._cache[contact.cache_key][0]["timestamp"]
        app.run_worker.reset_mock()
        with patch("tui.send.get_attachment_path", return_value=Path("/tmp/att-1.jpg")):
            app._retry_failed_message(ts, "risposta")
            _run_workers(app)

        kwargs = app.signal_backend.send_message_sync.call_args.kwargs
        assert kwargs["quote_message"] == "Che bella!"
        assert kwargs["quote_attachments"] == ["image/jpeg:att-1.jpg:/tmp/att-1.jpg"]

    def test_media_reply_without_content_type_omits_quote_attachments(self, tmp_db):
        """(Bug #2) riga legacy senza ``content_type`` → nessun ``quoteAttachments``.

        La derivazione del mime da ``msg_type`` è stata rimossa (inaffidabile:
        video/audio Signal sono ``msg_type="attachment"``).  Il degrado è il
        comportamento V2: quote corretta ma senza thumbnail, nessun crash.
        """
        app = _signal_app()
        contact = ChatContact(
            id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        app.selected_contact = contact
        _prepare_send(app)
        app._reply_to = {
            "text": "🖼️ Immagine",
            "timestamp": 1234,
            "message_id": "sig-1234",
            "quote_wire_body": None,
            "content_type": None,
            "attachment_id": "att-legacy",
        }
        app.signal_backend.send_message_sync = MagicMock(return_value="ts-1")

        with patch("tui.send.get_attachment_path", return_value=Path("/tmp/x")):
            _send_text(app, "risposta")
            _run_workers(app)

        kwargs = app.signal_backend.send_message_sync.call_args.kwargs
        assert "quote_attachments" not in kwargs
        assert kwargs["quote_message"] == ""

    def test_retry_media_reply_reconstructs_empty_wire_body(self, tmp_db):
        """Retry post-reload di reply media → ``quote_wire_body == ""`` (buco #3)."""
        app = _signal_app()
        contact = ChatContact(
            id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        app.selected_contact = contact
        _prepare_send(app)
        app.call_from_thread = MagicMock(side_effect=lambda fn, *a, **k: fn(*a, **k))
        app._reply_to = {
            "text": "🖼️ Immagine",
            "timestamp": 1234,
            "message_id": "sig-1234",
            "quote_wire_body": None,
        }
        app.signal_backend.send_message_sync = MagicMock(
            side_effect=RuntimeError("offline")
        )

        _send_text(app, "risposta")
        _run_workers(app)

        assert app._cache[contact.cache_key][0]["status"] == "failed"
        assert _db_rows(tmp_db)[0]["quote_text"] == "🖼️ Immagine"

        app.signal_backend.send_message_sync = MagicMock(return_value="ok")
        ts = app._cache[contact.cache_key][0]["timestamp"]
        app.run_worker.reset_mock()
        app._retry_failed_message(ts, "risposta")
        _run_workers(app)

        kwargs = app.signal_backend.send_message_sync.call_args.kwargs
        assert kwargs["quote_message"] == ""
        assert kwargs["quote_timestamp"] == 1234
        assert kwargs["reply_to_message_id"] == "sig-1234"

    def test_retry_text_reply_is_unchanged(self, tmp_db):
        """Retry post-reload di reply a testo → invariato (nessuna chiave wire)."""
        app = _signal_app()
        contact = ChatContact(
            id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        app.selected_contact = contact
        _prepare_send(app)
        app.call_from_thread = MagicMock(side_effect=lambda fn, *a, **k: fn(*a, **k))
        app._reply_to = {"text": "domanda", "timestamp": 1234}
        app.signal_backend.send_message_sync = MagicMock(
            side_effect=RuntimeError("offline")
        )

        _send_text(app, "risposta")
        _run_workers(app)

        app.signal_backend.send_message_sync = MagicMock(return_value="ok")
        ts = app._cache[contact.cache_key][0]["timestamp"]
        app.run_worker.reset_mock()
        app._retry_failed_message(ts, "risposta")
        _run_workers(app)

        kwargs = app.signal_backend.send_message_sync.call_args.kwargs
        assert kwargs["quote_message"] == "domanda"

    def test_whatsapp_media_reply_without_id_is_blocked(self, tmp_db):
        """Guardia WhatsApp: reply media senza Baileys id → bloccata prima del send."""
        app = _signal_app()
        whatsapp = WhatsAppBackend()
        whatsapp.send_message_sync = MagicMock(return_value="wa-ts")
        app.manager.register(whatsapp)
        contact = ChatContact(
            id="391234567890@c.us", display_name="Pix", protocol=PROTOCOL_WHATSAPP
        )
        app.selected_contact = contact
        _prepare_send(app)
        app._reply_to = {
            "text": "🖼️ Immagine",
            "timestamp": 1234,
            "quote_wire_body": None,
        }  # no message_id

        _send_text(app, "risposta")
        _run_workers(app)

        app._status.assert_called_with(
            "❌ Cannot reply: the original WhatsApp message ID is unavailable", 0
        )
        whatsapp.send_message_sync.assert_not_called()

    def test_whatsapp_media_reply_sends_placeholder_and_native_reply_id(self, tmp_db):
        app = _signal_app()
        whatsapp = WhatsAppBackend()
        whatsapp.send_message_sync = MagicMock(return_value="wa-ts")
        app.manager.register(whatsapp)
        contact = ChatContact(
            id="391234567890@c.us", display_name="Pix", protocol=PROTOCOL_WHATSAPP
        )
        app.selected_contact = contact
        _prepare_send(app)
        app._reply_to = {
            "text": "🖼️ Immagine",
            "timestamp": 1234,
            "message_id": "wa-image-id",
            "quote_wire_body": None,
        }

        _send_text(app, "risposta")
        _run_workers(app)

        kwargs = whatsapp.send_message_sync.call_args.kwargs
        assert kwargs["quote_message"] == "🖼️ Immagine"
        assert kwargs["reply_to_message_id"] == "wa-image-id"

    def test_telegram_media_reply_guard_unchanged(self, tmp_db):
        """Guardia Telegram: reply media senza id server valido → bloccata."""
        from backends import TelegramBackend

        app = _signal_app()
        telegram = TelegramBackend()
        telegram.send_message_sync = MagicMock(return_value="tg-ts")
        app.manager.register(telegram)
        contact = ChatContact(id="42", display_name="Ada", protocol=PROTOCOL_TELEGRAM)
        app.selected_contact = contact
        _prepare_send(app)
        app._reply_to = {
            "text": "🖼️ Immagine",
            "timestamp": 1234,
            "quote_wire_body": None,
        }  # no message_id

        _send_text(app, "risposta")
        _run_workers(app)

        app._status.assert_called_with(
            "❌ Cannot reply: the original Telegram message ID is unavailable", 0
        )
        telegram.send_message_sync.assert_not_called()
