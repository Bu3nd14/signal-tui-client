"""``POST /api/send`` restituisce gli id reali creati dal backend.

Il client web li usa per accoppiare in modo deterministico la bolla
optimistic alla riga reale (niente doppione se le signature divergono, ad
esempio immagine con caption).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from models import ChatContact
from tests.test_web_plugin import AUTH, FakeManager, make_app

_PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


def _manager() -> FakeManager:
    return FakeManager([ChatContact("alice", "Alice", "signal")])


def test_text_send_returns_backend_message_id():
    manager = _manager()
    manager.send_message_sync = MagicMock(return_value=1700000000123)
    with (
        patch("server.api.ChatAPI.publish_web_push"),
        TestClient(make_app(manager)) as client,
    ):
        response = client.post(
            "/api/send",
            json={
                "protocol": "signal",
                "contact_id": "alice",
                "text": "ciao",
                "client_msg_id": "cid-ids",
            },
            headers=AUTH,
        )
        replay = client.post(
            "/api/send",
            json={
                "protocol": "signal",
                "contact_id": "alice",
                "text": "ciao",
                "client_msg_id": "cid-ids",
            },
            headers=AUTH,
        )
    assert response.json() == {"ok": True, "message_ids": ["1700000000123"]}
    # Design §8.3: il replay idempotente NON espone id.
    assert replay.json() == {"ok": True, "duplicate": True}
    assert manager.send_message_sync.call_count == 1


def test_text_send_without_backend_id_returns_empty_list():
    manager = _manager()
    manager.send_message_sync = MagicMock(return_value=None)
    with (
        patch("server.api.ChatAPI.publish_web_push"),
        TestClient(make_app(manager)) as client,
    ):
        response = client.post(
            "/api/send",
            json={"protocol": "signal", "contact_id": "alice", "text": "ciao"},
            headers=AUTH,
        )
    assert response.json() == {"ok": True, "message_ids": []}


def test_attachment_send_returns_backend_message_ids():
    manager = _manager()
    manager.send_attachments_sync = MagicMock(return_value=["1700000000123"])
    with (
        patch("server.api.ChatAPI.publish_web_push"),
        TestClient(make_app(manager)) as client,
    ):
        response = client.post(
            "/api/send",
            data={"protocol": "signal", "contact_id": "alice", "text": "caption"},
            files={"file": ("photo.png", _PNG_1X1, "image/png")},
            headers=AUTH,
        )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "message_ids": ["1700000000123"]}
