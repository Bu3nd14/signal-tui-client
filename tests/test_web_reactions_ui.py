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
assert.equal(intervals[0].delay, 15000);
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
    assert "startReaction(item, event)" in source
    assert len(emojis) > 6
    assert {"👍", "❤️", "😂", "🔥", "🎉", "💯", "🤣", "💪"} <= set(emojis)
    assert 'state.active?.protocol === "telegram"' in source
    assert "state.telegramReactions?.length" in source
    assert ": REACTION_EMOJIS" in source
    assert 'apiFetch("/api/messages/reaction"' in source
    assert "applyReactionUpdate({" in source


def test_telegram_picker_uses_available_reactions_and_falls_back():
    _run_node(r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const start = app.indexOf("function closeReactionPicker(");
const end = app.indexOf("\nasync function sendReaction", start);
function node() {
  return {
    children: [], parentNode: null,
    append(child) { child.parentNode = this; this.children.push(child); },
    remove() {}, setAttribute() {}, addEventListener() {}, focus() {},
  };
}
globalThis.document = { createElement: node };
globalThis.REACTION_EMOJIS = ["👍", "💀"];
globalThis.sendReaction = () => {};
globalThis.state = {
  active: { protocol: "telegram" }, telegramReactions: ["❤️", "🔥"], reactionPicker: null,
};
vm.runInThisContext(app.slice(start, end));
const parent = node();
const anchor = node();
parent.append(anchor);
startReaction({ id: "1" }, { stopPropagation() {}, currentTarget: anchor });
assert.deepEqual(state.reactionPicker.picker.children.map((button) => button.textContent), ["❤️", "🔥"]);
closeReactionPicker();
state.telegramReactions = [];
startReaction({ id: "1" }, { stopPropagation() {}, currentTarget: anchor });
assert.deepEqual(state.reactionPicker.picker.children.map((button) => button.textContent), ["👍", "💀"]);
""")


def test_render_messages_preserves_scroll_when_user_scrolled_up():
    _run_node(r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const start = app.indexOf("function renderMessages(");
const end = app.indexOf("\nfunction copyReactions", start);
let bottomCalls = 0;
globalThis.state = {
  optimistic: [], active: { protocol: "telegram", id: "42" }, userScrolledUp: true,
};
globalThis.window = { SignalTuiReconcile: {
  reconcileOptimisticMessages: () => ({ optimistic: [], visible: [] }),
  messageDisplayText: (item) => item.text || "",
} };
globalThis.pruneOrphanObjectUrls = () => {};
globalThis.scrollThreadToBottom = () => { bottomCalls += 1; };
globalThis.timestampMilliseconds = Number;
globalThis.formatTimestamp = () => "10:00";
globalThis.appendRenderedQuote = () => {};
globalThis.startReply = () => {};
function node() {
  return {
    className: "", textContent: "", children: [], parentNode: null,
    append(...children) { for (const child of children) { child.parentNode = this; this.children.push(child); } },
    replaceChildren() { this.children = []; }, setAttribute() {}, addEventListener() {},
  };
}
globalThis.document = { createElement: node };
globalThis.elements = { messages: node() };
elements.messages.scrollTop = 240;
elements.messages.scrollHeight = 1200;
elements.messages.clientHeight = 600;
vm.runInThisContext(app.slice(start, end));
renderMessages([], "telegram");
assert.equal(bottomCalls, 0);
assert.equal(elements.messages.scrollTop, 240);
""")


def test_render_messages_scrolls_to_bottom_when_user_is_at_bottom():
    _run_node(r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const start = app.indexOf("function renderMessages(");
const end = app.indexOf("\nfunction copyReactions", start);
let bottomCalls = 0;
globalThis.state = {
  optimistic: [], active: { protocol: "telegram", id: "42" }, userScrolledUp: false,
};
globalThis.window = { SignalTuiReconcile: {
  reconcileOptimisticMessages: () => ({ optimistic: [], visible: [] }),
  messageDisplayText: (item) => item.text || "",
} };
globalThis.pruneOrphanObjectUrls = () => {};
globalThis.timestampMilliseconds = Number;
globalThis.formatTimestamp = () => "10:00";
globalThis.appendRenderedQuote = () => {};
globalThis.startReply = () => {};
function node() {
  return {
    className: "", textContent: "", children: [], parentNode: null,
    append(...children) { for (const child of children) { child.parentNode = this; this.children.push(child); } },
    replaceChildren() { this.children = []; }, setAttribute() {}, addEventListener() {},
  };
}
globalThis.document = { createElement: node };
globalThis.elements = { messages: node() };
globalThis.scrollThreadToBottom = () => {
  bottomCalls += 1;
  elements.messages.scrollTop = elements.messages.scrollHeight;
};
vm.runInThisContext(app.slice(start, end));

elements.messages.scrollHeight = 1200;
elements.messages.scrollTop = 600;
elements.messages.clientHeight = 600;
renderMessages([], "telegram");
assert.equal(bottomCalls, 1);
assert.equal(elements.messages.scrollTop, 1200);
""")


def test_render_messages_scrolls_to_bottom_after_composer_layout_change():
    _run_node(r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const start = app.indexOf("function renderMessages(");
const end = app.indexOf("\nfunction copyReactions", start);
let bottomCalls = 0;
globalThis.state = {
  optimistic: [], active: { protocol: "telegram", id: "42" }, userScrolledUp: false,
};
globalThis.window = { SignalTuiReconcile: {
  reconcileOptimisticMessages: () => ({ optimistic: [], visible: [] }),
  messageDisplayText: (item) => item.text || "",
} };
globalThis.pruneOrphanObjectUrls = () => {};
globalThis.timestampMilliseconds = Number;
globalThis.formatTimestamp = () => "10:00";
globalThis.appendRenderedQuote = () => {};
globalThis.startReply = () => {};
function node() {
  return {
    className: "", textContent: "", children: [], parentNode: null,
    append(...children) { for (const child of children) { child.parentNode = this; this.children.push(child); } },
    replaceChildren() { this.children = []; }, setAttribute() {}, addEventListener() {},
  };
}
globalThis.document = { createElement: node };
globalThis.elements = { messages: node() };
globalThis.scrollThreadToBottom = () => {
  bottomCalls += 1;
  elements.messages.scrollTop = elements.messages.scrollHeight;
};
elements.messages.scrollHeight = 1500;
elements.messages.scrollTop = 1050;
elements.messages.clientHeight = 340;
vm.runInThisContext(app.slice(start, end));
renderMessages([], "telegram");
assert.equal(bottomCalls, 1);
assert.equal(elements.messages.scrollTop, 1500);
""")


def test_render_messages_bottom_threshold_is_inclusive():
    _run_node(r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const start = app.indexOf("function renderMessages(");
const end = app.indexOf("\nfunction copyReactions", start);
let bottomCalls = 0;
globalThis.state = {
  optimistic: [], active: { protocol: "telegram", id: "42" }, userScrolledUp: true,
};
globalThis.window = { SignalTuiReconcile: {
  reconcileOptimisticMessages: () => ({ optimistic: [], visible: [] }),
  messageDisplayText: (item) => item.text || "",
} };
globalThis.pruneOrphanObjectUrls = () => {};
globalThis.timestampMilliseconds = Number;
globalThis.formatTimestamp = () => "10:00";
globalThis.appendRenderedQuote = () => {};
globalThis.startReply = () => {};
function node() {
  return {
    className: "", textContent: "", children: [], parentNode: null,
    append(...children) { for (const child of children) { child.parentNode = this; this.children.push(child); } },
    replaceChildren() { this.children = []; }, setAttribute() {}, addEventListener() {},
  };
}
globalThis.document = { createElement: node };
globalThis.elements = { messages: node() };
globalThis.scrollThreadToBottom = () => {
  bottomCalls += 1;
  elements.messages.scrollTop = elements.messages.scrollHeight;
};
vm.runInThisContext(app.slice(start, end));

elements.messages.scrollHeight = 1200;
elements.messages.clientHeight = 600;
elements.messages.scrollTop = 520;
renderMessages([], "telegram");
assert.equal(bottomCalls, 1);
assert.equal(elements.messages.scrollTop, 1200);

bottomCalls = 0;
elements.messages.scrollTop = 519;
renderMessages([], "telegram");
assert.equal(bottomCalls, 0);
assert.equal(elements.messages.scrollTop, 519);
""")


def test_pickers_close_on_pointerdown_outside():
    _run_node(r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const pointerMark = app.indexOf('"pointerdown"');
const start = app.lastIndexOf("document.addEventListener(", pointerMark);
const end = app.indexOf("true\n);", pointerMark) + 7;
let closed = 0;
let emojiClosed = 0;
globalThis.state = { reactionPicker: null };
globalThis.closeReactionPicker = () => { closed += 1; state.reactionPicker = null; };
const emojiPicker = { hidden: true, contains: (t) => t === "emojiTab" || t === "emojiCell" };
const emojiToggle = { contains: (t) => t === "toggleChild" };
globalThis.elements = { emojiPicker, emojiToggle };
globalThis.closeEmojiPicker = ({ focus = true } = {}) => { emojiClosed += 1; emojiPicker.hidden = true; };
let handler = null;
globalThis.document = { addEventListener(type, callback) { handler = callback; } };
vm.runInThisContext(app.slice(start, end));
assert.ok(handler);

// ---- reaction picker ----
// picker aperto: click FUORI dal picker e dal bottone -> chiude
state.reactionPicker = {
  anchor: { contains: (t) => t === "anchorChild" },
  picker: { contains: (t) => t === "emoji" },
};
handler({ target: "altrove" });
assert.equal(closed, 1);
assert.equal(state.reactionPicker, null);

// riapriamo: click su una emoji del picker -> resta aperto
closed = 0;
state.reactionPicker = {
  anchor: { contains: (t) => t === "anchorChild" },
  picker: { contains: (t) => t === "emoji" },
};
handler({ target: "emoji" });
assert.equal(closed, 0);
assert.ok(state.reactionPicker);

// click sul bottone anchor -> resta aperto (lo gestisce il toggle)
handler({ target: state.reactionPicker.anchor });
assert.equal(closed, 0);

// click su un figlio del bottone -> resta aperto
handler({ target: "anchorChild" });
assert.equal(closed, 0);

// ---- emoji picker dell'editor ----
// nessun picker aperto: click generico -> nessuna chiusura
closed = 0;
state.reactionPicker = null;
emojiClosed = 0;
handler({ target: "altrove" });
assert.equal(emojiClosed, 0);

// emoji picker aperto: click fuori -> chiude
emojiPicker.hidden = false;
handler({ target: "altrove" });
assert.equal(emojiClosed, 1);
assert.equal(emojiPicker.hidden, true);

// riapriamo: click dentro il picker -> resta aperto
emojiClosed = 0;
emojiPicker.hidden = false;
handler({ target: "emojiCell" });
assert.equal(emojiClosed, 0);
assert.equal(emojiPicker.hidden, false);

// click sul toggle -> resta aperto (lo gestisce il toggle)
handler({ target: emojiToggle });
assert.equal(emojiClosed, 0);

// click su un figlio del toggle -> resta aperto
handler({ target: "toggleChild" });
assert.equal(emojiClosed, 0);
""")


def test_ctrl_shift_x_toggles_emoji_picker_from_composer():
    _run_node(r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const start = app.indexOf('elements.messageInput.addEventListener("keydown"');
const end = app.indexOf("});", start) + 3;
let toggled = 0;
globalThis.state = { editing: false, sending: false, editSending: false };
let handler = null;
globalThis.elements = { messageInput: { addEventListener(type, cb) { handler = cb; } } };
globalThis.cancelEdit = () => {};
globalThis.toggleEmojiPicker = () => { toggled += 1; };
globalThis.submitEdit = () => {};
globalThis.submitMessage = () => {};
vm.runInThisContext(app.slice(start, end));
assert.ok(handler);

// Ctrl+Shift+X nel campo di testo -> apre/chiude il picker emoji
handler({ ctrlKey: true, shiftKey: true, key: "x", preventDefault() {} });
assert.equal(toggled, 1);

// altro tasto (Enter senza shift) -> non tocca il picker
handler({ ctrlKey: false, shiftKey: false, key: "Enter", preventDefault() {} });
assert.equal(toggled, 1);
""")
