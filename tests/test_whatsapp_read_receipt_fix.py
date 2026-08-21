"""Targeted regression tests for the WhatsApp read-receipt fix.

Covers the plan items for "sent WhatsApp messages never reach read in the UI":

  N1 — ``send_message_sync`` propagates the Baileys message id.
  N2 — ``handle_webhook`` does NOT mutate the cache before enqueueing.
  S1 — ``fetch_history`` emits receipt events for is_mine messages (ack >= 3).
  S3 — ``process_receipt`` persists by ``msg_id`` with a rank guard.
  S2 — UI cache merges upgrade the status instead of a plain ``continue``.

All tests use an isolated temporary SQLite DB (``backend.DB_FILE`` /
``backend.CACHE_DIR`` patched); the real DB is never touched.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import backend as backend_mod
from backends.whatsapp import WhatsAppBackend
from models import PROTOCOL_WHATSAPP, ChatContact, contact_cache_key


@pytest.fixture
def tmp_db(tmp_path: Path):
    """Point the backend at a fresh temporary SQLite DB for this test."""
    db_file = tmp_path / "messages.db"
    with (
        patch.object(backend_mod, "DB_FILE", db_file),
        patch.object(backend_mod, "CACHE_DIR", tmp_path),
    ):
        yield db_file


def _make_backend(api_url: str = "http://api.test") -> WhatsAppBackend:
    backend = WhatsAppBackend(api_url=api_url, media_dir="")
    backend._rest = MagicMock()
    return backend


# ─── N1: send propagates the Baileys id ──────────────────────────────────────


class TestSendReturnsBaileysId:
    """📤 ``send_message_sync`` ritorna l'id Baileys (flat, nested, o None)."""

    def test_send_message_sync_returns_flat_id(self):
        backend = _make_backend()
        backend._rest.send_message.return_value = {"id": "BAYES-123"}
        assert backend.send_message_sync("1@c.us", "ciao") == "BAYES-123"

    def test_send_message_sync_returns_nested_key_id(self):
        backend = _make_backend()
        backend._rest.send_message.return_value = {"key": {"id": "NESTED-9"}}
        assert backend.send_message_sync("1@c.us", "ciao") == "NESTED-9"

    def test_send_message_sync_returns_none_without_id(self):
        backend = _make_backend()
        backend._rest.send_message.return_value = {"status": "ok"}
        assert backend.send_message_sync("1@c.us", "ciao") is None

    def test_extract_message_id_probes_multiple_fields(self):
        assert WhatsAppBackend._extract_message_id({"messageId": "A"}) == "A"
        assert WhatsAppBackend._extract_message_id({"msg_id": "B"}) == "B"
        assert WhatsAppBackend._extract_message_id({"key": {"id": "C"}}) == "C"
        assert WhatsAppBackend._extract_message_id({"other": "D"}) is None


# ─── N2: single mutation point ───────────────────────────────────────────────


class TestWebhookSingleMutationPoint:
    """🎯 ``handle_webhook`` accoda, non muta: l'ingest avviene nel consumatore."""

    def test_handle_webhook_ack_does_not_mutate_cache(self):
        backend = _make_backend()
        payload = {
            "id": "ack-1",
            "to": "1@c.us",
            "fromMe": True,
            "timestamp": 1700000000,
            "body": "echo",
            "status": 2,
        }
        assert backend.handle_webhook({"event": "message.ack", "payload": payload})
        # The cache stays empty: ingestion is the consumer's job.
        assert backend.cache == {}
        events = backend.poll_once()
        assert [e.type for e in events] == ["message", "receipt"]
        assert events[0].payload["id"] == "ack-1"
        assert events[1].payload == {"message_ids": ["ack-1"], "is_read": False}


# ─── S1: history reconciliation via ack ──────────────────────────────────────


class TestHistoryReconciliation:
    """📥 ``fetch_history`` emette receipt per i messaggi miei con ack >= 3."""

    def test_fetch_history_emits_read_receipt(self, tmp_db):
        backend = _make_backend()
        backend._rest.list_messages.return_value = [
            {
                "id": "hist-1",
                "from": "1@c.us",
                "fromMe": True,
                "timestamp": 1700000000,
                "body": "sent earlier",
                "ack": 3,
            }
        ]
        backend.fetch_history("1@c.us", limit=20)

        assert backend.cache["1@c.us"][0]["id"] == "hist-1"
        receipts = [e for e in backend.poll_once() if e.type == "receipt"]
        assert len(receipts) == 1
        assert receipts[0].payload == {"message_ids": ["hist-1"], "is_read": True}

    def test_fetch_history_emits_delivery_receipt_for_ack2(self, tmp_db):
        backend = _make_backend()
        backend._rest.list_messages.return_value = [
            {
                "id": "hist-2",
                "from": "1@c.us",
                "fromMe": True,
                "timestamp": 1700000000,
                "body": "sent",
                "ack": 2,
            }
        ]
        backend.fetch_history("1@c.us", limit=20)

        receipts = [e for e in backend.poll_once() if e.type == "receipt"]
        assert len(receipts) == 1
        assert receipts[0].payload["is_read"] is False

    def test_fetch_history_no_receipt_for_server_ack(self, tmp_db):
        backend = _make_backend()
        backend._rest.list_messages.return_value = [
            {
                "id": "hist-3",
                "from": "1@c.us",
                "fromMe": True,
                "timestamp": 1700000000,
                "body": "sent",
                "ack": 1,
            }
        ]
        backend.fetch_history("1@c.us", limit=20)

        receipts = [e for e in backend.poll_once() if e.type == "receipt"]
        assert receipts == []


# ─── S3: persist receipt by msg_id ───────────────────────────────────────────


class TestProcessReceiptPersistsById:
    """📝 ``process_receipt`` persiste via ``_update_message_status_by_id``."""

    def test_process_receipt_persists_read_by_msg_id(self, tmp_db):
        backend_mod._add_message_to_cache(
            "1@c.us",
            "hello",
            True,
            "You",
            1000,
            protocol=PROTOCOL_WHATSAPP,
            msg_id="m1",
            status="sent",
        )
        backend = _make_backend()
        backend.cache = backend._load_protocol_cache()

        updated = backend.process_receipt({"message_ids": ["m1"], "is_read": True})

        assert updated == [
            {
                "id": "m1",
                "timestamp": 1000,
                "status": "read",
                "text": "hello",
                "is_mine": True,
            }
        ]
        assert backend.cache["1@c.us"][0]["status"] == "read"
        db = backend_mod._load_cache(protocol=PROTOCOL_WHATSAPP)
        assert db["1@c.us"][0]["status"] == "read"

    def test_process_receipt_never_downgrades_read(self, tmp_db):
        backend = _make_backend()
        backend.cache = {
            "1@c.us": [
                {
                    "id": "m1",
                    "is_mine": True,
                    "status": "read",
                    "timestamp": 1000,
                    "text": "hello",
                }
            ]
        }
        assert backend.process_receipt({"message_ids": ["m1"], "is_read": False}) == []
        assert backend.cache["1@c.us"][0]["status"] == "read"


# ─── S2: UI cache merge upgrades the status ──────────────────────────────────


class TestMergeUpdatesStatus:
    """🔄 Il merge della UI cache aggiorna lo status invece di un ``continue``."""

    def test_merge_backend_cache_updates_status_higher_rank(self):
        from tui.chat_view import ChatViewMixin

        mixin = ChatViewMixin()
        contact = ChatContact(id="1@c.us", display_name="X", protocol=PROTOCOL_WHATSAPP)
        mixin._cache = {
            contact.cache_key: [
                {
                    "id": "m1",
                    "text": "hello",
                    "is_mine": True,
                    "timestamp": 1000,
                    "status": "sent",
                }
            ]
        }
        backend = SimpleNamespace(
            cache={
                "1@c.us": [
                    {
                        "id": "m1",
                        "text": "hello",
                        "is_mine": True,
                        "timestamp": 1000,
                        "status": "read",
                    }
                ]
            }
        )

        changed = mixin._merge_backend_cache(contact, backend)

        assert changed is False  # nothing appended
        assert mixin._cache[contact.cache_key][0]["status"] == "read"

    def test_on_backend_ready_updates_existing_entry_status(self):
        from signal_tui import SignalTUI

        app = SignalTUI()
        app._render_contact_list = MagicMock()
        app._update_unread_badges = MagicMock()
        app._status = MagicMock()
        app._sync_last_ts = MagicMock()
        app._sort_contacts = MagicMock()
        app.contacts = []
        app._pending_backends = set()
        app.selected_contact = None

        key = contact_cache_key(PROTOCOL_WHATSAPP, "1@c.us")
        app._cache = {
            key: [
                {
                    "id": "m1",
                    "text": "hello",
                    "is_mine": True,
                    "timestamp": 1000,
                    "status": "sent",
                }
            ]
        }
        backend = SimpleNamespace(
            protocol=PROTOCOL_WHATSAPP,
            cache={
                "1@c.us": [
                    {
                        "id": "m1",
                        "text": "hello",
                        "is_mine": True,
                        "timestamp": 1000,
                        "status": "read",
                    }
                ]
            },
            contacts=[],
        )

        app._on_backend_ready(backend)

        assert app._cache[key][0]["status"] == "read"


class TestSendPropagatesIdToUi:
    """📤 N1 (TUI) — l'id ritornato da ``send_message_sync`` raggiunge UI cache e widget."""

    def test_whatsapp_send_result_updates_ui_cache_and_widget_id(self):
        from tui.send import SendMixin
        from ui_components import MessageWidget

        class Handler(SendMixin):
            def __init__(self):
                self._cache = {}
                self._status = MagicMock()
                self._transition_outgoing_status = MagicMock(return_value=True)
                self.run_worker = MagicMock()
                self.call_from_thread = MagicMock(
                    side_effect=lambda cb, *a, **k: cb(*a, **k)
                )

        contact = SimpleNamespace(
            cache_key=contact_cache_key(PROTOCOL_WHATSAPP, "1@c.us"),
            protocol=PROTOCOL_WHATSAPP,
            id="1@c.us",
        )
        timestamp = 1234
        text = "ciao wa"
        handler = Handler()
        handler._cache[contact.cache_key] = [
            {"is_mine": True, "timestamp": timestamp, "text": text, "id": None}
        ]
        bubble = MessageWidget(
            text, timestamp=timestamp, is_mine=True, status="pending"
        )
        handler.chat_log = SimpleNamespace(children=[bubble])
        backend = MagicMock()
        backend.send_message_sync.return_value = "BAYES-9"
        backend.ingest_message = MagicMock(return_value=False)
        handler.manager = SimpleNamespace(get=MagicMock(return_value=backend))

        handler._send_message_worker(
            text,
            timestamp,
            None,
            protocol=PROTOCOL_WHATSAPP,
            contact_id=contact.id,
        )

        # ingest_message riceve l'id reale...
        assert backend.ingest_message.call_args[0][1]["id"] == "BAYES-9"
        # ...la UI cache viene aggiornata con l'id...
        assert handler._cache[contact.cache_key][0]["id"] == "BAYES-9"
        # ...e il widget montato espone ``_message_id`` per il match by-id di N3.
        assert bubble._message_id == "BAYES-9"

    def test_echo_does_not_rewrite_timestamp_when_id_already_present(self, tmp_db):
        backend_mod._add_message_to_cache(
            "1@c.us",
            "ciao wa",
            True,
            "You",
            1000,
            protocol=PROTOCOL_WHATSAPP,
            msg_id="BAYES-9",
            status="sent",
        )
        backend = _make_backend()
        backend.cache = backend._load_protocol_cache()

        # Echo con lo stesso id ma timestamp server molto diverso.
        added = backend.ingest_message(
            "1@c.us",
            {"id": "BAYES-9", "text": "ciao wa", "is_mine": True, "sender": "You"},
            1000 + 100_000,
        )

        assert added is False
        assert len(backend.cache["1@c.us"]) == 1
        # Il timestamp ottimistico NON deve essere riscritto dall'echo.
        assert backend.cache["1@c.us"][0]["timestamp"] == 1000
