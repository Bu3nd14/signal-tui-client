"""Contract: POST /api/messages/edit and /api/messages/reaction == operations.

Success bodies equal ``ChatAPI.call_json``; every failure has the status and
``detail`` of the matching ``ApiError`` (via ``web.ops.HTTP_STATUS``); the
published ``/ws`` frame is the legacy one.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from client_harness import api_for
from event_harness import legacy_frames
from fastapi import FastAPI
from fastapi.testclient import TestClient

from protocols import db
from protocols.db import _init_db
from server.api import ApiError
from server.bus import EventBus
from web.api import create_api_router
from web.auth import install_auth
from web.ops import HTTP_STATUS, get_api

AUTH = {"Authorization": "Bearer contract-secret"}


class Manager:
    def __init__(self, edit_result=True):
        self.backend = SimpleNamespace(
            apply_edit=MagicMock(return_value={}),
            user_number="+me",
            send_reaction_sync=MagicMock(return_value=True),
            apply_reaction=MagicMock(
                return_value={
                    "message_id": "m1",
                    "timestamp": 1000,
                    "reactions": [{"emoji": "👍", "count": 1, "is_mine": True}],
                }
            ),
        )
        self.edit_message_sync = MagicMock(return_value=edit_result)
        self.missing: set[str] = set()

    def get(self, protocol):
        return None if protocol in self.missing else self.backend


@pytest.fixture
def env(monkeypatch, tmp_path):
    db_file = tmp_path / "messages.db"
    monkeypatch.setattr(db, "DB_FILE", db_file)
    _init_db()
    with closing(sqlite3.connect(db_file)) as con, con:
        con.execute(
            "INSERT INTO messages(protocol, contact_number, text, is_mine, "
            "timestamp, msg_id, msg_type, status) "
            "VALUES ('signal', 'alice', 'Prima', 1, 1000, 'm1', 'text', 'sent')"
        )
    manager = Manager()
    app = FastAPI()
    bus = EventBus()
    app.state.api = api_for(manager, bus)
    install_auth(app, "contract-secret")
    app.include_router(create_api_router())
    sub = bus.subscribe()
    with TestClient(app) as client:
        yield client, get_api(app), manager, sub, db_file


def _edit(**over):
    body = {
        "protocol": "signal",
        "contact_id": "alice",
        "message_id": "m1",
        "new_text": "Dopo",
    }
    body.update(over)
    return body


def _react(**over):
    body = {
        "protocol": "signal",
        "contact_id": "alice",
        "message_id": "m1",
        "emoji": "👍",
    }
    body.update(over)
    return body


def _assert_same_failure(client, api, route, op, body):
    response = client.post(route, json=body, headers=AUTH)
    with pytest.raises(ApiError) as exc:
        api.call_json(op, body)
    assert response.status_code == HTTP_STATUS[exc.value.code]
    assert response.json() == {"detail": exc.value.message}
    return exc.value


@pytest.mark.parametrize(
    "over",
    [
        {"protocol": "irc"},
        {"protocol": 3},
        {"contact_id": "  "},
        {"contact_id": None},
        {"message_id": ""},
        {"new_text": "   "},
        {"new_text": 5},
        {"new_text": "x" * (64 * 1024 + 1)},
    ],
)
def test_edit_validation_failures(env, over):
    client, api, manager, sub, _ = env
    err = _assert_same_failure(
        client, api, "/api/messages/edit", "messages.edit", _edit(**over)
    )
    assert (err.code, err.message) == ("invalid_request", "Invalid request")
    manager.edit_message_sync.assert_not_called()
    assert legacy_frames(sub.drain()) == []


def test_edit_missing_field_is_invalid_request(env):
    client, api, *_ = env
    body = _edit()
    del body["new_text"]
    err = _assert_same_failure(client, api, "/api/messages/edit", "messages.edit", body)
    assert err.code == "invalid_request"


def test_edit_not_found_and_not_editable(env):
    client, api, _, _, db_file = env
    err = _assert_same_failure(
        client, api, "/api/messages/edit", "messages.edit", _edit(message_id="zz")
    )
    assert (err.code, err.message) == ("not_found", "Not Found")
    with closing(sqlite3.connect(db_file)) as con, con:
        con.execute("UPDATE messages SET is_mine = 0")
    err = _assert_same_failure(
        client, api, "/api/messages/edit", "messages.edit", _edit()
    )
    assert (err.code, err.message) == ("invalid_request", "Message not editable")


def test_edit_backend_failure(env):
    client, api, manager, sub, _ = env
    manager.edit_message_sync.return_value = False
    err = _assert_same_failure(
        client, api, "/api/messages/edit", "messages.edit", _edit()
    )
    assert (err.code, err.message) == ("backend_error", "Message edit failed")
    assert legacy_frames(sub.drain()) == []


def test_edit_database_error(env, monkeypatch):
    client, api, *_ = env

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("down")

    monkeypatch.setattr("sqlite3.connect", boom)
    err = _assert_same_failure(
        client, api, "/api/messages/edit", "messages.edit", _edit()
    )
    assert (err.code, err.message) == ("internal", "Database error")


def test_edit_success_matches_operation_and_frame(env):
    client, api, _, sub, db_file = env
    response = client.post("/api/messages/edit", json=_edit(), headers=AUTH)
    assert response.status_code == 200
    assert response.json() == api.call_json("messages.edit", _edit()) == {"ok": True}
    frames = legacy_frames(sub.drain())
    assert frames[0] == {
        "type": "message_edit",
        "payload": {
            "protocol": "signal",
            "contact_id": "alice",
            "message_id": "m1",
            "timestamp": 1000,
            "old_text": "Prima",
            "text": "Dopo",
            "is_mine": True,
        },
    }
    assert len(frames) == 2  # route call, then the direct operation call
    with closing(sqlite3.connect(db_file)) as con, con:
        assert con.execute("SELECT text, edited FROM messages").fetchone() == (
            "Dopo",
            1,
        )


@pytest.mark.parametrize(
    "over",
    [
        {"protocol": "irc"},
        {"contact_id": " "},
        {"message_id": ""},
        {"emoji": ""},
        {"emoji": "not-emoji"},
        {"emoji": "👍" * 9},
        {"emoji": 3},
    ],
)
def test_reaction_validation_failures(env, over):
    client, api, manager, sub, _ = env
    err = _assert_same_failure(
        client, api, "/api/messages/reaction", "reactions.set", _react(**over)
    )
    assert (err.code, err.message) == ("invalid_request", "Invalid request")
    manager.backend.send_reaction_sync.assert_not_called()
    assert legacy_frames(sub.drain()) == []


def test_reaction_backend_missing_and_message_missing(env):
    client, api, manager, _, _ = env
    err = _assert_same_failure(
        client,
        api,
        "/api/messages/reaction",
        "reactions.set",
        _react(message_id="zz"),
    )
    assert (err.code, err.message) == ("not_found", "Message not found")
    manager.missing.add("signal")
    err = _assert_same_failure(
        client, api, "/api/messages/reaction", "reactions.set", _react()
    )
    assert (err.code, err.message) == ("not_found", "Backend not found")


@pytest.mark.parametrize("failure", ["false", "raises"])
def test_reaction_send_failure(env, failure):
    client, api, manager, sub, _ = env
    send = manager.backend.send_reaction_sync
    if failure == "false":
        send.return_value = False
    else:
        send.side_effect = RuntimeError("down")
    err = _assert_same_failure(
        client, api, "/api/messages/reaction", "reactions.set", _react()
    )
    assert (err.code, err.message) == ("backend_error", "Reaction send failed")
    assert legacy_frames(sub.drain()) == []


def test_reaction_success_matches_operation_and_frame(env):
    client, api, manager, sub, _ = env
    response = client.post("/api/messages/reaction", json=_react(), headers=AUTH)
    assert response.status_code == 200
    assert response.json() == api.call_json("reactions.set", _react())
    assert list(response.json()) == ["ok", "reactions"]
    frames = legacy_frames(sub.drain())
    assert frames[0] == {
        "type": "reaction_update",
        "payload": {
            "protocol": "signal",
            "contact_id": "alice",
            "message_id": "m1",
            "timestamp": 1000,
            "reactions": [{"emoji": "👍", "count": 1, "is_mine": True}],
        },
    }
    manager.backend.send_reaction_sync.assert_called_with(
        "alice", "1000", "👍", target_author="+me"
    )
