from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import backend as backend_mod
from backends.signal import SignalBackend
from backends.telegram import TelegramBackend
from backends.whatsapp import WhatsAppBackend


def _db(monkeypatch, tmp_path: Path) -> Path:
    db_file = tmp_path / "messages.db"
    monkeypatch.setattr(backend_mod, "DB_FILE", db_file)
    monkeypatch.setattr(backend_mod, "CACHE_DIR", tmp_path)
    return db_file


def _media_data(attachment_id: str) -> dict:
    return {
        "id": "77",
        "text": "",
        "is_mine": True,
        "sender": "You",
        "quote_text": None,
        "msg_type": "image",
        "attachment_info": None,
        "attachment_id": attachment_id,
    }


def test_telegram_attachment_upgrade_works_in_both_event_orders(monkeypatch, tmp_path):
    db_file = _db(monkeypatch, tmp_path)
    media_dir = tmp_path / "telegram-media"
    media_dir.mkdir()
    monkeypatch.setattr("backends.telegram._media_dir", lambda: media_dir)
    mirrored = media_dir / "42-77-sent.png"
    mirrored.write_bytes(b"image")

    for first, second in (
        ("tgref:42:77", str(mirrored)),
        (str(mirrored), "tgref:42:77"),
    ):
        db_file.unlink(missing_ok=True)
        backend = TelegramBackend()
        assert backend.ingest_message("42", _media_data(first), 1000)
        assert not backend.ingest_message("42", _media_data(second), 1001)
        assert len(backend.cache["42"]) == 1
        assert backend.cache["42"][0]["attachment_id"] == str(mirrored)
        with sqlite3.connect(db_file) as connection:
            assert connection.execute(
                "SELECT attachment_id FROM messages"
            ).fetchone() == (str(mirrored),)


def test_telegram_tgref_prefers_mirrored_file(monkeypatch, tmp_path):
    media_dir = tmp_path / "telegram-media"
    media_dir.mkdir()
    mirrored = media_dir / "42-77-sent.jpg"
    mirrored.write_bytes(b"image")
    monkeypatch.setattr("backends.telegram._media_dir", lambda: media_dir)
    backend = TelegramBackend()
    backend._download_media_by_ref = MagicMock()

    assert backend.get_attachment_path("tgref:42:77") == mirrored
    backend._download_media_by_ref.assert_not_called()


def test_signal_unresolved_echo_is_upgraded_to_sent_attachment(monkeypatch, tmp_path):
    db_file = _db(monkeypatch, tmp_path)
    media_dir = tmp_path / "signal-media"
    media_dir.mkdir()
    monkeypatch.setattr("backends.signal.SIGNAL_CLI_ATTACHMENTS_DIR", media_dir)
    mirrored = media_dir / "sent-local.png"
    mirrored.write_bytes(b"image")
    backend = SignalBackend()

    assert backend.ingest_message("42", _media_data("signal-cli-id"), 1000)
    assert not backend.ingest_message("42", _media_data(mirrored.name), 1001)
    assert len(backend.cache["42"]) == 1
    assert backend.cache["42"][0]["attachment_id"] == mirrored.name
    with sqlite3.connect(db_file) as connection:
        assert connection.execute("SELECT attachment_id FROM messages").fetchone() == (
            mirrored.name,
        )


def test_whatsapp_echo_first_is_upgraded_to_sent_attachment(monkeypatch, tmp_path):
    db_file = _db(monkeypatch, tmp_path)
    media_dir = tmp_path / "whatsapp-media"
    media_dir.mkdir()
    mirrored = media_dir / "sent-local.png"
    mirrored.write_bytes(b"image")
    backend = WhatsAppBackend()
    backend.media_dir = str(media_dir)

    assert backend.ingest_message("42", _media_data("waha-media-id"), 1000)
    assert not backend.ingest_message("42", _media_data(mirrored.name), 1001)
    assert backend.cache["42"][0]["attachment_id"] == mirrored.name
    with sqlite3.connect(db_file) as connection:
        assert connection.execute("SELECT attachment_id FROM messages").fetchone() == (
            mirrored.name,
        )


def test_signal_and_whatsapp_never_downgrade_sent_attachment(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    signal_dir = tmp_path / "signal-media"
    signal_dir.mkdir()
    monkeypatch.setattr("backends.signal.SIGNAL_CLI_ATTACHMENTS_DIR", signal_dir)
    signal_file = signal_dir / "sent-signal.png"
    signal_file.write_bytes(b"image")
    signal = SignalBackend()
    assert signal.ingest_message("42", _media_data(signal_file.name), 1000)
    assert not signal.ingest_message("42", _media_data("signal-cli-id"), 1001)
    assert signal.cache["42"][0]["attachment_id"] == signal_file.name

    whatsapp_dir = tmp_path / "whatsapp-media"
    whatsapp_dir.mkdir()
    whatsapp_file = whatsapp_dir / "sent-whatsapp.png"
    whatsapp_file.write_bytes(b"image")
    whatsapp = WhatsAppBackend()
    whatsapp.media_dir = str(whatsapp_dir)
    assert whatsapp.ingest_message("43", _media_data(whatsapp_file.name), 2000)
    assert not whatsapp.ingest_message("43", _media_data("waha-media-id"), 2001)
    assert whatsapp.cache["43"][0]["attachment_id"] == whatsapp_file.name


def test_whatsapp_dedup_fills_missing_quote_fields(monkeypatch, tmp_path):
    db_file = _db(monkeypatch, tmp_path)
    backend = WhatsAppBackend()
    ack = {
        "id": "wa-77",
        "text": "answer",
        "is_mine": True,
        "sender": "You",
        "quote_text": None,
        "msg_type": "text",
        "attachment_info": None,
    }
    mirrored = {
        **ack,
        "quote_text": "question",
        "quote_timestamp": 123000,
        "quote_author": "42",
        "reply_to_message_id": "wa-11",
    }

    assert backend.ingest_message("42", ack, 2000)
    assert not backend.ingest_message("42", mirrored, 3000)
    assert backend.cache["42"][0]["quote_timestamp"] == 123000
    with sqlite3.connect(db_file) as connection:
        assert connection.execute(
            "SELECT quote_text, quote_timestamp, quote_author, reply_to_message_id "
            "FROM messages"
        ).fetchone() == ("question", 123000, "42", "wa-11")


def test_whatsapp_text_quote_reconciliation_is_timestamp_tolerant_and_unique():
    source = r"""
const assert = require("node:assert/strict");
const { reconcileOptimisticMessages } = require("./web/static/reconcile.js");
const optimistic = { optimistic_id: "local", protocol: "whatsapp", contactId: "42", direction: "out", text: "answer", timestamp: 9000, known_message_ids: [], optimisticStatus: "sent", quote_timestamp: 8000, quote_message: "question" };
let echo = { id: "wa-1", direction: "out", text: "answer", timestamp: 1000, quote_timestamp: 2000, quote_text: "question" };
let result = reconcileOptimisticMessages([echo], [optimistic], "whatsapp", "42");
assert.equal(result.optimistic[0].confirmed_message_id, "wa-1");
echo = { ...echo, quote_timestamp: null };
result = reconcileOptimisticMessages([echo], [optimistic], "whatsapp", "42");
assert.equal(result.optimistic[0].confirmed_message_id, "wa-1");
result = reconcileOptimisticMessages([echo, { ...echo, id: "wa-2" }], [optimistic], "whatsapp", "42");
assert.equal(result.optimistic[0].confirmed_message_id, undefined);
"""
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_web_image_retry_recovers_expires_and_aborts():
    source = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const clearStart = app.indexOf("const MEDIA_CACHE_LIMIT");
const loadEnd = app.indexOf("\nfunction imageAttachment", clearStart);
globalThis.state = { mediaRequests: new Set(), mediaLoads: new Map(), objectUrls: new Set(), mediaCache: new Map() };
globalThis.scrollThreadToBottom = () => {};
globalThis.DOMException = globalThis.DOMException || class extends Error { constructor(message, name) { super(message); this.name = name; } };
let now = 0;
Date.now = () => now;
globalThis.window = {
  setTimeout(callback, delay) { now += delay; queueMicrotask(callback); return 1; },
  clearTimeout() {},
};
globalThis.URL = { createObjectURL: () => "blob:image", revokeObjectURL() {} };
globalThis.document = { createElement: () => ({ className: "", textContent: "" }) };
function view() {
  const loading = { removed: false, remove() { this.removed = true; } };
  const container = {
    children: [],
    querySelector: () => loading,
    replaceChildren() { this.children = []; },
    append(child) { this.children.push(child); },
  };
  const image = {
    loaded: false,
    addEventListener(_name, callback) { this.callback = callback; },
    set src(value) { this.value = value; this.loaded = true; this.callback(); },
  };
  return { container, image, loading };
}
vm.runInThisContext(app.slice(clearStart, loadEnd));
(async () => {
  let calls = 0;
  globalThis.apiFetch = async () => { calls += 1; return { blob: async () => ({}) }; };
  let first = view();
  let second = view();
  await Promise.all([
    loadImage(first.container, first.image, "/media", "shared-image"),
    loadImage(second.container, second.image, "/media", "shared-image"),
  ]);
  assert.equal(calls, 1);
  assert.equal(first.image.loaded, true);
  assert.equal(second.image.loaded, true);

  calls = 0;
  globalThis.apiFetch = async () => {
    calls += 1;
    if (calls < 3) throw new Error("HTTP 404");
    return { blob: async () => ({}) };
  };
  let target = view();
  await loadImage(target.container, target.image, "/media", "sent-image");
  assert.equal(calls, 3);
  assert.equal(target.image.loaded, true);
  assert.equal(target.loading.removed, true);
  assert.equal(state.mediaCache.get("sent-image"), "blob:image");

  calls = 0;
  now = 0;
  globalThis.apiFetch = async () => { calls += 1; throw new Error("HTTP 404"); };
  target = view();
  await loadImage(target.container, target.image, "/media", "missing-image");
  assert.equal(target.container.children[0].textContent, "▧  Immagine non disponibile");
  assert.equal(calls, 3);

  calls = 0;
  now = 0;
  globalThis.apiFetch = async () => { calls += 1; queueMicrotask(clearMedia); throw new Error("HTTP 404"); };
  target = view();
  await loadImage(target.container, target.image, "/media", "aborted-image");
  assert.equal(calls, 1);
  assert.equal(target.container.children.length, 0);
  assert.equal(state.mediaCache.get("sent-image"), "blob:image");
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_web_reconcile_transfers_preview_to_media_cache_without_revoking():
    source = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const mediaStart = app.indexOf("const MEDIA_CACHE_LIMIT");
const mediaEnd = app.indexOf("\nfunction attachmentName", mediaStart);
const renderStart = app.indexOf("function renderMessages(");
const renderEnd = app.indexOf("\nasync function loadMessages", renderStart);
globalThis.state = {
  mediaRequests: new Set(), mediaLoads: new Map(), objectUrls: new Set(), mediaCache: new Map(),
  optimistic: [], active: { protocol: "whatsapp", id: "42" },
};
const revoked = [];
globalThis.URL = { createObjectURL: () => "blob:fetched", revokeObjectURL: (url) => revoked.push(url) };
globalThis.scrollThreadToBottom = () => {};
globalThis.requestAnimationFrame = () => {};
globalThis.timestampMilliseconds = (value) => Number(value);
globalThis.formatTimestamp = () => "";
globalThis.appendRenderedQuote = () => {};
function node() { return { className: "", append() {}, addEventListener() {}, setAttribute() {}, classList: { add() {} } }; }
globalThis.document = { createElement: node };
globalThis.elements = { messages: { replaceChildren() {}, append() {}, scrollTop: 0, scrollHeight: 0 } };
const real = { id: "wa-1", direction: "out", text: "", timestamp: 2, attachment: { type: "image/png", attachment_id: "sent-real.png" } };
const confirmed = { confirmed_message_id: "wa-1", localPreviewUrl: "blob:preview", protocol: "whatsapp", contactId: "42", direction: "out", timestamp: 1 };
globalThis.window = { SignalTuiReconcile: {
  reconcileOptimisticMessages: () => ({ optimistic: [confirmed], visible: [] }),
  messageIdentity: (message) => String(message.id),
  messageDisplayText: () => "",
} };
vm.runInThisContext(app.slice(mediaStart, mediaEnd));
vm.runInThisContext(app.slice(renderStart, renderEnd));
renderMessages([real], "whatsapp");
assert.equal(state.mediaCache.get("sent-real.png"), "blob:preview");
assert.equal(confirmed.localPreviewUrl, undefined);
assert.deepEqual(revoked, []);
"""
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
