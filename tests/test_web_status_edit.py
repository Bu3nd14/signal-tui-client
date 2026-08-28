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
