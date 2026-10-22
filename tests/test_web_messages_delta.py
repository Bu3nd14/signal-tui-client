"""``/api/messages`` ``limit``/``since``: the web message cache's delta sync."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import protocols.db as backend
from protocols.db import _init_db
from web.api import create_api_router


@pytest.fixture
def client(monkeypatch, tmp_path: Path):
    db_file = tmp_path / "messages.db"
    monkeypatch.setattr(backend, "DB_FILE", db_file)
    monkeypatch.setattr(backend, "CACHE_DIR", tmp_path)
    _init_db()
    with sqlite3.connect(db_file) as connection:
        for ts in range(1, 11):
            connection.execute(
                "INSERT INTO messages (protocol, contact_number, text, is_mine, "
                "timestamp, msg_id) VALUES ('signal', 'alice', ?, 0, ?, ?)",
                (f"m{ts}", ts * 1000, f"id{ts}"),
            )
    app = FastAPI()
    app.state.manager = SimpleNamespace(get_attachment_path=lambda *_args: None)
    app.include_router(create_api_router())
    with TestClient(app) as test_client:
        yield test_client


def _texts(client, **params):
    response = client.get(
        "/api/messages", params={"proto": "signal", "contact_id": "alice", **params}
    )
    assert response.status_code == 200, response.text
    return [message["text"] for message in response.json()]


def test_default_returns_everything_oldest_first(client):
    assert _texts(client) == [f"m{i}" for i in range(1, 11)]


def test_limit_returns_newest_in_chronological_order(client):
    assert _texts(client, limit=3) == ["m8", "m9", "m10"]


def test_since_returns_delta_inclusive(client):
    assert _texts(client, since=8000) == ["m8", "m9", "m10"]
    assert _texts(client, since=11000) == []


def test_since_and_limit_combined(client):
    assert _texts(client, since=3000, limit=2) == ["m9", "m10"]


@pytest.mark.parametrize("limit", [0, -1, 2001])
def test_invalid_limit_rejected(client, limit):
    response = client.get(
        "/api/messages",
        params={"proto": "signal", "contact_id": "alice", "limit": limit},
    )
    assert response.status_code == 400
