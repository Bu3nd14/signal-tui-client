"""Acceptance tests for the optional read-only HTML5 web plug-in."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from base64 import b64decode
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import quote

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
        self.send_calls = []
        self.attachment_calls = []

    def list_contacts(self):
        return list(self.contacts)

    def get_attachment_path(self, proto, attachment_id):
        return self.paths.get((proto, attachment_id))

    def get(self, proto):
        return SimpleNamespace(media_dir=None)

    def send_message_sync(self, protocol, contact_id, text, **kwargs):
        self.send_calls.append((protocol, contact_id, text, kwargs))
        return "sent-id"

    def send_attachment_sync(self, protocol, contact_id, file_path, **kwargs):
        path = str(file_path)
        self.attachment_calls.append(
            (protocol, contact_id, path, kwargs, Path(path).exists())
        )
        return "attachment-id"


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


def test_emoji_requires_bearer_token(web_client):
    client, _, _ = web_client

    response = client.get("/api/emoji")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_emoji_uses_raw_predefined_categories(web_client):
    from emoji_data import PREDEFINED_CATEGORIES

    client, _, _ = web_client
    response = client.get("/api/emoji", headers=AUTH)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=3600"
    categories = response.json()
    assert len(categories) == len(PREDEFINED_CATEGORIES)
    for served, (name, icon, chars) in zip(categories, PREDEFINED_CATEGORIES):
        assert served["category"] == name
        assert served["icon"] == icon
        assert served["emojis"] == chars
        assert chars[0] in served["aliases"]
    raw_chars = [char for category in categories for char in category["emojis"]]
    assert any("\u200d" in char for char in raw_chars)


def test_send_requires_bearer_token():
    contact = ChatContact("alice", "Alice", "signal")
    manager = FakeManager([contact])
    with TestClient(make_app(manager)) as client:
        response = client.post(
            "/api/send",
            json={"protocol": "signal", "contact_id": "alice", "text": "Ciao"},
        )
    assert response.status_code == 401
    assert manager.send_calls == []


def test_send_routes_reply_metadata_according_to_protocol():
    for protocol, reply_to_message_id in (
        ("signal", "message-1"),
        ("whatsapp", "message-1"),
        ("telegram", "123"),
    ):
        contact = ChatContact("alice", "Alice", protocol)
        manager = FakeManager([contact])
        payload = {
            "protocol": protocol,
            "contact_id": "alice",
            "text": "Ciao",
            "quote_timestamp": 123456,
            "quote_author": "alice",
            "quote_message": "Prima",
            "reply_to_message_id": reply_to_message_id,
        }
        if protocol != "signal":
            payload.update(
                quote_content_type={"ignored": True},
                quote_attachment_id=["ignored"],
            )
        with TestClient(make_app(manager)) as client:
            response = client.post("/api/send", json=payload, headers=AUTH)

        assert response.status_code == 200
        kwargs = manager.send_calls[0][3]
        assert kwargs["quote_timestamp"] == 123456
        assert kwargs["quote_author"] == "alice"
        assert kwargs["quote_message"] == "Prima"
        if protocol == "signal":
            assert "reply_to_message_id" not in kwargs
        else:
            assert kwargs["reply_to_message_id"] == reply_to_message_id


def test_whatsapp_text_reply_strips_boundary_whitespace_from_quote_metadata():
    contact = ChatContact("alice", "Alice", "whatsapp")
    manager = FakeManager([contact])
    payload = {
        "protocol": "whatsapp",
        "contact_id": "alice",
        "text": "Risposta",
        "quote_timestamp": 123456,
        "quote_author": "  alice\n",
        "quote_message": " \nDomanda su\npiù righe\n ",
        "reply_to_message_id": " message-1\n",
    }

    with TestClient(make_app(manager)) as client:
        response = client.post("/api/send", json=payload, headers=AUTH)

    assert response.status_code == 200
    kwargs = manager.send_calls[0][3]
    assert kwargs["quote_author"] == "alice"
    assert kwargs["quote_message"] == "Domanda su\npiù righe"
    assert kwargs["reply_to_message_id"] == "message-1"


def test_send_rejects_empty_text():
    contact = ChatContact("alice", "Alice", "signal")
    manager = FakeManager([contact])
    with TestClient(make_app(manager)) as client:
        response = client.post(
            "/api/send",
            json={"protocol": "signal", "contact_id": "alice", "text": "  \n"},
            headers=AUTH,
        )
    assert response.status_code == 400
    assert manager.send_calls == []


def test_send_internal_timeout_waits_for_worker_and_returns_generic_502():
    from web import api

    contact = ChatContact("alice", "Alice", "signal")
    manager = FakeManager([contact])
    active_jobs = 0
    pending_pool_jobs = 0
    calls = 0
    original_to_thread = api.asyncio.to_thread

    async def tracked_to_thread(*args, **kwargs):
        nonlocal pending_pool_jobs
        pending_pool_jobs += 1
        try:
            return await original_to_thread(*args, **kwargs)
        finally:
            pending_pool_jobs -= 1

    def timed_out_send(*args, **kwargs):
        nonlocal active_jobs, calls
        calls += 1
        active_jobs += 1
        time.sleep(0.05)
        active_jobs -= 1
        raise TimeoutError("backend timeout")

    manager.send_message_sync = timed_out_send
    with (
        patch.object(api.asyncio, "to_thread", tracked_to_thread),
        TestClient(make_app(manager)) as client,
    ):
        response = client.post(
            "/api/send",
            json={"protocol": "signal", "contact_id": "alice", "text": "Ciao"},
            headers=AUTH,
        )
    assert response.status_code == 502
    assert response.json() == {"detail": "Message send failed"}
    assert calls == 1
    assert active_jobs == 0
    assert pending_pool_jobs == 0


def _run_reconciliation_node(source):
    script = f"""
const assert = require("node:assert/strict");
const {{ reconcileOptimisticMessages, replyQuoteMessage }} = require("./web/static/reconcile.js");
{source}
"""
    completed = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_optimistic_dedup_two_identical_messages_is_one_to_one():
    _run_reconciliation_node("""
const old = { id: "old", direction: "out", text: "same", timestamp: 1000 };
const first = { optimistic_id: "local-1", protocol: "signal", contactId: "alice", direction: "out", text: "same", timestamp: 2000, known_message_ids: ["old"], optimisticStatus: "sent" };
const second = { optimistic_id: "local-2", protocol: "signal", contactId: "alice", direction: "out", text: "same", timestamp: 2001, known_message_ids: ["old"], optimisticStatus: "sent" };
const oneEcho = { id: "echo-1", direction: "out", text: "same", timestamp: 2002 };
const firstPass = reconcileOptimisticMessages([old, oneEcho], [first, second], "signal", "alice");
assert.deepEqual(firstPass.visible.map((item) => item.optimistic_id), ["local-1"]);
assert.equal(firstPass.optimistic.filter((item) => item.confirmed_message_id === "echo-1").length, 1);
const secondEcho = { id: "echo-2", direction: "out", text: "same", timestamp: 2003 };
const secondPass = reconcileOptimisticMessages([old, oneEcho, secondEcho], firstPass.optimistic, "signal", "alice");
assert.equal(secondPass.visible.length, 0);
assert.equal(new Set(secondPass.optimistic.map((item) => item.confirmed_message_id)).size, 2);
""")


def test_optimistic_dedup_accepts_echo_after_120_seconds():
    _run_reconciliation_node("""
const optimistic = { optimistic_id: "late", protocol: "signal", contactId: "alice", direction: "out", text: "late echo", timestamp: 1000, known_message_ids: [], optimisticStatus: "sent" };
const echo = { id: "echo-late", direction: "out", text: "late echo", timestamp: 122000 };
const result = reconcileOptimisticMessages([echo], [optimistic], "signal", "alice");
assert.equal(result.visible.length, 0);
assert.equal(result.optimistic[0].confirmed_message_id, "echo-late");
""")


def test_optimistic_failed_message_remains_without_echo():
    _run_reconciliation_node("""
const failed = { optimistic_id: "failed", protocol: "signal", contactId: "alice", direction: "out", text: "not sent", timestamp: 1000, known_message_ids: [], optimisticStatus: "failed" };
const result = reconcileOptimisticMessages([], [failed], "signal", "alice");
assert.equal(result.visible.length, 1);
assert.equal(result.visible[0].optimisticStatus, "failed");
assert.equal(result.optimistic[0].optimistic_id, "failed");
""")


def test_optimistic_reply_reconciles_echo_with_partial_quote_metadata():
    _run_reconciliation_node("""
const optimistic = { optimistic_id: "reply", protocol: "whatsapp", contactId: "alice", direction: "out", text: "answer", timestamp: 2000, known_message_ids: [], optimisticStatus: "sent", quote_timestamp: 1000, quote_author: "alice", quote_message: "photo.png" };
const echo = { id: "wa-1", direction: "out", text: "answer", timestamp: 2001, quote_timestamp: 1000, quote_text: "photo.png" };
const result = reconcileOptimisticMessages([echo], [optimistic], "whatsapp", "alice");
assert.equal(result.visible.length, 0);
assert.equal(result.optimistic[0].confirmed_message_id, "wa-1");
    """)


def test_whatsapp_optimistic_reply_reconciles_echo_without_quote():
    _run_reconciliation_node("""
const old = { id: "old", direction: "out", text: "answer", timestamp: 1000 };
const optimistic = { optimistic_id: "reply", protocol: "whatsapp", contactId: "alice", direction: "out", text: "answer", timestamp: 2000, known_message_ids: ["old"], optimisticStatus: "sent", quote_timestamp: 1500, quote_author: "alice", quote_message: "question" };
const echo = { id: "wa-echo", direction: "out", text: "answer", timestamp: 2001 };
const result = reconcileOptimisticMessages([old, echo], [optimistic], "whatsapp", "alice");
assert.equal(result.visible.length, 0);
assert.equal(result.optimistic[0].confirmed_message_id, "wa-echo");
""")


def test_optimistic_image_reply_reconciles_waha_media_placeholder_echo():
    _run_reconciliation_node("""
const optimistic = { optimistic_id: "image-reply", protocol: "whatsapp", contactId: "alice", direction: "out", text: "answer", timestamp: 2000, known_message_ids: [], optimisticStatus: "sent", quote_timestamp: 1000, quote_author: "alice", quote_message: "photo.jpg", quote_media_type: "image" };
const echo = { id: "wa-image-1", direction: "out", text: "answer", timestamp: 2001, quote_timestamp: 1000, quote_text: "🖼️ Immagine" };
const result = reconcileOptimisticMessages([echo], [optimistic], "whatsapp", "alice");
assert.equal(result.visible.length, 0);
assert.equal(result.optimistic[0].confirmed_message_id, "wa-image-1");
""")


@pytest.mark.parametrize("protocol", ["telegram", "whatsapp"])
@pytest.mark.parametrize(
    ("optimistic_has_timestamp", "echo_has_timestamp"),
    [(True, True), (True, False), (False, True), (False, False)],
)
@pytest.mark.parametrize(
    "echo_quote",
    [
        "Photo",
        "photo.jpg",
        "🖼️ Immagine",
        "photo.jpg — 🖼️ Immagine",
        "upload-a1b2c3.png",
    ],
)
def test_optimistic_media_reply_reconciles_quote_forms(
    protocol, optimistic_has_timestamp, echo_has_timestamp, echo_quote
):
    optimistic_timestamp = ", quote_timestamp: 1000" if optimistic_has_timestamp else ""
    echo_timestamp = ", quote_timestamp: 1000" if echo_has_timestamp else ""
    _run_reconciliation_node(f"""
const optimistic = {{ optimistic_id: "media-reply", protocol: {json.dumps(protocol)}, contactId: "alice", direction: "out", text: "answer", timestamp: 2000, known_message_ids: [], optimisticStatus: "sent", quote_author: "alice", quote_message: "photo.jpg", quote_media_type: "image"{optimistic_timestamp} }};
const echo = {{ id: "echo-1", direction: "out", text: "answer", timestamp: 2001, quote_text: {json.dumps(echo_quote)}{echo_timestamp} }};
const result = reconcileOptimisticMessages([echo], [optimistic], {json.dumps(protocol)}, "alice");
assert.equal(result.visible.length, 0);
assert.equal(result.optimistic[0].confirmed_message_id, "echo-1");
""")


def test_optimistic_signal_image_reply_reconciles_forwarded_quote_echo():
    from backends.signal import _signal_quote_text

    echo_quote = _signal_quote_text({"text": "Image: photo.jpg"})
    _run_reconciliation_node(f"""
const target = {{ id: "1000", direction: "in", text: "", timestamp: 1000, attachment: {{ type: "image/jpeg", name: "Image: photo.jpg", attachment_id: "signal-att" }} }};
const optimistic = {{ optimistic_id: "signal-image-reply", protocol: "signal", contactId: "alice", direction: "out", text: "answer", timestamp: 2000, known_message_ids: [], optimisticStatus: "sent", quote_timestamp: target.timestamp, quote_author: "alice", quote_message: replyQuoteMessage(target) }};
const echo = {{ id: "2001", direction: "out", text: "answer", timestamp: 2001, quote_timestamp: target.timestamp, quote_text: {json.dumps(echo_quote)} }};
const result = reconcileOptimisticMessages([echo], [optimistic], "signal", "alice");
assert.equal(optimistic.quote_message, "Image: photo.jpg");
assert.equal(echo.quote_text, optimistic.quote_message);
assert.equal(result.visible.length, 0);
assert.equal(result.optimistic[0].confirmed_message_id, "2001");
""")


def test_optimistic_telegram_image_reply_reconciles_media_echo():
    from backends.telegram import _tg_quote_text_from_cached

    echo_quote = _tg_quote_text_from_cached(
        {"id": "10", "text": "", "msg_type": "image", "attachment_info": "Photo"}
    )
    _run_reconciliation_node(f"""
const target = {{ id: "10", direction: "in", text: "", timestamp: 1000, attachment: {{ type: "image/jpeg", name: "Photo", attachment_id: "tgref:alice:10" }} }};
const optimistic = {{ optimistic_id: "telegram-image-reply", protocol: "telegram", contactId: "alice", direction: "out", text: "answer", timestamp: 2000, known_message_ids: [], optimisticStatus: "sent", quote_timestamp: target.timestamp, quote_author: "alice", quote_message: replyQuoteMessage(target), reply_to_message_id: target.id, quote_media_type: "image" }};
const echo = {{ id: "11", direction: "out", text: "answer", timestamp: 2001, quote_timestamp: target.timestamp, quote_text: {json.dumps(echo_quote)} }};
const result = reconcileOptimisticMessages([echo], [optimistic], "telegram", "alice");
assert.equal(optimistic.quote_message, "Photo");
assert.equal(echo.quote_text, "🖼️ Immagine");
assert.equal(result.visible.length, 0);
assert.equal(result.optimistic[0].confirmed_message_id, "11");
    """)


def test_optimistic_image_reconciles_media_placeholder_and_legacy_upload_echo():
    _run_reconciliation_node("""
const optimistic = { optimistic_id: "image", protocol: "telegram", contactId: "alice", direction: "out", text: "", timestamp: 2000, known_message_ids: [], optimisticStatus: "sent", attachment: { type: "image/png", name: "clipboard.png", attachment_id: "clipboard.png" } };
const echo = { id: "11", direction: "out", text: "🖼️ Immagine", timestamp: 2001, attachment: { type: "image/png", name: "🖼️ Immagine", attachment_id: "tgref:alice:11" } };
const result = reconcileOptimisticMessages([echo], [optimistic], "telegram", "alice");
assert.equal(result.visible.length, 0);
assert.equal(result.optimistic[0].confirmed_message_id, "11");

const legacyEcho = { ...echo, id: "12", text: "upload-a1b2c3.png" };
const legacyOptimistic = { ...optimistic, optimistic_id: "legacy" };
const legacy = reconcileOptimisticMessages([legacyEcho], [legacyOptimistic], "telegram", "alice");
assert.equal(legacy.visible.length, 0);
assert.equal(legacy.optimistic[0].confirmed_message_id, "12");
assert.equal(require("./web/static/reconcile.js").messageDisplayText(legacyEcho), "");
""")


def test_backend_manager_send_message_sync_routes_to_backend():
    from backends.manager import BackendManager

    backend = MagicMock(protocol="signal")
    backend.send_message_sync.return_value = "message-id"
    manager = BackendManager()
    manager.register(backend)

    result = manager.send_message_sync(
        "signal",
        "alice",
        "Ciao",
        quote_timestamp=123,
        quote_author="alice",
        quote_message="Prima",
        reply_to_message_id="reply-id",
        quote_attachments=["image/png"],
    )

    assert result == "message-id"
    backend.send_message_sync.assert_called_once_with(
        "alice",
        "Ciao",
        quote_timestamp=123,
        quote_author="alice",
        quote_message="Prima",
        reply_to_message_id="reply-id",
        quote_attachments=["image/png"],
    )


def test_backend_manager_send_attachment_sync_routes_to_backend(tmp_path):
    from backends.manager import BackendManager

    image = tmp_path / "image.png"
    backend = MagicMock(protocol="signal")
    backend.send_attachment_sync.return_value = "message-id"
    manager = BackendManager()
    manager.register(backend)

    result = manager.send_attachment_sync(
        "signal",
        "alice",
        image,
        caption="Ciao",
        mime_type="image/png",
        quote_timestamp=123,
        quote_author="alice",
        quote_message="Prima",
        reply_to_message_id="reply-id",
        quote_attachments=["image/png"],
    )

    assert result == "message-id"
    backend.send_attachment_sync.assert_called_once_with(
        "alice",
        image,
        caption="Ciao",
        mime_type="image/png",
        quote_timestamp=123,
        quote_author="alice",
        quote_message="Prima",
        reply_to_message_id="reply-id",
        quote_attachments=["image/png"],
    )


_PNG_1X1 = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_send_multipart_image_routes_and_cleans_temporary_file(web_client):
    client, manager, _ = web_client
    manager.contacts = [ChatContact("alice", "Alice", "signal")]

    response = client.post(
        "/api/send",
        data={"protocol": "signal", "contact_id": "alice", "text": "caption"},
        files={"file": ("clipboard.png", _PNG_1X1, "image/png")},
        headers=AUTH,
    )

    assert response.status_code == 200
    protocol, contact_id, path, kwargs, existed_during_send = manager.attachment_calls[
        0
    ]
    assert (protocol, contact_id, existed_during_send) == ("signal", "alice", True)
    assert kwargs["caption"] == "caption"
    assert kwargs["mime_type"] == "image/png"
    assert not Path(path).exists()


@pytest.mark.parametrize("multipart", [False, True])
def test_send_signal_media_reply_builds_quote_attachment_descriptor(
    web_client, multipart
):
    from backend.db import _add_message_to_cache

    client, manager, db_file = web_client
    manager.contacts = [ChatContact("alice", "Alice", "signal")]
    quoted = db_file.parent / "quoted.jpg"
    quoted.write_bytes(b"quoted-image")
    manager.paths[("signal", "quoted-id")] = quoted
    _add_message_to_cache(
        "alice",
        "",
        False,
        "alice",
        123456,
        msg_type="image",
        attachment_info="quoted.jpg",
        attachment_id="quoted-id",
        content_type="image/jpeg",
    )
    payload = {
        "protocol": "signal",
        "contact_id": "alice",
        "text": "Risposta",
        "quote_timestamp": "123456" if multipart else 123456,
        "quote_author": "alice",
        "quote_message": "",
        "quote_content_type": "image/jpeg",
        "quote_attachment_id": "quoted-id",
    }

    if multipart:
        response = client.post(
            "/api/send",
            data=payload,
            files={"file": ("reply.png", _PNG_1X1, "image/png")},
            headers=AUTH,
        )
        kwargs = manager.attachment_calls[0][3]
    else:
        response = client.post("/api/send", json=payload, headers=AUTH)
        kwargs = manager.send_calls[0][3]

    assert response.status_code == 200
    assert kwargs["quote_message"] == ""
    assert kwargs["quote_attachments"] == [f"image/jpeg:{quoted.name}:{quoted}"]


def test_send_signal_media_reply_rejects_unknown_attachment(web_client):
    client, manager, _ = web_client
    manager.contacts = [ChatContact("alice", "Alice", "signal")]

    response = client.post(
        "/api/send",
        json={
            "protocol": "signal",
            "contact_id": "alice",
            "text": "Risposta",
            "quote_timestamp": 123456,
            "quote_message": "",
            "quote_content_type": "image/jpeg",
            "quote_attachment_id": "unknown",
        },
        headers=AUTH,
    )

    assert response.status_code == 400
    assert manager.send_calls == []


def test_send_multipart_whatsapp_lid_uses_resolved_chat_id(tmp_path):
    from backends.manager import BackendManager
    from backends.whatsapp import WhatsAppBackend

    backend = WhatsAppBackend(api_url="http://api.test", media_dir=str(tmp_path))
    backend.contacts = [ChatContact("139153@lid", "Bob", "whatsapp")]
    backend._lid_map = {
        "139153@lid": {"phone": "393331234567", "resolved_at": 9999999999}
    }
    backend._rest._request = MagicMock(return_value={"id": "image-id"})
    manager = BackendManager()
    manager.register(backend)

    with TestClient(make_app(manager)) as client:
        response = client.post(
            "/api/send",
            data={
                "protocol": "whatsapp",
                "contact_id": "139153@lid",
                "text": "",
            },
            files={"file": ("clipboard.png", _PNG_1X1, "image/png")},
            headers=AUTH,
        )

    assert response.status_code == 200
    assert backend._rest._request.call_args.args[2]["chatId"] == "393331234567@c.us"


def test_send_multipart_whatsapp_unresolved_lid_returns_502(tmp_path):
    from backends.manager import BackendManager
    from backends.whatsapp import WhatsAppBackend

    backend = WhatsAppBackend(api_url="http://api.test", media_dir=str(tmp_path))
    backend.contacts = [ChatContact("139153@lid", "Bob", "whatsapp")]
    backend._lid_lookup = MagicMock(return_value=None)
    backend._lid_resolve_remote = MagicMock(return_value=None)
    backend._rest._request = MagicMock()
    manager = BackendManager()
    manager.register(backend)

    with TestClient(make_app(manager)) as client:
        response = client.post(
            "/api/send",
            data={
                "protocol": "whatsapp",
                "contact_id": "139153@lid",
                "text": "",
            },
            files={"file": ("clipboard.png", _PNG_1X1, "image/png")},
            headers=AUTH,
        )

    assert response.status_code == 502
    backend._rest._request.assert_not_called()


def test_send_multipart_image_routes_quote(web_client):
    client, manager, _ = web_client
    manager.contacts = [ChatContact("alice", "Alice", "whatsapp")]

    response = client.post(
        "/api/send",
        data={
            "protocol": "whatsapp",
            "contact_id": "alice",
            "text": "",
            "quote_timestamp": "123456",
            "quote_author": "alice",
            "quote_message": "photo.png",
            "reply_to_message_id": "wa-message-1",
        },
        files={"file": ("reply.png", _PNG_1X1, "image/png")},
        headers=AUTH,
    )

    assert response.status_code == 200
    assert manager.attachment_calls[0][3] == {
        "caption": None,
        "mime_type": "image/png",
        "quote_timestamp": 123456,
        "quote_author": "alice",
        "quote_message": "photo.png",
        "reply_to_message_id": "wa-message-1",
    }


def test_send_multipart_rejects_image_over_20_mib(web_client):
    client, manager, _ = web_client
    manager.contacts = [ChatContact("alice", "Alice", "signal")]
    oversized = b"\x89PNG\r\n\x1a\n" + b"x" * (20 * 1024 * 1024)

    response = client.post(
        "/api/send",
        data={"protocol": "signal", "contact_id": "alice", "text": ""},
        files={"file": ("large.png", oversized, "image/png")},
        headers=AUTH,
    )

    assert response.status_code == 413
    assert manager.attachment_calls == []


def test_send_multipart_rejects_non_image_magic(web_client):
    client, manager, _ = web_client
    manager.contacts = [ChatContact("alice", "Alice", "signal")]

    response = client.post(
        "/api/send",
        data={"protocol": "signal", "contact_id": "alice", "text": ""},
        files={"file": ("renamed.png", b"not an image", "image/png")},
        headers=AUTH,
    )

    assert response.status_code == 400
    assert manager.attachment_calls == []


@pytest.mark.parametrize(
    "exception,status", [(RuntimeError("down"), 502), (NotImplementedError(), 501)]
)
def test_send_multipart_maps_backend_errors(web_client, exception, status):
    client, manager, _ = web_client
    manager.contacts = [ChatContact("alice", "Alice", "signal")]
    manager.send_attachment_sync = MagicMock(side_effect=exception)

    response = client.post(
        "/api/send",
        data={"protocol": "signal", "contact_id": "alice", "text": ""},
        files={"file": ("clipboard.png", _PNG_1X1, "image/png")},
        headers=AUTH,
    )

    assert response.status_code == status


def test_upload_janitor_removes_only_files_older_than_one_hour(tmp_path):
    import backend
    from web.uploads import prepare_upload_directory

    upload_dir = tmp_path / "web-uploads"
    upload_dir.mkdir()
    old = upload_dir / "old.png"
    recent = upload_dir / "recent.png"
    old.write_bytes(_PNG_1X1)
    recent.write_bytes(_PNG_1X1)
    now = time.time()
    os.utime(old, (now - 3601, now - 3601))

    with patch.object(backend, "CACHE_DIR", tmp_path):
        prepare_upload_directory(now=now)

    assert not old.exists()
    assert recent.exists()


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


def _media_url(proto, attachment_id):
    return f"/api/media/{proto}/{quote(attachment_id, safe='')}"


def _assert_uniform_not_found(response, *secrets):
    assert response.status_code == 404
    assert response.status_code != 400
    assert response.json() == {"detail": "Not Found"}
    assert all(str(secret) not in response.text for secret in secrets)


def test_media_whatsapp_accepts_full_waha_url_and_masks_download_failure(tmp_path):
    root = tmp_path / "whatsapp-media"
    root.mkdir()
    image = root / "photo.jpg"
    image.write_bytes(b"whatsapp-image")
    success_url = "http://localhost:3000/api/files/default/photo.jpg"
    failed_url = "http://localhost:3000/api/files/default/missing.jpg"
    manager = FakeManager()
    manager.get = MagicMock(
        return_value=SimpleNamespace(_ensure_media_dir=MagicMock(return_value=root))
    )
    manager.get_attachment_path = MagicMock(
        side_effect=lambda proto, attachment_id: (
            image if (proto, attachment_id) == ("whatsapp", success_url) else None
        )
    )

    with TestClient(make_app(manager)) as client:
        success = client.get(_media_url("whatsapp", success_url), headers=AUTH)
        failure = client.get(_media_url("whatsapp", failed_url), headers=AUTH)

    assert success.status_code == 200
    assert success.content == b"whatsapp-image"
    assert success.headers["cache-control"] == "private, max-age=86400"
    _assert_uniform_not_found(failure, failed_url, root)
    assert "cache-control" not in failure.headers
    assert manager.get_attachment_path.call_args_list == [
        (("whatsapp", success_url),),
        (("whatsapp", failed_url),),
    ]


def test_media_telegram_accepts_absolute_path_only_inside_media_root(tmp_path):
    from backends import telegram

    root = tmp_path / "telegram-media"
    root.mkdir()
    image = root / "photo.jpg"
    image.write_bytes(b"telegram-image")
    outside = "/etc/passwd"
    manager = FakeManager()
    manager.get_attachment_path = MagicMock(
        side_effect=lambda proto, attachment_id: attachment_id
    )

    with (
        patch.object(telegram, "_media_dir", return_value=root),
        TestClient(make_app(manager)) as client,
    ):
        success = client.get(_media_url("telegram", str(image)), headers=AUTH)
        failure = client.get(_media_url("telegram", outside), headers=AUTH)

    assert success.status_code == 200
    assert success.content == b"telegram-image"
    _assert_uniform_not_found(failure, outside, root)


def test_media_telegram_tgref_download_success_and_failure(tmp_path):
    from backends import telegram

    root = tmp_path / "telegram-media"
    root.mkdir()
    image = root / "downloaded.jpg"
    image.write_bytes(b"downloaded-telegram-image")
    good_ref = "tgref:123:456"
    failed_ref = "tgref:123:999"
    manager = FakeManager()
    manager.get_attachment_path = MagicMock(
        side_effect=lambda proto, attachment_id: (
            image if attachment_id == good_ref else None
        )
    )

    with (
        patch.object(telegram, "_media_dir", return_value=root),
        TestClient(make_app(manager)) as client,
    ):
        success = client.get(_media_url("telegram", good_ref), headers=AUTH)
        failure = client.get(_media_url("telegram", failed_ref), headers=AUTH)

    assert success.status_code == 200
    assert success.content == b"downloaded-telegram-image"
    _assert_uniform_not_found(failure, failed_ref, root)


def test_media_rejects_nested_traversal_and_symlink_escape(tmp_path):
    import backend

    root = tmp_path / "attachments"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("never disclose")
    symlink = root / "innocent.jpg"
    try:
        symlink.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink not supported: {exc}")
    manager = FakeManager()
    traversal = "safe/../../nested/../../../secret.txt"
    manager.get_attachment_path = MagicMock(
        side_effect=lambda proto, attachment_id: (
            symlink if attachment_id == "innocent.jpg" else outside
        )
    )
    with (
        patch.object(backend, "SIGNAL_CLI_ATTACHMENTS_DIR", root),
        TestClient(make_app(manager)) as client,
    ):
        traversal_response = client.get(_media_url("signal", traversal), headers=AUTH)
        symlink_response = client.get(
            _media_url("signal", "innocent.jpg"), headers=AUTH
        )

    _assert_uniform_not_found(traversal_response, traversal, outside)
    _assert_uniform_not_found(symlink_response, symlink, outside)


def test_media_signal_serves_file_inside_configured_root_and_rejects_outside(tmp_path):
    import backend

    root = tmp_path / "attachments"
    root.mkdir()
    image = root / "photo.jpg"
    image.write_bytes(b"native-high-resolution")
    outside = tmp_path / "private.jpg"
    outside.write_bytes(b"private")
    manager = FakeManager(
        paths={
            ("signal", "photo.jpg"): image,
            ("signal", "private.jpg"): outside,
        }
    )
    with (
        patch.object(backend, "SIGNAL_CLI_ATTACHMENTS_DIR", root),
        TestClient(make_app(manager)) as client,
    ):
        success = client.get(_media_url("signal", "photo.jpg"), headers=AUTH)
        failure = client.get(_media_url("signal", "private.jpg"), headers=AUTH)
    assert success.status_code == 200
    assert success.content == b"native-high-resolution"
    _assert_uniform_not_found(failure, outside)


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
    assert [item["text"] for item in body] == ["", "second tie", "later"]
    assert all(
        set(item)
        == {
            "id",
            "text",
            "direction",
            "timestamp",
            "attachment",
            "quote_text",
            "quote_timestamp",
            "quote_author",
            "quote_attachment_id",
            "quote_content_type",
            "quote_thumb_url",
            "status",
            "read",
            "edited",
            "edit_id",
        }
        for item in body
    )
    assert body[0]["attachment"] == {
        "attachment_id": "pic",
        "name": "x.jpg",
        "type": "image/jpeg",
    }
    assert body[1]["id"].isdigit()
    assert body[2]["direction"] == "out"


def test_messages_exposes_delivery_and_edit_state(web_client):
    client, _, db_file = web_client
    import sqlite3

    with sqlite3.connect(db_file) as connection:
        connection.executemany(
            "INSERT INTO messages(protocol, contact_number, text, is_mine, "
            "timestamp, msg_id, msg_type, status, read, edited) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("signal", "alice", "mine", 1, 10, "s1", "text", "read", 1, 1),
                ("signal", "alice", "incoming", 0, 11, "s2", "text", "read", 0, 0),
                ("signal", "alice", "fallback", 1, 12, "s3", "text", None, 0, 0),
            ],
        )

    response = client.get(
        "/api/messages",
        params={"proto": "signal", "contact_id": "alice"},
        headers=AUTH,
    )

    assert response.status_code == 200
    mine, incoming, fallback = response.json()
    assert (mine["status"], mine["read"], mine["edited"], mine["edit_id"]) == (
        "read",
        True,
        True,
        "s1",
    )
    assert incoming["status"] is None
    assert (incoming["read"], incoming["edited"]) == (False, False)
    assert fallback["status"] == "sent"


@pytest.mark.parametrize(
    ("protocol", "is_mine", "msg_id", "msg_type", "status", "expected"),
    [
        ("signal", 1, None, "text", "sent", "1234"),
        ("whatsapp", 1, None, "text", "sent", None),
        ("telegram", 1, None, "text", "sent", None),
        ("whatsapp", 1, "wa-1", "image", "sent", None),
        ("telegram", 0, "10", "text", "read", None),
        ("signal", 1, "s-pending", "text", "pending", None),
        ("signal", 1, "s-failed", "text", "failed", None),
    ],
)
def test_messages_edit_id_rules(
    web_client, protocol, is_mine, msg_id, msg_type, status, expected
):
    client, _, db_file = web_client
    import sqlite3

    with sqlite3.connect(db_file) as connection:
        connection.execute(
            "INSERT INTO messages(protocol, contact_number, text, is_mine, "
            "timestamp, msg_id, msg_type, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (protocol, "alice", "message", is_mine, 1234, msg_id, msg_type, status),
        )

    response = client.get(
        "/api/messages",
        params={"proto": protocol, "contact_id": "alice"},
        headers=AUTH,
    )

    assert response.status_code == 200
    assert response.json()[0]["edit_id"] == expected


def test_messages_exposes_persisted_quote_fields(web_client):
    client, _, db_file = web_client
    import sqlite3

    with sqlite3.connect(db_file) as connection:
        connection.execute(
            "INSERT INTO messages(protocol, contact_number, text, is_mine, "
            "timestamp, quote_text, quote_timestamp, quote_author) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("signal", "alice", "reply", 0, 20, "quoted", 10, "alice"),
        )

    response = client.get(
        "/api/messages",
        params={"proto": "signal", "contact_id": "alice"},
        headers=AUTH,
    )

    assert response.status_code == 200
    message = response.json()[0]
    assert {
        key: message[key] for key in ("quote_text", "quote_timestamp", "quote_author")
    } == {
        "quote_text": "quoted",
        "quote_timestamp": 10,
        "quote_author": "alice",
    }


@pytest.mark.parametrize("protocol", ["signal", "telegram", "whatsapp"])
def test_messages_never_serializes_image_placeholder_text(web_client, protocol):
    client, _, db_file = web_client
    import sqlite3

    with sqlite3.connect(db_file) as connection:
        connection.execute(
            "INSERT INTO messages(protocol, contact_number, text, is_mine, "
            "timestamp, msg_type, attachment_id, attachment_info, content_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                protocol,
                "alice",
                "Immagine: upload-random.png",
                1,
                1,
                "image",
                "photo.png",
                "upload-random.png",
                "image/png",
            ),
        )

    response = client.get(
        "/api/messages",
        params={"proto": protocol, "contact_id": "alice"},
        headers=AUTH,
    )

    assert response.status_code == 200
    assert response.json()[0]["text"] == ""
    assert response.json()[0]["attachment"]["type"] == "image/png"


def test_web_ui_static_contracts():
    source = Path("web/static/app.js").read_text()
    assert 'elements.cancelReply.addEventListener("click", cancelReply)' in source
    assert "state.replyTo = null" in source

    css = Path("web/static/style.css").read_text()
    assert ".contact-copy { min-width: 0; overflow: hidden; }" in css
    assert ".contact-main .contact-name { min-width: 0; flex: 1 1 auto; }" in css
    assert "grid-template-columns: 42px minmax(0, 1fr) auto auto;" in css
    assert "white-space: nowrap;" in css

    _run_reconciliation_node("""
const image = { text: "", attachment: { name: "photo.png", type: "image/png" } };
assert.equal(replyQuoteMessage(image), "photo.png");
""")


def test_messages_infers_missing_image_content_type(web_client):
    client, _, db_file = web_client
    import sqlite3

    with sqlite3.connect(db_file) as connection:
        connection.executemany(
            "INSERT INTO messages(protocol, contact_number, text, is_mine, timestamp, attachment_id, attachment_info, content_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "whatsapp",
                    "alice",
                    "Media: http://localhost:3000/api/files/photo.jpeg?download=1",
                    0,
                    1,
                    "http://localhost:3000/api/files/photo.jpeg?download=1",
                    None,
                    None,
                ),
                ("whatsapp", "alice", "unknown", 0, 2, "media.unknown", "Media", None),
                (
                    "whatsapp",
                    "alice",
                    "existing",
                    0,
                    3,
                    "photo.png",
                    "Photo",
                    "application/custom",
                ),
                (
                    "signal",
                    "bob",
                    "Media: att-1",
                    0,
                    4,
                    "att-1",
                    None,
                    None,
                ),
            ],
        )

    response = client.get(
        "/api/messages",
        params={"proto": "whatsapp", "contact_id": "alice"},
        headers=AUTH,
    )

    assert response.status_code == 200
    messages = response.json()
    attachments = [message["attachment"] for message in messages]
    assert messages[0]["text"] == ""
    assert attachments[0]["type"] == "image/jpeg"
    assert attachments[0]["name"] == "photo.jpeg"
    assert attachments[1]["type"] is None
    assert attachments[2]["type"] == "application/custom"

    # Non-WhatsApp captions starting with "Media: " must be preserved.
    response = client.get(
        "/api/messages",
        params={"proto": "signal", "contact_id": "bob"},
        headers=AUTH,
    )
    assert response.status_code == 200
    assert response.json()[0]["text"] == "Media: att-1"


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
