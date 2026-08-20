"""
Phase 4 (Telegram backend) tests for message editing.

Covers the Telegram-side edit surface described in DESIGN_EDIT_MESSAGES.md
§3.2 and the test plan in §7 (``tests/test_telegram_edit.py``):

- ``_handle_message_edited`` → a single ``ChatEvent("message_edit")`` with the
  ORIGINAL timestamp (``msg.date``) and the edit timestamp (``msg.edit_date``);
  media edits and empty-text edits are skipped;
- ``edit_message_sync`` → ``client.edit_message(entity, int(id), text)`` on the
  dedicated loop, with ValueError on non-numeric / non-positive ids;
- ``apply_edit`` → cache + SQLite mutation keyed by server id, idempotence and
  media / unknown-id / is_mine-mismatch guards;
- ``ingest_message`` dedup branch → applies the edit (no new row) and
  ``fetch_recent_history`` reconciliation.

Tests that touch SQLite use an isolated temporary DB (``backend.DB_FILE`` /
``backend.CACHE_DIR`` patched), mirroring ``tests/test_db_edit.py``; the real
DB in ``~/.local/share/signal-tui-client`` is never touched.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import backend as backend_mod
from backends.telegram import TelegramBackend
from models import PROTOCOL_TELEGRAM, ChatContact

# ─── Helpers / fixtures ───────────────────────────────────────────────────────

# Original message timestamp (ms) and edit timestamp (ms) used in assertions.
_ORIG_TS = 1735787045000  # 2025-01-02 03:04:05 UTC (msg.date)
_EDIT_TS = 1735787100000  # 2025-01-02 03:05:00 UTC (msg.edit_date, +55s)


def _backend() -> TelegramBackend:
    """A TelegramBackend with Telethon config stubbed (no network)."""
    backend = TelegramBackend()
    backend._api_id = 123
    backend._api_hash = "hash"
    return backend


def _message(**overrides) -> SimpleNamespace:
    """A Telethon-like ``Message`` mock (mirrors ``tests/test_telegram.py``)."""
    fields = {
        "chat_id": 42,
        "text": "hello",
        "out": False,
        "sender": SimpleNamespace(first_name="Ada", last_name="Lovelace", id=7),
        "date": datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC),
        "edit_date": None,
        "photo": None,
        "document": None,
        "sticker": None,
        "video": None,
        "voice": None,
        "audio": None,
        "reply_to": None,
        "id": 99,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _cached_message(**overrides) -> dict:
    """A cached Telegram message dict (text) with sensible defaults."""
    msg = {
        "id": "99",
        "text": "vecchio",
        "is_mine": False,
        "sender": "Mario",
        "timestamp": _ORIG_TS,
        "quote_text": None,
        "msg_type": "text",
        "attachment_info": None,
        "attachment_id": None,
        "read": False,
        "status": "read",
    }
    msg.update(overrides)
    return msg


@pytest.fixture
def tmp_db(tmp_path: Path):
    """Point the backend at a temp DB/CACHE_DIR for the duration of a test."""
    db_file = tmp_path / "messages.db"
    with (
        patch.object(backend_mod, "DB_FILE", db_file),
        patch.object(backend_mod, "CACHE_DIR", tmp_path),
    ):
        yield db_file


def _db_rows(db_file: Path) -> list[tuple]:
    """Raw ``(text, edited)`` rows, ordered by primary key."""
    conn = sqlite3.connect(db_file)
    rows = conn.execute("SELECT text, edited FROM messages ORDER BY id").fetchall()
    conn.close()
    return rows


# ─── _handle_message_edited ───────────────────────────────────────────────────


class TestHandleMessageEdited:
    """📥 ``events.MessageEdited`` → un singolo ``ChatEvent("message_edit")``."""

    def test_produces_correct_message_edit_event(self):
        """Payload normalizzato: id, ts ORIGINALE, edit_timestamp, is_mine."""
        backend = _backend()
        contact = ChatContact(id="42", display_name="Ada", protocol=PROTOCOL_TELEGRAM)
        backend._contacts_by_id = {42: contact}
        msg = _message(
            id=99,
            chat_id=42,
            text="nuovo testo",
            out=True,
            date=datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC),
            edit_date=datetime(2025, 1, 2, 3, 5, 0, tzinfo=UTC),
        )

        asyncio.run(backend._handle_message_edited(SimpleNamespace(message=msg)))

        events = backend.poll_once()
        assert len(events) == 1
        ev = events[0]
        assert ev.type == "message_edit"
        assert ev.protocol == PROTOCOL_TELEGRAM
        assert ev.contact_id == "42"
        assert ev.payload["edit_message_id"] == "99"
        assert ev.payload["text"] == "nuovo testo"
        assert ev.payload["timestamp"] == _ORIG_TS  # msg.date = ts ORIGINALE
        assert ev.payload["edit_timestamp"] == _EDIT_TS  # msg.edit_date
        assert ev.payload["is_mine"] is True  # msg.out
        assert ev.payload["sender"] == ""
        assert ev.payload["contact"] is contact
        assert ev.payload["msg_type"] == "text"

    def test_incoming_edit_is_mine_false_and_no_edit_date(self):
        """``out=False`` → is_mine False; ``edit_date`` assente → edit_timestamp None."""
        backend = _backend()
        msg = _message(out=False, edit_date=None)

        asyncio.run(backend._handle_message_edited(SimpleNamespace(message=msg)))

        ev = backend.poll_once()[0]
        assert ev.payload["is_mine"] is False
        assert ev.payload["edit_timestamp"] is None
        assert ev.payload["timestamp"] == _ORIG_TS

    @pytest.mark.parametrize(
        "media_field", ["photo", "document", "sticker", "video", "voice", "audio"]
    )
    def test_media_edit_is_skipped(self, media_field):
        """Un edit con media (caption/media fuori scope) non produce eventi."""
        backend = _backend()
        msg = _message(**{media_field: object()}, text="caption")

        asyncio.run(backend._handle_message_edited(SimpleNamespace(message=msg)))

        assert backend.poll_once() == []

    @pytest.mark.parametrize("empty", ["", "   ", "\n\t"])
    def test_empty_text_is_skipped(self, empty):
        """Edit di sola formattazione / testo vuoto → nessun evento."""
        backend = _backend()
        msg = _message(text=empty)

        asyncio.run(backend._handle_message_edited(SimpleNamespace(message=msg)))

        assert backend.poll_once() == []

    def test_none_message_is_skipped(self):
        backend = _backend()
        asyncio.run(backend._handle_message_edited(SimpleNamespace(message=None)))
        assert backend.poll_once() == []

    def test_none_chat_id_is_skipped(self):
        backend = _backend()
        msg = _message(chat_id=None)
        asyncio.run(backend._handle_message_edited(SimpleNamespace(message=msg)))
        assert backend.poll_once() == []


# ─── edit_message_sync ────────────────────────────────────────────────────────


class TestEditMessageSync:
    """✏️ ``edit_message_sync``: edit via Telethon sul loop dedicato."""

    def test_not_connected_raises_runtime_error(self):
        backend = _backend()
        with pytest.raises(RuntimeError, match="not connected"):
            backend.edit_message_sync("42", "99", "nuovo")

    def test_success_calls_edit_message_with_resolved_entity(self, monkeypatch):
        """Chiama ``client.edit_message(entity, int(id), text)`` e ritorna True."""
        backend = _backend()
        backend._loop = MagicMock()
        backend._client = SimpleNamespace(edit_message=AsyncMock())
        monkeypatch.setattr(
            backend, "_resolve_input_entity", AsyncMock(return_value="entity")
        )
        monkeypatch.setattr(
            "backends.telegram.asyncio.run_coroutine_threadsafe",
            lambda coro, _loop: SimpleNamespace(
                result=lambda timeout: asyncio.run(coro)
            ),
        )

        result = backend.edit_message_sync("42", "99", "nuovo testo")

        assert result is True
        backend._resolve_input_entity.assert_awaited_once_with(42)
        backend._client.edit_message.assert_awaited_once_with(
            "entity", 99, "nuovo testo"
        )

    def test_success_with_client_get_input_entity(self, monkeypatch):
        """Variante: entity risolta dal client (get_input_entity) invece che mock."""
        backend = _backend()
        backend._loop = MagicMock()
        backend._client = SimpleNamespace(
            get_input_entity=AsyncMock(return_value="entity"),
            edit_message=AsyncMock(),
        )
        monkeypatch.setattr(
            "backends.telegram.asyncio.run_coroutine_threadsafe",
            lambda coro, _loop: SimpleNamespace(
                result=lambda timeout: asyncio.run(coro)
            ),
        )

        assert backend.edit_message_sync("42", "99", "nuovo testo") is True
        backend._client.edit_message.assert_awaited_once_with(
            "entity", 99, "nuovo testo"
        )

    def test_non_numeric_message_id_raises_value_error(self):
        backend = _backend()
        backend._loop = MagicMock()
        backend._client = SimpleNamespace()
        with pytest.raises(ValueError, match="contact/message id"):
            backend.edit_message_sync("42", "abc", "nuovo")

    def test_non_numeric_contact_id_raises_value_error(self):
        backend = _backend()
        backend._loop = MagicMock()
        backend._client = SimpleNamespace()
        with pytest.raises(ValueError, match="contact/message id"):
            backend.edit_message_sync("abc", "99", "nuovo")

    def test_negative_mid_raises_value_error(self):
        backend = _backend()
        backend._loop = MagicMock()
        backend._client = SimpleNamespace()
        with pytest.raises(ValueError, match="message id"):
            backend.edit_message_sync("42", "-5", "nuovo")

    def test_zero_mid_raises_value_error(self):
        """``message_id="0"`` → ValueError (id non positivo)."""
        backend = _backend()
        backend._loop = MagicMock()
        backend._client = SimpleNamespace()
        with pytest.raises(ValueError, match="message id"):
            backend.edit_message_sync("42", "0", "nuovo")

    def test_validation_does_not_schedule_on_loop(self, monkeypatch):
        """Id non valido → nessuna coroutine schedulata sul loop."""
        backend = _backend()
        backend._loop = MagicMock()
        backend._client = SimpleNamespace()
        run_threadsafe = MagicMock()
        monkeypatch.setattr(
            "backends.telegram.asyncio.run_coroutine_threadsafe", run_threadsafe
        )

        with pytest.raises(ValueError):
            backend.edit_message_sync("42", "0", "nuovo")

        run_threadsafe.assert_not_called()


# ─── apply_edit ───────────────────────────────────────────────────────────────


class TestApplyEdit:
    """✏️ ``apply_edit``: match per server id, mutazione cache+DB, guardie."""

    def test_hit_updates_cache_and_db(self, tmp_db):
        """Hit per id → cache (text+edited) e riga SQLite (text + edited=1)."""
        backend_mod._add_message_to_cache(
            "42",
            "vecchio",
            False,
            "Mario",
            _ORIG_TS,
            protocol=PROTOCOL_TELEGRAM,
            msg_id="99",
        )
        backend = _backend()
        backend.cache["42"] = [_cached_message()]

        result = backend.apply_edit("42", "99", "nuovo")

        assert result == {
            "message_id": "99",
            "timestamp": _ORIG_TS,
            "old_text": "vecchio",
            "text": "nuovo",
            "is_mine": False,
        }
        cached = backend.cache["42"][0]
        assert cached["text"] == "nuovo"
        assert cached["edited"] is True
        # id / timestamp identity untouched
        assert cached["id"] == "99"
        assert cached["timestamp"] == _ORIG_TS
        loaded = backend_mod._load_cache(protocol=PROTOCOL_TELEGRAM)["42"][0]
        assert loaded["text"] == "nuovo"
        assert loaded["edited"] is True
        assert _db_rows(tmp_db) == [("nuovo", 1)]

    def test_calls_update_message_text_with_correct_params(self):
        """``_update_message_text`` invocato con ``msg_id`` giusto."""
        backend = _backend()
        backend.cache["42"] = [_cached_message()]

        with patch.object(backend_mod, "_update_message_text") as mock_update:
            mock_update.return_value = True
            backend.apply_edit("42", "99", "nuovo")

        mock_update.assert_called_once_with(
            "42", "nuovo", protocol=PROTOCOL_TELEGRAM, msg_id="99"
        )

    def test_identical_text_returns_none(self):
        """Testo già identico → None (idempotente, niente ``edited``)."""
        backend = _backend()
        backend.cache["42"] = [_cached_message(text="stesso")]

        result = backend.apply_edit("42", "99", "stesso")

        assert result is None
        msg = backend.cache["42"][0]
        assert msg["text"] == "stesso"
        assert "edited" not in msg

    def test_media_message_returns_none(self):
        """``msg_type != "text"`` → None (mai riscrivere label media)."""
        backend = _backend()
        backend.cache["42"] = [_cached_message(msg_type="image")]

        result = backend.apply_edit("42", "99", "nuovo")

        assert result is None
        msg = backend.cache["42"][0]
        assert msg["text"] == "vecchio"
        assert "edited" not in msg

    def test_unknown_id_returns_none(self):
        """Id ignoto → None, cache intatta."""
        backend = _backend()
        backend.cache["42"] = [_cached_message()]

        result = backend.apply_edit("42", "999", "nuovo")

        assert result is None
        assert backend.cache["42"][0]["text"] == "vecchio"

    def test_is_mine_mismatch_returns_none(self):
        """``is_mine`` esplicito non combaciante → None."""
        backend = _backend()
        backend.cache["42"] = [_cached_message(is_mine=False)]

        result = backend.apply_edit("42", "99", "nuovo", is_mine=True)

        assert result is None
        assert backend.cache["42"][0]["text"] == "vecchio"

    def test_is_mine_matching_applies(self):
        """``is_mine`` combaciante → edit applicato."""
        backend = _backend()
        backend.cache["42"] = [_cached_message(is_mine=True, sender="You")]

        with patch.object(backend_mod, "_update_message_text") as mock_update:
            mock_update.return_value = True
            result = backend.apply_edit("42", "99", "nuovo", is_mine=True)

        assert result is not None
        assert result["is_mine"] is True
        mock_update.assert_called_once()


# ─── ingest_message (dedup branch) ────────────────────────────────────────────


class TestIngestMessageEdit:
    """🔄 ``ingest_message`` con id noto e testo diverso → edit, non nuovo."""

    def test_known_id_different_text_invokes_apply_edit(self, monkeypatch):
        """Dedup per id: ``apply_edit`` invocato, ritorna False, nessuna riga nuova."""
        backend = _backend()
        backend.cache["42"] = [_cached_message(text="vecchio")]
        backend._seen_msg_ids = {"99"}
        apply_edit = MagicMock()
        monkeypatch.setattr(backend, "apply_edit", apply_edit)

        result = backend.ingest_message(
            "42",
            {"id": "99", "text": "nuovo", "is_mine": False, "msg_type": "text"},
            _ORIG_TS,
        )

        assert result is False
        apply_edit.assert_called_once_with("42", "99", "nuovo")
        assert len(backend.cache["42"]) == 1

    def test_known_id_different_text_updates_cache_and_db(self, tmp_db):
        """Flusso reale (senza mock di ``apply_edit``): cache e DB aggiornati."""
        backend_mod._add_message_to_cache(
            "42",
            "vecchio",
            False,
            "Mario",
            _ORIG_TS,
            protocol=PROTOCOL_TELEGRAM,
            msg_id="99",
        )
        backend = _backend()
        backend.cache["42"] = [_cached_message(text="vecchio")]
        backend._seen_msg_ids = {"99"}

        result = backend.ingest_message(
            "42",
            {"id": "99", "text": "nuovo", "is_mine": False, "msg_type": "text"},
            _ORIG_TS,
        )

        assert result is False
        assert len(backend.cache["42"]) == 1
        assert backend.cache["42"][0]["text"] == "nuovo"
        assert backend.cache["42"][0]["edited"] is True
        loaded = backend_mod._load_cache(protocol=PROTOCOL_TELEGRAM)["42"][0]
        assert loaded["text"] == "nuovo"
        assert _db_rows(tmp_db) == [("nuovo", 1)]

    def test_known_id_same_text_does_not_apply_edit(self, monkeypatch):
        """Testo identico → nessun ``apply_edit`` (dedup puro)."""
        backend = _backend()
        backend.cache["42"] = [_cached_message(text="stesso")]
        backend._seen_msg_ids = {"99"}
        apply_edit = MagicMock()
        monkeypatch.setattr(backend, "apply_edit", apply_edit)

        result = backend.ingest_message(
            "42",
            {"id": "99", "text": "stesso", "is_mine": False, "msg_type": "text"},
            _ORIG_TS,
        )

        assert result is False
        apply_edit.assert_not_called()
        assert len(backend.cache["42"]) == 1

    def test_known_id_empty_text_does_not_apply_edit(self, monkeypatch):
        """Testo vuoto → nessun ``apply_edit`` (guard ``and text``)."""
        backend = _backend()
        backend.cache["42"] = [_cached_message(text="vecchio")]
        backend._seen_msg_ids = {"99"}
        apply_edit = MagicMock()
        monkeypatch.setattr(backend, "apply_edit", apply_edit)

        result = backend.ingest_message(
            "42",
            {"id": "99", "text": "", "is_mine": False, "msg_type": "text"},
            _ORIG_TS,
        )

        assert result is False
        apply_edit.assert_not_called()

    def test_known_id_media_entry_does_not_apply_edit(self, monkeypatch):
        """Entry media (msg_type != text) → nessun ``apply_edit``."""
        backend = _backend()
        backend.cache["42"] = [_cached_message(text="vecchio", msg_type="image")]
        backend._seen_msg_ids = {"99"}
        apply_edit = MagicMock()
        monkeypatch.setattr(backend, "apply_edit", apply_edit)

        result = backend.ingest_message(
            "42",
            {"id": "99", "text": "nuovo", "is_mine": False, "msg_type": "image"},
            _ORIG_TS,
        )

        assert result is False
        apply_edit.assert_not_called()


# ─── fetch_recent_history (reconciliation) ───────────────────────────────────


class TestFetchRecentHistoryEdit:
    """🕰️ Riconciliazione: uno storico già editato aggiorna la riga esistente."""

    def test_fetch_recent_history_applies_edit_to_cached_message(
        self, tmp_db, monkeypatch
    ):
        """Messaggio già noto ri-fetchato col testo nuovo → cache/DB aggiornati."""
        backend_mod._add_message_to_cache(
            "42",
            "vecchio",
            False,
            "Mario",
            _ORIG_TS,
            protocol=PROTOCOL_TELEGRAM,
            msg_id="99",
        )
        backend = _backend()
        backend._connected = True
        backend._loop = MagicMock()
        backend.contacts = [
            ChatContact(id="42", display_name="Ada", protocol=PROTOCOL_TELEGRAM)
        ]
        backend.cache["42"] = [_cached_message(text="vecchio")]
        backend._seen_msg_ids = {"99"}

        edited_msg = _message(id=99, text="nuovo")
        backend._client = SimpleNamespace(
            get_input_entity=AsyncMock(return_value="entity"),
            get_messages=AsyncMock(return_value=[edited_msg]),
        )
        monkeypatch.setattr(
            "backends.telegram.asyncio.run_coroutine_threadsafe",
            lambda coro, _loop: SimpleNamespace(
                result=lambda timeout: asyncio.run(coro)
            ),
        )

        total = backend.fetch_recent_history()

        assert total == 1
        assert len(backend.cache["42"]) == 1
        assert backend.cache["42"][0]["text"] == "nuovo"
        assert backend.cache["42"][0]["edited"] is True
        assert _db_rows(tmp_db) == [("nuovo", 1)]
