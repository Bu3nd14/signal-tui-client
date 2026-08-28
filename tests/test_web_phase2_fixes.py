from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import backend as backend_mod
from backends.signal import SignalBackend
from backends.telegram import TelegramBackend
from backends.whatsapp import WhatsAppBackend
from models import ChatContact, ChatEvent
from tui.events import EventHandlingMixin
from web.api import _messages


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
        result = backend.ingest_message("42", _media_data(second), 1001)
        assert result == ("changed" if first.startswith("tgref:") else False)
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
    assert backend.ingest_message("42", _media_data(mirrored.name), 1001) == "changed"
    assert len(backend.cache["42"]) == 1
    assert backend.cache["42"][0]["attachment_id"] == mirrored.name
    with sqlite3.connect(db_file) as connection:
        assert connection.execute("SELECT attachment_id FROM messages").fetchone() == (
            mirrored.name,
        )


def test_signal_echo_never_adds_row_and_keeps_mirrored_file(monkeypatch, tmp_path):
    db_file = _db(monkeypatch, tmp_path)
    media_dir = tmp_path / "signal-media"
    media_dir.mkdir()
    monkeypatch.setattr("backends.signal.SIGNAL_CLI_ATTACHMENTS_DIR", media_dir)
    mirrored = media_dir / "sent-local.png"
    remote = media_dir / "remote-signal-id"
    mirrored.write_bytes(b"mirror")
    remote.write_bytes(b"remote")
    backend = SignalBackend()
    echo_ts = 13_000
    mirror = {**_media_data(mirrored.name), "id": str(echo_ts)}
    echo = {**_media_data(remote.name), "id": str(echo_ts)}

    assert backend.ingest_message("42", mirror, 1_000)
    assert backend.ingest_message("42", echo, echo_ts) is False
    assert len(backend.cache["42"]) == 1
    assert backend.cache["42"][0]["attachment_id"] == mirrored.name
    with sqlite3.connect(db_file) as connection:
        assert connection.execute(
            "SELECT COUNT(*), attachment_id FROM messages"
        ).fetchone() == (1, mirrored.name)


def test_signal_outgoing_attachment_is_resolvable_or_null(monkeypatch, tmp_path):
    db_file = _db(monkeypatch, tmp_path)
    media_dir = tmp_path / "signal-media"
    media_dir.mkdir()
    monkeypatch.setattr("backends.signal.SIGNAL_CLI_ATTACHMENTS_DIR", media_dir)
    backend = SignalBackend()
    unresolved = {**_media_data("remote-missing"), "id": "77"}

    assert backend.ingest_message("42", unresolved, 77)
    assert backend.cache["42"][0]["attachment_id"] is None
    with sqlite3.connect(db_file) as connection:
        assert connection.execute("SELECT attachment_id FROM messages").fetchone() == (
            None,
        )


def test_telegram_persists_content_type_and_api_marks_tgref_as_image(
    monkeypatch, tmp_path
):
    db_file = _db(monkeypatch, tmp_path)
    backend = TelegramBackend()
    data = {**_media_data("tgref:42:77"), "content_type": "image/png"}

    assert backend.ingest_message("42", data, 1_000)
    with sqlite3.connect(db_file) as connection:
        assert connection.execute("SELECT content_type FROM messages").fetchone() == (
            "image/png",
        )
    assert _messages("telegram", "42")[0]["attachment"]["type"] == "image/png"

    with sqlite3.connect(db_file) as connection:
        connection.execute("UPDATE messages SET content_type = NULL")
        connection.commit()
    assert _messages("telegram", "42")[0]["attachment"]["type"] == "image/*"


def test_push_on_attachment_upgrade():
    contact = ChatContact(id="42", display_name="Test", protocol="telegram")
    backend = SimpleNamespace(ingest_message=MagicMock(return_value="changed"))
    app = SimpleNamespace(
        manager=SimpleNamespace(get=lambda _protocol: backend),
        contacts=[contact],
        selected_contact=None,
        _contact_list_dirty=False,
        _dirty_contact_keys=set(),
        _cache={},
        _typing_contacts={},
        _typing_mumbling={},
        _web_enabled=True,
    )
    event = ChatEvent(
        type="message",
        protocol="telegram",
        contact_id="42",
        payload={
            **_media_data("/tmp/42-77-sent.png"),
            "contact": contact,
            "timestamp": 1_001,
        },
    )

    with patch("web.bridge.push_event") as push_event:
        assert EventHandlingMixin._handle_message_event(app, event)

    push_event.assert_called_once()
    assert app._cache == {}


def _receipt_app(updated, web_enabled=True):
    backend = SimpleNamespace(process_receipt=MagicMock(return_value=updated))
    return SimpleNamespace(
        manager=SimpleNamespace(get=lambda _protocol: backend),
        _cache={},
        selected_contact=None,
        _web_enabled=web_enabled,
    )


def test_receipt_event_pushes_normalized_web_update():
    app = _receipt_app(
        [
            {"id": 77, "timestamp": "1000", "status": "delivered", "text": "Ciao"},
            {"id": "", "timestamp": None, "status": None, "text": None},
        ]
    )
    event = ChatEvent(
        type="receipt",
        protocol="telegram",
        contact_id="42",
        payload={"message_ids": ["77"], "is_read": False},
    )

    with patch("web.bridge.push_event") as push_event:
        assert EventHandlingMixin._handle_receipt_event(app, event)

    push_event.assert_called_once_with(
        {
            "type": "receipt",
            "payload": {
                "protocol": "telegram",
                "contact_id": "42",
                "updates": [
                    {
                        "id": "77",
                        "timestamp": 1000,
                        "status": "delivered",
                        "text": "Ciao",
                    },
                    {"id": None, "timestamp": 0, "status": "sent", "text": ""},
                ],
            },
        }
    )


def test_receipt_event_does_not_push_when_web_is_disabled():
    app = _receipt_app(
        [{"id": "77", "timestamp": 1000, "status": "read", "text": "Ciao"}],
        web_enabled=False,
    )
    event = ChatEvent(
        type="receipt",
        protocol="telegram",
        contact_id="42",
        payload={"message_ids": ["77"], "is_read": True},
    )

    with patch("web.bridge.push_event") as push_event:
        assert EventHandlingMixin._handle_receipt_event(app, event)

    push_event.assert_not_called()


def _edit_app(web_enabled=True):
    info = {
        "message_id": "77",
        "timestamp": 1000,
        "old_text": "Prima",
        "text": "Dopo",
        "is_mine": False,
    }
    backend = SimpleNamespace(apply_edit=MagicMock(return_value=info))
    return SimpleNamespace(
        manager=SimpleNamespace(get=lambda _protocol: backend),
        _cache={},
        selected_contact=None,
        _web_enabled=web_enabled,
    )


def test_edit_event_pushes_web_update():
    app = _edit_app()
    event = ChatEvent(
        type="message_edit",
        protocol="telegram",
        contact_id="42",
        payload={"edit_message_id": 77, "text": "Dopo", "is_mine": False},
    )

    with patch("web.bridge.push_event") as push_event:
        assert EventHandlingMixin._handle_edit_event(app, event)

    push_event.assert_called_once_with(
        {
            "type": "message_edit",
            "payload": {
                "protocol": "telegram",
                "contact_id": "42",
                "message_id": "77",
                "timestamp": 1000,
                "old_text": "Prima",
                "text": "Dopo",
                "is_mine": False,
            },
        }
    )


def test_edit_event_does_not_push_when_web_is_disabled():
    app = _edit_app(web_enabled=False)
    event = ChatEvent(
        type="message_edit",
        protocol="telegram",
        contact_id="42",
        payload={"edit_message_id": "77", "text": "Dopo"},
    )

    with patch("web.bridge.push_event") as push_event:
        assert EventHandlingMixin._handle_edit_event(app, event)

    push_event.assert_not_called()


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
globalThis.state = { mediaRequests: new Set(), mediaLoads: new Map(), mediaFailures: new Set(), objectUrls: new Set(), mediaCache: new Map(), optimistic: [] };
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
    if (calls < 3) throw new Error("network");
    return { blob: async () => ({}) };
  };
  let target = view();
    await loadImage(target.container, target.image, "/media", "sent-image", "in");
  assert.equal(calls, 3);
  assert.equal(target.image.loaded, true);
  assert.equal(target.loading.removed, true);
  assert.equal(state.mediaCache.get("sent-image"), "blob:image");

  calls = 0;
  now = 0;
  globalThis.apiFetch = async () => { calls += 1; throw Object.assign(new Error("HTTP 404"), { status: 404 }); };
  target = view();
  await loadImage(target.container, target.image, "/media", "missing-image", "in");
  assert.equal(target.container.children[0].textContent, "▧  Immagine non disponibile");
  assert.equal(calls, 1);
  assert.equal(state.mediaFailures.has("missing-image"), true);
  target = view();
  await loadImage(target.container, target.image, "/media", "missing-image", "in");
  assert.equal(calls, 1);
  assert.equal(target.container.children[0].textContent, "▧  Immagine non disponibile");

  calls = 0;
  target = view();
  await loadImage(target.container, target.image, "/media", "missing-outgoing", "out");
  assert.equal(calls, 1);

  calls = 0;
  now = 0;
  globalThis.apiFetch = async () => { calls += 1; queueMicrotask(abortMediaRequests); throw new Error("network"); };
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


def test_web_reconcile_caches_preview_and_reuses_it_without_fetching():
    source = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const reconcile = fs.readFileSync("./web/static/reconcile.js", "utf8");
const mediaStart = app.indexOf("const MEDIA_CACHE_LIMIT");
const mediaEnd = app.indexOf("\nfunction attachmentName", mediaStart);
const renderStart = app.indexOf("function renderMessages(");
const renderEnd = app.indexOf("\nasync function loadMessages", renderStart);
globalThis.state = {
  mediaRequests: new Set(), mediaLoads: new Map(), mediaFailures: new Set(), objectUrls: new Set(["blob:preview"]), mediaCache: new Map(),
  optimistic: [{
    optimistic_id: "local-1", localPreviewUrl: "blob:preview", known_message_ids: [],
    protocol: "signal", contactId: "42", direction: "out", text: "", timestamp: 1,
    attachment: { type: "image/png", attachment_id: "upload-local.png", name: "photo.png" },
  }],
  active: { protocol: "signal", id: "42" },
};
const revoked = [];
globalThis.URL = { createObjectURL: () => "blob:fetched", revokeObjectURL: (url) => revoked.push(url) };
let fetchCalls = 0;
globalThis.apiFetch = async () => { fetchCalls += 1; throw new Error("unexpected media fetch"); };
globalThis.scrollThreadToBottom = () => {};
globalThis.requestAnimationFrame = () => {};
globalThis.timestampMilliseconds = (value) => Number(value);
globalThis.formatTimestamp = () => "";
globalThis.appendRenderedQuote = () => {};
const images = [];
function node(tag) {
  const value = {
    tag, className: "", children: [],
    append(...children) { this.children.push(...children); },
    addEventListener(_name, callback) { this.callback = callback; },
    setAttribute() {}, remove() {}, classList: { add() {} },
    set src(url) { this.url = url; },
  };
  if (tag === "img") images.push(value);
  return value;
}
globalThis.document = { createElement: node };
globalThis.elements = { messages: { children: [], replaceChildren() { this.children = []; }, append(child) { this.children.push(child); }, scrollTop: 0, scrollHeight: 0 } };
const real = { id: "wa-1", direction: "out", text: "", timestamp: 2, attachment: { type: "image/png", attachment_id: "sent-real.png" } };
const messages = [real];
globalThis.window = {};
vm.runInThisContext(reconcile);
vm.runInThisContext(app.slice(mediaStart, mediaEnd));
vm.runInThisContext(app.slice(renderStart, renderEnd));
renderMessages(messages, "signal");
assert.equal(messages[0].localPreviewUrl, "blob:preview");
assert.equal(state.mediaCache.get("sent-real.png"), "blob:preview");
assert.equal(state.optimistic[0].localPreviewUrl, undefined);
assert.deepEqual(revoked, []);

renderMessages([{ ...real }], "signal");
assert.equal(images.at(-1).url, "blob:preview");
assert.equal(fetchCalls, 0);

const cachedBeforeMissingId = [...state.mediaCache.entries()];
state.optimistic = [{
  confirmed_message_id: "missing-id", localPreviewUrl: "blob:missing-id", known_message_ids: [],
  protocol: "signal", contactId: "42", direction: "out", text: "", timestamp: 3,
  attachment: { type: "image/png", name: "missing.png" },
}];
const missingIdMessages = [{ id: "missing-id", direction: "out", text: "", timestamp: 3, attachment: { type: "image/png", name: "missing.png" } }];
renderMessages(missingIdMessages, "signal");
assert.equal(missingIdMessages[0].localPreviewUrl, "blob:missing-id");
assert.equal(images.at(-1).url, "blob:missing-id");
assert.deepEqual([...state.mediaCache.entries()], cachedBeforeMissingId);
assert.equal(fetchCalls, 0);
"""
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_web_media_cache_lru_revokes_only_oldest_url():
    source = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const cacheStart = app.indexOf("const MEDIA_CACHE_LIMIT");
const cacheEnd = app.indexOf("\nfunction abortMediaRequests", cacheStart);
globalThis.state = { mediaCache: new Map(), mediaFailures: new Set(), objectUrls: new Set() };
const revoked = [];
globalThis.URL = { revokeObjectURL: (url) => revoked.push(url) };
vm.runInThisContext(app.slice(cacheStart, cacheEnd));
for (let index = 0; index < 51; index += 1) cacheMedia(`image-${index}`, `blob:${index}`);
assert.equal(state.mediaCache.size, 50);
assert.equal(state.mediaCache.has("image-0"), false);
assert.equal(state.mediaCache.get("image-50"), "blob:50");
assert.deepEqual(revoked, ["blob:0"]);
assert.equal(state.objectUrls.has("blob:0"), false);
assert.equal(state.objectUrls.has("blob:50"), true);
"""
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_web_media_renders_reuse_inflight_request_and_limit_concurrency():
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
  mediaRequests: new Set(), mediaLoads: new Map(), mediaFailures: new Set(),
  objectUrls: new Set(), mediaCache: new Map(), optimistic: [],
  active: { protocol: "signal", id: "42" },
};
globalThis.window = {
  SignalTuiReconcile: {
    reconcileOptimisticMessages: () => ({ optimistic: [], visible: [] }),
    messageDisplayText: (item) => item.text || "",
  },
  setTimeout, clearTimeout,
};
globalThis.URL = { createObjectURL: (() => { let id = 0; return () => `blob:${++id}`; })(), revokeObjectURL() {} };
globalThis.scrollThreadToBottom = () => {};
globalThis.requestAnimationFrame = () => {};
globalThis.timestampMilliseconds = Number;
globalThis.formatTimestamp = () => "";
globalThis.appendRenderedQuote = () => {};
function node(tag) {
  return {
    tag, className: "", children: [], classList: { add() {} },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    querySelector(selector) { return selector === ".attachment-loading" ? this.children.find((child) => child.className === "attachment-loading") : null; },
    addEventListener(_name, callback) { this.callback = callback; },
    setAttribute() {}, remove() {},
    set src(value) { this.url = value; this.callback?.(); },
  };
}
globalThis.document = { createElement: node };
globalThis.elements = { messages: node("main") };
vm.runInThisContext(app.slice(mediaStart, mediaEnd));
vm.runInThisContext(app.slice(renderStart, renderEnd));
const message = { id: "1", direction: "in", text: "", timestamp: 1, attachment: { type: "image/png", attachment_id: "shared" } };
let resolveShared;
let calls = 0;
globalThis.apiFetch = async () => { calls += 1; await new Promise((resolve) => { resolveShared = resolve; }); return { status: 200, blob: async () => ({}) }; };
(async () => {
  renderMessages([message], "signal");
  const firstRequest = state.mediaLoads.get("shared");
  await Promise.resolve();
  renderMessages([message], "signal");
  assert.equal(state.mediaLoads.get("shared"), firstRequest);
  assert.equal(calls, 1);
  resolveShared();
  await firstRequest;

  let active = 0;
  let maximum = 0;
  const gates = [];
  globalThis.apiFetch = async () => {
    calls += 1;
    active += 1;
    maximum = Math.max(maximum, active);
    await new Promise((resolve) => gates.push(resolve));
    active -= 1;
    return { status: 200, blob: async () => ({}) };
  };
  const loads = Array.from({ length: 20 }, (_, index) => loadImage(node("div"), node("img"), `/media/${index}`, `many-${index}`, "in"));
  await new Promise(setImmediate);
  assert.equal(maximum, 6);
  assert.equal(gates.length, 6);
  while (gates.length) {
    gates.splice(0).forEach((resolve) => resolve());
    await new Promise(setImmediate);
  }
  await Promise.all(loads);
  assert.equal(maximum, 6);
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_web_thread_switch_aborts_media_but_submit_does_not():
    source = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const mediaStart = app.indexOf("function abortMediaRequests(");
const mediaEnd = app.indexOf("\nfunction pruneOrphanObjectUrls", mediaStart);
const openStart = app.indexOf("function openThread(");
const openEnd = app.indexOf("\nfunction normalizeEmojiSearch", openStart);
const submitStart = app.indexOf("async function submitMessage(");
const submitEnd = app.indexOf("\nfunction encodeToken", submitStart);
const controller = { aborted: false, abort() { this.aborted = true; } };
const inflight = Promise.resolve();
globalThis.state = {
  mediaRequests: new Set([controller]), mediaLoads: new Map([["old", inflight]]),
  active: null, messages: [], optimistic: [], optimisticSequence: 0,
  sending: false, stagedAttachment: null, replyTo: null,
};
function node() { return { children: [], classList: { add() {} }, append(child) { this.children.push(child); }, replaceChildren() { this.children = []; }, focus() {} }; }
globalThis.elements = {
  threadName: {}, threadMeta: {}, app: node(), composerShell: {}, messages: node(),
  messageInput: { value: "hello", focus() {} },
};
globalThis.closeEmojiPicker = () => {};
globalThis.cancelReply = () => {};
globalThis.protocolIcon = () => "";
globalThis.renderContacts = () => {};
globalThis.document = { createElement: node };
globalThis.loadMessages = () => {
  assert.equal(controller.aborted, true);
  assert.equal(state.mediaLoads.size, 0);
};
globalThis.markRead = () => {};
vm.runInThisContext(app.slice(mediaStart, mediaEnd));
vm.runInThisContext(app.slice(openStart, openEnd));
openThread({ id: "new", protocol: "signal", display_name: "New" });

const submitController = { aborted: false, abort() { this.aborted = true; } };
state.mediaRequests.add(submitController);
state.mediaLoads.set("current", inflight);
state.active = { id: "new", protocol: "signal" };
globalThis.window = { SignalTuiReconcile: { messageIdentity: (message) => message.id } };
globalThis.resizeComposer = () => {};
globalThis.updateComposer = () => {};
globalThis.renderMessages = () => {
  assert.equal(submitController.aborted, false);
  assert.equal(state.mediaLoads.get("current"), inflight);
};
globalThis.apiFetch = async () => ({ status: 200 });
vm.runInThisContext(app.slice(submitStart, submitEnd));
(async () => {
  await submitMessage();
  assert.equal(submitController.aborted, false);
  assert.equal(state.mediaLoads.get("current"), inflight);
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
