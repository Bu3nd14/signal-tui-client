from __future__ import annotations

import io
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import backend
from web.api import create_api_router


def _client(monkeypatch, tmp_path: Path, files: dict[str, Path]) -> TestClient:
    root = tmp_path / "media"
    root.mkdir(exist_ok=True)
    monkeypatch.setattr(backend, "SIGNAL_CLI_ATTACHMENTS_DIR", root)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    manager = SimpleNamespace(
        get_attachment_path=lambda _proto, attachment_id: files.get(attachment_id),
    )
    app = FastAPI()
    app.state.manager = manager
    app.include_router(create_api_router())
    return TestClient(app)


def _url(attachment_id: str, width: int = 480) -> str:
    return f"/api/media/signal/{quote(attachment_id, safe='')}?w={width}"


def test_thumbnail_is_small_jpeg_and_second_request_is_cached(monkeypatch, tmp_path):
    source = tmp_path / "media" / "large.bmp"
    source.parent.mkdir()
    Image.new("RGB", (2400, 1600), "#3a7bac").save(source)
    client = _client(monkeypatch, tmp_path, {source.name: source})

    first = client.get(_url(source.name))

    assert first.status_code == 200
    assert first.headers["content-type"].startswith("image/jpeg")
    assert len(first.content) < source.stat().st_size * 0.05
    with Image.open(io.BytesIO(first.content)) as thumbnail:
        assert max(thumbnail.size) <= 480
    with patch("PIL.Image.open", side_effect=AssertionError("cache miss")):
        second = client.get(_url(source.name))
    assert second.content == first.content


def test_non_thumbnail_media_and_invalid_width_serve_original(monkeypatch, tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    gif = root / "animated.gif"
    Image.new("RGB", (10, 10), "red").save(gif, "GIF")
    heic = root / "photo.heic"
    video = root / "clip.mp4"
    heic.write_bytes(b"heic-original")
    video.write_bytes(b"video-original")
    client = _client(
        monkeypatch,
        tmp_path,
        {path.name: path for path in (gif, heic, video)},
    )

    for path in (gif, heic, video):
        response = client.get(_url(path.name))
        assert response.content == path.read_bytes()
        assert response.headers["cache-control"] == "private, max-age=86400"
    assert client.get(_url(gif.name, 241)).content == gif.read_bytes()


def test_thumbnail_request_keeps_path_validation(monkeypatch, tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    outside = tmp_path / "outside.jpg"
    Image.new("RGB", (20, 20)).save(outside)
    client = _client(monkeypatch, tmp_path, {"../outside.jpg": outside})

    assert client.get(_url("../outside.jpg")).status_code == 404


def test_concurrent_thumbnail_requests_generate_once(monkeypatch, tmp_path):
    source = tmp_path / "media" / "concurrent.png"
    source.parent.mkdir()
    Image.new("RGB", (1000, 800), "green").save(source)
    client = _client(monkeypatch, tmp_path, {source.name: source})
    real_open = Image.open
    calls = 0

    def slow_open(*args, **kwargs):
        nonlocal calls
        calls += 1
        time.sleep(0.05)
        return real_open(*args, **kwargs)

    with (
        patch("PIL.Image.open", side_effect=slow_open),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        responses = list(
            pool.map(lambda _index: client.get(_url(source.name)), range(2))
        )

    assert [response.status_code for response in responses] == [200, 200]
    assert calls == 1


def test_spa_lazy_negative_cache_and_upload_optimization():
    source = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const media = app.slice(app.indexOf("const MEDIA_CACHE_LIMIT"), app.indexOf("\nfunction attachmentName"));
globalThis.state = { mediaRequests: new Set(), mediaLoads: new Map(), mediaFailures: new Set(), objectUrls: new Set(), mediaCache: new Map(), optimistic: [] };
globalThis.URL = { createObjectURL: () => "blob:thumb", revokeObjectURL() {} };
globalThis.scrollThreadToBottom = () => {};
let fetches = 0;
globalThis.apiFetch = async () => { fetches += 1; throw Object.assign(new Error("404"), { status: 404 }); };
function node(tag) { return { tag, children: [], className: "", append(...items) { this.children.push(...items); }, replaceChildren(...items) { this.children = items; }, querySelector() { return null; }, addEventListener() {}, setAttribute() {}, remove() {} }; }
globalThis.document = { createElement: node, querySelector: () => null };
globalThis.elements = { messages: { parent: { id: "scroll-root" } } };
let callback;
let observed;
globalThis.window = { setTimeout, clearTimeout, IntersectionObserver: class { constructor(cb, options) { callback = cb; assert.equal(options.root, elements.messages.parent); assert.equal(options.rootMargin, "300px"); } observe(target) { observed = target; } unobserve() {} disconnect() {} } };
vm.runInThisContext(media);
const lazy = imageAttachment({ attachment_id: "missing.jpg", name: "missing" }, "signal", "in");
assert.equal(fetches, 0);
callback([{ target: observed, isIntersecting: true }], state.mediaObserver);
(async () => {
  await new Promise(setImmediate);
  assert.equal(fetches, 1);
  await loadImage(node("div"), node("img"), "/api/media/signal/missing.jpg?w=480", "missing.jpg", "in");
  assert.equal(fetches, 1);

  const stage = app.slice(app.indexOf("async function stageAttachment("), app.indexOf("\nasync function submitMessage"));
  globalThis.clearStagedAttachment = () => {};
  globalThis.updateComposer = () => {};
  globalThis.showError = assert.fail;
  globalThis.elements = { attachmentPreview: {}, attachmentPreviewImage: {}, attachmentPreviewName: {} };
  globalThis.File = class { constructor(parts, name, options) { this.size = parts[0].size; this.name = name; this.type = options.type; } };
  let bitmapCalls = 0;
  let encodeCalls = 0;
  globalThis.createImageBitmap = async (file, options) => {
    bitmapCalls += 1;
    assert.equal(options.imageOrientation, "from-image");
    return { width: file.width, height: file.height, close() {} };
  };
  globalThis.document = { createElement: () => ({ getContext: () => ({ fillRect() {}, drawImage() {}, set fillStyle(_value) {} }), toBlob: (cb) => { encodeCalls += 1; cb({ size: 300 * 1024 }); } }) };
  globalThis.URL.createObjectURL = () => "blob:preview";
  state.sending = false;
  vm.runInThisContext(stage);
  await stageAttachment({ type: "image/png", size: 1024 * 1024, name: "large.png", width: 1500, height: 1000 });
  assert.equal(state.stagedAttachment.file.type, "image/jpeg");
  assert.ok(state.stagedAttachment.file.size < 1024 * 1024 * 0.4);
  const smallPng = { type: "image/png", size: 300 * 1024, name: "small.png", width: 1500, height: 1000 };
  await stageAttachment(smallPng);
  assert.equal(state.stagedAttachment.file, smallPng);
  const smallJpeg = { type: "image/jpeg", size: 200 * 1024, name: "small.jpg", width: 1500, height: 1000 };
  await stageAttachment(smallJpeg);
  assert.equal(state.stagedAttachment.file, smallJpeg);
  const largeJpeg = { type: "image/jpeg", size: 2 * 1024 * 1024, name: "large.jpg", width: 2500, height: 1000 };
  await stageAttachment(largeJpeg);
  assert.notEqual(state.stagedAttachment.file, largeJpeg);
  assert.equal(state.stagedAttachment.file.type, "image/jpeg");
  const gif = { type: "image/gif", size: 1000, name: "animated.gif" };
  await stageAttachment(gif);
  assert.equal(state.stagedAttachment.file, gif);
  assert.equal(bitmapCalls, 4);
  assert.equal(encodeCalls, 2);
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
