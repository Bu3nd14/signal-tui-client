"""Contract: link-preview and client-log routes equal their operations.

``links.preview`` / ``links.preview_image`` / ``server.client_log`` carry the
behaviour (SSRF guard included); the routes only add headers and delivery.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.link_preview as lp
from server.api import ApiError, ChatAPI
from web.api import create_api_router
from web.auth import install_auth

TOKEN = "links-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


def make():
    app = FastAPI()
    host = SimpleNamespace(manager=SimpleNamespace(), bus=None)
    app.state.api = ChatAPI(host)
    install_auth(app, TOKEN)
    app.include_router(create_api_router())
    return TestClient(app), app.state.api


def _preview(url, image=True):
    return {
        "url": url,
        "title": "T",
        "description": "D",
        "site_name": "S",
        "image_url": "https://cdn.example.com/x.png" if image else None,
    }


def test_preview_matches_operation_and_sets_cache_header(monkeypatch):
    async def fake(url):
        return _preview(url)

    monkeypatch.setattr(lp, "get_preview", fake)
    client, api = make()
    response = client.get(
        "/api/link-preview", params={"url": "https://a.com/x"}, headers=AUTH
    )
    assert response.status_code == 200
    assert response.json() == asyncio.run(
        api.acall_json("links.preview", {"url": "https://a.com/x"})
    )
    assert list(response.json()) == [
        "url",
        "title",
        "description",
        "site_name",
        "has_image",
    ]
    assert response.headers["cache-control"] == "private, max-age=3600"


def test_preview_image_matches_operation_and_headers(monkeypatch):
    async def fake_preview(url):
        return _preview(url)

    async def fake_image(url):
        return PNG, "image/png"

    monkeypatch.setattr(lp, "get_preview", fake_preview)
    monkeypatch.setattr(lp, "get_preview_image", fake_image)
    client, api = make()
    response = client.get(
        "/api/link-preview/image", params={"url": "https://a.com/x"}, headers=AUTH
    )
    blob = asyncio.run(
        api.acall_json("links.preview_image", {"url": "https://a.com/x"})
    )
    assert response.status_code == 200
    assert response.content == PNG
    assert blob["content_type"] == "image/png"
    assert blob["size"] == len(PNG)
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, max-age=86400"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.1/x",
        "http://[::1]/",
        "http://localhost/",
        "ftp://example.com/",
        "https://user:pw@example.com/",
        "https://example.com:8443/",
        "file:///etc/passwd",
        "http://169.254.169.254/latest/meta-data",
    ],
)
def test_blocked_urls_are_refused_by_operations_and_routes(url):
    client, api = make()
    for name in ("links.preview", "links.preview_image"):
        with pytest.raises(ApiError) as caught:
            asyncio.run(api.acall(name, {"url": url}))
        assert (caught.value.code, caught.value.message) == (
            "invalid_request",
            "URL not allowed",
        )
    for route in ("/api/link-preview", "/api/link-preview/image"):
        response = client.get(route, params={"url": url}, headers=AUTH)
        assert response.status_code == 400
        assert response.json() == {"detail": "URL not allowed"}


def test_missing_preview_and_image_are_404(monkeypatch):
    async def none(_url):
        return None

    monkeypatch.setattr(lp, "get_preview", none)
    monkeypatch.setattr(lp, "get_preview_image", none)
    client, _api = make()
    first = client.get(
        "/api/link-preview", params={"url": "https://a.com"}, headers=AUTH
    )
    second = client.get(
        "/api/link-preview/image", params={"url": "https://a.com"}, headers=AUTH
    )
    assert (first.status_code, first.json()) == (404, {"detail": "No preview"})
    assert (second.status_code, second.json()) == (404, {"detail": "No preview image"})


def test_preview_routes_require_auth():
    client, _api = make()
    for route in ("/api/link-preview", "/api/link-preview/image"):
        assert client.get(route, params={"url": "https://a.com"}).status_code == 401


def test_client_log_route_and_operation(caplog):
    client, api = make()
    with caplog.at_level(logging.INFO):
        response = client.post(
            "/api/log", json={"message": "x" * 600, "source": "evil"}, headers=AUTH
        )
        assert response.status_code == 200
        assert response.json() == api.call_json("server.client_log", {"message": "m"})
    lines = [r.getMessage() for r in caplog.records]
    assert "[web-client] " + "x" * 500 in lines  # truncated, source not client-set
    assert "[client] m" in lines
    assert not any("evil" in line for line in lines)
