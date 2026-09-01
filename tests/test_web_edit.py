from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

TOKEN = "correct-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class EditManager:
    def __init__(self):
        self.backend = SimpleNamespace(apply_edit=MagicMock(return_value={"ok": True}))
        self.edit_message_sync = MagicMock(return_value=True)

    def get(self, _protocol):
        return self.backend


@pytest.fixture
def edit_client(monkeypatch, tmp_path):
    import protocols.db as backend
    from protocols.db import _init_db
    from web.api import create_api_router
    from web.auth import install_auth

    db_file = tmp_path / "messages.db"
    monkeypatch.setattr(backend, "DB_FILE", db_file)
    _init_db()
    manager = EditManager()
    app = FastAPI()
    app.state.manager = manager
    install_auth(app, TOKEN)
    app.include_router(create_api_router())
    with TestClient(app) as client:
        yield client, manager, db_file


def _insert_message(
    db_file,
    *,
    protocol="signal",
    contact_id="alice",
    text="Prima",
    is_mine=1,
    timestamp=1000,
    msg_id="message-1",
    msg_type="text",
    status="sent",
):
    with sqlite3.connect(db_file) as connection:
        connection.execute(
            "INSERT INTO messages(protocol, contact_number, text, is_mine, "
            "timestamp, msg_id, msg_type, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                protocol,
                contact_id,
                text,
                is_mine,
                timestamp,
                msg_id,
                msg_type,
                status,
            ),
        )


def _payload(**overrides):
    return {
        "protocol": "signal",
        "contact_id": "alice",
        "message_id": "message-1",
        "new_text": "Dopo",
        **overrides,
    }


def test_edit_requires_bearer_token(edit_client):
    client, manager, _ = edit_client
    response = client.post("/api/messages/edit", json=_payload())
    assert response.status_code == 401
    manager.edit_message_sync.assert_not_called()


@pytest.mark.parametrize(
    "overrides",
    [
        {"protocol": "invalid"},
        {"contact_id": ""},
        {"contact_id": 1},
        {"message_id": ""},
        {"message_id": 1},
        {"new_text": "  \n"},
        {"new_text": 1},
        {"new_text": "x" * 65537},
    ],
)
def test_edit_rejects_invalid_payload(edit_client, overrides):
    client, manager, _ = edit_client
    response = client.post(
        "/api/messages/edit", json=_payload(**overrides), headers=AUTH
    )
    assert response.status_code == 400
    manager.edit_message_sync.assert_not_called()


def test_edit_returns_404_for_unknown_or_wrong_scope(edit_client):
    client, manager, db_file = edit_client
    _insert_message(db_file, contact_id="bob")
    response = client.post("/api/messages/edit", json=_payload(), headers=AUTH)
    assert response.status_code == 404
    manager.edit_message_sync.assert_not_called()


def test_edit_returns_500_for_database_lookup_error(edit_client):
    client, manager, _ = edit_client

    with patch(
        "web.api.sqlite3.connect",
        side_effect=sqlite3.OperationalError("database unavailable"),
    ):
        response = client.post("/api/messages/edit", json=_payload(), headers=AUTH)

    assert response.status_code == 500
    assert response.json() == {"detail": "Database error"}
    manager.edit_message_sync.assert_not_called()


@pytest.mark.parametrize(
    "fields",
    [
        {"is_mine": 0},
        {"msg_type": "image"},
        {"status": "pending"},
        {"status": "failed"},
    ],
)
def test_edit_rejects_non_editable_message(edit_client, fields):
    client, manager, db_file = edit_client
    _insert_message(db_file, **fields)
    response = client.post("/api/messages/edit", json=_payload(), headers=AUTH)
    assert response.status_code == 400
    assert response.json() == {"detail": "Message not editable"}
    manager.edit_message_sync.assert_not_called()


@pytest.mark.parametrize("failure", [False, RuntimeError("backend down")])
def test_edit_maps_backend_failure_to_502(edit_client, failure):
    client, manager, db_file = edit_client
    _insert_message(db_file)
    manager.edit_message_sync.side_effect = (
        failure if isinstance(failure, Exception) else None
    )
    manager.edit_message_sync.return_value = failure
    response = client.post("/api/messages/edit", json=_payload(), headers=AUTH)
    assert response.status_code == 502
    assert response.json() == {"detail": "Message edit failed"}
    manager.backend.apply_edit.assert_not_called()


def test_edit_signal_idless_message_uses_timestamp_fallback(edit_client):
    client, manager, db_file = edit_client
    _insert_message(db_file, msg_id=None, timestamp=1234)
    response = client.post(
        "/api/messages/edit",
        json=_payload(message_id="1234"),
        headers=AUTH,
    )
    assert response.status_code == 200
    manager.edit_message_sync.assert_called_once_with("signal", "alice", "1234", "Dopo")


def test_edit_success_applies_locally_and_pushes_event(edit_client):
    client, manager, db_file = edit_client
    _insert_message(db_file, timestamp=4321)

    with patch("web.api.push_event") as push_event:
        response = client.post("/api/messages/edit", json=_payload(), headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    manager.edit_message_sync.assert_called_once_with(
        "signal", "alice", "message-1", "Dopo"
    )
    manager.backend.apply_edit.assert_called_once_with(
        "alice", "message-1", "Dopo", is_mine=True
    )
    push_event.assert_called_once_with(
        {
            "type": "message_edit",
            "payload": {
                "protocol": "signal",
                "contact_id": "alice",
                "message_id": "message-1",
                "timestamp": 4321,
                "old_text": "Prima",
                "text": "Dopo",
                "is_mine": True,
            },
        }
    )
