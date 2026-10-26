"""The TUI edits through ``messages.edit`` only (optimistic view, rollback)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from client_harness import StubClient, run_workers, view_app

from models import PROTOCOL_SIGNAL
from server.api import ApiError

ROOT = Path(__file__).resolve().parent.parent


# -- edit ------------------------------------------------------------------------


def _editview_app(client: StubClient):
    app, contact = view_app(client)
    entry = {
        "id": "sig-1000",
        "text": "vecchio",
        "is_mine": True,
        "timestamp": 1000,
        "msg_type": "text",
        "status": "sent",
    }
    app._cache[contact.cache_key] = [entry]
    app._editing_message = {
        "protocol": PROTOCOL_SIGNAL,
        "contact_id": contact.id,
        "cache_key": contact.cache_key,
        "timestamp": 1000,
        "message_id": "sig-1000",
        "old_text": "vecchio",
        "_widget": None,
    }
    app._update_reply_bar = MagicMock()
    app._seen_message_ids.add((PROTOCOL_SIGNAL, contact.cache_key, 1000, "vecchio"))
    return app, contact, entry


def test_edit_applies_to_the_view_at_once_and_asks_the_core_from_the_worker():
    client = StubClient({"ok": True})
    app, contact, entry = _editview_app(client)

    app._submit_edit("nuovo")

    assert (entry["text"], entry["edited"]) == ("nuovo", True)
    assert client.calls == []  # nothing on the UI thread
    run_workers(app)
    assert client.calls == [
        (
            "messages.edit",
            {
                "protocol": PROTOCOL_SIGNAL,
                "contact_id": contact.id,
                "message_id": "sig-1000",
                "new_text": "nuovo",
            },
        )
    ]
    app._status.assert_called_with("✏️ Message edited")


def test_edit_error_rolls_the_view_back():
    client = StubClient(
        ApiError("backend_error", "Message edit failed", details={"error": "boom"})
    )
    app, contact, entry = _editview_app(client)

    app._submit_edit("nuovo")
    run_workers(app)

    assert (entry["text"], entry["edited"]) == ("vecchio", False)
    ident = (PROTOCOL_SIGNAL, contact.cache_key, 1000, "vecchio")
    assert ident in app._seen_message_ids
    app._status.assert_called_with("❌ Edit failed: boom", 0)


# -- boundary ---------------------------------------------------------------------


def test_edit_never_touches_backends_or_the_database():
    source = (ROOT / "tui/edit.py").read_text(encoding="utf-8")
    for forbidden in (
        "self.manager",
        "signal_backend",
        "protocols",
        "sqlite3",
        "edit_message_sync",
        "apply_edit(",
    ):
        assert forbidden not in source, forbidden
