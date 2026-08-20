"""
Phase 3 (Signal backend) tests for message editing.

Covers the Signal-side edit surface described in DESIGN_EDIT_MESSAGES.md
§3.1 and the test plan in §7 (``tests/test_edit_signal.py``):

- ``SignalRPCClient.send_message(..., edit_timestamp=...)`` → ``params["editTimestamp"]``;
- ``_send_subprocess(..., edit_timestamp=...)`` → ``--edit-timestamp`` argv flag;
- ``SignalBackend.edit_message_sync`` (daemon RPC / subprocess fallback / non-numeric id);
- ``envelope_to_event`` with ``editMessage`` (top-level incoming + sync sent) and
  the malformed-envelope guards;
- ``SignalBackend.apply_edit`` (cache + SQLite mutation, idempotence, media /
  unknown-timestamp / is_mine-mismatch guards).

The two ``apply_edit`` tests that touch SQLite use an isolated temporary DB
(``backend.DB_FILE`` / ``backend.CACHE_DIR`` are patched), mirroring the
``tests/test_db_edit.py`` pattern; the real DB is never touched.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import backend as backend_mod
from backend import SignalRPCClient, _send_subprocess
from backends import SignalBackend
from models import PROTOCOL_SIGNAL, ChatContact

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


def _db_rows(db_file: Path) -> list[tuple]:
    """Raw ``(text, edited)`` rows, ordered by primary key."""
    conn = sqlite3.connect(db_file)
    rows = conn.execute("SELECT text, edited FROM messages ORDER BY id").fetchall()
    conn.close()
    return rows


# ─── SignalRPCClient.send_message ─────────────────────────────────────────────


class TestRPCSendMessageEdit:
    """🔌 ``send_message`` popola ``editTimestamp`` solo quando richiesto."""

    def test_edit_timestamp_populated(self):
        """``edit_timestamp=123`` → ``params["editTimestamp"] == 123``."""
        client = SignalRPCClient("http://localhost:9999")
        with patch.object(client, "_call") as mock_call:
            mock_call.return_value = {"result": {}}
            client.send_message("nuovo", "+391234567890", edit_timestamp=123)

        method, params = mock_call.call_args[0]
        assert method == "send"
        assert params["editTimestamp"] == 123
        # Other params untouched.
        assert params["message"] == "nuovo"
        assert params["recipient"] == ["+391234567890"]

    def test_edit_timestamp_absent_when_none(self):
        """``edit_timestamp=None`` → chiave assente."""
        client = SignalRPCClient("http://localhost:9999")
        with patch.object(client, "_call") as mock_call:
            mock_call.return_value = {"result": {}}
            client.send_message("nuovo", "+391234567890", edit_timestamp=None)

        params = mock_call.call_args[0][1]
        assert "editTimestamp" not in params

    def test_edit_timestamp_absent_when_not_passed(self):
        """Senza argomento → chiave assente (send normale invariato)."""
        client = SignalRPCClient("http://localhost:9999")
        with patch.object(client, "_call") as mock_call:
            mock_call.return_value = {"result": {}}
            client.send_message("nuovo", "+391234567890")

        params = mock_call.call_args[0][1]
        assert "editTimestamp" not in params


# ─── _send_subprocess ─────────────────────────────────────────────────────────


class TestSendSubprocessEdit:
    """📤 ``_send_subprocess`` aggiunge ``--edit-timestamp`` all'argv."""

    def test_argv_contains_edit_timestamp(self):
        """``edit_timestamp=123`` → ``--edit-timestamp 123``."""
        with patch.object(backend_mod, "_run_subprocess") as mock_run:
            mock_run.return_value = ""
            _send_subprocess("nuovo", "+391234567890", edit_timestamp=123)

        args = mock_run.call_args[0][0]
        assert "--edit-timestamp" in args
        idx = args.index("--edit-timestamp")
        assert args[idx + 1] == "123"
        # Base send args preserved.
        assert args[0:4] == ["send", "-m", "nuovo", "+391234567890"]

    def test_argv_omits_edit_timestamp_when_none(self):
        """``edit_timestamp=None`` → flag assente."""
        with patch.object(backend_mod, "_run_subprocess") as mock_run:
            mock_run.return_value = ""
            _send_subprocess("nuovo", "+391234567890", edit_timestamp=None)

        args = mock_run.call_args[0][0]
        assert "--edit-timestamp" not in args

    def test_argv_omits_edit_timestamp_when_not_passed(self):
        """Senza argomento → flag assente (send normale invariato)."""
        with patch.object(backend_mod, "_run_subprocess") as mock_run:
            mock_run.return_value = ""
            _send_subprocess("nuovo", "+391234567890")

        args = mock_run.call_args[0][0]
        assert "--edit-timestamp" not in args


# ─── SignalBackend.edit_message_sync ──────────────────────────────────────────


class TestEditMessageSync:
    """✏️ ``edit_message_sync``: daemon RPC, subprocess fallback, id non numerico."""

    def test_daemon_rpc_success_returns_true(self):
        """Daemon: RPC senza errore → True, con ``edit_timestamp`` convertito."""
        backend = SignalBackend()
        backend._use_daemon = True
        with patch.object(backend._rpc, "send_message") as mock_send:
            mock_send.return_value = {"result": {}}
            result = backend.edit_message_sync("+391234567890", "1000", "nuovo")

        assert result is True
        mock_send.assert_called_once_with("nuovo", "+391234567890", edit_timestamp=1000)

    def test_daemon_rpc_error_raises_runtime_error(self):
        """Daemon: ``{"error": ...}`` → RuntimeError propagato al worker."""
        backend = SignalBackend()
        backend._use_daemon = True
        with patch.object(backend._rpc, "send_message") as mock_send:
            mock_send.return_value = {"error": "boom"}
            with pytest.raises(RuntimeError):
                backend.edit_message_sync("+391234567890", "1000", "nuovo")

    def test_subprocess_fallback(self):
        """Senza daemon → invia via ``_send_subprocess``."""
        backend = SignalBackend()
        backend._use_daemon = False
        with patch("backends.signal._send_subprocess") as mock_sub:
            result = backend.edit_message_sync("+391234567890", "1000", "nuovo")

        assert result is True
        mock_sub.assert_called_once_with("nuovo", "+391234567890", edit_timestamp=1000)

    def test_non_numeric_message_id_returns_false(self):
        """``message_id`` non numerico → False (nessun invio)."""
        backend = SignalBackend()
        assert backend.edit_message_sync("+391234567890", "abc", "nuovo") is False


# ─── envelope_to_event (editMessage) ──────────────────────────────────────────


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


class TestEditEnvelopeToEvent:
    """📥 ``editMessage`` → un singolo ``ChatEvent("message_edit")``, mai ``message``."""

    def test_incoming_edit_produces_single_message_edit_event(self):
        """Forma top-level: esattamente 1 evento, payload normalizzato."""
        backend = _backend_with_contact()
        orig_ts = 1755001000
        edit_ts = 1755002000
        envelope = {
            "source": "+391234567890",
            "sourceNumber": "+391234567890",
            "sourceName": "Mario",
            "timestamp": edit_ts,
            "editMessage": {
                "targetSentTimestamp": orig_ts,
                "dataMessage": {"timestamp": edit_ts, "message": "nuovo"},
            },
        }

        events = backend.envelope_to_event(envelope)

        assert len(events) == 1
        ev = events[0]
        assert ev.type == "message_edit"
        assert ev.protocol == PROTOCOL_SIGNAL
        assert ev.contact_id == "+391234567890"
        assert ev.payload["edit_message_id"] == str(orig_ts)
        assert ev.payload["timestamp"] == orig_ts
        assert ev.payload["text"] == "nuovo"
        assert ev.payload["is_mine"] is False
        assert ev.payload["edit_timestamp"] == edit_ts
        assert ev.payload["msg_type"] == "text"
        assert ev.payload["sender"] == "Mario"
        assert ev.payload["contact"].id == "+391234567890"

    def test_incoming_edit_no_duplicate_message_event(self):
        """Nessun evento ``message`` accanto al ``message_edit`` (no duplicati)."""
        backend = _backend_with_contact()
        envelope = {
            "source": "+391234567890",
            "sourceNumber": "+391234567890",
            "timestamp": 1755002000,
            "editMessage": {
                "targetSentTimestamp": 1755001000,
                "dataMessage": {"timestamp": 1755002000, "message": "nuovo"},
            },
        }

        events = backend.envelope_to_event(envelope)

        assert [e.type for e in events] == ["message_edit"]

    @pytest.mark.parametrize("dest_field", ["destination", "destinationNumber"])
    def test_sync_sent_edit_is_mine_true(self, dest_field):
        """Edit da altro device linked → ``is_mine=True``, contatto via destination."""
        backend = _backend_with_contact()
        orig_ts = 1755001000
        edit_ts = 1755002000
        envelope = {
            "source": "+391234567890",  # local user
            "timestamp": edit_ts,
            "syncMessage": {
                "sentMessage": {
                    dest_field: "+391234567890",
                    "editMessage": {
                        "targetSentTimestamp": orig_ts,
                        "dataMessage": {"timestamp": edit_ts, "message": "nuovo"},
                    },
                },
            },
        }

        events = backend.envelope_to_event(envelope)

        assert len(events) == 1
        ev = events[0]
        assert ev.type == "message_edit"
        assert ev.contact_id == "+391234567890"
        assert ev.payload["is_mine"] is True
        assert ev.payload["sender"] == "You"
        assert ev.payload["edit_message_id"] == str(orig_ts)
        assert ev.payload["timestamp"] == orig_ts
        assert ev.payload["text"] == "nuovo"

    def test_edit_hook_does_not_break_normal_message(self):
        """Regressione: un envelope normale resta un evento ``message``."""
        backend = _backend_with_contact()
        envelope = {
            "source": "+391234567890",
            "sourceNumber": "+391234567890",
            "sourceName": "Mario",
            "timestamp": 2000,
            "dataMessage": {"message": "Ciao!", "timestamp": 2000},
        }

        events = backend.envelope_to_event(envelope)

        assert len(events) == 1
        assert events[0].type == "message"

    def test_unknown_contact_edit_returns_empty(self):
        """Edit da contatto non noto → ``[]`` (nessun evento)."""
        backend = SignalBackend()  # no contacts
        envelope = {
            "source": "+391234567890",
            "sourceNumber": "+391234567890",
            "timestamp": 1755002000,
            "editMessage": {
                "targetSentTimestamp": 1755001000,
                "dataMessage": {"timestamp": 1755002000, "message": "nuovo"},
            },
        }

        assert backend._edit_envelope_to_event(envelope) is None
        assert backend.envelope_to_event(envelope) == []


class TestEditEnvelopeMalformed:
    """🛑 Envelope edit malformati → ``None`` (e ``envelope_to_event`` → ``[]``)."""

    @staticmethod
    def _backend() -> SignalBackend:
        return _backend_with_contact()

    def test_no_target_sent_timestamp(self):
        backend = self._backend()
        envelope = {
            "source": "+391234567890",
            "sourceNumber": "+391234567890",
            "timestamp": 1755002000,
            "editMessage": {
                "dataMessage": {"timestamp": 1755002000, "message": "nuovo"},
            },
        }

        assert backend._edit_envelope_to_event(envelope) is None
        assert backend.envelope_to_event(envelope) == []

    def test_empty_text(self):
        backend = self._backend()
        envelope = {
            "source": "+391234567890",
            "sourceNumber": "+391234567890",
            "timestamp": 1755002000,
            "editMessage": {
                "targetSentTimestamp": 1755001000,
                "dataMessage": {"timestamp": 1755002000, "message": ""},
            },
        }

        assert backend._edit_envelope_to_event(envelope) is None
        assert backend.envelope_to_event(envelope) == []

    def test_attachments_present(self):
        backend = self._backend()
        envelope = {
            "source": "+391234567890",
            "sourceNumber": "+391234567890",
            "timestamp": 1755002000,
            "editMessage": {
                "targetSentTimestamp": 1755001000,
                "dataMessage": {
                    "timestamp": 1755002000,
                    "message": "nuovo",
                    "attachments": [{"contentType": "image/jpeg", "id": "att-1"}],
                },
            },
        }

        assert backend._edit_envelope_to_event(envelope) is None
        assert backend.envelope_to_event(envelope) == []

    def test_no_edit_message(self):
        """Envelope senza ``editMessage`` né sync edit → ``None``."""
        backend = self._backend()
        envelope = {
            "source": "+391234567890",
            "sourceNumber": "+391234567890",
            "timestamp": 1755002000,
            "dataMessage": {"timestamp": 1755002000, "message": "ciao"},
        }

        assert backend._edit_envelope_to_event(envelope) is None


# ─── apply_edit ───────────────────────────────────────────────────────────────


def _cached_message(**overrides: object) -> dict:
    """A cached Signal message dict (incoming, text) with sensible defaults."""
    msg = {
        "text": "vecchio",
        "is_mine": False,
        "sender": "Mario",
        "timestamp": 1755001000,
        "quote_text": None,
        "msg_type": "text",
        "attachment_info": None,
        "attachment_id": None,
        "read": False,
        "status": "read",
    }
    msg.update(overrides)
    return msg


class TestApplyEdit:
    """✏️ ``apply_edit``: mutazione cache+DB e guardie di sicurezza."""

    def test_hit_updates_cache_and_db(self, tmp_db):
        """Hit per ts → cache (text+edited) e riga SQLite (text + edited=1)."""
        backend_mod._add_message_to_cache(
            "+391234567890",
            "vecchio",
            False,
            "Mario",
            1755001000,
            protocol="signal",
        )
        backend = SignalBackend()
        backend.cache["+391234567890"] = [_cached_message()]

        result = backend.apply_edit("+391234567890", "1755001000", "nuovo")

        assert result == {
            "message_id": "1755001000",
            "timestamp": 1755001000,
            "old_text": "vecchio",
            "text": "nuovo",
            "is_mine": False,
        }
        # In-memory cache mutated.
        cached = backend.cache["+391234567890"][0]
        assert cached["text"] == "nuovo"
        assert cached["edited"] is True
        # SQLite row mutated (real temporary DB).
        loaded = backend_mod._load_cache(protocol="signal")["+391234567890"][0]
        assert loaded["text"] == "nuovo"
        assert loaded["edited"] is True
        assert _db_rows(tmp_db) == [("nuovo", 1)]

    def test_calls_update_message_text_with_correct_params(self):
        """``_update_message_text`` invocato con i parametri giusti."""
        backend = SignalBackend()
        backend.cache["+391234567890"] = [_cached_message()]

        with patch.object(backend_mod, "_update_message_text") as mock_update:
            mock_update.return_value = True
            backend.apply_edit("+391234567890", "1755001000", "nuovo")

        mock_update.assert_called_once_with(
            "+391234567890",
            "nuovo",
            protocol="signal",
            timestamp=1755001000,
            old_text="vecchio",
            is_mine=False,
        )

    def test_identical_text_returns_none(self):
        """Testo già identico → None (idempotente, niente ``edited``)."""
        backend = SignalBackend()
        backend.cache["+391234567890"] = [_cached_message(text="stesso")]

        result = backend.apply_edit("+391234567890", "1755001000", "stesso")

        assert result is None
        msg = backend.cache["+391234567890"][0]
        assert msg["text"] == "stesso"
        assert "edited" not in msg

    def test_media_message_returns_none(self):
        """``msg_type != "text"`` → None (mai riscrivere label media)."""
        backend = SignalBackend()
        backend.cache["+391234567890"] = [_cached_message(msg_type="image")]

        result = backend.apply_edit("+391234567890", "1755001000", "nuovo")

        assert result is None
        msg = backend.cache["+391234567890"][0]
        assert msg["text"] == "vecchio"
        assert "edited" not in msg

    def test_unknown_timestamp_returns_none(self):
        """Timestamp ignoto → None, cache intatta."""
        backend = SignalBackend()
        backend.cache["+391234567890"] = [_cached_message()]

        result = backend.apply_edit("+391234567890", "999999", "nuovo")

        assert result is None
        assert backend.cache["+391234567890"][0]["text"] == "vecchio"

    def test_is_mine_mismatch_returns_none(self):
        """``is_mine`` esplicito non combaciante → None."""
        backend = SignalBackend()
        backend.cache["+391234567890"] = [_cached_message(is_mine=False)]

        result = backend.apply_edit(
            "+391234567890", "1755001000", "nuovo", is_mine=True
        )

        assert result is None
        assert backend.cache["+391234567890"][0]["text"] == "vecchio"

    def test_is_mine_matching_applies(self):
        """``is_mine`` combaciante → edit applicato."""
        backend = SignalBackend()
        backend.cache["+391234567890"] = [_cached_message(is_mine=True, sender="You")]

        with patch.object(backend_mod, "_update_message_text") as mock_update:
            mock_update.return_value = True
            result = backend.apply_edit(
                "+391234567890", "1755001000", "nuovo", is_mine=True
            )

        assert result is not None
        assert result["is_mine"] is True
        mock_update.assert_called_once()

    def test_non_numeric_message_id_returns_none(self):
        """``message_id`` non numerico → None."""
        backend = SignalBackend()
        backend.cache["+391234567890"] = [_cached_message()]

        result = backend.apply_edit("+391234567890", "abc", "nuovo")

        assert result is None
