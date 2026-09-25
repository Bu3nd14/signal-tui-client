"""Tests for GET /api/status — real per-protocol connection state."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

TOKEN = "correct-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class StatusManager:
    """Minimal ``BackendManager`` stand-in keyed by protocol → backend."""

    def __init__(self, backends: dict):
        self._backends = backends

    def get(self, protocol):
        return self._backends.get(protocol)


@pytest.fixture
def status_client():
    from web.api import create_api_router
    from web.auth import install_auth

    def make(backends: dict):
        app = FastAPI()
        app.state.manager = StatusManager(backends)
        install_auth(app, TOKEN)
        app.include_router(create_api_router())
        return TestClient(app)

    return make


def test_status_reports_connected_backend_with_zero_contacts(status_client):
    """A backend with no chats yet is still 'connected' — not inferred from contacts."""
    client = status_client(
        {
            "signal": SimpleNamespace(is_connected=True, contacts=[]),
        }
    )
    response = client.get("/api/status", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"signal": True, "whatsapp": False, "telegram": False}


def test_status_reports_unregistered_protocol_as_false(status_client):
    """A protocol with no registered backend at all (e.g. Telegram unconfigured)."""
    client = status_client({"signal": SimpleNamespace(is_connected=True)})
    response = client.get("/api/status", headers=AUTH)
    assert response.json()["telegram"] is False


def test_status_reports_registered_but_not_yet_connected_backend(status_client):
    client = status_client(
        {"whatsapp": SimpleNamespace(is_connected=False, contacts=[])}
    )
    response = client.get("/api/status", headers=AUTH)
    assert response.json()["whatsapp"] is False


def test_status_requires_auth(status_client):
    client = status_client({})
    response = client.get("/api/status")
    assert response.status_code == 401
