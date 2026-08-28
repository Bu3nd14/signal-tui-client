from __future__ import annotations

import subprocess


def _run_node(source: str) -> None:
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_render_delivery_ticks_and_edited_marker():
    _run_node(r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const start = app.indexOf("function renderMessages(");
const end = app.indexOf("\nasync function loadMessages", start);
globalThis.state = { optimistic: [], active: { protocol: "signal", id: "42" } };
globalThis.window = { SignalTuiReconcile: {
  reconcileOptimisticMessages: (messages) => ({ optimistic: [], visible: [] }),
  messageDisplayText: (item) => item.text || "",
} };
globalThis.pruneOrphanObjectUrls = () => {};
globalThis.scrollThreadToBottom = () => {};
globalThis.requestAnimationFrame = () => {};
globalThis.timestampMilliseconds = Number;
globalThis.formatTimestamp = () => "10:00";
globalThis.appendRenderedQuote = () => {};
globalThis.startReply = () => {};
function node(tag) {
  return {
    tag, className: "", textContent: "", children: [], attributes: {},
    append(...children) { this.children.push(...children); },
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener() {}, classList: { add() {} },
  };
}
globalThis.document = { createElement: node };
globalThis.elements = { messages: { children: [], replaceChildren() { this.children = []; }, append(child) { this.children.push(child); } } };
vm.runInThisContext(app.slice(start, end));
renderMessages([
  { id: "1", direction: "out", text: "letto", timestamp: 1, status: "read", edited: true },
  { id: "2", direction: "out", text: "attesa", timestamp: 2, status: "pending" },
  { id: "3", direction: "in", text: "ricevuto", timestamp: 3, status: "read" },
], "signal");
const firstTime = elements.messages.children[0].children[0].children.at(-1);
assert.equal(firstTime.children[0].textContent, " · modificato");
assert.equal(firstTime.children[1].textContent, "✓✓");
assert.equal(firstTime.children[1].className, "message-tick tick-read");
assert.equal(firstTime.children[1].title, "Letto");
assert.equal(elements.messages.children[0].attributes["data-mid"], "1");
assert.equal(elements.messages.children[0].attributes["data-ts"], "1");
assert.equal(state.messageNodes.get("2").tickEl.textContent, "🕓");
assert.equal(state.messageNodes.get("3").tickEl, null);
const editable = { id: "4", edit_id: "edit-4", direction: "out", text: "edit", timestamp: 4, status: "sent" };
renderMessages([editable], "signal");
assert.equal(elements.messages.children[0].children[2].className, "message-edit");
assert.equal(elements.messages.children[0].children[2].textContent, "✎");
""")


def test_apply_receipt_updates_uses_rank_and_timestamp_fallback():
    _run_node(r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const rankStart = app.indexOf("const STATUS_RANK");
const rankEnd = app.indexOf("\nasync function loadMessages", rankStart);
const applyStart = app.indexOf("function applyReceiptUpdates(");
const applyEnd = app.indexOf("\nfunction markRead", applyStart);
globalThis.timestampMilliseconds = Number;
const directTick = { className: "", textContent: "" };
const fallbackTick = { className: "", textContent: "" };
globalThis.state = {
  active: { protocol: "signal", id: "42" },
  messages: [
    { id: "one", text: "A", timestamp: 1000, status: "sent" },
    { id: "local", text: "B", timestamp: 5000, status: "sent" },
  ],
  messageNodes: new Map([
    ["one", { tickEl: directTick, text: "A", ts: 1000, status: "sent" }],
    ["local", { tickEl: fallbackTick, text: "B", ts: 5000, status: "sent" }],
  ]),
};
vm.runInThisContext(app.slice(rankStart, rankEnd));
vm.runInThisContext(app.slice(applyStart, applyEnd));
applyReceiptUpdates({ protocol: "signal", contact_id: "42", updates: [
  { id: "one", timestamp: 1000, text: "A", status: "delivered" },
  { id: null, timestamp: 5001, text: "B", status: "read" },
] });
assert.equal(directTick.textContent, "✓✓");
assert.equal(state.messageNodes.get("one").status, "delivered");
assert.equal(fallbackTick.className, "message-tick tick-read");
assert.equal(state.messages[1].status, "read");
applyReceiptUpdates({ protocol: "signal", contact_id: "42", updates: [{ id: "one", timestamp: 1000, text: "A", status: "sent" }] });
assert.equal(state.messageNodes.get("one").status, "delivered");
state.active = { protocol: "signal", id: "other" };
applyReceiptUpdates({ protocol: "signal", contact_id: "42", updates: [{ id: "one", status: "read" }] });
assert.equal(state.messageNodes.get("one").status, "delivered");
""")


def test_remote_edit_updates_message_in_place():
    _run_node(r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const start = app.indexOf("function ensureEditedMarker(");
const end = app.indexOf("\nfunction markRead", start);
globalThis.timestampMilliseconds = Number;
function node() { return { className: "", textContent: "", remove() { this.removed = true; } }; }
globalThis.document = { createElement: node };
const textEl = { textContent: "Prima" };
const timeEl = { children: [], append(child) { this.children.push(child); }, insertBefore(child) { this.children.unshift(child); }, querySelector(selector) { return selector === ".message-edited" ? this.children.find((child) => child.className === "message-edited") : null; } };
const entry = { textEl, timeEl, tickEl: {}, ts: 1000, text: "Prima", edited: false };
globalThis.state = {
  active: { protocol: "signal", id: "42" },
  messageNodes: new Map([["db-id", entry]]),
  messages: [{ id: "db-id", timestamp: 1000, text: "Prima", edited: false }],
};
vm.runInThisContext(app.slice(start, end));
applyRemoteEdit({ protocol: "signal", contact_id: "42", message_id: "1000", timestamp: 1000, old_text: "Prima", text: "Dopo" });
assert.equal(textEl.textContent, "Dopo");
assert.equal(timeEl.children[0].textContent, " · modificato");
assert.equal(state.messages[0].edited, true);
state.active.id = "other";
applyRemoteEdit({ protocol: "signal", contact_id: "42", message_id: "db-id", text: "Ignora" });
assert.equal(textEl.textContent, "Dopo");
""")


def test_submit_edit_success_failure_and_unchanged_text():
    _run_node(r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const start = app.indexOf("function ensureEditedMarker(");
const end = app.indexOf("\nfunction markRead", start);
globalThis.timestampMilliseconds = Number;
function marker() { return { className: "", textContent: "", remove() { this.removed = true; } }; }
globalThis.document = { createElement: marker };
const textEl = { textContent: "Prima" };
const timeEl = { children: [], append(child) { this.children.push(child); }, insertBefore(child) { this.children.unshift(child); }, querySelector(selector) { return selector === ".message-edited" ? this.children.find((child) => child.className === "message-edited" && !child.removed) : null; } };
const entry = { textEl, timeEl, tickEl: {}, ts: 1000, text: "Prima", edited: false };
globalThis.state = {
  active: { protocol: "signal", id: "42" }, editSending: false,
  editing: { edit_id: "server-1", id: "db-1", timestamp: 1000, oldText: "Prima", protocol: "signal", contactId: "42" },
  messageNodes: new Map([["db-1", entry]]),
  messages: [{ id: "db-1", timestamp: 1000, text: "Prima", edited: false }],
};
globalThis.elements = { messageInput: { value: "Dopo", focus() {} } };
globalThis.resizeComposer = () => {};
globalThis.updateReplyBanner = () => {};
globalThis.updateComposer = () => {};
const errors = [];
globalThis.showError = (message) => errors.push(message);
vm.runInThisContext(app.slice(start, end));
let requests = [];
globalThis.apiFetch = async (_path, options) => { requests.push(JSON.parse(options.body)); };
(async () => {
  await submitEdit();
  assert.equal(textEl.textContent, "Dopo");
  assert.equal(state.messages[0].edited, true);
  assert.equal(requests[0].message_id, "server-1");
  assert.equal(state.editing, null);

  entry.text = "Dopo"; entry.edited = false; timeEl.children = []; textEl.textContent = "Dopo";
  state.messages[0] = { id: "db-1", timestamp: 1000, text: "Dopo", edited: false };
  state.editing = { edit_id: "server-1", id: "db-1", timestamp: 1000, oldText: "Dopo", protocol: "signal", contactId: "42" };
  elements.messageInput.value = "Tentativo";
  globalThis.apiFetch = async () => { throw new Error("network"); };
  await submitEdit();
  assert.equal(textEl.textContent, "Dopo");
  assert.equal(state.messages[0].edited, false);
  assert.equal(errors.at(-1), "Modifica non riuscita.");
  assert.equal(state.editing.oldText, "Dopo");
  assert.equal(elements.messageInput.value, "Tentativo");

  let calls = 0;
  globalThis.apiFetch = async () => { calls += 1; };
  elements.messageInput.value = "  Dopo  ";
  await submitEdit();
  assert.equal(calls, 0);
  assert.equal(state.editing, null);
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")
