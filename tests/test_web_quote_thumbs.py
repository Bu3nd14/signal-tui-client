from __future__ import annotations

import io
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import protocols.db as backend
from protocols.db import _init_db
from web.api import create_api_router
from web.auth import install_auth

TOKEN = "correct-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def web_client(monkeypatch, tmp_path: Path):
    db_file = tmp_path / "messages.db"
    monkeypatch.setattr(backend, "DB_FILE", db_file)
    monkeypatch.setattr(backend, "CACHE_DIR", tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    _init_db()
    app = FastAPI()
    app.state.manager = SimpleNamespace(get_attachment_path=lambda *_args: None)
    install_auth(app, TOKEN)
    app.include_router(create_api_router())
    with TestClient(app) as client:
        yield client, db_file, tmp_path


def _insert_message(db_file: Path, **values) -> int:
    fields = {
        "protocol": "signal",
        "contact_number": "alice",
        "text": "reply",
        "is_mine": 0,
        "timestamp": 1,
        **values,
    }
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    with sqlite3.connect(db_file) as connection:
        cursor = connection.execute(
            f"INSERT INTO messages ({columns}) VALUES ({placeholders})",
            tuple(fields.values()),
        )
        return cursor.lastrowid


def test_message_payload_maps_quote_thumbnails_and_raw_fields(web_client):
    client, db_file, tmp_path = web_client
    local_path = tmp_path / "quote-thumbs" / "local.jpg"
    local_id = _insert_message(
        db_file,
        protocol="telegram",
        timestamp=1,
        quote_attachment_id="local-id",
        quote_attachment_path=str(local_path),
        quote_content_type="image/jpeg",
    )
    remote_id = "tgref:folder/photo id"
    _insert_message(
        db_file,
        protocol="telegram",
        timestamp=2,
        quote_attachment_id=remote_id,
        quote_content_type="image/jpeg",
    )
    _insert_message(
        db_file,
        protocol="telegram",
        timestamp=3,
        quote_attachment_id="video-id",
        quote_content_type="video/mp4",
    )
    _insert_message(db_file, protocol="telegram", timestamp=4)

    response = client.get("/api/messages?proto=telegram&contact_id=alice", headers=AUTH)

    assert response.status_code == 200
    messages = response.json()
    assert messages[0]["quote_attachment_id"] == "local-id"
    assert messages[0]["quote_content_type"] == "image/jpeg"
    assert messages[0]["quote_thumb_url"] == (
        f"/api/quote-media/telegram/{local_id}?w=96"
    )
    encoded_id = "/".join(quote(part, safe="") for part in remote_id.split("/"))
    assert messages[1]["quote_attachment_id"] == remote_id
    assert messages[1]["quote_content_type"] == "image/jpeg"
    assert messages[1]["quote_thumb_url"] == (f"/api/media/telegram/{encoded_id}?w=96")
    assert messages[2]["quote_thumb_url"] == "/api/media/telegram/video-id?w=96"
    assert messages[3]["quote_attachment_id"] is None
    assert messages[3]["quote_content_type"] is None
    assert messages[3]["quote_thumb_url"] is None


def test_signal_embedded_video_thumbnail_is_content_type_agnostic(web_client):
    client, db_file, tmp_path = web_client
    embedded = tmp_path / "quote-thumbs" / "video-frame.jpg"
    embedded.parent.mkdir()
    Image.new("RGB", (24, 24)).save(embedded)
    row_id = _insert_message(
        db_file,
        quote_attachment_id="video.mp4",
        quote_attachment_path=str(embedded),
        quote_content_type="video/mp4",
    )

    response = client.get("/api/messages?proto=signal&contact_id=alice", headers=AUTH)

    assert response.status_code == 200
    assert response.json()[0]["quote_thumb_url"] == (
        f"/api/quote-media/signal/{row_id}?w=96"
    )


def test_quote_media_thumbnail_is_cached(web_client):
    client, db_file, tmp_path = web_client
    quote_root = tmp_path / "quote-thumbs"
    quote_root.mkdir()
    source = quote_root / "large.png"
    Image.new("RGB", (1200, 800), "#3578a8").save(source)
    row_id = _insert_message(db_file, quote_attachment_path=str(source))
    url = f"/api/quote-media/signal/{row_id}?w=96"

    first = client.get(url, headers=AUTH)

    assert first.status_code == 200
    assert first.headers["content-type"].startswith("image/jpeg")
    assert first.headers["cache-control"] == ("private, max-age=31536000, immutable")
    with Image.open(io.BytesIO(first.content)) as thumbnail:
        assert max(thumbnail.size) <= 96
    with patch("PIL.Image.open", side_effect=AssertionError("cache miss")):
        second = client.get(url, headers=AUTH)
    assert second.content == first.content


def test_quote_media_rejects_missing_and_unsafe_paths(web_client):
    client, db_file, tmp_path = web_client
    quote_root = tmp_path / "quote-thumbs"
    quote_root.mkdir()
    missing_id = _insert_message(
        db_file, quote_attachment_path=str(quote_root / "missing.jpg")
    )
    passwd_id = _insert_message(
        db_file, timestamp=2, quote_attachment_path="/etc/passwd"
    )
    outside = tmp_path / "outside.jpg"
    Image.new("RGB", (10, 10)).save(outside)
    outside_id = _insert_message(
        db_file, timestamp=3, quote_attachment_path=str(outside)
    )

    for url in (
        f"/api/quote-media/signal/{missing_id}",
        f"/api/quote-media/signal/{passwd_id}",
        f"/api/quote-media/signal/{outside_id}",
        "/api/quote-media/signal/999999",
        f"/api/quote-media/telegram/{missing_id}",
    ):
        assert client.get(url, headers=AUTH).status_code == 404


def test_quote_media_requires_bearer(web_client):
    client, _, _ = web_client

    response = client.get("/api/quote-media/signal/1")

    assert response.status_code == 401


def test_quote_media_database_error_returns_500(web_client):
    client, _, _ = web_client

    with patch("web.api.sqlite3.connect", side_effect=sqlite3.OperationalError):
        response = client.get("/api/quote-media/signal/1", headers=AUTH)

    assert response.status_code == 500
    assert response.json() == {"detail": "Database error"}


def test_spa_renders_eager_quote_thumbnails():
    source = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const media = app.slice(app.indexOf("const MEDIA_CACHE_LIMIT"), app.indexOf("\nfunction attachmentName"));
const quote = app.slice(app.indexOf("function fetchQuoteThumb("), app.indexOf("\nfunction renderMessages("));
globalThis.state = { mediaRequests: new Set(), mediaLoads: new Map(), mediaFailures: new Set(), objectUrls: new Set(), pinnedUrls: new Set(), mediaCache: new Map(), optimistic: [] };
globalThis.URL = { createObjectURL: () => "blob:quote-thumb", revokeObjectURL() {} };
globalThis.scrollThreadToBottom = () => {};
let mode = "ok";
let requestedPath;
globalThis.apiFetch = async (path) => {
  requestedPath = path;
  if (mode !== "ok") throw Object.assign(new Error(String(mode)), { status: mode });
  return { status: 200, blob: async () => ({}) };
};
function node(tag) {
  return {
    tag,
    children: [],
    className: "",
    attributes: {},
    removed: false,
    append(...items) { for (const item of items) { item.parent = this; this.children.push(item); } },
    replaceChildren(...items) { this.children = items; },
    querySelector() { return null; },
    addEventListener(type, callback) { this.listeners ??= {}; this.listeners[type] = callback; },
    setAttribute(name, value) { this.attributes[name] = value; },
    remove() { this.removed = true; if (this.parent) this.parent.children = this.parent.children.filter((item) => item !== this); },
  };
}
globalThis.document = { createElement: node, querySelector: () => null };
globalThis.elements = { messages: { parent: { id: "scroll-root" } } };
vm.runInThisContext(media);
vm.runInThisContext(quote);

(async () => {
  const bubble = node("div");
  appendRenderedQuote(bubble, { direction: "in", quote_author: "Alice", quote_text: "Foto", quote_thumb_url: "/api/quote-media/signal/7?w=96" });
  const renderedQuote = bubble.children[0];
  const image = renderedQuote.children[0];
  assert.equal(renderedQuote.className, "message-quote has-thumb");
  assert.equal(image.tag, "img");
  assert.equal(image.className, "message-quote-thumb");
  assert.equal(image.attributes.loading, undefined);
  assert.equal(image.src, undefined);
  await new Promise(setImmediate);
  assert.equal(requestedPath, "/api/quote-media/signal/7?w=96");
  assert.equal(image.src, "blob:quote-thumb");
  // Il blob della quote è pinnato: mai evittato dalla LRU né revocato dal prune.
  assert.ok(state.pinnedUrls.has("blob:quote-thumb"));
  assert.ok(!state.mediaCache.has("quote:/api/quote-media/signal/7?w=96"));

  const videoBubble = node("div");
  appendRenderedQuote(videoBubble, { direction: "in", quote_author: "Bob", quote_text: "Video", quote_content_type: "video/mp4", quote_thumb_url: "/api/media/signal/video.mp4?w=96" });
  const videoWrapper = videoBubble.children[0].children[0];
  assert.equal(videoWrapper.className, "message-quote-thumb-video");
  assert.equal(videoWrapper.children[0].className, "message-quote-thumb");
  assert.equal(videoWrapper.children[1].className, "attachment-video-badge");
  assert.equal(videoWrapper.children[1].textContent, "▶");

  for (const status of [404, 422]) {
    mode = status;
    const failedBubble = node("div");
    appendRenderedQuote(failedBubble, { direction: "in", quote_author: "Bob", quote_text: "Testo", quote_content_type: "video/mp4", quote_thumb_url: `/api/media/signal/broken-${status}.mp4?w=96` });
    const failedQuote = failedBubble.children[0];
    const failedWrapper = failedQuote.children[0];
    await new Promise(setImmediate);
    assert.equal(failedWrapper.removed, true);
    assert.equal(failedQuote.children.length, 1);
    assert.equal(failedQuote.children[0].children[0].textContent, "Bob");
    assert.equal(failedQuote.children[0].children[1].textContent, "Testo");
  }

  const plainBubble = node("div");
  appendRenderedQuote(plainBubble, { quote_author: "Carol", quote_text: "Solo testo" });
  const images = plainBubble.children[0].children.filter((child) => child.tag === "img");
  assert.equal(images.length, 0);
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_spa_optimistic_reply_thumbnail_all_protocols():
    source = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const submit = app.slice(app.indexOf("async function submitMessage("), app.indexOf("\nfunction encodeToken"));
globalThis.state = {
  sending: false,
  active: { id: "alice", protocol: "signal" },
  stagedAttachment: null,
  replyTo: { timestamp: 10, quoteAuthor: "alice", quoteMessage: "Foto", isImage: true, isMedia: true, contentType: "image/jpeg", attachmentId: "folder/photo id:1", id: "10" },
  messages: [],
  optimistic: [],
  optimisticSequence: 0,
};
globalThis.elements = { messageInput: { value: "reply", focus() {} } };
globalThis.window = { SignalTuiReconcile: { messageIdentity: (message) => message.id } };
globalThis.resizeComposer = () => {};
globalThis.updateComposer = () => {};
globalThis.renderMessages = () => {};
globalThis.cancelReply = () => { state.replyTo = null; };
globalThis.showError = assert.fail;
globalThis.apiFetch = async () => ({ status: 200 });
vm.runInThisContext(submit);

(async () => {
  await submitMessage();
  assert.equal(state.optimistic[0].quote_thumb_url, "/api/media/signal/folder/photo%20id%3A1?w=96");

  state.active = { id: "bob", protocol: "whatsapp" };
  state.replyTo = { timestamp: 11, quoteAuthor: "bob", quoteMessage: "Foto WA", isImage: true, isMedia: true, contentType: "image/jpeg", attachmentId: "wa/image", id: "wa-11" };
  elements.messageInput.value = "reply wa";
  await submitMessage();
  assert.equal(state.optimistic[1].quote_thumb_url, "/api/media/whatsapp/wa/image?w=96");

  state.active = { id: "carol", protocol: "telegram" };
  state.replyTo = { timestamp: 12, quoteAuthor: "carol", quoteMessage: "Video TG", isImage: false, isVideo: true, isMedia: true, contentType: "video/mp4", attachmentId: "tgref:video id", id: "12" };
  elements.messageInput.value = "reply video";
  await submitMessage();
  assert.equal(state.optimistic[2].quote_thumb_url, "/api/media/telegram/tgref%3Avideo%20id?w=96");
  assert.equal(state.optimistic[2].quote_content_type, "video/mp4");
  assert.equal(state.optimistic[2].quote_media_type, "video");
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_spa_start_reply_marks_video():
    source = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const reply = app.slice(app.indexOf("function replyAuthor("), app.indexOf("\nfunction remoteLog("));
globalThis.state = { active: { id: "alice", protocol: "signal", display_name: "Alice" }, replyTo: null };
globalThis.elements = {
  replyBanner: {}, replyMark: {}, replyAuthor: {}, replySnippet: {},
  messageInput: { focus() {}, placeholder: "" },
};
globalThis.window = { SignalTuiReconcile: { replyQuoteMessage: () => "clip.mp4" } };
globalThis.cancelEdit = () => {};
globalThis.timestampMilliseconds = (value) => value;
vm.runInThisContext(reply);
startReply({ id: 7, timestamp: 1000, direction: "in", attachment: { type: "application/octet-stream", media_kind: "video", attachment_id: "clip.mp4" } });
assert.equal(state.replyTo.isVideo, true);
assert.equal(state.replyTo.isImage, false);
"""
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_quote_thumb_resolved_by_filename_when_timestamp_missing(web_client):
    """Quote a immagine senza quote_timestamp né metadati allegato: la thumb
    è risolta cercando il nome file (es. da attachment_info) nella stessa chat.
    """
    client, db_file, _ = web_client
    _insert_message(
        db_file,
        contact_number="alice",
        timestamp=1000,
        is_mine=1,
        attachment_id="ZNIWHsP-XwhFe3PWTpnx.jpg",
        attachment_info="Image: IMG_1303.jpg",
        content_type="image/jpeg",
    )
    _insert_message(
        db_file,
        contact_number="alice",
        timestamp=2000,
        is_mine=0,
        text="what a cool",
        quote_text="IMG_1303.jpg — 🖼️ Immagine",
        quote_timestamp=None,
        quote_content_type="image/jpeg",
    )

    response = client.get("/api/messages?proto=signal&contact_id=alice", headers=AUTH)

    assert response.status_code == 200
    messages = response.json()
    quoting = next(m for m in messages if m["text"] == "what a cool")
    assert quoting["quote_thumb_url"] == (
        "/api/media/signal/ZNIWHsP-XwhFe3PWTpnx.jpg?w=96"
    )


def test_quote_video_resolver_path_timestamp_and_filename(web_client):
    client, db_file, tmp_path = web_client
    local_video = tmp_path / "clip local.mp4"
    local_video.write_bytes(b"video")
    _insert_message(
        db_file,
        timestamp=500,
        text="path reply",
        quote_attachment_path=str(local_video),
        quote_content_type="video/mp4",
    )
    _insert_message(
        db_file,
        timestamp=1000,
        is_mine=1,
        attachment_id="photo.jpg",
        content_type="image/jpeg",
    )
    _insert_message(
        db_file,
        timestamp=1000,
        is_mine=1,
        attachment_id="timestamp-video.mp4",
        content_type="video/mp4",
    )
    _insert_message(
        db_file,
        timestamp=2000,
        text="timestamp reply",
        quote_timestamp=1000,
        quote_content_type="video/mp4",
    )
    _insert_message(
        db_file,
        timestamp=2500,
        is_mine=1,
        attachment_id="stored-video-id",
        attachment_info="Video: holiday clip.mp4",
        content_type="video/mp4",
    )
    _insert_message(
        db_file,
        timestamp=3000,
        text="filename reply",
        quote_text="holiday clip.mp4 — 🎬 Video",
        quote_content_type=None,
    )

    response = client.get("/api/messages?proto=signal&contact_id=alice", headers=AUTH)

    assert response.status_code == 200
    messages = {message["text"]: message for message in response.json()}
    encoded_path = "/".join(
        quote(part, safe="") for part in str(local_video).split("/")
    )
    assert messages["path reply"]["quote_thumb_url"] == (
        f"/api/media/signal/{encoded_path}?w=96"
    )
    assert messages["timestamp reply"]["quote_thumb_url"] == (
        "/api/media/signal/timestamp-video.mp4?w=96"
    )
    assert messages["filename reply"]["quote_thumb_url"] == (
        "/api/media/signal/stored-video-id?w=96"
    )


def test_quote_filename_fallback_ignores_unknown_names_and_future():
    """Nessun nome file → nessuna risoluzione; allegati futuri esclusi."""
    from web.api import _quoted_media_by_filename

    assert _quoted_media_by_filename("signal", "alice", "solo testo", 2000) is None
    assert (
        _quoted_media_by_filename("signal", "alice", "IMG_1303.jpg — 🖼️ Immagine", 2000)
        is None
    )


def test_quote_thumb_resolved_by_reply_to_message_id_telegram(web_client):
    """Telegram non persiste quote_timestamp ma ha reply_to_message_id:
    la thumb è risolta per msg_id del messaggio quotato."""
    client, db_file, _ = web_client
    _insert_message(
        db_file,
        protocol="telegram",
        contact_number="alice",
        timestamp=1000,
        is_mine=1,
        msg_id="9001",
        attachment_id="tgref:folder/photo id",
        content_type="image/jpeg",
    )
    _insert_message(
        db_file,
        protocol="telegram",
        contact_number="alice",
        timestamp=2000,
        is_mine=0,
        msg_id="9002",
        text="reply to photo",
        reply_to_message_id="9001",
        quote_text="🖼️ Immagine",
        quote_content_type=None,
    )

    response = client.get("/api/messages?proto=telegram&contact_id=alice", headers=AUTH)

    assert response.status_code == 200
    messages = response.json()
    quoting = next(m for m in messages if m["text"] == "reply to photo")
    assert quoting["quote_thumb_url"] == (
        "/api/media/telegram/tgref%3Afolder/photo%20id?w=96"
    )


def test_quote_thumb_whatsapp_prefixed_msg_id(web_client):
    """WhatsApp: reply_to_message_id è l'id senza prefisso, ma il msg_id del
    messaggio quotato è 'true_<jid>_<id>' — il match deve accettare il suffisso."""
    client, db_file, _ = web_client
    _insert_message(
        db_file,
        protocol="whatsapp",
        contact_number="189025889575055@lid",
        timestamp=1000,
        is_mine=1,
        msg_id="true_189025889575055@lid_3EB0795971ED487CC7627F",
        attachment_id="sent-8a44cb8499554832aa70afa2e0d998ca.jpg",
        content_type="image/jpeg",
    )
    _insert_message(
        db_file,
        protocol="whatsapp",
        contact_number="189025889575055@lid",
        timestamp=2000,
        is_mine=0,
        msg_id="false_189025889575055@lid_XXXX",
        text="Davvero bella",
        quote_text="🖼️ Immagine",
        quote_timestamp=None,
        quote_content_type=None,
        reply_to_message_id="3EB0795971ED487CC7627F",
    )

    response = client.get(
        "/api/messages?proto=whatsapp&contact_id=189025889575055@lid", headers=AUTH
    )

    assert response.status_code == 200
    messages = response.json()
    quoting = next(m for m in messages if m["text"] == "Davvero bella")
    assert quoting["quote_thumb_url"] == (
        "/api/media/whatsapp/sent-8a44cb8499554832aa70afa2e0d998ca.jpg?w=96"
    )
