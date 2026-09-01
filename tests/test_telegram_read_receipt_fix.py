"""Targeted regression tests for the Telegram read-receipt fix (defects A/B/C).

Covers the three combined root causes of "sent Telegram messages never reach
'read' after a TUI restart":

  A — double processing: ``_handle_read_receipt`` must be a pure translator and
      ``process_receipt`` the single mutator (no rank-guard no-op).
  B — startup duplicate: a history fetch with the same ``msg_id`` but a server
      timestamp >2s away must NOT insert a duplicate "sent" row.
  C — reconciliation: ``_load_contacts`` must honour ``read_outbox_max_id`` so
      receipts that arrived while the TUI was closed are not lost.
  + Cleanup: ``_dedup_messages_by_id`` keeps the highest-rank status row.

Every test uses an isolated temporary SQLite DB (patched ``protocols.db.DB_FILE`` /
``protocols.db.CACHE_DIR``); the real DB in ``~/.local/share/signal-tui-client`` is
never touched.
"""

from __future__ import annotations

import asyncio
import queue
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telethon.tl.types import (
    PeerChannel,
    PeerChat,
    PeerUser,
    UpdateReadHistoryOutbox,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import protocols.db as backend_mod  # patched DB_FILE / CACHE_DIR
from models import PROTOCOL_TELEGRAM, ChatContact
from protocols.telegram import TelegramBackend


@pytest.fixture
def tmp_db(tmp_path: Path):
    """Point the backend at a fresh temporary SQLite DB for this test."""
    db_file = tmp_path / "messages.db"
    with (
        patch.object(backend_mod, "DB_FILE", db_file),
        patch.object(backend_mod, "CACHE_DIR", tmp_path),
    ):
        yield db_file


def _make_backend() -> TelegramBackend:
    """Build a TelegramBackend bypassing __init__ (no config/network I/O)."""
    backend = TelegramBackend.__new__(TelegramBackend)
    backend._api_id = 123
    backend._api_hash = "hash"
    backend._session_path = "/tmp/telegram-test.session"
    backend._client = None
    backend._loop = None
    backend._loop_thread = None
    backend._running = False
    backend._connected = False
    backend.contacts = []
    backend._contacts_by_id = {}
    backend.cache = {}
    backend._events = queue.Queue()
    backend._seen_msg_ids = set()
    backend._needs_2fa = False
    return backend


# ─── Defect A ────────────────────────────────────────────────────────────────


class TestDefectADoubleProcessing:
    """📥 A — receipt tradotta senza mutazione, applicata una sola volta."""

    def test_handle_read_receipt_is_pure_translator(self):
        backend = _make_backend()
        backend.cache = {
            "42": [{"id": "1", "is_mine": True, "status": "sent", "timestamp": 100}]
        }
        update = UpdateReadHistoryOutbox(
            peer=PeerUser(user_id=42), max_id=5, pts=1, pts_count=1
        )

        asyncio.run(backend._handle_read_receipt(update))

        # No cache mutation and no SQLite write: pure translation.
        assert backend.cache["42"][0]["status"] == "sent"
        events = backend.poll_once()
        assert len(events) == 1
        assert events[0].type == "receipt"
        assert events[0].protocol == PROTOCOL_TELEGRAM
        assert events[0].contact_id == "42"
        assert events[0].payload == {"message_ids": ["1"], "is_read": True}

    def test_process_receipt_returns_updated_and_persists_read(self, tmp_db):
        backend_mod._add_message_to_cache(
            "42",
            "hello",
            True,
            "You",
            1000,
            protocol=PROTOCOL_TELEGRAM,
            msg_id="1",
            status="sent",
        )
        backend = _make_backend()
        backend.cache = {
            "42": [
                {
                    "id": "1",
                    "text": "hello",
                    "is_mine": True,
                    "status": "sent",
                    "timestamp": 1000,
                }
            ]
        }

        updated = backend.process_receipt(
            {"message_ids": ["1"], "is_read": True, "contact_id": "42"}
        )

        # Not a no-op: a non-empty updated list is returned...
        assert updated == [
            {
                "id": "1",
                "timestamp": 1000,
                "status": "read",
                "text": "hello",
                "is_mine": True,
            }
        ]
        # ...the in-memory cache reflects "read"...
        assert backend.cache["42"][0]["status"] == "read"
        # ...and the change is persisted to SQLite.
        db_rows = backend_mod._load_cache(protocol=PROTOCOL_TELEGRAM)["42"]
        assert db_rows[0]["status"] == "read"

    def test_process_receipt_is_idempotent_no_repeat_update(self, tmp_db):
        backend = _make_backend()
        backend.cache = {
            "42": [{"id": "1", "is_mine": True, "status": "read", "timestamp": 100}]
        }
        # Already read → second receipt must NOT produce another update.
        assert backend.process_receipt({"message_ids": ["1"], "is_read": True}) == []

    def test_process_receipt_never_downgrades_read(self, tmp_db):
        backend = _make_backend()
        backend.cache = {
            "42": [{"id": "1", "is_mine": True, "status": "read", "timestamp": 100}]
        }
        # delivery receipt (is_read=False) must not move read → delivered.
        assert backend.process_receipt({"message_ids": ["1"], "is_read": False}) == []
        assert backend.cache["42"][0]["status"] == "read"

    def test_handle_read_receipt_peer_id_conventions(self):
        """The fix translates PeerUser/PeerChat/PeerChannel to the same
        ``contact_id`` used by ``Message.chat_id``."""
        backend = _make_backend()

        backend.cache = {"42": [{"id": "1", "is_mine": True, "status": "sent"}]}
        asyncio.run(
            backend._handle_read_receipt(
                UpdateReadHistoryOutbox(
                    peer=PeerUser(user_id=42), max_id=5, pts=1, pts_count=1
                )
            )
        )
        assert backend.poll_once()[0].contact_id == "42"

        backend.cache = {"-123": [{"id": "1", "is_mine": True, "status": "sent"}]}
        asyncio.run(
            backend._handle_read_receipt(
                UpdateReadHistoryOutbox(
                    peer=PeerChat(chat_id=123), max_id=5, pts=1, pts_count=1
                )
            )
        )
        assert backend.poll_once()[0].contact_id == "-123"

        channel_id = str(-1000000000000 - 456)
        backend.cache = {channel_id: [{"id": "1", "is_mine": True, "status": "sent"}]}
        asyncio.run(
            backend._handle_read_receipt(
                UpdateReadHistoryOutbox(
                    peer=PeerChannel(channel_id=456), max_id=5, pts=1, pts_count=1
                )
            )
        )
        assert backend.poll_once()[0].contact_id == channel_id


class TestReceiptScoping:
    """🎯 Le receipt Telegram devono restare confinate al chat di origine.

    Gli id messaggio Telegram sono per-peer: due chat diverse possono avere
    entrambe un messaggio proprio con lo stesso id numerico.  ``process_receipt``
    riceve solo ``message_ids`` (senza contact_id) e matcha per id su TUTTE le
    chat, quindi rischia di marcare "read" un messaggio di un'altra chat.
    """

    def test_process_receipt_does_not_update_other_chats_with_same_id(self, tmp_db):
        backend_mod._add_message_to_cache(
            "42",
            "hello A",
            True,
            "You",
            100,
            protocol=PROTOCOL_TELEGRAM,
            msg_id="5",
            status="sent",
        )
        backend_mod._add_message_to_cache(
            "43",
            "hello B",
            True,
            "You",
            200,
            protocol=PROTOCOL_TELEGRAM,
            msg_id="5",
            status="sent",
        )
        backend = _make_backend()
        backend.cache = backend_mod._load_cache(protocol=PROTOCOL_TELEGRAM)

        # Receipt generata dal chat 42: solo il suo id "5" deve cambiare stato.
        backend.process_receipt(
            {"message_ids": ["5"], "is_read": True, "contact_id": "42"}
        )

        db = backend_mod._load_cache(protocol=PROTOCOL_TELEGRAM)
        assert db["42"][0]["status"] == "read"
        # Il messaggio id "5" del chat 43 NON deve essere toccato.
        assert db["43"][0]["status"] == "sent"


# ─── Defect B ────────────────────────────────────────────────────────────────


class TestDefectBStartupDuplicate:
    """📥 B — ingest da storico con stesso msg_id non duplica e non degrada."""

    def test_history_ingest_same_msg_id_no_duplicate_keeps_read(self, tmp_db):
        backend_mod._add_message_to_cache(
            "42",
            "hello",
            True,
            "You",
            1000,
            protocol=PROTOCOL_TELEGRAM,
            msg_id="42",
            status="read",
        )
        backend = _make_backend()

        backend.cache = backend._load_protocol_cache()
        assert backend._seen_msg_ids == {"42"}
        assert backend.cache["42"][0]["status"] == "read"

        # History fetch: same msg_id, server timestamp far away (>2s window).
        data = {
            "id": "42",
            "text": "hello",
            "is_mine": True,
            "sender": "You",
            "status": "sent",  # old behaviour: history hardcoded "sent"
        }
        added = backend.ingest_message("42", data, 1000 + 100_000)

        assert added is False  # deduped cross-session by msg_id
        assert len(backend.cache["42"]) == 1
        assert backend.cache["42"][0]["status"] == "read"
        # SQLite still has exactly one row, still "read".
        db_rows = backend_mod._load_cache(protocol=PROTOCOL_TELEGRAM)["42"]
        assert len(db_rows) == 1
        assert db_rows[0]["status"] == "read"

    def test_message_to_chat_event_does_not_hardcode_status(self):
        backend = _make_backend()
        backend._contacts_by_id = {42: None}
        msg = SimpleNamespace(
            id=7,
            chat_id=42,
            text="hello",
            out=True,
            sender=SimpleNamespace(first_name="Ada", last_name="", id=42),
            date=datetime(2025, 1, 1, tzinfo=UTC),
            photo=None,
            document=None,
            sticker=None,
            video=None,
            voice=None,
            audio=None,
            reply_to=None,
        )
        evt = backend._message_to_chat_event(msg)
        assert evt is not None
        assert "status" not in evt.payload


# ─── Defect C ────────────────────────────────────────────────────────────────


class TestDefectCReconciliation:
    """📥 C — riconciliazione ``read_outbox_max_id`` all'avvio."""

    def test_load_contacts_reconciles_read_outbox_max_id(self, tmp_db):
        backend_mod._add_message_to_cache(
            "42",
            "hello",
            True,
            "You",
            1000,
            protocol=PROTOCOL_TELEGRAM,
            msg_id="5",
            status="sent",
        )
        backend = _make_backend()
        backend.cache = backend._load_protocol_cache()
        assert backend.cache["42"][0]["status"] == "sent"

        dialog = SimpleNamespace(
            entity=SimpleNamespace(
                id=42, first_name="Ada", last_name="", username="", phone=""
            ),
            message=SimpleNamespace(date=datetime(2025, 1, 1, tzinfo=UTC)),
            read_outbox_max_id=10,  # receipt arrived while TUI was closed
        )
        backend._client = SimpleNamespace(get_dialogs=AsyncMock(return_value=[dialog]))

        asyncio.run(backend._load_contacts())

        # msg_id "5" <= read_outbox_max_id 10 → must be reconciled to "read".
        assert backend.cache["42"][0]["status"] == "read"
        db_rows = backend_mod._load_cache(protocol=PROTOCOL_TELEGRAM)["42"]
        assert db_rows[0]["status"] == "read"


# ─── Cleanup / rank guard ────────────────────────────────────────────────────


class TestDedupById:
    """🧹 ``_dedup_messages_by_id`` conserva lo status di rank più alto."""

    def test_dedup_keeps_read_over_lower_rowid_sent(self, tmp_db):
        backend_mod._add_message_to_cache(
            "42",
            "hello",
            True,
            "You",
            1000,
            protocol=PROTOCOL_TELEGRAM,
            msg_id="7",
            status="sent",
        )
        backend_mod._add_message_to_cache(
            "42",
            "hello",
            True,
            "You",
            1001,
            protocol=PROTOCOL_TELEGRAM,
            msg_id="7",
            status="read",
        )
        removed = backend_mod._dedup_messages_by_id()
        assert removed == 1
        rows = backend_mod._load_cache(protocol=PROTOCOL_TELEGRAM)["42"]
        assert len(rows) == 1
        assert rows[0]["status"] == "read"

    def test_dedup_is_idempotent(self, tmp_db):
        backend_mod._add_message_to_cache(
            "42",
            "hello",
            True,
            "You",
            1000,
            protocol=PROTOCOL_TELEGRAM,
            msg_id="7",
            status="sent",
        )
        backend_mod._add_message_to_cache(
            "42",
            "hello",
            True,
            "You",
            1001,
            protocol=PROTOCOL_TELEGRAM,
            msg_id="7",
            status="read",
        )
        assert backend_mod._dedup_messages_by_id() == 1
        assert backend_mod._dedup_messages_by_id() == 0

    def test_dedup_keeps_distinct_text_for_same_msg_id(self, tmp_db):
        # WhatsApp splits one message into several rows sharing msg_id.
        backend_mod._add_message_to_cache(
            "c",
            "part1",
            False,
            "X",
            1000,
            protocol="whatsapp",
            msg_id="m1",
            status="read",
        )
        backend_mod._add_message_to_cache(
            "c",
            "part2",
            False,
            "X",
            1000,
            protocol="whatsapp",
            msg_id="m1",
            status="read",
        )
        assert backend_mod._dedup_messages_by_id() == 0
        assert len(backend_mod._load_cache(protocol="whatsapp")["c"]) == 2

    def test_dedup_ignores_rows_without_msg_id(self, tmp_db):
        backend_mod._add_message_to_cache(
            "42", "a", False, "X", 1000, protocol=PROTOCOL_TELEGRAM
        )
        backend_mod._add_message_to_cache(
            "42", "a", False, "X", 1001, protocol=PROTOCOL_TELEGRAM
        )
        assert backend_mod._dedup_messages_by_id() == 0
        assert len(backend_mod._load_cache(protocol=PROTOCOL_TELEGRAM)["42"]) == 2


class TestUpdateStatusById:
    """📝 ``_update_message_status_by_id`` rispetta il rank-guard."""

    def test_rank_guard_never_downgrades_read(self, tmp_db):
        backend_mod._add_message_to_cache(
            "42",
            "hello",
            True,
            "You",
            1000,
            protocol=PROTOCOL_TELEGRAM,
            msg_id="9",
            status="read",
        )
        assert not backend_mod._update_message_status_by_id(
            "9", "sent", PROTOCOL_TELEGRAM, contact_number="42"
        )
        assert (
            backend_mod._load_cache(protocol=PROTOCOL_TELEGRAM)["42"][0]["status"]
            == "read"
        )

    def test_rank_guard_upgrades_sent_to_read(self, tmp_db):
        backend_mod._add_message_to_cache(
            "42",
            "hello",
            True,
            "You",
            1000,
            protocol=PROTOCOL_TELEGRAM,
            msg_id="9",
            status="sent",
        )
        assert backend_mod._update_message_status_by_id(
            "9", "read", PROTOCOL_TELEGRAM, contact_number="42"
        )
        assert (
            backend_mod._load_cache(protocol=PROTOCOL_TELEGRAM)["42"][0]["status"]
            == "read"
        )

    def test_update_scoped_by_contact(self, tmp_db):
        backend_mod._add_message_to_cache(
            "42",
            "x",
            True,
            "You",
            1000,
            protocol=PROTOCOL_TELEGRAM,
            msg_id="9",
            status="sent",
        )
        backend_mod._add_message_to_cache(
            "43",
            "x",
            True,
            "You",
            1000,
            protocol=PROTOCOL_TELEGRAM,
            msg_id="9",
            status="sent",
        )
        assert backend_mod._update_message_status_by_id(
            "9", "read", PROTOCOL_TELEGRAM, contact_number="42"
        )
        db = backend_mod._load_cache(protocol=PROTOCOL_TELEGRAM)
        assert db["42"][0]["status"] == "read"
        assert db["43"][0]["status"] == "sent"


class TestReconcileReadState:
    """📥 C (approfondimento) — ``_reconcile_read_state``: scope, rank, persistenza."""

    def test_reconcile_is_scoped_per_contact(self, tmp_db):
        backend_mod._add_message_to_cache(
            "42",
            "hello A",
            True,
            "You",
            100,
            protocol=PROTOCOL_TELEGRAM,
            msg_id="5",
            status="sent",
        )
        backend_mod._add_message_to_cache(
            "43",
            "hello B",
            True,
            "You",
            200,
            protocol=PROTOCOL_TELEGRAM,
            msg_id="5",
            status="sent",
        )
        backend = _make_backend()
        backend.cache = backend._load_protocol_cache()
        backend.contacts = [
            ChatContact(
                id="42",
                display_name="A",
                protocol=PROTOCOL_TELEGRAM,
                extras={"read_outbox_max_id": 10},
            ),
            ChatContact(id="43", display_name="B", protocol=PROTOCOL_TELEGRAM),
        ]

        backend._reconcile_read_state()

        assert backend.cache["42"][0]["status"] == "read"
        assert backend.cache["43"][0]["status"] == "sent"
        db = backend_mod._load_cache(protocol=PROTOCOL_TELEGRAM)
        assert db["42"][0]["status"] == "read"
        assert db["43"][0]["status"] == "sent"
        # Un solo evento receipt, per il contatto corretto.
        events = backend.poll_once()
        assert len(events) == 1
        assert events[0].contact_id == "42"

    def test_reconcile_does_not_mark_above_max_id(self, tmp_db):
        backend_mod._add_message_to_cache(
            "42",
            "hello",
            True,
            "You",
            100,
            protocol=PROTOCOL_TELEGRAM,
            msg_id="50",
            status="sent",
        )
        backend = _make_backend()
        backend.cache = backend._load_protocol_cache()
        backend.contacts = [
            ChatContact(
                id="42",
                display_name="A",
                protocol=PROTOCOL_TELEGRAM,
                extras={"read_outbox_max_id": 10},
            )
        ]

        backend._reconcile_read_state()

        # msg_id 50 > max_id 10 → resta "sent", nessun evento.
        assert backend.cache["42"][0]["status"] == "sent"
        assert backend.poll_once() == []

    def test_reconcile_never_downgrades(self, tmp_db):
        backend_mod._add_message_to_cache(
            "42",
            "hello",
            True,
            "You",
            100,
            protocol=PROTOCOL_TELEGRAM,
            msg_id="5",
            status="read",
        )
        backend = _make_backend()
        backend.cache = backend._load_protocol_cache()
        backend.contacts = [
            ChatContact(
                id="42",
                display_name="A",
                protocol=PROTOCOL_TELEGRAM,
                extras={"read_outbox_max_id": 10},
            )
        ]

        backend._reconcile_read_state()

        assert backend.cache["42"][0]["status"] == "read"
        # Già read → nessun evento receipt spurio.
        assert backend.poll_once() == []


class TestReceiptEndToEndContract:
    """🔗 handler → evento (contact_id) → process_receipt scoped."""

    def test_live_receipt_event_scopes_process_receipt(self, tmp_db):
        backend_mod._add_message_to_cache(
            "42",
            "hello A",
            True,
            "You",
            100,
            protocol=PROTOCOL_TELEGRAM,
            msg_id="5",
            status="sent",
        )
        backend_mod._add_message_to_cache(
            "43",
            "hello B",
            True,
            "You",
            200,
            protocol=PROTOCOL_TELEGRAM,
            msg_id="5",
            status="sent",
        )
        backend = _make_backend()
        backend.cache = backend._load_protocol_cache()

        # Receipt live per il chat 42 (max_id copre id 5).
        update = UpdateReadHistoryOutbox(
            peer=PeerUser(user_id=42), max_id=5, pts=1, pts_count=1
        )
        asyncio.run(backend._handle_read_receipt(update))
        event = backend.poll_once()[0]
        assert event.contact_id == "42"
        assert event.payload["message_ids"] == ["5"]

        # Mimica della costruzione dell'envelope in _handle_receipt_event.
        updated = backend.process_receipt(
            {
                "message_ids": event.payload.get("message_ids", []),
                "is_read": event.payload.get("is_read", False),
                "contact_id": event.contact_id,
            }
        )
        assert [u["id"] for u in updated] == ["5"]
        assert updated[0]["text"] == "hello A"
        db = backend_mod._load_cache(protocol=PROTOCOL_TELEGRAM)
        assert db["42"][0]["status"] == "read"
        assert db["43"][0]["status"] == "sent"
