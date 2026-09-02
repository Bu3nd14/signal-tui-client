from __future__ import annotations

import io
import mimetypes
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

import protocols.rpc as signal_rpc
from web.api import create_api_router


def _client(monkeypatch, tmp_path: Path, files: dict[str, Path]) -> TestClient:
    root = tmp_path / "media"
    root.mkdir(exist_ok=True)
    monkeypatch.setattr(signal_rpc, "SIGNAL_CLI_ATTACHMENTS_DIR", root)
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

    for path in (gif, heic):
        response = client.get(_url(path.name))
        assert response.content == path.read_bytes()
        assert response.headers["cache-control"] == "private, max-age=86400"
    with patch("web.video_thumbs._video_thumbnail", return_value=None):
        response = client.get(_url(video.name))
    assert response.status_code == 422
    assert response.json() == {"detail": "Video thumbnail unavailable"}
    assert client.get(_url(gif.name, 241)).content == gif.read_bytes()


def test_m4a_media_uses_audio_mp4_content_type(monkeypatch, tmp_path):
    source = tmp_path / "media" / "voice.m4a"
    source.parent.mkdir()
    source.write_bytes(b"m4a")
    client = _client(monkeypatch, tmp_path, {source.name: source})

    response = client.get(f"/api/media/signal/{source.name}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mp4"


def test_m4a_media_ignores_mimetypes_contamination(monkeypatch, tmp_path):
    source = tmp_path / "media" / "voice.m4a"
    source.parent.mkdir()
    source.write_bytes(b"m4a")
    client = _client(monkeypatch, tmp_path, {source.name: source})
    previous = mimetypes.guess_type(source.name)[0]
    mimetypes.add_type("audio/m4a", ".m4a")

    try:
        response = client.get(f"/api/media/signal/{source.name}")
    finally:
        if previous is None:
            mimetypes.types_map.pop(".m4a", None)
        else:
            mimetypes.add_type(previous, ".m4a")

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mp4"


def test_aac_media_keeps_audio_aac_content_type(monkeypatch, tmp_path):
    source = tmp_path / "media" / "voice.aac"
    source.parent.mkdir()
    source.write_bytes(b"aac")
    client = _client(monkeypatch, tmp_path, {source.name: source})

    response = client.get(f"/api/media/signal/{source.name}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/aac"


def test_oga_media_uses_audio_ogg_content_type(monkeypatch, tmp_path):
    source = tmp_path / "media" / "voice.oga"
    source.parent.mkdir()
    source.write_bytes(b"oga")
    client = _client(monkeypatch, tmp_path, {source.name: source})

    response = client.get(f"/api/media/signal/{source.name}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/ogg"


def test_non_thumbnail_image_keeps_guessed_content_type(monkeypatch, tmp_path):
    source = tmp_path / "media" / "image.png"
    source.parent.mkdir()
    source.write_bytes(b"png")
    client = _client(monkeypatch, tmp_path, {source.name: source})

    response = client.get(f"/api/media/signal/{source.name}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


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

  const helper = app.slice(app.indexOf("function mediaKindFromMime("), app.indexOf("\nfunction clearStagedAttachment"));
  const stage = helper + "\n" + app.slice(app.indexOf("async function stageAttachment("), app.indexOf("\nasync function submitMessage"));
  globalThis.clearStagedAttachment = () => {};
  globalThis.updateComposer = () => {};
  const errors = [];
  globalThis.showError = (message) => errors.push(message);
  globalThis.elements = { attachmentPreview: {}, attachmentPreviewImage: {}, attachmentPreviewName: {} };
  globalThis.File = class { constructor(parts, name, options) { this.size = parts[0].size; this.name = name; this.type = options.type; } };
  let bitmapCalls = 0;
  let encodeCalls = 0;
  globalThis.createImageBitmap = async (file, options) => {
    bitmapCalls += 1;
    assert.equal(options.imageOrientation, "from-image");
    return { width: file.width, height: file.height, close() {} };
  };
  const canvasBlobs = [];
  const previewSources = [];
  globalThis.document = { createElement: () => ({ getContext: () => ({ fillRect() {}, drawImage() {}, set fillStyle(_value) {} }), toBlob: (cb) => { encodeCalls += 1; const blob = { size: 300 * 1024, fromCanvas: true }; canvasBlobs.push(blob); cb(blob); } }) };
  globalThis.URL.createObjectURL = (blob) => { previewSources.push(blob); return "blob:preview"; };
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
  const gif = { type: "image/gif", size: 1000, name: "animated.gif", width: 320, height: 240 };
  await stageAttachment(gif);
  assert.equal(state.stagedAttachment.file, gif);
  const webp = { type: "image/webp", size: 1000, name: "image.webp", width: 640, height: 480 };
  await stageAttachment(webp);
  assert.equal(state.stagedAttachment.file, webp);
  assert.equal(bitmapCalls, 6);
  assert.equal(encodeCalls, 6);
  assert.deepEqual(previewSources, canvasBlobs);
  assert.ok(previewSources.every((blob) => blob.fromCanvas));
  assert.ok(!previewSources.includes(gif));
  assert.ok(!previewSources.includes(webp));

  const stagedBeforeInvalidImage = state.stagedAttachment;
  globalThis.createImageBitmap = async () => { throw new Error("invalid image"); };
  await stageAttachment({ type: "image/gif", size: 1000, name: "invalid.gif" });
  assert.equal(state.stagedAttachment, stagedBeforeInvalidImage);
  assert.deepEqual(errors, ["Impossibile elaborare l'immagine."]);
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_spa_video_thumbnail_badge_dispatch_and_text_fallback():
    source = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const media = app.slice(app.indexOf("const MEDIA_CACHE_LIMIT"), app.indexOf("\nfunction replyAuthor"));
function node(tag) {
  return {
    tag, className: "", textContent: "", children: [], listeners: {}, attributes: {}, parentNode: null,
    append(...items) { for (const item of items) { item.parentNode = this; this.children.push(item); } },
    replaceChildren(...items) { this.children = items; },
    replaceWith(item) {
      this.replacement = item;
      if (this.parentNode) {
        const index = this.parentNode.children.indexOf(this);
        this.parentNode.children[index] = item;
        item.parentNode = this.parentNode;
      }
    },
    querySelector(selector) { return this.children.find((item) => `.${item.className}` === selector) || null; },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    setAttribute(name, value) { this.attributes[name] = value; },
    remove() {},
  };
}
globalThis.document = { createElement: node };
globalThis.elements = { messages: { parent: null } };
globalThis.window = { setTimeout, clearTimeout, open: assert.fail };
globalThis.URL = { createObjectURL: () => "blob:thumb", revokeObjectURL() {} };
globalThis.showError = assert.fail;
globalThis.state = {
  mediaRequests: new Set(), mediaLoads: new Map(), mediaFailures: new Set(),
  objectUrls: new Set(), fileTabUrls: new Map(), mediaCache: new Map(), optimistic: [],
};
let status = 422;
globalThis.apiFetch = async () => { throw Object.assign(new Error(String(status)), { status }); };
vm.runInThisContext(media);

;(async () => {
  for (const code of [422, 404]) {
    status = code;
    const id = `broken-${code}.mp4`;
    const card = videoThumbAttachment({ attachment_id: id, name: "clip.mp4", media_kind: "video" }, "signal", "in");
    const badge = card.children.find((item) => item.className === "attachment-video-badge");
    assert.equal(badge.textContent, "▶");
    const bubble = node("div");
    bubble.append(card);
    await new Promise(setImmediate);
    assert.equal(bubble.children[0].className, "attachment-file");
    assert.equal(bubble.children[0].children[0].textContent, "🎬");
    assert.equal(bubble.children[0].children[1].textContent, "clip.mp4");
    assert.ok(state.mediaFailures.has(id));
  }

  const renderStart = app.indexOf("function renderMessages(");
  const renderEnd = app.indexOf("\nfunction copyReactions", renderStart);
  let videoCalls = 0;
  let imageCalls = 0;
  let fileCalls = 0;
  globalThis.videoThumbAttachment = () => { videoCalls += 1; return node("video-thumb"); };
  globalThis.imageAttachment = () => { imageCalls += 1; return node("image"); };
  globalThis.fileAttachment = () => { fileCalls += 1; return node("file"); };
  globalThis.pruneOrphanObjectUrls = () => {};
  globalThis.timestampMilliseconds = Number;
  globalThis.formatTimestamp = () => "10:00";
  globalThis.appendRenderedQuote = () => {};
  globalThis.scrollThreadToBottom = () => {};
  globalThis.copyReactions = () => [];
  state.active = { protocol: "signal", id: "alice" };
  state.userScrolledUp = false;
  state.messageNodes = new Map();
  state.optimistic = [];
  elements.messages = node("main");
  elements.messages.scrollHeight = 0;
  elements.messages.scrollTop = 0;
  elements.messages.clientHeight = 0;
  window.SignalTuiReconcile = {
    reconcileOptimisticMessages: () => ({ optimistic: [], visible: [] }),
    messageDisplayText: () => "",
  };
  vm.runInThisContext(app.slice(renderStart, renderEnd));
  renderMessages([
    { id: "v1", timestamp: 1, direction: "in", attachment: { attachment_id: "one.mp4", type: "video/mp4" } },
    { id: "v2", timestamp: 2, direction: "in", attachment: { attachment_id: "two.bin", type: "application/octet-stream", media_kind: "video" } },
    { id: "g1", timestamp: 3, direction: "in", attachment: { attachment_id: "loop.gif", type: "image/gif", media_kind: "gif" } },
  ], "signal");
  assert.equal(videoCalls, 2);
  assert.equal(imageCalls, 0);
  assert.equal(fileCalls, 1);
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
