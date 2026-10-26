"""``messages.send`` with ``optimistic``: the core owns the sender's pending row.

Real backend + real SQLite (temp DB); the network send is a stub.  The web
flavour of the operation (no ``optimistic``) is covered by the contract tests.
"""

from __future__ import annotations

import errno
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from client_harness import client_for
from event_harness import legacy_frames

import protocols.db as backend_mod
from models import PROTOCOL_SIGNAL, PROTOCOL_TELEGRAM, ChatContact
from protocols import BackendManager, SignalBackend
from server.api import ApiError
from server.bus import EventBus

CONTACT = "+391234567890"


def _rows(db_file: Path) -> list[dict]:
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM messages ORDER BY id")]
    except sqlite3.OperationalError:  # table not created: nothing stored
        return []
    finally:
        conn.close()


@pytest.fixture
def tmp_db(tmp_path: Path):
    db_file = tmp_path / "messages.db"
    with (
        patch.object(backend_mod, "DB_FILE", db_file),
        patch.object(backend_mod, "CACHE_DIR", tmp_path),
    ):
        yield db_file


class _Env:
    def __init__(self, db_file: Path) -> None:
        self.db_file = db_file
        self.backend = SignalBackend()
        self.contact = ChatContact(
            id=CONTACT, display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        self.backend.register_contact(self.contact)
        self.manager = BackendManager()
        self.manager.register(self.backend)
        self.bus = EventBus()
        self.sub = self.bus.subscribe()
        self.client = client_for(self.manager, self.bus)
        self.rows_at_send: list[list[dict]] = []
        self.send = MagicMock(side_effect=self._send)
        self.backend.send_message_sync = self.send
        self.result: object = "srv-1"

    def _send(self, contact_id, text, **kwargs):
        self.rows_at_send.append(_rows(self.db_file))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    def call(self, ts: int = 1_000_000, text: str = "ciao", **extra):
        data = {
            "protocol": PROTOCOL_SIGNAL,
            "contact_id": CONTACT,
            "text": text,
            "retry": False,
            "optimistic": {"timestamp": ts, **extra.pop("optimistic", {})},
            **extra,
        }
        return self.client.call("messages.send", data)

    def frames(self) -> list[dict]:
        return legacy_frames(self.sub.drain())


@pytest.fixture
def env(tmp_db):
    return _Env(tmp_db)


def test_sends_and_settles_the_pending_row(env):
    out = env.call()

    assert out == {
        "ok": True,
        "status": "sent",
        "status_applied": True,
        "message_id": "srv-1",
    }
    # The row was already persisted (pending) when the network send ran.
    assert [r["status"] for r in env.rows_at_send[0]] == ["pending"]
    (row,) = _rows(env.db_file)
    assert (row["status"], row["msg_id"], row["is_mine"]) == ("sent", "srv-1", 1)
    assert row["timestamp"] == 1_000_000
    (cached,) = env.backend.cache[CONTACT]
    assert (cached["status"], cached["id"]) == ("sent", "srv-1")
    # The backend, not the manager (no sent_mirror), got the text once.
    env.send.assert_called_once()
    assert env.send.call_args.args == (CONTACT, "ciao")


def test_recency_bumped_and_one_message_frame(env):
    env.call(ts=5_000)

    assert env.contact.last_message_ts == 5_000
    assert env.frames() == [
        {
            "type": "message",
            "payload": {
                "id": None,
                "protocol": PROTOCOL_SIGNAL,
                "contact_id": CONTACT,
                "timestamp": 5_000,
                "is_mine": True,
            },
        }
    ]


def test_failure_marks_failed_and_attempts_once(env):
    env.result = RuntimeError("boom")

    with pytest.raises(ApiError) as caught:
        env.call()

    assert (caught.value.code, caught.value.message) == (
        "backend_error",
        "Message send failed",
    )
    assert caught.value.details == {"status_applied": True, "error": "boom"}
    assert env.send.call_count == 1  # retry=false: single attempt, no back-off
    (row,) = _rows(env.db_file)
    assert row["status"] == "failed"
    assert env.backend.cache[CONTACT][0]["status"] == "failed"
    assert [f["type"] for f in env.frames()] == ["message"]  # no send_retry


def test_retry_policy_is_explicit(env):
    env.result = OSError(errno.ECONNREFUSED, "down")  # retryable for Signal

    with (
        patch("server.ops.send.compute_delay", return_value=0.0),
        pytest.raises(ApiError),
    ):
        env.call(retry=True)

    assert env.send.call_count == 3
    assert [f["type"] for f in env.frames()].count("send_retry") == 2


def test_resend_rearms_the_failed_row(env):
    env.result = RuntimeError("boom")
    with pytest.raises(ApiError):
        env.call()
    env.result = "srv-2"

    out = env.call(optimistic={"resend": True})

    assert out["status"] == "sent" and out["message_id"] == "srv-2"
    # Still one row, pending while the retry was on the wire.
    assert [r["status"] for r in env.rows_at_send[-1]] == ["pending"]
    (row,) = _rows(env.db_file)
    assert (row["status"], row["msg_id"]) == ("sent", "srv-2")
    assert len(env.backend.cache[CONTACT]) == 1


def test_resend_of_a_row_that_is_not_failed_conflicts(env):
    env.call()
    sent = env.send.call_count

    with pytest.raises(ApiError) as caught:
        env.call(optimistic={"resend": True})

    assert caught.value.code == "conflict"
    assert env.send.call_count == sent
    assert _rows(env.db_file)[0]["status"] == "sent"


def test_identical_send_inside_dedup_window_adds_and_persists_once(env):
    env.result = None  # no server id yet: the first row is still "optimistic"
    env.call(ts=1_000_000)
    env.call(ts=1_000_002)

    assert len(_rows(env.db_file)) == 1
    assert len(env.backend.cache[CONTACT]) == 1
    assert [f["type"] for f in env.frames()] == ["message"]  # second adds none


def test_row_keeps_quote_and_display_metadata(env):
    env.call(
        text="risposta",
        quote_timestamp=1234,
        quote_author=CONTACT,
        quote_message="",
        quote_content_type="image/png",
        optimistic={
            "quote_text": "🖼️ Immagine",
            "attachment_id": "att-1",
            "content_type": "image/png",
            "quote_attachment_id": "att-1",
            "quote_attachment_path": "/tmp/quote-thumbs/abc.png",
            "quote_content_type": "image/png",
        },
    )

    (row,) = _rows(env.db_file)
    assert row["quote_text"] == "🖼️ Immagine"
    assert row["quote_timestamp"] == 1234
    assert row["quote_author"] == CONTACT
    assert row["attachment_id"] == "att-1"
    assert row["quote_attachment_path"] == "/tmp/quote-thumbs/abc.png"
    kwargs = env.send.call_args.kwargs
    assert kwargs["quote_timestamp"] == 1234
    assert kwargs["quote_attachments"] == ["image/png"]  # no id: type only


def test_telegram_reply_carries_the_original_id():
    backend = MagicMock()
    backend.protocol = PROTOCOL_TELEGRAM
    backend.ingest_message.return_value = False
    backend.send_message_sync.return_value = None
    backend.contacts = []
    manager = MagicMock()
    manager.get.return_value = backend

    client_for(manager).call(
        "messages.send",
        {
            "protocol": PROTOCOL_TELEGRAM,
            "contact_id": "42",
            "text": "re",
            "quote_timestamp": 1000,
            "quote_author": "42",
            "quote_message": "orig",
            "reply_to_message_id": "12",
            "retry": False,
            "optimistic": {"timestamp": 7},
        },
    )

    backend.send_message_sync.assert_called_once_with(
        "42",
        "re",
        quote_timestamp=1000,
        quote_author="42",
        quote_message="orig",
        reply_to_message_id="12",
    )


def test_unreachable_contact_is_rejected_before_anything_is_stored(env):
    with pytest.raises(ApiError) as caught:
        env.client.call(
            "messages.send",
            {
                "protocol": PROTOCOL_SIGNAL,
                "contact_id": "+390000000",
                "text": "x",
                "optimistic": {"timestamp": 1},
            },
        )

    assert caught.value.code == "not_found"
    assert _rows(env.db_file) == []
    assert env.backend.cache == {}


def test_optimistic_is_text_only(env):
    with pytest.raises(ApiError) as caught:
        env.call(attachments=[{"path": "/tmp/x.png", "filename": "x.png"}])

    assert caught.value.code == "invalid_request"
    assert _rows(env.db_file) == []


def test_client_msg_id_makes_a_replayed_call_a_duplicate(env):
    first = env.call(client_msg_id="c-1")
    again = env.call(ts=1_000_001, client_msg_id="c-1")

    assert first["status"] == "sent"
    assert again == {"ok": True, "duplicate": True}
    assert env.send.call_count == 1


def test_empty_quote_body_stays_on_the_wire_for_the_optimistic_caller(env):
    """A media reply without caption quotes with "" (the web's blank -> None
    normalisation would omit the body, which the Signal quote must not)."""
    env.call(quote_timestamp=1234, quote_author=CONTACT, quote_message="")

    assert env.send.call_args.kwargs["quote_message"] == ""


def test_the_web_flavour_still_normalises_a_blank_quote_body(env):
    env.client.call(
        "messages.send",
        {
            "protocol": PROTOCOL_SIGNAL,
            "contact_id": CONTACT,
            "text": "x",
            "quote_timestamp": 1234,
            "quote_author": CONTACT,
            "quote_message": "  ",
        },
    )

    assert env.manager.get(PROTOCOL_SIGNAL) is env.backend
    assert env.send.call_args.kwargs["quote_message"] is None
