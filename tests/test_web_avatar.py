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

from models import PROTOCOL_SIGNAL, ChatContact
from protocols import rpc as rpc_module
from protocols.rpc import resolve_avatar_path
from protocols.signal import SignalBackend
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


@pytest.mark.parametrize("proto", ["email"])
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
    assert "app.js?v=125" in index

    app = Path("web/static/app.js").read_text(encoding="utf-8")
    assert "STEALTH_KEY" in app
    assert "function renderThreadAvatar(" in app


def test_thread_avatar_opens_image_modal_only_with_photo():
    app = Path("web/static/app.js").read_text(encoding="utf-8")
    style = Path("web/static/style.css").read_text(encoding="utf-8")

    block = app[
        app.index("function renderThreadAvatar(") : app.index("function protocolIcon(")
    ]
    assert "openImageModal(url" in block
    assert 'avatar.classList.add("has-photo")' in block
    assert 'avatar.classList.remove("has-photo")' in block

    assert ".thread-avatar.has-photo" in style
    assert "cursor: pointer" in style


def test_attach_contact_avatar_calls_on_loaded_from_cache():
    source = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const block = app.slice(
  app.indexOf("async function attachContactAvatar("),
  app.indexOf("function contactAvatarObserverInstance("),
);
vm.runInThisContext(block);
globalThis.state = { avatarCache: new Map([["1@c.us", "blob:cached"]]) };
globalThis.document = { createElement: () => ({ className: "", alt: "", src: "" }) };
let replaced = null;
const avatarEl = { replaceChildren(node) { replaced = node; } };
let loaded = 0;
(async () => {
  await attachContactAvatar(avatarEl, { id: "1@c.us" }, "/api/avatar", () => { loaded += 1; });
  assert.equal(loaded, 1);
  assert.equal(replaced.src, "blob:cached");
})().catch((error) => { console.error(error); process.exit(1); });
"""
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


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
assert.equal(
  contactAvatarUrl({ protocol: "signal", id: "+393357405121" }),
  "/api/contact-avatar?proto=signal&contact_id=%2B393357405121",
);
assert.equal(
  contactAvatarUrl({ protocol: "telegram", id: "1@c.us" }),
  "/api/contact-avatar?proto=telegram&contact_id=1%40c.us",
);
assert.equal(contactAvatarUrl(null), null);
"""
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


_SIGNAL_JPEG = b"\xff\xd8\xff\xe0signal-jpeg"
_SIGNAL_PNG = b"\x89PNG\r\n\x1a\nsignal-png"
_SIGNAL_UUID = "2233ac4d-1b9e-4e0a-9d3f-12ab34cd56ef"


def _signal_client(backend) -> TestClient:
    manager = SimpleNamespace(
        get=lambda protocol: backend if protocol == "signal" else None
    )
    app = FastAPI()
    app.state.manager = manager
    app.include_router(create_api_router())
    return TestClient(app)


def _signal_avatar(client: TestClient, contact_id: str, proto: str = "signal"):
    return client.get(
        "/api/contact-avatar",
        params={"proto": proto, "contact_id": contact_id},
    )


def test_signal_avatar_serves_jpeg(monkeypatch, tmp_path):
    monkeypatch.setattr(rpc_module, "SIGNAL_CLI_AVATARS_DIR", tmp_path)
    (tmp_path / "contact-+393357405121").write_bytes(_SIGNAL_JPEG)
    client = _signal_client(SignalBackend())

    response = _signal_avatar(client, "+393357405121")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == _SIGNAL_JPEG
    assert response.headers["cache-control"] == "private, max-age=86400"


def test_signal_avatar_serves_png(monkeypatch, tmp_path):
    monkeypatch.setattr(rpc_module, "SIGNAL_CLI_AVATARS_DIR", tmp_path)
    (tmp_path / "contact-+393357405121").write_bytes(_SIGNAL_PNG)

    response = _signal_avatar(_signal_client(SignalBackend()), "+393357405121")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == _SIGNAL_PNG


def test_signal_avatar_missing_returns_404(monkeypatch, tmp_path):
    monkeypatch.setattr(rpc_module, "SIGNAL_CLI_AVATARS_DIR", tmp_path)

    response = _signal_avatar(_signal_client(SignalBackend()), "+393357405121")

    assert response.status_code == 404
    assert response.json() == {"detail": "No profile picture"}


def test_signal_avatar_uuid_literal(monkeypatch, tmp_path):
    monkeypatch.setattr(rpc_module, "SIGNAL_CLI_AVATARS_DIR", tmp_path)
    (tmp_path / f"contact-{_SIGNAL_UUID}").write_bytes(_SIGNAL_JPEG)

    response = _signal_avatar(_signal_client(SignalBackend()), _SIGNAL_UUID)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == _SIGNAL_JPEG


def test_signal_avatar_phone_fallback_for_uuid(monkeypatch, tmp_path):
    monkeypatch.setattr(rpc_module, "SIGNAL_CLI_AVATARS_DIR", tmp_path)
    (tmp_path / "contact-+393357405121").write_bytes(_SIGNAL_JPEG)
    backend = SignalBackend()
    backend._set_contacts(
        [
            ChatContact(
                _SIGNAL_UUID,
                "Mario",
                PROTOCOL_SIGNAL,
                extras={"phone": "393357405121"},
            )
        ]
    )

    response = _signal_avatar(_signal_client(backend), _SIGNAL_UUID)

    assert response.status_code == 200
    assert response.content == _SIGNAL_JPEG


def test_signal_avatar_profile_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(rpc_module, "SIGNAL_CLI_AVATARS_DIR", tmp_path)
    (tmp_path / "profile-+393356912240").write_bytes(_SIGNAL_JPEG)

    response = _signal_avatar(_signal_client(SignalBackend()), "+393356912240")

    assert response.status_code == 200
    assert response.content == _SIGNAL_JPEG


def test_signal_avatar_prefers_profile_over_contact(monkeypatch, tmp_path):
    monkeypatch.setattr(rpc_module, "SIGNAL_CLI_AVATARS_DIR", tmp_path)
    profile = b"\xff\xd8\xff\xe0profile"
    contact = b"\xff\xd8\xff\xe0contact"
    (tmp_path / "profile-+393357405121").write_bytes(profile)
    (tmp_path / "contact-+393357405121").write_bytes(contact)

    response = _signal_avatar(_signal_client(SignalBackend()), "+393357405121")

    assert response.status_code == 200
    assert response.content == profile


def test_signal_avatar_rejects_path_traversal(monkeypatch, tmp_path):
    monkeypatch.setattr(rpc_module, "SIGNAL_CLI_AVATARS_DIR", tmp_path)

    response = _signal_avatar(_signal_client(SignalBackend()), "../../etc/passwd")

    assert response.status_code == 404


@pytest.mark.parametrize("identifier", ["", "../x", "a/b", "a\\b", ".."])
def test_resolve_avatar_path_rejects_unsafe(monkeypatch, tmp_path, identifier):
    monkeypatch.setattr(rpc_module, "SIGNAL_CLI_AVATARS_DIR", tmp_path)

    assert resolve_avatar_path(identifier) is None


_TG_JPEG = b"\xff\xd8\xff\xe0telegram-jpeg"
_TG_PNG = b"\x89PNG\r\n\x1a\ntelegram-png"


class FakeTelegramBackend:
    def __init__(self, data: bytes | None):
        self.data = data
        self.calls = 0

    def get_profile_photo_bytes(self, contact_id: str):
        self.calls += 1
        if self.data is None:
            return None
        return self.data, "image/jpeg"


def _telegram_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: FakeTelegramBackend
) -> TestClient:
    monkeypatch.setattr(avatar_cache, "CACHE_DIR", tmp_path)
    manager = SimpleNamespace(
        get=lambda protocol: backend if protocol == "telegram" else None
    )
    app = FastAPI()
    app.state.manager = manager
    app.include_router(create_api_router())
    return TestClient(app)


def test_telegram_avatar_serves_jpeg(monkeypatch, tmp_path):
    backend = FakeTelegramBackend(_TG_JPEG)
    client = _telegram_client(monkeypatch, tmp_path, backend)

    response = client.get(_avatar_request(contact_id="123", proto="telegram"))

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == _TG_JPEG
    assert response.headers["cache-control"] == "private, max-age=86400"
    assert backend.calls == 1


def test_telegram_avatar_serves_png(monkeypatch, tmp_path):
    backend = FakeTelegramBackend(_TG_PNG)
    client = _telegram_client(monkeypatch, tmp_path, backend)

    response = client.get(_avatar_request(contact_id="123", proto="telegram"))

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == _TG_PNG


def test_telegram_avatar_missing_returns_404(monkeypatch, tmp_path):
    backend = FakeTelegramBackend(None)
    client = _telegram_client(monkeypatch, tmp_path, backend)

    response = client.get(_avatar_request(contact_id="123", proto="telegram"))

    assert response.status_code == 404
    assert response.json() == {"detail": "No profile picture"}
    assert backend.calls == 1


def test_telegram_avatar_cache_hit_and_ttl_refresh(monkeypatch, tmp_path):
    monkeypatch.setattr(avatar_cache, "CACHE_DIR", tmp_path)
    backend = FakeTelegramBackend(b"one")

    first = avatar_cache.resolve_telegram_avatar(backend, "123")
    second = avatar_cache.resolve_telegram_avatar(backend, "123")

    assert first is not None and first == second
    assert backend.calls == 1

    backend.data = b"two"
    beyond_ttl = time.time() + avatar_cache.TELEGRAM_AVATAR_TTL_SECONDS + 1
    refreshed = avatar_cache.resolve_telegram_avatar(backend, "123", now=beyond_ttl)

    assert refreshed is not None
    assert refreshed[0] == b"two"
    assert backend.calls == 2


def test_concurrent_telegram_avatar_requests_fetch_once(monkeypatch, tmp_path):
    monkeypatch.setattr(avatar_cache, "CACHE_DIR", tmp_path)

    class SlowBackend:
        def __init__(self) -> None:
            self.calls = 0

        def get_profile_photo_bytes(self, contact_id: str):
            self.calls += 1
            time.sleep(0.05)
            return _TG_JPEG, "image/jpeg"

    backend = SlowBackend()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _index: avatar_cache.resolve_telegram_avatar(backend, "123"),
                range(2),
            )
        )

    assert all(result is not None for result in results)
    assert backend.calls == 1


def test_telegram_negative_marker_used_within_ttl(monkeypatch, tmp_path):
    monkeypatch.setattr(avatar_cache, "CACHE_DIR", tmp_path)
    backend = FakeTelegramBackend(None)

    assert avatar_cache.resolve_telegram_avatar(backend, "123") is None
    assert backend.calls == 1
    path = avatar_cache._avatar_path_for("telegram-avatars", "123")
    assert avatar_cache._no_photo_marker(path).is_file()

    assert (
        avatar_cache.resolve_telegram_avatar(
            backend, "123", now=time.time() + avatar_cache.NO_PHOTO_TTL_SECONDS - 1
        )
        is None
    )
    assert backend.calls == 1

    assert (
        avatar_cache.resolve_telegram_avatar(
            backend, "123", now=time.time() + avatar_cache.NO_PHOTO_TTL_SECONDS + 1
        )
        is None
    )
    assert backend.calls == 2


def test_resolve_avatar_dispatches_telegram(monkeypatch, tmp_path):
    monkeypatch.setattr(avatar_cache, "CACHE_DIR", tmp_path)
    backend = FakeTelegramBackend(_TG_PNG)
    manager = SimpleNamespace(
        get=lambda protocol: backend if protocol == "telegram" else None
    )

    result = avatar_cache.resolve_avatar("telegram", manager, "123")

    assert result == (_TG_PNG, "image/png")
    assert backend.calls == 1
