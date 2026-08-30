from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


def _run_node(source: str) -> None:
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_render_messages_mounts_reaction_chips():
    _run_node(r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const start = app.indexOf("function renderMessages(");
const end = app.indexOf("\nasync function loadMessages", start);
globalThis.state = { optimistic: [], active: { protocol: "signal", id: "42" } };
globalThis.window = { SignalTuiReconcile: {
  reconcileOptimisticMessages: () => ({ optimistic: [], visible: [] }),
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
    tag, className: "", textContent: "", title: "", children: [], attributes: {}, parentNode: null,
    append(...children) { for (const child of children) { child.parentNode = this; this.children.push(child); } },
    replaceChildren(...children) { this.children = []; this.append(...children); },
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener() {}, classList: { add() {} },
    remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((child) => child !== this); },
  };
}
globalThis.document = { createElement: node };
globalThis.elements = { messages: node("main") };
vm.runInThisContext(app.slice(start, end));
const reactions = [
  { emoji: "👍", count: 2, is_mine: true, authors: ["Giovanni", "You"] },
  { emoji: "❤️", count: 1, is_mine: false, authors: ["Anna"] },
];
renderMessages([
  { id: "1", direction: "out", text: "ciao", timestamp: 1, status: "read", reactions },
  { id: "2", direction: "in", text: "nessuna", timestamp: 2 },
  { id: "3", direction: "out", text: "optimistic", timestamp: 3, optimistic_id: "local", reactions },
], "signal");
const firstBubble = elements.messages.children[0].children[0];
const reactionGroup = firstBubble.children.at(-1);
assert.equal(reactionGroup.className, "message-reactions");
assert.equal(firstBubble.children.at(-2).className, "message-time");
assert.equal(reactionGroup.attributes.role, "group");
assert.equal(reactionGroup.attributes["aria-label"], "Reazioni: 👍 2, ❤️");
assert.equal(reactionGroup.children.length, 2);
assert.equal(reactionGroup.children[0].className, "reaction-chip mine");
assert.equal(reactionGroup.children[0].title, "Giovanni, You");
assert.equal(reactionGroup.children[0].children[0].className, "reaction-count");
assert.equal(reactionGroup.children[0].children[0].textContent, "2");
assert.equal(reactionGroup.children[1].children.length, 0);
assert.equal(elements.messages.children[0].children[1].className, "message-reply");
// I messaggi OUT non hanno il bottone reaction (solo reply); IN sì.
assert.equal(elements.messages.children[0].children.length, 2);
assert.equal(elements.messages.children[1].children[1].className, "message-reply");
assert.equal(elements.messages.children[1].children[2].className, "message-reaction");
assert.equal(elements.messages.children[1].children[0].children.at(-1).className, "message-time");
assert.equal(elements.messages.children[2].children[0].children.at(-1).className, "message-time");
assert.deepEqual(state.messageNodes.get("1").reactions, reactions);
assert.notEqual(state.messageNodes.get("1").reactions, reactions);
assert.equal(state.messageNodes.get("2").reactionsEl, null);
""")


def test_apply_reaction_update_add_change_remove_guard_and_fallback():
    _run_node(r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const start = app.indexOf("function copyReactions(");
const end = app.indexOf("\nfunction markRead", start);
function node(tag) {
  return {
    tag, className: "", textContent: "", title: "", children: [], attributes: {}, parentNode: null,
    append(...children) { for (const child of children) { child.parentNode = this; this.children.push(child); } },
    replaceChildren(...children) { this.children = []; this.append(...children); },
    setAttribute(name, value) { this.attributes[name] = value; },
    remove() {
      if (!this.parentNode) return;
      this.parentNode.children = this.parentNode.children.filter((child) => child !== this);
      this.parentNode = null;
    },
  };
}
globalThis.document = { createElement: node };
globalThis.timestampMilliseconds = Number;
const bubbleOne = node("div");
const timeOne = node("time");
bubbleOne.append(timeOne);
const bubbleTwo = node("div");
const timeTwo = node("time");
bubbleTwo.append(timeTwo);
const bubbleThree = node("div");
const timeThree = node("time");
bubbleThree.append(timeThree);
const firstEntry = { timeEl: timeOne, ts: 1000, reactionsEl: null, reactions: [] };
const fallbackEntry = { timeEl: timeTwo, ts: 2000, reactionsEl: null, reactions: [] };
const emptyEntry = { timeEl: timeThree, ts: 3000, reactionsEl: null, reactions: [] };
globalThis.state = {
  active: { protocol: "signal", id: "42" },
  messageNodes: new Map([["one", firstEntry], ["local", fallbackEntry], ["empty", emptyEntry]]),
  messages: [
    { id: "one", timestamp: 1000 },
    { id: "local", timestamp: 2000 },
    { id: "empty", timestamp: 3000 },
  ],
};
vm.runInThisContext(app.slice(start, end));
applyReactionUpdate({ protocol: "signal", contact_id: 42, message_id: "one", timestamp: 1000, reactions: [
  { emoji: "👍", count: 1, is_mine: false, authors: ["Anna"] },
] });
assert.equal(firstEntry.reactionsEl.className, "message-reactions");
assert.equal(bubbleOne.children.at(-1), firstEntry.reactionsEl);
assert.equal(firstEntry.reactionsEl.children.length, 1);
const originalGroup = firstEntry.reactionsEl;
applyReactionUpdate({ protocol: "signal", contact_id: "42", message_id: "one", timestamp: 1000, reactions: [
  { emoji: "❤️", count: 3, is_mine: true, authors: [] },
] });
assert.equal(firstEntry.reactionsEl, originalGroup);
assert.equal(originalGroup.children.length, 1);
assert.equal(originalGroup.children[0].textContent, "❤️");
assert.equal(originalGroup.children[0].className, "reaction-chip mine");
assert.equal(originalGroup.children[0].title, "❤️");
assert.equal(originalGroup.children[0].children[0].textContent, "3");
assert.equal(originalGroup.attributes["aria-label"], "Reazioni: ❤️ 3");
assert.deepEqual(state.messages[0].reactions, firstEntry.reactions);
applyReactionUpdate({ protocol: "signal", contact_id: "42", message_id: "one", timestamp: 1000, reactions: [] });
assert.equal(firstEntry.reactionsEl, null);
assert.equal(bubbleOne.children.length, 1);
assert.deepEqual(state.messages[0].reactions, []);

applyReactionUpdate({ protocol: "signal", contact_id: "42", message_id: "server-id", timestamp: 2000, reactions: [
  { emoji: "🔥", count: 2, is_mine: false, authors: ["Luca"] },
] });
assert.equal(fallbackEntry.reactionsEl.children[0].textContent, "🔥");
assert.equal(state.messages[1].reactions[0].count, 2);

applyReactionUpdate({ protocol: "signal", contact_id: "42", message_id: "empty", timestamp: 3000, reactions: [] });
assert.equal(emptyEntry.reactionsEl, null);
assert.equal(bubbleThree.children.length, 1);

state.active.id = "other";
applyReactionUpdate({ protocol: "signal", contact_id: "42", message_id: "empty", timestamp: 3000, reactions: [
  { emoji: "❌", count: 1, is_mine: false, authors: [] },
] });
assert.equal(emptyEntry.reactionsEl, null);
assert.deepEqual(state.messages[2].reactions, []);
""")


def test_websocket_dispatches_reaction_updates():
    source = Path("web/static/app.js").read_text(encoding="utf-8")
    assert (
        'case "reaction_update":\n          applyReactionUpdate(update.payload);'
        in source
    )


def test_open_thread_starts_refresh_timer_only_for_telegram():
    _run_node(r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const start = app.indexOf("function clearTelegramRefreshTimer(");
const end = app.indexOf("\nfunction normalizeEmojiSearch", start);
const intervals = [];
const cleared = [];
globalThis.window = {
  setInterval(callback, delay) { intervals.push({ callback, delay }); return intervals.length; },
  clearInterval(id) { cleared.push(id); },
};
globalThis.state = { active: null, editing: null, messages: [], telegramRefreshTimer: null };
globalThis.loadMessages = () => {};
globalThis.closeEmojiPicker = () => {};
globalThis.closeReactionPicker = () => {};
globalThis.cancelReply = () => {};
globalThis.cancelEdit = () => {};
globalThis.protocolIcon = () => "";
globalThis.renderContacts = () => {};
globalThis.markRead = () => {};
globalThis.abortMediaRequests = () => {};
globalThis.document = { createElement: () => ({ className: "", textContent: "" }) };
const appClasses = { add() {} };
globalThis.elements = {
  threadName: {}, threadMeta: {}, app: { classList: appClasses }, composerShell: {},
  messages: { replaceChildren() {}, append() {} },
};
vm.runInThisContext(app.slice(start, end));

openThread({ id: "42", protocol: "telegram", display_name: "Telegram" });
assert.equal(intervals.length, 1);
assert.equal(intervals[0].callback, loadMessages);
assert.equal(intervals[0].delay, 20000);
assert.equal(state.telegramRefreshTimer, 1);

openThread({ id: "alice", protocol: "signal", display_name: "Signal" });
assert.deepEqual(cleared, [1]);
assert.equal(intervals.length, 1);
assert.equal(state.telegramRefreshTimer, null);
""")


def test_reaction_button_picker_and_send_flow_are_wired():
    source = Path("web/static/app.js").read_text(encoding="utf-8")
    match = re.search(r"const REACTION_EMOJIS = (\[.*?\]);", source, re.DOTALL)
    assert match is not None
    emojis = json.loads(match.group(1))
    assert 'reaction.className = "message-reaction";' in source
    assert 'reaction.title = "Reagisci";' in source
    assert 'startReaction(item, event)' in source
    assert len(emojis) > 6
    assert {"👍", "❤️", "😂", "🔥", "🎉", "💯", "🤣", "💪"} <= set(emojis)
    assert "for (const emoji of REACTION_EMOJIS)" in source
    assert 'apiFetch("/api/messages/reaction"' in source
    assert "applyReactionUpdate({" in source
