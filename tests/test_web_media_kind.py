from __future__ import annotations

import subprocess

from web.api import _messages


def _run_node(source: str) -> None:
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_messages_exposes_media_kind_and_keeps_type_fallback():
    from backend import _add_message_to_cache

    _add_message_to_cache(
        "alice",
        "",
        is_mine=False,
        sender="Alice",
        timestamp=1_000,
        msg_type="attachment",
        attachment_info="clip.mp4",
        attachment_id="clip.mp4",
        content_type="video/mp4",
        media_kind="video",
    )
    _add_message_to_cache(
        "alice",
        "",
        is_mine=False,
        sender="Alice",
        timestamp=2_000,
        msg_type="image",
        attachment_info="remote-photo",
        attachment_id="opaque-id",
        content_type=None,
        media_kind="image",
    )

    video, image = _messages("signal", "alice")

    assert video["attachment"] == {
        "attachment_id": "clip.mp4",
        "name": "clip.mp4",
        "type": "video/mp4",
        "media_kind": "video",
    }
    assert image["attachment"]["media_kind"] == "image"
    assert image["attachment"]["type"] == "image/*"


def test_file_attachment_renders_kind_icon_name_and_opens_media():
    _run_node(r"""
const assert = require("node:assert/strict")
const fs = require("node:fs")
const vm = require("node:vm")
const app = fs.readFileSync("./web/static/app.js", "utf8")
const start = app.indexOf("function attachmentName(")
const end = app.indexOf("\nfunction replyAuthor", start)
const events = []
const tabs = []
let resolveBlob
function node(tag) {
  return {
    tag, className: "", textContent: "", children: [], attributes: {}, listeners: {},
    append(...children) { this.children.push(...children) },
    setAttribute(name, value) { this.attributes[name] = value },
    addEventListener(name, callback) { this.listeners[name] = callback },
  }
}
globalThis.document = { createElement: node }
globalThis.window = { open: (path, target) => {
  events.push(["open", path, target])
  const tab = {
    closed: false,
    writtenHtml: "",
    document: {
      title: "",
      body: { textContent: "" },
      write(html) { this.title = "Caricamento allegato…"; tab.writtenHtml = html; },
      close() {},
    },
    location: { href: "" },
    close() { this.closed = true },
  }
  tabs.push(tab)
  return tab
} }
globalThis.state = { objectUrls: new Set(), fileTabUrls: new Map() }
globalThis.apiFetch = async (path) => {
  events.push(["fetch", path])
  return { blob: () => new Promise((resolve) => { resolveBlob = resolve }) }
}
globalThis.URL = { createObjectURL: () => "blob:authenticated-media" }
globalThis.showError = assert.fail
vm.runInThisContext(app.slice(start, end))

;(async () => {
const icons = {
  video: "🎬",
  voice: "🎤",
  audio: "🎵",
  document: "📎",
  sticker: "🎨",
  gif: "🎞️",
  unknown: "📎",
}
for (const [kind, expected] of Object.entries(icons)) {
  const card = fileAttachment({
    attachment_id: `folder/${kind} report.bin`,
    name: `${kind}.bin`,
    media_kind: kind,
  }, "telegram", "in")
  assert.equal(card.className, "attachment-file")
  assert.equal(card.children[0].textContent, expected)
  assert.equal(card.children[1].textContent, `${kind}.bin`)
  if (kind === "video") {
    const result = card.listeners.click()
    assert.equal(result, undefined)
    assert.deepEqual(events, [
      ["open", "", "_blank"],
      ["fetch", "/api/media/telegram/folder/video%20report.bin"],
    ])
    assert.equal(tabs[0].document.title, "Caricamento allegato…")
    assert.match(tabs[0].writtenHtml, /Loading\.\.\./)
    assert.match(tabs[0].writtenHtml, /@keyframes spin/)
    assert.match(tabs[0].writtenHtml, /background:#000/)
    assert.equal(tabs[0].location.href, "")
    await Promise.resolve()
    resolveBlob({ media: true })
    await new Promise(setImmediate)
    assert.equal(tabs[0].location.href, "blob:authenticated-media")
  } else {
    card.listeners.click()
    await Promise.resolve()
    resolveBlob({ media: true })
    await new Promise(setImmediate)
  }
}
assert.equal(events[0][0], "open")
assert.equal(events[1][0], "fetch")
assert.deepEqual([...state.objectUrls], ["blob:authenticated-media"])
assert.equal(state.fileTabUrls.get("blob:authenticated-media"), tabs.at(-1))
const fallbackName = fileAttachment({ attachment_id: "path/report.pdf", media_kind: "document" }, "signal", "out")
assert.equal(fallbackName.children[1].textContent, "report.pdf")
})().catch((error) => { console.error(error); process.exitCode = 1 })
    """)


def test_file_attachment_shows_unavailable_whatsapp_media_on_404():
    _run_node(r"""
const assert = require("node:assert/strict")
const fs = require("node:fs")
const vm = require("node:vm")
const app = fs.readFileSync("./web/static/app.js", "utf8")
const start = app.indexOf("function attachmentName(")
const end = app.indexOf("\nfunction replyAuthor", start)
const errors = []
function node() {
  return {
    className: "", textContent: "", children: [], listeners: {},
    append(...children) { this.children.push(...children) },
    setAttribute() {},
    addEventListener(name, callback) { this.listeners[name] = callback },
  }
}
globalThis.document = { createElement: node }
globalThis.window = { open: () => ({
  document: { title: "", write() {}, close() {} },
  close() {},
}) }
globalThis.state = { objectUrls: new Set(), fileTabUrls: new Map() }
globalThis.apiFetch = async () => {
  const error = new Error("HTTP 404")
  error.status = 404
  throw error
}
globalThis.showError = (message) => errors.push(message)
vm.runInThisContext(app.slice(start, end))

;(async () => {
  const card = fileAttachment({ attachment_id: "old.oga", media_kind: "voice" }, "whatsapp", "in")
  card.listeners.click()
  await new Promise(setImmediate)
  assert.deepEqual(errors, ["Media non più disponibile su WhatsApp."])
})().catch((error) => { console.error(error); process.exitCode = 1 })
""")


def test_message_media_type_prefers_kind_and_keeps_reconciliation_categories():
    _run_node(r"""
const assert = require("node:assert/strict")
const { messageMediaType, reconcileOptimisticMessages } = require("./web/static/reconcile.js")

const kindCategories = {
  image: "image",
  gif: "image",
  video: "video",
  voice: "audio",
  audio: "audio",
  document: "attachment",
  sticker: "sticker",
}
for (const [mediaKind, category] of Object.entries(kindCategories)) {
  assert.equal(messageMediaType({
    attachment: { media_kind: mediaKind, type: "application/octet-stream" },
    msg_type: "legacy-type",
  }), category)
}

assert.equal(messageMediaType({ attachment: { type: "image/webp" }, msg_type: "sticker" }), "sticker")
assert.equal(messageMediaType({ attachment: { type: "video/mp4" }, msg_type: "attachment" }), "attachment")
assert.equal(messageMediaType({ attachment: { type: "video/mp4" } }), "video")
assert.equal(messageMediaType({ attachment: {} }), "attachment")

const cases = [
  ["video", "video/mp4"],
  ["voice", "audio/ogg"],
  ["document", "application/pdf"],
  ["gif", "image/gif"],
]
for (const [mediaKind, type] of cases) {
  const optimistic = {
    optimistic_id: `local-${mediaKind}`,
    optimisticStatus: "sending",
    protocol: "signal",
    contactId: "alice",
    direction: "out",
    text: "",
    timestamp: 1,
    known_message_ids: [],
    attachment: { type, name: `file-${mediaKind}` },
  }
  const real = {
    id: `real-${mediaKind}`,
    direction: "out",
    text: "",
    timestamp: 2,
    attachment: { type, media_kind: mediaKind },
  }
  const result = reconcileOptimisticMessages([real], [optimistic], "signal", "alice")
  assert.equal(result.visible.length, 0)
  assert.equal(result.optimistic[0].confirmed_message_id, `real-${mediaKind}`)
}
""")
