from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

TOKEN = "test-token"


class Manager:
    def __init__(self):
        self.backend = SimpleNamespace(apply_edit=MagicMock(return_value=None))
        self.edit_message_sync = MagicMock(return_value=True)

    def get(self, _protocol):
        return self.backend


def test_web_edit_persists_when_message_is_not_in_backend_cache(monkeypatch, tmp_path):
    import backend
    from backend.db import _init_db
    from web.api import create_api_router
    from web.auth import install_auth

    db_file = tmp_path / "messages.db"
    monkeypatch.setattr(backend, "DB_FILE", db_file)
    _init_db()
    with sqlite3.connect(db_file) as connection:
        connection.execute(
            "INSERT INTO messages(protocol, contact_number, text, is_mine, "
            "timestamp, msg_id, msg_type, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("signal", "alice", "Prima", 1, 1000, "1000", "text", "sent"),
        )

    manager = Manager()
    app = FastAPI()
    app.state.manager = manager
    install_auth(app, TOKEN)
    app.include_router(create_api_router())
    with TestClient(app) as client:
        response = client.post(
            "/api/messages/edit",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={
                "protocol": "signal",
                "contact_id": "alice",
                "message_id": "1000",
                "new_text": "Dopo",
            },
        )

    assert response.status_code == 200
    manager.backend.apply_edit.assert_called_once_with(
        "alice", "1000", "Dopo", is_mine=True
    )
    with sqlite3.connect(db_file) as connection:
        persisted = connection.execute(
            "SELECT text, edited FROM messages WHERE msg_id = '1000'"
        ).fetchone()
    assert persisted == ("Dopo", 1)
