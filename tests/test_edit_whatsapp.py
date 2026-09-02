"""
Phase 5 (WhatsApp backend) tests for message editing.

Covers the WhatsApp-side edit surface described in DESIGN_EDIT_MESSAGES.md
§3.3 and the test plan in §7 (``tests/test_edit_whatsapp.py``):

- ``WhatsAppRESTClient.edit_message`` → PUT with percent-encoded path segments
  and ``{"text": ..., "linkPreview": True}`` body, ``None`` passthrough on error;
- ``handle_webhook`` → ``_detect_edit`` rewrite (``message`` → ``message_edit``)
  with ``_seen_message_keys`` dedup on retries; our-edit echo absorbed by the
  existing id-based dedup of ``ingest_message``;
- ``_detect_edit`` → id match + ts±2s single-candidate fallback + ambiguity;
- the synthetic ``message.ack`` path (edit → ``message_edit``, no synthetic
  ``message`` event);
- ``fetch_history`` → ``apply_edit`` reconciliation (row/cache count unchanged);
- ``edit_message_sync`` and ``apply_edit`` (cache + SQLite mutation and guards).

Tests that touch SQLite use an isolated temporary DB (``protocols.db.DB_FILE`` /
``protocols.db.CACHE_DIR`` patched), mirroring ``tests/test_db_edit.py``; the real
DB in ``~/.local/share/signal-tui-client`` is never touched.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import protocols.db as backend_mod
from models import PROTOCOL_WHATSAPP
from protocols.whatsapp import WhatsAppBackend, WhatsAppRESTClient

# ─── Constants / helpers ───────────────────────────────────────────────────────

_TS_SEC = 1700000000
_TS_MS = 1700000000000
_CID = "391234567890@c.us"


def _make_backend(
    api_url: str = "http://api.test", media_dir: str = ""
) -> WhatsAppBackend:
    return WhatsAppBackend(api_url=api_url, media_dir=media_dir)


def _cached_message(**overrides: object) -> dict:
    """A cached WhatsApp message dict (incoming, text) with sensible defaults."""
    msg = {
        "id": "m1",
        "text": "vecchio",
        "is_mine": False,
        "sender": "Mario",
        "timestamp": _TS_MS,
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


def _webhook_backend() -> WhatsAppBackend:
    """A WhatsAppBackend with the REST client stubbed (no network)."""
    backend = _make_backend("http://api.test")
    backend._rest = MagicMock()
    return backend


# ─── WhatsAppRESTClient.edit_message ───────────────────────────────────────────


class TestWhatsAppRESTClientEdit:
    """🔌 ``edit_message`` → PUT con path percent-encoded e body corretto."""

    def test_edit_message_sends_put_with_encoded_path_and_body(self):
        client = WhatsAppRESTClient("http://api.test")
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = {"id": "m1", "text": "nuovo"}
            result = client.edit_message(_CID, "true_1@c.us_ABC", "nuovo")

        mock_req.assert_called_once_with(
            "PUT",
            "/api/default/chats/391234567890%40c.us/messages/true_1%40c.us_ABC",
            {"text": "nuovo", "linkPreview": True},
        )
        assert result == {"id": "m1", "text": "nuovo"}

    def test_edit_message_returns_none_on_error(self):
        """``_request`` che ritorna ``None`` → ``None`` (contratto pass-through)."""
        client = WhatsAppRESTClient("http://api.test")
        with patch.object(client, "_request", return_value=None):
            assert client.edit_message(_CID, "m1", "nuovo") is None


# ─── _extract_message_id ───────────────────────────────────────────────────────


class TestExtractMessageId:
    """🆔 ``_extract_message_id`` gestisce id stringa e id dict (WAHA recente)."""

    def test_id_dict_prefers_serialized(self):
        result = {
            "id": {
                "fromMe": True,
                "remote": "189025889575055@lid",
                "id": "3EB0CFB50E158CFB92131E",
                "$1": "true_189025889575055@lid_3EB0CFB50E158CFB92131E",
                "_serialized": "true_189025889575055@lid_3EB0CFB50E158CFB92131E",
            },
            "key": None,
        }
        assert (
            WhatsAppBackend._extract_message_id(result)
            == "true_189025889575055@lid_3EB0CFB50E158CFB92131E"
        )

    def test_id_dict_falls_back_to_dollar1(self):
        result = {
            "id": {
                "id": "3EB0CFB50E158CFB92131E",
                "$1": "true_189025889575055@lid_3EB0CFB50E158CFB92131E",
            }
        }
        assert (
            WhatsAppBackend._extract_message_id(result)
            == "true_189025889575055@lid_3EB0CFB50E158CFB92131E"
        )

    def test_id_dict_hex_only_uses_id(self):
        result = {"id": {"id": "3EB0CFB50E158CFB92131E"}}
        assert WhatsAppBackend._extract_message_id(result) == "3EB0CFB50E158CFB92131E"

    def test_flat_string_id_regression(self):
        assert WhatsAppBackend._extract_message_id({"id": "BAYES-123"}) == "BAYES-123"

    def test_key_id(self):
        assert (
            WhatsAppBackend._extract_message_id({"key": {"id": "NESTED-9"}})
            == "NESTED-9"
        )

    def test_no_id_returns_none(self):
        assert WhatsAppBackend._extract_message_id({"status": "ok"}) is None


# ─── _detect_edit ──────────────────────────────────────────────────────────────


class TestDetectEdit:
    """🔍 ``_detect_edit``: match per id + fallback ts (±2s, candidato unico)."""

    def _backend(self, entries: list[dict]) -> WhatsAppBackend:
        backend = _make_backend()
        backend.cache[_CID] = entries
        return backend

    def test_match_by_id_returns_entry(self):
        backend = self._backend([_cached_message()])
        hit = backend._detect_edit(_CID, "m1", "nuovo", False, _TS_MS)
        assert hit is not None
        assert hit["text"] == "vecchio"

    def test_match_by_id_identical_text_returns_none(self):
        backend = self._backend([_cached_message(text="nuovo")])
        assert backend._detect_edit(_CID, "m1", "nuovo", False, _TS_MS) is None

    def test_empty_text_returns_none(self):
        backend = self._backend([_cached_message()])
        assert backend._detect_edit(_CID, "m1", "", False, _TS_MS) is None

    def test_match_by_id_skips_media(self):
        backend = self._backend([_cached_message(msg_type="image")])
        assert backend._detect_edit(_CID, "m1", "nuovo", False, _TS_MS) is None

    def test_fallback_single_candidate_returns_hit(self):
        """Id diverso → fallback ts; candidato unico entro ±2s → hit."""
        backend = self._backend([_cached_message(id="other-id")])
        hit = backend._detect_edit(_CID, "unknown-id", "nuovo", False, _TS_MS + 1000)
        assert hit is not None
        assert hit["text"] == "vecchio"

    def test_fallback_two_candidates_in_same_second_returns_none(self):
        """Due candidati entro la finestra → ambiguità → None (skip)."""
        a = _cached_message(id="a", timestamp=_TS_MS)
        b = _cached_message(id="b", timestamp=_TS_MS + 1000)
        backend = self._backend([a, b])
        assert (
            backend._detect_edit(_CID, "unknown", "nuovo", False, _TS_MS + 500) is None
        )

    def test_fallback_identical_text_returns_none(self):
        """Fallback: testo già identico in cache → candidato escluso → None."""
        backend = self._backend([_cached_message(id="other-id", text="nuovo")])
        assert (
            backend._detect_edit(_CID, "unknown", "nuovo", False, _TS_MS + 500) is None
        )

    def test_fallback_not_applied_for_outgoing(self):
        """Fallback ts si applica SOLO agli incoming (``is_mine=False``)."""
        backend = self._backend([_cached_message(id="other-id", is_mine=True)])
        assert (
            backend._detect_edit(_CID, "unknown", "nuovo", True, _TS_MS + 500) is None
        )


# ─── handle_webhook ───────────────────────────────────────────────────────────


class TestHandleWebhookEdit:
    """📥 Webhook: edit → ``message_edit`` (mai ``message``); echo assorbito."""

    def test_incoming_edit_enqueues_message_edit_not_message(self):
        backend = _webhook_backend()
        backend.cache[_CID] = [_cached_message()]  # id "m1", text "vecchio"
        envelope = {
            "event": "message",
            "payload": {
                "id": "m1",
                "from": _CID,
                "fromMe": False,
                "body": "nuovo",
                "timestamp": _TS_SEC,
            },
        }

        assert backend.handle_webhook(envelope) is True
        events = backend.poll_once()

        assert [e.type for e in events] == ["message_edit"]
        ev = events[0]
        assert ev.protocol == PROTOCOL_WHATSAPP
        assert ev.contact_id == _CID
        assert ev.payload["edit_message_id"] == "m1"
        assert ev.payload["text"] == "nuovo"
        assert ev.payload["is_mine"] is False
        assert ev.payload["msg_type"] == "text"

    def test_retry_is_deduplicated(self):
        backend = _webhook_backend()
        backend.cache[_CID] = [_cached_message()]
        envelope = {
            "event": "message",
            "payload": {
                "id": "m1",
                "from": _CID,
                "fromMe": False,
                "body": "nuovo",
                "timestamp": _TS_SEC,
            },
        }

        assert backend.handle_webhook(envelope) is True
        assert backend.handle_webhook(envelope) is True  # retry

        events = backend.poll_once()
        assert len(events) == 1
        assert events[0].type == "message_edit"
        assert (_CID, "m1", "nuovo", "") in backend._seen_message_keys

    def test_own_edit_echo_is_absorbed_by_dedup(self):
        """fromMe con testo già nuovo in cache → ``message`` normale, nessuna
        bolla nuova: ``ingest_message`` dedup per id → False."""
        backend = _webhook_backend()
        backend.cache[_CID] = [
            _cached_message(is_mine=True, text="nuovo", sender="You")
        ]
        envelope = {
            "event": "message",
            "payload": {
                "id": "m1",
                "to": _CID,
                "fromMe": True,
                "body": "nuovo",
                "timestamp": _TS_SEC,
            },
        }

        assert backend.handle_webhook(envelope) is True
        events = backend.poll_once()

        assert [e.type for e in events] == ["message"]  # NOT message_edit
        added = backend.ingest_message(
            _CID, events[0].payload, events[0].payload["timestamp"]
        )
        assert added is False
        assert len(backend.cache[_CID]) == 1

    def test_synthetic_ack_edit_enqueues_message_edit_not_message(self):
        """``message.ack`` con body nuovo (cache vecchio) → ``message_edit``,
        nessun evento sintetico ``message``."""
        backend = _webhook_backend()
        backend.cache[_CID] = [_cached_message(is_mine=True)]  # text "vecchio"
        envelope = {
            "event": "message.ack",
            "payload": {
                "id": "m1",
                "to": _CID,
                "fromMe": True,
                "timestamp": _TS_SEC,
                "status": 2,  # DEVICE (2 → delivered receipt)
                "body": "nuovo",
            },
        }

        backend.handle_webhook(envelope)
        events = backend.poll_once()

        assert [e.type for e in events] == ["message_edit", "receipt"]
        ev = events[0]
        assert ev.payload["edit_message_id"] == "m1"
        assert ev.payload["text"] == "nuovo"
        assert ev.payload["is_mine"] is True
        assert (_CID, "m1", "nuovo", "") in backend._seen_message_keys


# ─── fetch_history ────────────────────────────────────────────────────────────


class TestFetchHistoryEdit:
    """🕰️ Storico già editato → ``apply_edit``, count righe invariato."""

    def test_fetch_history_applies_edit_and_keeps_row_count(self, tmp_db):
        backend_mod._add_message_to_cache(
            _CID,
            "vecchio",
            False,
            "Mario",
            _TS_MS,
            protocol=PROTOCOL_WHATSAPP,
            msg_id="m1",
        )
        backend = _webhook_backend()
        backend._rest.list_messages.return_value = [
            {
                "id": "m1",
                "from": _CID,
                "fromMe": False,
                "body": "nuovo",
                "timestamp": _TS_SEC,
            }
        ]
        backend.cache[_CID] = [_cached_message()]  # id "m1", text "vecchio"

        result = backend.fetch_history(_CID, limit=20)

        assert len(result) == 1
        # Count invariato: cache e DB restano con UNA sola riga.
        assert len(backend.cache[_CID]) == 1
        assert backend.cache[_CID][0]["text"] == "nuovo"
        assert backend.cache[_CID][0]["edited"] is True
        loaded = backend_mod._load_cache(protocol=PROTOCOL_WHATSAPP)[_CID][0]
        assert loaded["text"] == "nuovo"
        assert loaded["edited"] is True
        assert _db_rows(tmp_db) == [("nuovo", 1)]

    def test_fetch_history_calls_apply_edit_with_correct_args(self):
        backend = _webhook_backend()
        backend._rest.list_messages.return_value = [
            {
                "id": "m1",
                "from": _CID,
                "fromMe": False,
                "body": "nuovo",
                "timestamp": _TS_SEC,
            }
        ]
        backend.cache[_CID] = [_cached_message()]
        apply_edit = MagicMock()
        backend.apply_edit = apply_edit

        backend.fetch_history(_CID, limit=20)

        apply_edit.assert_called_once_with(_CID, "m1", "nuovo", is_mine=False)


# ─── edit_message_sync ────────────────────────────────────────────────────────


class TestEditMessageSync:
    """✏️ ``edit_message_sync``: delega a ``_rest.edit_message``."""

    def test_no_rest_returns_false(self):
        backend = _make_backend("http://api.test")
        backend._rest = None
        assert backend.edit_message_sync(_CID, "m1", "nuovo") is False

    def test_rest_dict_returns_true(self):
        backend = _make_backend("http://api.test")
        backend._rest = MagicMock()
        backend._rest.edit_message.return_value = {"id": "m1", "text": "nuovo"}

        assert backend.edit_message_sync(_CID, "m1", "nuovo") is True
        backend._rest.edit_message.assert_called_once_with(_CID, "m1", "nuovo")

    def test_rest_none_returns_false(self):
        backend = _make_backend("http://api.test")
        backend._rest = MagicMock()
        backend._rest.edit_message.return_value = None

        assert backend.edit_message_sync(_CID, "m1", "nuovo") is False


# ─── apply_edit ───────────────────────────────────────────────────────────────


class TestApplyEdit:
    """✏️ ``apply_edit``: match per id, mutazione cache+DB, guardie."""

    def test_hit_updates_cache_and_db(self, tmp_db):
        """Hit per id → cache (text+edited) e riga SQLite (text + edited=1)."""
        backend_mod._add_message_to_cache(
            _CID,
            "vecchio",
            False,
            "Mario",
            _TS_MS,
            protocol=PROTOCOL_WHATSAPP,
            msg_id="m1",
        )
        backend = _make_backend("http://api.test")
        backend.cache[_CID] = [_cached_message()]

        result = backend.apply_edit(_CID, "m1", "nuovo")

        assert result == {
            "message_id": "m1",
            "timestamp": _TS_MS,
            "old_text": "vecchio",
            "text": "nuovo",
            "is_mine": False,
        }
        cached = backend.cache[_CID][0]
        assert cached["text"] == "nuovo"
        assert cached["edited"] is True
        # id / timestamp identity untouched.
        assert cached["id"] == "m1"
        assert cached["timestamp"] == _TS_MS
        loaded = backend_mod._load_cache(protocol=PROTOCOL_WHATSAPP)[_CID][0]
        assert loaded["text"] == "nuovo"
        assert loaded["edited"] is True
        assert _db_rows(tmp_db) == [("nuovo", 1)]

    def test_calls_update_message_text_with_msg_id(self):
        """``_update_message_text`` invocato con ``msg_id`` giusto."""
        backend = _make_backend("http://api.test")
        backend.cache[_CID] = [_cached_message()]

        with patch.object(backend_mod, "_update_message_text") as mock_update:
            mock_update.return_value = True
            backend.apply_edit(_CID, "m1", "nuovo")

        mock_update.assert_called_once_with(
            _CID,
            "nuovo",
            protocol=PROTOCOL_WHATSAPP,
            msg_id="m1",
            mark_edited=True,
        )

    def test_no_id_falls_back_to_timestamp_and_old_text(self):
        """Entry senza ``id`` → DB aggiornato via ``timestamp`` + ``old_text``."""
        backend = _make_backend("http://api.test")
        backend.cache[_CID] = [_cached_message(id=None)]

        with patch.object(backend_mod, "_update_message_text") as mock_update:
            mock_update.return_value = True
            result = backend.apply_edit(_CID, "", "nuovo")

        assert result == {
            "message_id": "",
            "timestamp": _TS_MS,
            "old_text": "vecchio",
            "text": "nuovo",
            "is_mine": False,
        }
        mock_update.assert_called_once_with(
            _CID,
            "nuovo",
            protocol=PROTOCOL_WHATSAPP,
            timestamp=_TS_MS,
            old_text="vecchio",
            mark_edited=True,
        )

    def test_identical_text_returns_none(self):
        """Testo già identico → None (idempotente, niente ``edited``)."""
        backend = _make_backend("http://api.test")
        backend.cache[_CID] = [_cached_message(text="stesso")]

        result = backend.apply_edit(_CID, "m1", "stesso")

        assert result is None
        assert "edited" not in backend.cache[_CID][0]

    def test_media_returns_none(self):
        """``msg_type != "text"`` → None (mai riscrivere label media)."""
        backend = _make_backend("http://api.test")
        backend.cache[_CID] = [_cached_message(msg_type="image")]

        result = backend.apply_edit(_CID, "m1", "nuovo")

        assert result is None
        assert backend.cache[_CID][0]["text"] == "vecchio"

    def test_unknown_id_returns_none(self):
        """Id ignoto → None, cache intatta."""
        backend = _make_backend("http://api.test")
        backend.cache[_CID] = [_cached_message()]

        result = backend.apply_edit(_CID, "m999", "nuovo")

        assert result is None
        assert backend.cache[_CID][0]["text"] == "vecchio"

    def test_is_mine_mismatch_returns_none(self):
        """``is_mine`` esplicito non combaciante → None."""
        backend = _make_backend("http://api.test")
        backend.cache[_CID] = [_cached_message(is_mine=False)]

        result = backend.apply_edit(_CID, "m1", "nuovo", is_mine=True)

        assert result is None
        assert backend.cache[_CID][0]["text"] == "vecchio"

    def test_is_mine_matching_applies(self):
        """``is_mine`` combaciante → edit applicato."""
        backend = _make_backend("http://api.test")
        backend.cache[_CID] = [_cached_message(is_mine=True, sender="You")]

        with patch.object(backend_mod, "_update_message_text") as mock_update:
            mock_update.return_value = True
            result = backend.apply_edit(_CID, "m1", "nuovo", is_mine=True)

        assert result is not None
        assert result["is_mine"] is True
        mock_update.assert_called_once()
