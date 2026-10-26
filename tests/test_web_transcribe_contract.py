"""Contract: transcription / OpenAI-key routes equal their operations.

The service is core state (``api.transcription``); the routes only translate.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from client_harness import api_for
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.api import ApiError
from web.api import create_api_router
from web.auth import install_auth

TOKEN = "transcribe-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class Manager:
    def __init__(self, path=None):
        self.path = path

    def get_attachment_path(self, proto, attachment_id):
        return self.path

    def get(self, proto):
        return None


def make(manager=None, service=None):
    app = FastAPI()
    app.state.api = api_for(manager or Manager())
    app.state.api.transcription.service = service
    install_auth(app, TOKEN)
    app.include_router(create_api_router())
    return TestClient(app), app.state.api


def test_config_matches_operation():
    client, api = make()
    assert client.get("/api/transcribe/config", headers=AUTH).json() == api.call_json(
        "transcribe.config"
    )
    client, api = make(service=MagicMock())
    response = client.get("/api/transcribe/config", headers=AUTH)
    assert response.json() == api.call_json("transcribe.config") == {"enabled": True}


@pytest.mark.parametrize("record", [None, {"status": "ok", "text": "t"}])
def test_status_matches_operation(record):
    service = MagicMock()
    service.status.return_value = record
    client, api = make(service=service)
    response = client.get("/api/transcribe/signal/a/b.ogg", headers=AUTH)
    assert response.json() == api.call_json(
        "transcribe.status", {"protocol": "signal", "attachment_id": "a/b.ogg"}
    )


def test_unconfigured_is_503_with_the_same_detail():
    client, api = make()
    response = client.post(
        "/api/transcribe",
        json={"protocol": "signal", "attachment_id": "x"},
        headers=AUTH,
    )
    assert (response.status_code, response.json()) == (
        503,
        {"detail": "Trascrizione non configurata"},
    )
    with pytest.raises(ApiError) as caught:
        api.call("transcribe.status", {"protocol": "signal", "attachment_id": "x"})
    assert caught.value.code == "backend_down"


def test_submit_confines_the_audio_to_the_media_root(tmp_path: Path, monkeypatch):
    root = tmp_path / "media"
    root.mkdir()
    outside = tmp_path / "secret.ogg"
    outside.write_bytes(b"x")
    monkeypatch.setattr("server.media._allowed_media_root", lambda _m, _p: root)
    service = MagicMock()
    service.status.return_value = None
    client, _api = make(Manager(outside), service)
    response = client.post(
        "/api/transcribe",
        json={"protocol": "signal", "attachment_id": "x"},
        headers=AUTH,
    )
    assert (response.status_code, response.json()) == (
        404,
        {"detail": "Allegato non trovato"},
    )
    service.submit.assert_not_called()


@pytest.mark.parametrize("key", [None, 5, "", "  ", "abc", "pk-1"])
def test_openai_key_rejects_invalid_values(key, tmp_path, monkeypatch):
    monkeypatch.setattr("protocols.config.PROJECT_DIR", tmp_path)
    client, _api = make()
    response = client.post(
        "/api/config/openai-key", json={"api_key": key}, headers=AUTH
    )
    assert (response.status_code, response.json()) == (
        400,
        {"detail": "Chiave non valida"},
    )
    assert not (tmp_path / "config.json").exists()


def test_openai_key_is_saved_and_the_service_restarted(tmp_path, monkeypatch):
    monkeypatch.setattr("protocols.config.PROJECT_DIR", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"other": 1}))
    old = MagicMock()
    new = MagicMock()
    monkeypatch.setattr(
        "transcription.service.TranscriptionService", lambda *_a, **_k: new
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client, api = make(service=old)
    response = client.post(
        "/api/config/openai-key", json={"api_key": " sk-test "}, headers=AUTH
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert json.loads((tmp_path / "config.json").read_text()) == {
        "other": 1,
        "openai_api_key": "sk-test",
    }
    old.stop.assert_called_once()
    assert api.transcription.service is new
    assert client.get("/api/transcribe/config", headers=AUTH).json() == {
        "enabled": True
    }


def test_openai_key_save_and_init_failures(tmp_path, monkeypatch):
    client, api = make()
    monkeypatch.setattr("protocols.config.PROJECT_DIR", tmp_path / "missing")
    response = client.post(
        "/api/config/openai-key", json={"api_key": "sk-x"}, headers=AUTH
    )
    assert (response.status_code, response.json()) == (
        500,
        {"detail": "Errore di salvataggio"},
    )
    monkeypatch.setattr("protocols.config.PROJECT_DIR", tmp_path)
    api.transcription.init = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    response = client.post(
        "/api/config/openai-key", json={"api_key": "sk-x"}, headers=AUTH
    )
    assert (response.status_code, response.json()) == (
        500,
        {"detail": "Errore di inizializzazione"},
    )


def test_host_init_without_key_leaves_no_service(monkeypatch):
    monkeypatch.setattr("transcription.config.get_openai_api_key", lambda: None)
    _client, api = make(service=MagicMock())
    assert api.transcription.init() is None
    assert api.transcription.service is None


def test_host_stop_is_idempotent():
    service = MagicMock()
    _client, api = make(service=service)
    api.transcription.stop()
    api.transcription.stop()
    service.stop.assert_called_once()
    assert api.transcription.service is None
