"""Acceptance tests for the optional read-only HTML5 web plug-in."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from models import ChatContact

TOKEN = "correct-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeManager:
    def __init__(self, contacts=(), paths=None):
        self.contacts = list(contacts)
        self.paths = dict(paths or {})

    def list_contacts(self):
        return list(self.contacts)

    def get_attachment_path(self, proto, attachment_id):
        return self.paths.get((proto, attachment_id))

    def get(self, proto):
        return SimpleNamespace(media_dir=None)


def make_app(manager):
    from web.api import create_api_router
    from web.auth import install_auth
    from web.ws import install_websocket

    app = FastAPI()
    app.state.manager = manager
    app.state.websocket_connections = set()
    install_auth(app, TOKEN)
    app.include_router(create_api_router())
    install_websocket(app, TOKEN)
    return app


@pytest.fixture
def web_client(tmp_path):
    import backend
    from backend.db import _init_db

    db_file = tmp_path / "messages.db"
    with (
        patch.object(backend, "DB_FILE", db_file),
        patch.object(backend, "CACHE_DIR", tmp_path),
    ):
        _init_db()
        manager = FakeManager()
        with TestClient(make_app(manager)) as client:
            yield client, manager, db_file


def test_rest_auth_requires_correct_bearer(web_client):
    client, _, _ = web_client
    missing = client.get("/api/contacts")
    wrong = client.get("/api/contacts", headers={"Authorization": "Bearer wrong"})
    correct = client.get("/api/contacts", headers=AUTH)
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert wrong.status_code == 401
    assert correct.status_code == 200


def test_websocket_rejects_missing_token(web_client):
    from starlette.websockets import WebSocketDisconnect

    client, _, _ = web_client
    with (
        pytest.raises(WebSocketDisconnect) as caught,
        client.websocket_connect("/ws"),
    ):
        pass
    assert caught.value.code == 1008


def test_websocket_accepts_browser_subprotocol_token(web_client):
    import base64

    client, _, _ = web_client
    encoded = base64.urlsafe_b64encode(TOKEN.encode()).decode().rstrip("=")
    with client.websocket_connect(
        "/ws", subprotocols=["signal-tui-bearer", f"signal-tui-token.{encoded}"]
    ) as websocket:
        assert websocket.accepted_subprotocol == "signal-tui-bearer"


def test_media_rejects_traversal_absolute_nested_and_tgref(tmp_path):
    import backend

    root = tmp_path / "attachments"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("never disclose")
    manager = FakeManager()
    manager.get_attachment_path = MagicMock(return_value=outside)
    with (
        patch.object(backend, "SIGNAL_CLI_ATTACHMENTS_DIR", root),
        TestClient(make_app(manager)) as client,
    ):
        attempts = [
            "../../secret.txt",
            "/etc/passwd",
            "safe/../../../secret.txt",
            "tgref:42:12",
        ]
        for attachment_id in attempts:
            response = client.get(f"/api/media/signal/{attachment_id}", headers=AUTH)
            assert response.status_code == 404
            assert response.content != outside.read_bytes()
    assert manager.get_attachment_path.call_count == 2


def test_media_serves_legitimate_file(tmp_path):
    import backend

    root = tmp_path / "attachments"
    root.mkdir()
    image = root / "photo.jpg"
    image.write_bytes(b"native-high-resolution")
    manager = FakeManager(paths={("signal", "photo.jpg"): image})
    with (
        patch.object(backend, "SIGNAL_CLI_ATTACHMENTS_DIR", root),
        TestClient(make_app(manager)) as client,
    ):
        response = client.get("/api/media/signal/photo.jpg", headers=AUTH)
    assert response.status_code == 200
    assert response.content == b"native-high-resolution"


def test_contacts_schema_order_and_unread_count(web_client):
    client, manager, db_file = web_client
    import sqlite3

    manager.contacts = [
        ChatContact("second", "Second", "whatsapp", {"last_message_ts": 10}),
        ChatContact("first", "First", "signal", {"last_message_ts": 20}),
    ]
    with sqlite3.connect(db_file) as connection:
        connection.executemany(
            "INSERT INTO messages(protocol, contact_number, text, is_mine, timestamp, read) VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("signal", "first", "unread", 0, 1, 0),
                ("signal", "first", "mine", 1, 2, 0),
                ("whatsapp", "second", "read", 0, 3, 1),
            ],
        )
    response = client.get("/api/contacts", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body] == ["second", "first"]
    assert set(body[0]) == {
        "id",
        "display_name",
        "protocol",
        "extras",
        "last_message_ts",
        "unread",
    }
    assert body[0]["unread"] == 0
    assert body[1]["unread"] == 1


def test_messages_schema_filters_and_stable_chronological_order(web_client):
    client, _, db_file = web_client
    import sqlite3

    with sqlite3.connect(db_file) as connection:
        connection.executemany(
            "INSERT INTO messages(protocol, contact_number, text, is_mine, timestamp, attachment_id, attachment_info, content_type, msg_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("signal", "alice", "later", 1, 20, None, None, None, "m2"),
                (
                    "signal",
                    "alice",
                    "first tie",
                    0,
                    10,
                    "pic",
                    "x.jpg",
                    "image/jpeg",
                    "m1",
                ),
                ("whatsapp", "alice", "other protocol", 0, 5, None, None, None, "w1"),
                ("signal", "bob", "other contact", 0, 1, None, None, None, "b1"),
                ("signal", "alice", "second tie", 0, 10, None, None, None, None),
            ],
        )
    response = client.get(
        "/api/messages", params={"proto": "signal", "contact_id": "alice"}, headers=AUTH
    )
    assert response.status_code == 200
    body = response.json()
    assert [item["text"] for item in body] == ["first tie", "second tie", "later"]
    assert all(
        set(item) == {"id", "text", "direction", "timestamp", "attachment"}
        for item in body
    )
    assert body[0]["attachment"] == {
        "attachment_id": "pic",
        "name": "x.jpg",
        "type": "image/jpeg",
    }
    assert body[1]["id"].isdigit()
    assert body[2]["direction"] == "out"


def test_bridge_is_bounded_nonblocking_and_counts_drop():
    from web import bridge

    push_queue = bridge.init_bridge()
    started = time.monotonic()
    for index in range(1000):
        assert bridge.push_event({"n": index})
    assert not bridge.push_event({"n": "overflow"})
    elapsed = time.monotonic() - started
    assert push_queue.maxsize == 1000
    assert push_queue.qsize() == 1000
    assert bridge.dropped_events() == 1
    assert elapsed < 1
    bridge.close_bridge()


def test_occupied_port_degrades_to_down_without_raising():
    from web.server import start_web_server, stop_web_server

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    handle = start_web_server(FakeManager(), port=port, token=TOKEN)
    assert handle is not None
    deadline = time.monotonic() + 2
    while handle.status == "starting" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert handle.status == "down"
    stop_web_server(handle)
    assert not handle.thread.is_alive()
    listener.close()


def test_clean_start_and_stop_web_server():
    from web.server import start_web_server, stop_web_server

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    handle = start_web_server(FakeManager(), port=port, token=TOKEN)
    assert handle is not None
    deadline = time.monotonic() + 2
    while handle.status == "starting" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert handle.status == "up"
    stop_web_server(handle)
    assert handle.status == "down"
    assert not handle.thread.is_alive()


def test_default_tui_import_does_not_import_optional_web_package():
    code = "import sys; import tui.app; assert not any(n == 'web' or n.startswith('web.') for n in sys.modules)"
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
