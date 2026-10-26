"""Tests for the same-origin Gmail image proxy (no network)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from client_harness import api_for
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import gmail
from web.gmail_api import create_gmail_image_router


def _token(url: str) -> str:
    path = gmail.sign_image_url(url)
    return parse_qs(urlsplit(path).query)["token"][0]


def _client() -> TestClient:
    app = FastAPI()
    app.state.api = api_for(SimpleNamespace())
    app.include_router(create_gmail_image_router())
    return TestClient(app)


def test_invalid_token_returns_404():
    response = _client().get("/gmail-image", params={"token": "not-a-token"})

    assert response.status_code == 404


def test_proxy_returns_raster_image_with_safety_headers():
    token = _token("https://images.example/a.png")

    async def fake_fetch(url, max_bytes):
        assert url == "https://images.example/a.png"
        assert max_bytes > 0
        return b"\x89PNG\r\n\x1a\nfake", "image/png"

    with patch("server.link_preview.fetch_external_image", new=fake_fetch):
        response = _client().get("/gmail-image", params={"token": token})

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert response.content.startswith(b"\x89PNG")


def test_proxy_maps_fetch_failure_to_502():
    token = _token("https://images.example/a.png")

    async def fake_fetch(url, max_bytes):
        return None

    with patch("server.link_preview.fetch_external_image", new=fake_fetch):
        response = _client().get("/gmail-image", params={"token": token})

    assert response.status_code == 502
