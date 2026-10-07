from __future__ import annotations

import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web import avatar_cache
from web.api import create_api_router

_JPEG = b"\xff\xd8\xff\xe0fake-jpeg"
_AVATAR_URL = "https://pps.whatsapp.net/avatar.jpg"


class FakeRest:
    def __init__(self, url: str | None, status: int = 200):
        self.url = url
        self.last_status = status
        self.calls = 0

    def get_profile_picture_url(self, contact_id: str) -> str | None:
        self.calls += 1
        return self.url


def _client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rest: FakeRest
) -> TestClient:
    monkeypatch.setattr(avatar_cache, "CACHE_DIR", tmp_path)
    backend = SimpleNamespace(_rest=rest)
    manager = SimpleNamespace(
        get=lambda protocol: backend if protocol == "whatsapp" else None
    )
    app = FastAPI()
    app.state.manager = manager
    app.include_router(create_api_router())
    return TestClient(app)


def _avatar_request(contact_id: str = "123@c.us", proto: str = "whatsapp") -> str:
    return f"/api/contact-avatar?proto={proto}&contact_id={contact_id}"


def test_avatar_downloads_and_serves_jpeg(monkeypatch, tmp_path):
    rest = FakeRest(_AVATAR_URL)
    client = _client(monkeypatch, tmp_path, rest)

    with patch.object(avatar_cache, "_download", return_value=_JPEG) as download:
        response = client.get(_avatar_request())

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == _JPEG
    assert response.headers["cache-control"] == "private, max-age=86400"
    download.assert_called_once_with(_AVATAR_URL)
    assert rest.calls == 1
    assert avatar_cache._avatar_path("123@c.us").read_bytes() == _JPEG


def test_avatar_without_photo_writes_negative_marker(monkeypatch, tmp_path):
    rest = FakeRest(None, status=200)
    client = _client(monkeypatch, tmp_path, rest)

    first = client.get(_avatar_request())
    second = client.get(_avatar_request())

    assert first.status_code == 404
    assert first.json() == {"detail": "No profile picture"}
    assert second.status_code == 404
    assert rest.calls == 1
    path = avatar_cache._avatar_path("123@c.us")
    assert avatar_cache._no_photo_marker(path).is_file()


def test_avatar_errors_do_not_write_negative_marker(monkeypatch, tmp_path):
    rest = FakeRest(None, status=0)
    client = _client(monkeypatch, tmp_path, rest)

    assert client.get(_avatar_request()).status_code == 404

    path = avatar_cache._avatar_path("123@c.us")
    assert not avatar_cache._no_photo_marker(path).exists()


@pytest.mark.parametrize("proto", ["signal", "telegram"])
def test_avatar_rejects_other_protocols(monkeypatch, tmp_path, proto):
    client = _client(monkeypatch, tmp_path, FakeRest(_AVATAR_URL))

    assert client.get(_avatar_request(proto=proto)).status_code == 404


def test_avatar_rejects_empty_contact_id(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, FakeRest(_AVATAR_URL))

    assert client.get(_avatar_request(contact_id="")).status_code == 400


def test_avatar_cache_hit_and_ttl_refresh(monkeypatch, tmp_path):
    monkeypatch.setattr(avatar_cache, "CACHE_DIR", tmp_path)
    rest = FakeRest(_AVATAR_URL)

    with patch.object(avatar_cache, "_download", return_value=b"one") as download:
        first = avatar_cache.resolve_whatsapp_avatar(rest, "1@c.us")
        second = avatar_cache.resolve_whatsapp_avatar(rest, "1@c.us")

    assert first is not None and first == second
    assert rest.calls == 1
    download.assert_called_once()

    beyond_ttl = time.time() + avatar_cache.AVATAR_TTL_SECONDS + 1
    with patch.object(avatar_cache, "_download", return_value=b"two") as download2:
        refreshed = avatar_cache.resolve_whatsapp_avatar(rest, "1@c.us", now=beyond_ttl)

    assert refreshed is not None
    assert rest.calls == 2
    download2.assert_called_once()
    assert refreshed.read_bytes() == b"two"


def test_concurrent_avatar_requests_fetch_once(monkeypatch, tmp_path):
    monkeypatch.setattr(avatar_cache, "CACHE_DIR", tmp_path)
    rest = FakeRest(_AVATAR_URL)

    def slow_picture(contact_id: str) -> str | None:
        rest.calls += 1
        time.sleep(0.05)
        return rest.url

    rest.get_profile_picture_url = slow_picture

    with (
        patch.object(avatar_cache, "_download", return_value=_JPEG),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        results = list(
            pool.map(
                lambda _index: avatar_cache.resolve_whatsapp_avatar(rest, "x@c.us"),
                range(2),
            )
        )

    assert all(result is not None for result in results)
    assert rest.calls == 1


def test_avatar_download_failure_does_not_poison_negative_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(avatar_cache, "CACHE_DIR", tmp_path)
    rest = FakeRest(_AVATAR_URL)

    with patch.object(avatar_cache, "_download", side_effect=OSError("boom")):
        assert avatar_cache.resolve_whatsapp_avatar(rest, "1@c.us") is None

    path = avatar_cache._avatar_path("1@c.us")
    assert not path.exists()
    assert not avatar_cache._no_photo_marker(path).exists()


def test_avatar_missing_backend_returns_no_photo(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, FakeRest(_AVATAR_URL))
    client.app.state.manager = SimpleNamespace(get=lambda protocol: None)

    assert client.get(_avatar_request()).status_code == 404


def test_avatar_lock_is_shared_per_path():
    path = Path("/tmp/example.jpg")
    assert avatar_cache._avatar_lock(path) is avatar_cache._avatar_lock(path)


def test_negative_marker_is_used_within_ttl(monkeypatch, tmp_path):
    monkeypatch.setattr(avatar_cache, "CACHE_DIR", tmp_path)
    rest = FakeRest(None, status=200)

    assert avatar_cache.resolve_whatsapp_avatar(rest, "1@c.us") is None
    assert rest.calls == 1

    # Entro il TTL del marker: nessuna chiamata a WAHA.
    assert (
        avatar_cache.resolve_whatsapp_avatar(
            rest, "1@c.us", now=time.time() + avatar_cache.NO_PHOTO_TTL_SECONDS - 1
        )
        is None
    )
    assert rest.calls == 1

    # Oltre il TTL: WAHA viene interrogato di nuovo.
    assert (
        avatar_cache.resolve_whatsapp_avatar(
            rest, "1@c.us", now=time.time() + avatar_cache.NO_PHOTO_TTL_SECONDS + 1
        )
        is None
    )
    assert rest.calls == 2


def test_static_assets_declare_thread_avatar_and_stealth():
    index = Path("web/static/index.html").read_text(encoding="utf-8")
    assert 'id="thread-avatar"' in index
    assert 'id="stealth-toggle"' in index
    assert "style.css?v=68" in index
    assert "app.js?v=120" in index

    app = Path("web/static/app.js").read_text(encoding="utf-8")
    assert "STEALTH_KEY" in app
    assert "function renderThreadAvatar(" in app


def test_stealth_disables_contact_avatar_url_in_node():
    source = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const block = app.slice(
  app.indexOf("function contactInitial("),
  app.indexOf("function setupContactAvatar("),
);
vm.runInThisContext(block);
globalThis.state = { stealth: true };
assert.equal(contactAvatarUrl({ protocol: "whatsapp", id: "1@c.us" }), null);
globalThis.state = { stealth: false };
assert.equal(
  contactAvatarUrl({ protocol: "whatsapp", id: "1@c.us" }),
  "/api/contact-avatar?proto=whatsapp&contact_id=1%40c.us",
);
assert.equal(contactAvatarUrl({ protocol: "signal", id: "1@c.us" }), null);
assert.equal(contactAvatarUrl(null), null);
"""
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
