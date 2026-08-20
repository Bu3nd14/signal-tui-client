"""
Regression tests for the Signal real-timestamp fix (production bug: editing
messages ignored the client ``timestamp`` because signal-cli assigns its own).

signal-cli has no ``timestamp`` option for ``send``; it ignores the client
value and assigns the real timestamp itself.  The fix makes ``_send_message_sync``
return that real timestamp (from ``result.timestamp`` in daemon mode, or the
stdout value in subprocess mode, with an optimistic fallback), and threads it
through as the stable message ``id`` so the echo matches by id and the edit
target is the real server timestamp.

Covers (mirroring ``tests/test_edit_signal.py``, with a temporary DB):

- ``_send_message_sync`` daemon RPC / fallback / subprocess parsing;
- ``ingest_message`` echo upgrade (optimistic twin), idempotence, cross-device;
- ``envelope_to_event`` sync-sent id exposure + incoming no-id;
- ``apply_edit`` id-first match (entry ts) + legacy ts fallback;
- the Signal send worker "real id" path (id into ingest + UI cache + widget);
- the edit UI id-first / ts-fallback-with-warning.
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
from backends import SignalBackend
from models import PROTOCOL_SIGNAL, ChatContact
from tui.edit import EditMessageMixin
from tui.send import SendMixin
from ui_components import MessageWidget

# ─── Helpers / fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def tmp_db(tmp_path: Path):
    """Point the backend at a temp DB/CACHE_DIR for the duration of a test."""
    db_file = tmp_path / "messages.db"
    with (
        patch.object(backend_mod, "DB_FILE", db_file),
        patch.object(backend_mod, "CACHE_DIR", tmp_path),
    ):
        yield db_file


def _db_rows(db_file: Path) -> list[dict]:
    """Raw rows from the temp DB (oldest first), as dicts."""
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM messages ORDER BY id")]
    conn.close()
    return rows


def _backend_with_contact(contact_id: str = "+391234567890") -> SignalBackend:
    """A SignalBackend that knows a single contact (id == contact_id)."""
    backend = SignalBackend()
    contact = ChatContact(
        id=contact_id,
        display_name="Mario",
        protocol=PROTOCOL_SIGNAL,
        extras={"aci": "uuid-123"},
    )
    backend._set_contacts([contact])
    return backend


def _optimistic_data(text: str) -> dict:
    """An outgoing optimistic seed (no server id yet)."""
    return {
        "text": text,
        "is_mine": True,
        "sender": "You",
        "quote_text": None,
        "msg_type": "text",
        "attachment_info": None,
        "attachment_id": None,
    }


def _echo_data(text: str, real_ts: int) -> dict:
    """An outgoing echo carrying the real server id (``id == str(real_ts)``)."""
    return {
        **_optimistic_data(text),
        "id": str(real_ts),
    }


# ─── _send_message_sync: real timestamp ───────────────────────────────────────


class TestSendMessageSyncRealTimestamp:
    """📤 ``_send_message_sync`` returns the REAL server timestamp when available."""

    REAL = 1787250931234

    def test_daemon_rpc_returns_real_timestamp(self):
        """``result.timestamp`` from the RPC response is returned (int)."""
        backend = SignalBackend()
        backend._use_daemon = True
        with patch.object(
            backend._rpc,
            "send_message",
            return_value={"result": {"results": [], "timestamp": self.REAL}},
        ):
            result = backend._send_message_sync(
                "+391234567890", "ciao", None, None, None
            )

        assert result == self.REAL
        assert isinstance(result, int)

    def test_daemon_rpc_without_timestamp_falls_back_to_optimistic_ts(self):
        """``{"result": {}}`` → optimistic ts (int > 0 ≈ now)."""
        backend = SignalBackend()
        backend._use_daemon = True
        before = int(time.time() * 1000)
        with patch.object(backend._rpc, "send_message", return_value={"result": {}}):
            result = backend._send_message_sync(
                "+391234567890", "ciao", None, None, None
            )
        after = int(time.time() * 1000)

        assert isinstance(result, int)
        assert result > 0
        assert before - 5 <= result <= after + 5

    def test_subprocess_parses_stdout_timestamp(self):
        """Subprocess stdout ``"1787250931234\\n"`` → 1787250931234."""
        backend = SignalBackend()
        backend._use_daemon = False
        with patch("backends.signal._send_subprocess", return_value="1787250931234\n"):
            result = backend._send_message_sync(
                "+391234567890", "ciao", None, None, None
            )

        assert result == 1787250931234

    def test_subprocess_dirty_stdout_falls_back_to_optimistic_ts(self):
        """Non-numeric stdout → optimistic ts (int > 0 ≈ now)."""
        backend = SignalBackend()
        backend._use_daemon = False
        before = int(time.time() * 1000)
        with patch("backends.signal._send_subprocess", return_value="garbage\n"):
            result = backend._send_message_sync(
                "+391234567890", "ciao", None, None, None
            )
        after = int(time.time() * 1000)

        assert isinstance(result, int)
        assert result > 0
        assert before - 5 <= result <= after + 5


# ─── ingest_message: echo upgrade / idempotence / cross-device ────────────────


class TestIngestEchoUpgrade:
    """🔄 L'echo outgoing con id reale aggancia l'id al twin ottimistico."""

    CID = "+391234567890"
    OTT = 1787250930000
    REAL = 1787250931234
    TEXT = "ciao"

    def test_echo_upgrades_optimistic_twin_keeps_optimistic_ts(self, tmp_db):
        """Echo con id → False; entry id==REAL, ts INVARIATO; DB msg_id + ts."""
        backend = SignalBackend()
        assert (
            backend.ingest_message(self.CID, _optimistic_data(self.TEXT), self.OTT)
            is True
        )

        result = backend.ingest_message(
            self.CID, _echo_data(self.TEXT, self.REAL), self.REAL
        )

        assert result is False
        entry = backend.cache[self.CID][0]
        assert entry["id"] == str(self.REAL)
        assert entry["timestamp"] == self.OTT  # ts entry INVARIATO (ottimistico)
        rows = _db_rows(tmp_db)
        assert len(rows) == 1
        assert rows[0]["msg_id"] == str(self.REAL)
        assert rows[0]["timestamp"] == self.OTT  # ts OTTIMISTICO nel DB

    def test_second_echo_is_idempotent(self, tmp_db):
        """Secondo echo → False, 1 sola entry, 1 sola riga."""
        backend = SignalBackend()
        backend.ingest_message(self.CID, _optimistic_data(self.TEXT), self.OTT)
        backend.ingest_message(self.CID, _echo_data(self.TEXT, self.REAL), self.REAL)

        result = backend.ingest_message(
            self.CID, _echo_data(self.TEXT, self.REAL), self.REAL
        )

        assert result is False
        assert len(backend.cache[self.CID]) == 1
        assert backend.cache[self.CID][0]["id"] == str(self.REAL)
        assert len(_db_rows(tmp_db)) == 1

    def test_sync_sent_without_optimistic_twin_adds_entry_and_row_with_id(self, tmp_db):
        """Cross-device: nessun twin ottimistico → True; entry e riga DB con id."""
        backend = SignalBackend()
        result = backend.ingest_message(
            self.CID, _echo_data("cross-device", self.REAL), self.REAL
        )

        assert result is True
        rows = _db_rows(tmp_db)
        assert len(rows) == 1
        assert rows[0]["msg_id"] == str(self.REAL)  # riga DB con id
        entry = backend.cache[self.CID][0]
        assert entry.get("id") == str(self.REAL)  # entry in-memory con id


# ─── envelope_to_event: id exposure ───────────────────────────────────────────


class TestEnvelopeToEventRealId:
    """📥 sync sent → payload ``id``; incoming dataMessage → senza ``id``."""

    TS = 1787250931234

    def test_sync_sent_payload_carries_real_id(self):
        backend = _backend_with_contact()
        envelope = {
            "source": "+391234567890",
            "timestamp": self.TS,
            "syncMessage": {
                "sentMessage": {
                    "destination": "+391234567890",
                    "timestamp": self.TS,
                    "message": "ciao",
                }
            },
        }

        events = backend.envelope_to_event(envelope)

        assert len(events) == 1
        ev = events[0]
        assert ev.type == "message"
        assert ev.payload["is_mine"] is True
        assert ev.payload["id"] == str(self.TS)
        assert ev.payload["timestamp"] == self.TS

    def test_incoming_data_message_has_no_id(self):
        backend = _backend_with_contact()
        envelope = {
            "source": "+391234567890",
            "sourceNumber": "+391234567890",
            "sourceName": "Mario",
            "timestamp": self.TS,
            "dataMessage": {"message": "ciao", "timestamp": self.TS},
        }

        events = backend.envelope_to_event(envelope)

        assert len(events) == 1
        ev = events[0]
        assert ev.payload["is_mine"] is False
        assert ev.payload.get("id") is None


# ─── apply_edit: id-first + ts fallback ───────────────────────────────────────


class TestApplyEditIdFirst:
    """✏️ ``apply_edit``: match per id (ts entry), fallback ts su entry senza id."""

    CID = "+391234567890"
    OTT = 1787250930000
    REAL = 1787250931234

    def _cached(self, **overrides) -> dict:
        msg = {
            "id": str(self.REAL),
            "text": "vecchio",
            "is_mine": True,
            "sender": "You",
            "timestamp": self.OTT,
            "quote_text": None,
            "msg_type": "text",
            "attachment_info": None,
            "attachment_id": None,
            "read": True,
            "status": "sent",
        }
        msg.update(overrides)
        return msg

    def test_id_first_match_uses_entry_timestamp(self):
        """message_id == str(REAL) → hit; ``_update_message_text`` con ts entry."""
        backend = SignalBackend()
        backend.cache[self.CID] = [self._cached()]

        with patch.object(backend_mod, "_update_message_text") as mock_update:
            mock_update.return_value = True
            result = backend.apply_edit(self.CID, str(self.REAL), "nuovo")

        assert result == {
            "message_id": str(self.REAL),
            "timestamp": self.OTT,
            "old_text": "vecchio",
            "text": "nuovo",
            "is_mine": True,
        }
        mock_update.assert_called_once_with(
            self.CID,
            "nuovo",
            protocol="signal",
            timestamp=self.OTT,  # ts della ENTRY (ottimistico), non il REAL
            old_text="vecchio",
            is_mine=True,
        )
        assert backend.cache[self.CID][0]["text"] == "nuovo"
        assert backend.cache[self.CID][0]["edited"] is True

    def test_ts_fallback_still_works_for_entry_without_id(self):
        """Entry legacy senza id → match per timestamp (fallback)."""
        backend = SignalBackend()
        backend.cache[self.CID] = [self._cached(id=None)]

        with patch.object(backend_mod, "_update_message_text") as mock_update:
            mock_update.return_value = True
            result = backend.apply_edit(self.CID, str(self.OTT), "nuovo")

        assert result is not None
        assert result["message_id"] == str(self.OTT)
        assert result["timestamp"] == self.OTT
        mock_update.assert_called_once_with(
            self.CID,
            "nuovo",
            protocol="signal",
            timestamp=self.OTT,
            old_text="vecchio",
            is_mine=True,
        )
        assert backend.cache[self.CID][0]["text"] == "nuovo"


# ─── Worker Signal: "real id" path ────────────────────────────────────────────


class _SendHandler(SendMixin):
    """Minimal SendMixin host (mirrors ``test_failed_send_status.py``)."""

    def __init__(self, contact, messages=(), bubble=None):
        self.selected_contact = contact
        self._cache = {contact.cache_key: list(messages)}
        self._status = MagicMock()
        self._transition_outgoing_status = MagicMock(return_value=True)
        self.run_worker = MagicMock()
        self.manager = SimpleNamespace(get=MagicMock())
        self.call_from_thread = MagicMock(
            side_effect=lambda callback, *args, **kwargs: callback(*args, **kwargs)
        )
        self.chat_log = SimpleNamespace(children=[bubble] if bubble is not None else [])


class TestSignalWorkerRealId:
    """🧵 Il worker Signal ingerisce l'id reale e aggiorna cache UI + widget."""

    def test_worker_ingests_real_id_and_updates_cache_and_widget(self):
        contact = ChatContact(
            id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        timestamp = 1787250930000
        text = "ciao"
        real = 1787250931234
        bubble = MessageWidget(
            text, timestamp=timestamp, is_mine=True, status="pending"
        )
        handler = _SendHandler(
            contact,
            [{"is_mine": True, "timestamp": timestamp, "text": text}],
            bubble=bubble,
        )
        backend = MagicMock()
        backend.send_message_sync.return_value = real
        backend.ingest_message = MagicMock()
        handler.manager.get.return_value = backend

        handler._send_message_worker(
            text,
            timestamp,
            None,
            protocol=PROTOCOL_SIGNAL,
            contact_id=contact.id,
        )

        # ``ingest_message`` riceve l'id reale (path "real id" attivo per Signal).
        backend.ingest_message.assert_called_once()
        ingest_args = backend.ingest_message.call_args
        assert ingest_args.args[0] == contact.id
        assert ingest_args.args[1]["id"] == real
        assert ingest_args.args[1]["text"] == text
        # Cache UI e widget ``_message_id`` aggiornati con l'id reale.
        assert handler._cache[contact.cache_key][0]["id"] == str(real)
        assert bubble._message_id == str(real)


# ─── Edit UI: id-first / ts-fallback-with-warning ─────────────────────────────


class _EditHandler(EditMessageMixin):
    """Minimal EditMessageMixin host (no Textual app needed)."""

    def __init__(self, contact, entry):
        self._download_mode = False
        self.selected_contact = contact
        self._cache = {contact.cache_key: [entry]}
        self._status = MagicMock()
        self._cancel_reply = MagicMock()
        self._cancel_edit = MagicMock()
        self._update_reply_bar = MagicMock()
        self._editing_message = None
        self.chat_log = SimpleNamespace(children=[])
        self.query_one = MagicMock(return_value=MagicMock())


class TestEditUIRealId:
    """🖱️ L'apertura edit usa l'id reale (o fallback ts + avviso)."""

    def _contact(self):
        return ChatContact(
            id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )

    def test_entry_with_id_uses_entry_id(self):
        entry = {
            "id": "sig-1000",
            "text": "vecchio",
            "is_mine": True,
            "sender": "You",
            "timestamp": 1000,
            "msg_type": "text",
            "status": "sent",
        }
        handler = _EditHandler(self._contact(), entry)
        event = MessageWidget.EditRequested(
            "vecchio",
            1000,
            "You",
            is_mine=True,
            status="sent",
            message_id="sig-1000",
        )

        handler.on_message_widget_edit_requested(event)

        assert handler._editing_message["message_id"] == "sig-1000"
        handler._status.assert_not_called()  # nessun avviso: id noto

    def test_entry_without_id_falls_back_to_ts_and_warns(self):
        entry = {
            "text": "vecchio",
            "is_mine": True,
            "sender": "You",
            "timestamp": 1000,
            "msg_type": "text",
            "status": "sent",
        }
        handler = _EditHandler(self._contact(), entry)
        event = MessageWidget.EditRequested(
            "vecchio", 1000, "You", is_mine=True, status="sent", message_id=None
        )

        handler.on_message_widget_edit_requested(event)

        assert handler._editing_message["message_id"] == "1000"
        handler._status.assert_called_once_with(
            "⚠️ ID server non noto — la modifica potrebbe non propagarsi", 5
        )
