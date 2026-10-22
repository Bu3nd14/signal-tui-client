"""Cache-busting `version` degli allegati media (design v2, gate red team).

Copre l'helper server `_attachment_version`/`_attach_media_versions` (solo
fast-path locale, anti-traversal, best-effort) e l'uso del token `v=` lato SPA
(URL media versionata + chiave versionata di `state.mediaCache`), con la
separazione M2 fra `mediaFailures` (chiave RAW) e `mediaLoads`/`mediaCache`
(chiave versionata).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import protocols.rpc as signal_rpc
from web.api import (
    _attach_media_versions,
    _attachment_version,
    _thumbnail,
    create_api_router,
)


def _signal_root(tmp_path: Path) -> Path:
    root = tmp_path / "media"
    root.mkdir(exist_ok=True)
    return root


# ── _attachment_version ───────────────────────────────────────────────────────


def test_attachment_version_signal_present_and_missing(tmp_path):
    root = _signal_root(tmp_path)
    source = root / "image.png"
    source.write_bytes(b"abc")
    stat = source.stat()

    assert (
        _attachment_version(None, "signal", root, "image.png")
        == f"{stat.st_mtime_ns}-{stat.st_size}"
    )
    assert _attachment_version(None, "signal", root, "missing.png") is None


def test_attachment_version_rejects_traversal_and_symlink(tmp_path):
    root = _signal_root(tmp_path)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"segreto")

    assert _attachment_version(None, "signal", root, "../outside.png") is None

    link = root / "link.png"
    link.symlink_to(outside)
    assert _attachment_version(None, "signal", root, "link.png") is None


def test_attachment_version_unknown_protocol(tmp_path):
    root = _signal_root(tmp_path)
    (root / "image.png").write_bytes(b"abc")

    assert _attachment_version(None, "matrix", root, "image.png") is None


def test_attachment_version_uses_raw_id_for_query(tmp_path):
    root = _signal_root(tmp_path)
    source = root / "image.png"
    source.write_bytes(b"abc")
    stat = source.stat()

    assert (
        _attachment_version(None, "signal", root, "image.png?token=1")
        == f"{stat.st_mtime_ns}-{stat.st_size}"
    )


def test_attachment_version_telegram_rejects_tgref_and_outside(tmp_path):
    root = tmp_path / "tg-media"
    root.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"x")

    assert _attachment_version(None, "telegram", root, "tgref:42:77") is None
    assert _attachment_version(None, "telegram", root, str(outside)) is None


# ── _attach_media_versions ────────────────────────────────────────────────────


def test_attach_media_versions_is_best_effort_when_root_fails():
    messages = [{"attachment": {"attachment_id": "image.png"}}]

    with patch("web.api._allowed_media_root", side_effect=OSError("mkdir negato")):
        _attach_media_versions(None, "whatsapp", messages)

    assert "version" not in messages[0]["attachment"]


def test_attach_media_versions_resolves_root_once(tmp_path):
    root = tmp_path / "wa-media"
    root.mkdir()

    class FakeWA:
        def __init__(self):
            self.ensure_calls = 0

        def _ensure_media_dir(self):
            self.ensure_calls += 1
            return root

    wa = FakeWA()
    manager = SimpleNamespace(get=lambda _proto: wa)
    messages = [
        {"attachment": {"attachment_id": f"img-{index}.png"}} for index in range(3)
    ]

    _attach_media_versions(manager, "whatsapp", messages)

    assert wa.ensure_calls == 1


def test_whatsapp_version_never_downloads(tmp_path):
    root = tmp_path / "wa-media"
    root.mkdir()
    (root / "photo.png").write_bytes(b"abc")

    class FakeWA:
        def _ensure_media_dir(self):
            return root

    download = MagicMock(side_effect=AssertionError("non deve scaricare"))
    manager = SimpleNamespace(get=lambda _proto: FakeWA(), get_attachment_path=download)
    messages = [{"attachment": {"attachment_id": "photo.png"}}]

    _attach_media_versions(manager, "whatsapp", messages)

    assert messages[0]["attachment"]["version"]
    download.assert_not_called()


# ── endpoint /api/messages ────────────────────────────────────────────────────


def _messages_client(monkeypatch, tmp_path) -> TestClient:
    root = _signal_root(tmp_path)
    monkeypatch.setattr(signal_rpc, "SIGNAL_CLI_ATTACHMENTS_DIR", root)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    manager = SimpleNamespace(get=lambda _proto: None, list_contacts=list)
    app = FastAPI()
    app.state.manager = manager
    app.include_router(create_api_router())
    return TestClient(app)


def _payload(attachment_id: str) -> dict:
    return {"quote_author": "", "attachment": {"attachment_id": attachment_id}}


def test_messages_endpoint_attaches_and_refreshes_versions(monkeypatch, tmp_path):
    root = _signal_root(tmp_path)
    source = root / "image.png"
    source.write_bytes(b"abc")
    files = [_payload("image.png"), _payload("missing.png")]
    client = _messages_client(monkeypatch, tmp_path)

    with patch("web.api._messages", return_value=files):
        first = client.get(
            "/api/messages", params={"proto": "signal", "contact_id": "alice"}
        )
    assert first.status_code == 200
    data = first.json()
    first_version = data[0]["attachment"]["version"]
    assert first_version == f"{source.stat().st_mtime_ns}-{source.stat().st_size}"
    assert "version" not in data[1]["attachment"]

    source.write_bytes(b"a-larger-overwrite")
    with patch("web.api._messages", return_value=files):
        second = client.get(
            "/api/messages", params={"proto": "signal", "contact_id": "alice"}
        )
    assert second.json()[0]["attachment"]["version"] != first_version


# ── endpoint /api/media (v ignorato) ──────────────────────────────────────────


def test_media_endpoint_ignores_version_param(monkeypatch, tmp_path):
    root = _signal_root(tmp_path)
    source = root / "image.png"
    source.write_bytes(b"png")
    monkeypatch.setattr(signal_rpc, "SIGNAL_CLI_ATTACHMENTS_DIR", root)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    manager = SimpleNamespace(
        get_attachment_path=lambda _proto, attachment_id: (
            source if attachment_id == "image.png" else None
        )
    )
    app = FastAPI()
    app.state.manager = manager
    app.include_router(create_api_router())

    with TestClient(app) as client:
        response = client.get("/api/media/signal/image.png?v=qualsiasi&w=480")

    assert response.status_code == 200
    assert response.content == b"png"


# ── B2: digest thumbnail include la size ──────────────────────────────────────


def test_thumbnail_invalidates_on_size_change_with_same_mtime(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr("web.api._attachment_content_type", lambda *_args: None)
    root = _signal_root(tmp_path)
    source = root / "photo.png"
    Image.new("RGB", (800, 600), "#3a7bac").save(source)
    original = source.stat()

    first = _thumbnail(source, "signal", source.name, 480)
    assert first is not None
    assert first.exists()

    pixels = [
        ((index * 37) % 256, (index * 91) % 256, (index * 53) % 256)
        for index in range(800 * 600)
    ]
    noisy = Image.new("RGB", (800, 600))
    noisy.putdata(pixels)
    noisy.save(source)
    assert source.stat().st_size != original.st_size
    os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert source.stat().st_mtime_ns == original.st_mtime_ns

    second = _thumbnail(source, "signal", source.name, 480)

    assert second is not None
    assert second != first
    assert first.exists() and second.exists()


# ── index.html (M1) ───────────────────────────────────────────────────────────


def test_index_html_bumps_app_js_version():
    index = Path("web/static/index.html").read_text(encoding="utf-8")
    assert "app.js?v=127" in index


# ── SPA ───────────────────────────────────────────────────────────────────────


def test_spa_media_version_and_failure_namespaces():
    source = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const media = app.slice(app.indexOf("const MEDIA_CACHE_LIMIT"), app.indexOf("\nfunction attachmentName"));
globalThis.state = {
  mediaRequests: new Set(), mediaLoads: new Map(), mediaFailures: new Set(),
  objectUrls: new Set(), mediaCache: new Map(), optimistic: [],
};
globalThis.URL = { createObjectURL: () => "blob:thumb", revokeObjectURL() {} };
globalThis.scrollThreadToBottom = () => {};
const fetched = [];
globalThis.apiFetch = async (path) => { fetched.push(path); return { status: 200, blob: async () => ({}) }; };
function node(tag) {
  return {
    tag, className: "", textContent: "", children: [], listeners: {}, attributes: {},
    alt: "", width: 0, height: 0, complete: false, naturalWidth: 0, naturalHeight: 0, src: "",
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; },
    querySelector() { return null; },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    setAttribute(name, value) { this.attributes[name] = value; },
    remove() {},
  };
}
globalThis.document = { createElement: node, querySelector: () => null };
globalThis.elements = { messages: { parent: { id: "scroll-root" } } };
let observerCallback;
let observed;
globalThis.window = {
  setTimeout, clearTimeout,
  IntersectionObserver: class {
    constructor(callback, options) {
      observerCallback = callback;
      assert.equal(options.root, elements.messages.parent);
      assert.equal(options.rootMargin, "300px");
    }
    observe(target) { observed = target; }
    unobserve() {}
    disconnect() {}
  },
};
vm.runInThisContext(media);
let modalPath = null;
globalThis.openImageModal = (path) => { modalPath = path; };
const tick = () => new Promise(setImmediate);
const fire = () => observerCallback([{ target: observed, isIntersecting: true }], state.mediaObserver);
(async () => {
  const container = imageAttachment({ attachment_id: "image.png", version: "123-456" }, "signal", "in");
  fire();
  await tick();
  assert.equal(fetched.at(-1), "/api/media/signal/image.png?v=123-456&w=480");
  assert.equal(state.mediaCache.has("image.png|123-456"), true);
  assert.equal(state.mediaCache.has("image.png|999-000"), false);
  assert.notEqual(mediaCacheKey("image.png", "123-456"), mediaCacheKey("image.png", "999-000"));
  container.listeners.click();
  assert.equal(modalPath, "/api/media/signal/image.png?v=123-456");

  const legacy = imageAttachment({ attachment_id: "legacy.png" }, "signal", "in");
  fire();
  await tick();
  assert.equal(fetched.at(-1), "/api/media/signal/legacy.png?w=480");
  assert.equal(state.mediaCache.has("legacy.png"), true);

  globalThis.setDotColor = () => {};
  globalThis.loadContacts = () => {};
  globalThis.COLOR_GREEN = "#00ff00";
  const wsStart = app.indexOf("  socket.onmessage = (event) => {");
  const wsEnd = app.indexOf("  socket.onerror = () => socket.close();", wsStart);
  vm.runInThisContext(app.slice(wsStart, wsEnd).replace("socket.onmessage =", "globalThis.__wsMessage ="));

  globalThis.apiFetch = async () => { throw Object.assign(new Error("404"), { status: 404 }); };
  await fetchImage("/api/media/signal/image.png", "image.png", "image.png|123-456", "in").catch(() => {});
  assert.equal(state.mediaFailures.has("image.png"), true);
  assert.equal(state.mediaFailures.has("image.png|123-456"), false);

  // B1: un successo sulla versione nuova azzera il failure RAW registrato su
  // una versione precedente (altrimenti il re-render mostrerebbe il fallback).
  globalThis.apiFetch = async () => ({ status: 200, blob: async () => ({}) });
  await fetchImage("/api/media/signal/image.png", "image.png", "image.png|999-000", "in");
  assert.equal(state.mediaFailures.has("image.png"), false);
  assert.equal(state.mediaCache.has("image.png|999-000"), true);

  // Il WS handler deve continuare a pulire con l'id RAW.
  globalThis.apiFetch = async () => { throw Object.assign(new Error("404"), { status: 404 }); };
  await fetchImage("/api/media/signal/image.png", "image.png", "image.png|123-456", "in").catch(() => {});
  assert.equal(state.mediaFailures.has("image.png"), true);
  __wsMessage({ data: JSON.stringify({ type: "message", payload: { attachment_id: "image.png", protocol: "signal", contact_id: "alice" } }) });
  assert.equal(state.mediaFailures.has("image.png"), false);
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
