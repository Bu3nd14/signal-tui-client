"""The TUI sends through ``messages.send`` only.

``tui/send.py`` draws the view's own copy of a message and talks to the server
through ``self.client``; the core owns the backend, SQLite and chat cache work
(see ``test_send_optimistic_op.py`` for that side).  Here the client is a
recording stub: what matters is what the TUI asks for and how the view
reconciles with the answer.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from client_harness import StubClient, run_workers, view_app

from models import PROTOCOL_SIGNAL, PROTOCOL_TELEGRAM
from server.api import ApiError
from ui_components import MessageWidget

ROOT = Path(__file__).resolve().parent.parent


SENT = {"ok": True, "status": "sent", "status_applied": True, "message_id": "srv-1"}


def _submit(app, text: str) -> None:
    import signal_tui as stui

    event = MagicMock()
    event.value = text
    with patch.object(stui, "replace_emoji_aliases", side_effect=lambda x: x):
        app.on_input_submitted(event)


# -- send ------------------------------------------------------------------------


def test_submit_shows_the_bubble_at_once_and_asks_for_nothing():
    client = StubClient()
    app, contact = view_app(client)

    _submit(app, "ciao")

    row = app._cache[contact.cache_key][0]
    assert (row["text"], row["status"], row["is_mine"]) == ("ciao", "pending", True)
    assert app._add_message.call_args.kwargs["status"] == "pending"
    assert contact.last_message_ts == row["timestamp"]  # list order, at once
    assert client.calls == []  # the server is only asked from the worker
    assert app.run_worker.call_args.kwargs["thread"] is True


def test_worker_sends_one_optimistic_request_without_retry():
    client = StubClient(SENT)
    app, contact = view_app(client)
    _submit(app, "ciao")
    ts = app._cache[contact.cache_key][0]["timestamp"]

    run_workers(app)

    ((name, data),) = client.calls
    assert name == "messages.send"
    assert data["protocol"] == PROTOCOL_SIGNAL and data["contact_id"] == contact.id
    assert data["text"] == "ciao"
    assert data["retry"] is False  # the TUI's policy: the user clicks to retry
    assert data["optimistic"]["timestamp"] == ts
    assert data["optimistic"]["resend"] is False
    assert data["client_msg_id"]
    assert data["quote_timestamp"] is None and data["quote_author"] is None


def test_success_marks_the_bubble_sent_and_learns_the_server_id():
    client = StubClient(SENT)
    app, contact = view_app(client)
    bubble = MessageWidget("ciao", timestamp=1, is_mine=True, status="pending")
    app._chat_log = SimpleNamespace(children=[bubble])
    _submit(app, "ciao")
    row = app._cache[contact.cache_key][0]
    bubble._msg_timestamp = row["timestamp"]

    run_workers(app)

    assert row["status"] == "sent" and row["id"] == "srv-1"
    assert bubble._message_id == "srv-1"
    app._update_message_widgets_status.assert_called_once_with(
        [{"timestamp": row["timestamp"], "text": "ciao", "status": "sent"}]
    )


def test_a_receipt_that_arrived_first_is_not_overwritten_by_sent():
    client = StubClient({**SENT, "status_applied": False})
    app, contact = view_app(client)
    _submit(app, "ciao")
    row = app._cache[contact.cache_key][0]
    row["status"] = "delivered"  # the receipt won the race

    run_workers(app)

    assert row["status"] == "delivered"
    app._update_message_widgets_status.assert_not_called()


def test_failed_send_fails_the_bubble_and_reports_the_cause():
    error = ApiError(
        "backend_error",
        "Message send failed",
        details={"status_applied": True, "error": "signal-cli offline"},
    )
    client = StubClient(error)
    app, contact = view_app(client)
    _submit(app, "ciao")

    run_workers(app)

    assert app._cache[contact.cache_key][0]["status"] == "failed"
    app._status.assert_called_with("❌ Send error: signal-cli offline", 0)


def test_rejected_before_storage_still_fails_the_bubble():
    client = StubClient(ApiError("not_found", "Not Found"))
    app, contact = view_app(client)
    _submit(app, "ciao")

    run_workers(app)

    assert app._cache[contact.cache_key][0]["status"] == "failed"


def test_reply_fields_travel_in_the_request():
    client = StubClient(SENT)
    app, _ = view_app(client, PROTOCOL_TELEGRAM, "42")
    app._reply_to = {"text": "domanda", "timestamp": 1234, "message_id": 12}

    _submit(app, "risposta")
    run_workers(app)

    (_, data) = client.calls[0]
    assert data["quote_timestamp"] == 1234
    assert data["quote_author"] == "42"
    assert data["quote_message"] == "domanda"
    assert data["reply_to_message_id"] == "12"  # JSON-safe
    assert data["optimistic"]["quote_text"] == "domanda"


def test_signal_media_reply_carries_wire_body_and_attachment():
    client = StubClient(SENT)
    app, _ = view_app(client)
    app._reply_to = {
        "text": "🖼️ Immagine",
        "timestamp": 1234,
        "message_id": "sig-1234",
        "quote_wire_body": None,
        "attachment_id": "att-1",
        "content_type": "image/png",
        "quote_attachment_id": "att-1",
        "quote_attachment_path": Path("/tmp/quote-thumbs/a.png"),
        "quote_content_type": "image/png",
    }

    _submit(app, "risposta")
    run_workers(app)

    (_, data) = client.calls[0]
    assert data["quote_message"] == ""  # the wire body, never the placeholder
    assert data["quote_content_type"] == "image/png"
    assert data["quote_attachment_id"] == "att-1"
    assert data["optimistic"]["quote_text"] == "🖼️ Immagine"
    assert data["optimistic"]["quote_attachment_path"] == "/tmp/quote-thumbs/a.png"


def test_retry_rearms_the_bubble_and_resends_the_same_row():
    client = StubClient(SENT)
    app, contact = view_app(client)
    row = {
        "is_mine": True,
        "timestamp": 1234,
        "text": "ciao",
        "status": "failed",
        "id": None,
    }
    app._cache[contact.cache_key] = [row]

    app._retry_failed_message(1234, "ciao")

    assert row["status"] == "pending"
    run_workers(app)
    (_, data) = client.calls[0]
    assert data["optimistic"] == {
        **data["optimistic"],
        "timestamp": 1234,
        "resend": True,
    }
    assert row["status"] == "sent"


def test_retry_refused_by_the_core_puts_the_bubble_back_to_failed():
    client = StubClient(ApiError("conflict", "Message is not in a failed state"))
    app, contact = view_app(client)
    row = {"is_mine": True, "timestamp": 1234, "text": "ciao", "status": "failed"}
    app._cache[contact.cache_key] = [row]

    app._retry_failed_message(1234, "ciao")
    run_workers(app)

    assert row["status"] == "failed"


# -- boundary ----------------------------------------------------------------------


@pytest.mark.parametrize("module", ["tui/send.py"])
def test_send_never_touches_backends_or_the_database(module):
    source = (ROOT / module).read_text(encoding="utf-8")
    for forbidden in (
        "self.manager",
        "signal_backend",
        "whatsapp_backend",
        "telegram_backend",
        "protocols",
        "sqlite3",
        "ingest_message",
        "_persist_message",
        "send_message_sync",
        "edit_message_sync",
        "apply_edit(",
    ):
        assert forbidden not in source, (module, forbidden)
