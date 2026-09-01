from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

TOKEN = "correct-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeManager:
    def __init__(self, path=None):
        self.path = path
        self.path_calls = []

    def get_attachment_path(self, proto, attachment_id):
        self.path_calls.append((proto, attachment_id))
        return self.path

    def list_contacts(self):
        return []

    def get(self, proto):
        return None

    def send_message_sync(self, *args, **kwargs):
        return None

    def send_attachment_sync(self, *args, **kwargs):
        return None


def make_app(manager, service=None):
    from web.api import create_api_router
    from web.auth import install_auth

    app = FastAPI()
    app.state.manager = manager
    if service is not None:
        app.state.transcription_service = service
    install_auth(app, TOKEN)
    app.include_router(create_api_router())
    return app


def test_post_returns_503_without_service():
    with TestClient(make_app(FakeManager())) as client:
        response = client.post(
            "/api/transcribe",
            json={"protocol": "signal", "attachment_id": "audio-1"},
            headers=AUTH,
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "Trascrizione non configurata"


def test_post_returns_cached_transcription():
    service = MagicMock()
    service.status.return_value = {"status": "ok", "text": "già pronta"}
    manager = FakeManager()

    with TestClient(make_app(manager, service)) as client:
        response = client.post(
            "/api/transcribe",
            json={"protocol": "signal", "attachment_id": "audio-1"},
            headers=AUTH,
        )

    assert response.status_code == 200
    assert response.json() == {"status": "done", "text": "già pronta"}
    service.submit.assert_not_called()
    assert manager.path_calls == []


def test_post_returns_404_when_attachment_cannot_be_resolved():
    service = MagicMock()
    service.status.return_value = None

    with TestClient(make_app(FakeManager(path=None), service)) as client:
        response = client.post(
            "/api/transcribe",
            json={"protocol": "whatsapp", "attachment_id": "audio-2"},
            headers=AUTH,
        )

    assert response.status_code == 404
    service.submit.assert_not_called()


def test_post_submits_resolved_attachment(tmp_path: Path):
    audio = tmp_path / "audio.ogg"
    audio.write_bytes(b"audio")
    service = MagicMock()
    service.status.return_value = None

    with TestClient(make_app(FakeManager(path=audio), service)) as client:
        response = client.post(
            "/api/transcribe",
            json={"protocol": "telegram", "attachment_id": "folder/audio-3"},
            headers=AUTH,
        )

    assert response.status_code == 200
    assert response.json() == {"status": "pending"}
    service.submit.assert_called_once_with("telegram", "folder/audio-3", audio)


@pytest.mark.parametrize(
    "record",
    [
        {"status": "ok", "text": "pronta"},
        {"status": "pending"},
        {"status": "failed", "error": "boom"},
        None,
    ],
    ids=["ok", "pending", "failed", "unknown"],
)
def test_get_returns_transcription_status(record):
    service = MagicMock()
    service.status.return_value = record

    with TestClient(make_app(FakeManager(), service)) as client:
        response = client.get(
            "/api/transcribe/signal/folder/audio-1", headers=AUTH
        )

    assert response.status_code == 200
    assert response.json() == (record or {"status": "unknown"})
    service.status.assert_called_once_with("signal", "folder/audio-1")


@pytest.mark.parametrize(
    "payload",
    [
        {"protocol": "invalid", "attachment_id": "audio-1"},
        {"protocol": "signal", "attachment_id": "   "},
    ],
    ids=["invalid-protocol", "empty-attachment-id"],
)
def test_post_rejects_invalid_request(payload):
    service = MagicMock()

    with TestClient(make_app(FakeManager(), service)) as client:
        response = client.post("/api/transcribe", json=payload, headers=AUTH)

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid request"
    service.status.assert_not_called()
