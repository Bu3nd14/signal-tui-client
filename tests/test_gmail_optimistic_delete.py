"""Gmail optimistic delete: the list updates at once, the Trash move runs in
the background and is undone only when the server really refuses it.

The real ``gmail.js`` functions run in Node with stubbed DOM helpers and a
scripted ``request`` (``test_gmail_images_default.py`` helpers).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_gmail_images_default import (
    _GMAIL_JS_PATH,
    _NEXT_JS_TOP_LEVEL,
    _js_functions,
    _run_node,
)

_ROOT = Path(__file__).resolve().parents[1]


def _async_js_function(name: str) -> str:
    """A top-level ``async function`` block of gmail.js (the shared helper
    only extracts plain ``function`` blocks)."""
    source = _GMAIL_JS_PATH.read_text(encoding="utf-8")
    marker = f"  async function {name}("
    start = source.index(marker)
    following = _NEXT_JS_TOP_LEVEL.search(source, start + len(marker))
    end = following.start() + 1 if following else len(source)
    return source[start:end]


# Shared harness: three messages, a scripted request() (`responses` holds one
# entry per call: "ok", "hang", or an error status, 0 meaning a network error)
# and a loadMessages stub that records its options.
_HARNESS = r"""
const assert = require("node:assert/strict");
const DELETE_TIMEOUT_MS = 30;
const DELETE_RETRY_DELAY_MS = 1;
const MAX_BULK_DELETE = 200;
const message = (uid) => ({ uid, subject: `m${uid}`, seen: true });
const state = {
  folder: "INBOX",
  messages: [message(7), message(5), message(3)],
  selectedUid: 5,
  selectedUids: new Set(),
  selectionMode: false,
  pendingDeletes: new Map(),
  deleteQueue: [],
  deleteWorker: false,
  listSeq: 0,
};
const log = [];
const calls = [];
let responses = [];
const window = {
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (id) => clearTimeout(id),
};
function request(path, options) {
  const body = JSON.parse(options.body);
  calls.push({ path, uids: body.uids, folder: body.folder, keepalive: options.keepalive });
  const next = responses.length ? responses.shift() : "ok";
  if (next === "ok") return Promise.resolve({ ok: true });
  if (next === "hang") {
    return new Promise((resolve, reject) => {
      options.signal.addEventListener("abort", () => {
        const error = new Error("aborted");
        error.name = "AbortError";
        reject(error);
      });
    });
  }
  const error = new Error(next ? `HTTP ${next}` : "Errore di rete");
  if (next) error.status = next;
  return Promise.reject(error);
}
function closeReader() { log.push("close"); state.selectedUid = null; }
function renderList() { log.push(["render", state.messages.map((m) => m.uid)]); }
function showNotice(text) { log.push(["notice", text]); }
function showError(text) { log.push(["error", text]); }
async function loadMessages(options) { log.push(["reload", options]); }
const uids = () => state.messages.map((m) => m.uid);
const idle = () => new Promise((resolve) => {
  const check = () => (state.deleteWorker ? setTimeout(check, 2) : resolve());
  setTimeout(check, 0);
});
"""


def _delete_harness() -> str:
    return (
        _HARNESS
        + _js_functions(
            "normalizeUid",
            "sameUid",
            "uidKey",
            "trashMessages",
            "deleteErrorMessage",
            "restoreDeleted",
            "deleteMessage",
            "deleteSelected",
        )
        + _async_js_function("sendDeleteBatch")
        + _async_js_function("flushDeletes")
    )


def test_delete_removes_the_message_before_the_server_answers() -> None:
    _run_node(
        _delete_harness()
        + r"""
responses = ["hang"];
deleteMessage();
// Synchronously gone: no wait for IMAP.
assert.deepEqual(uids(), [7, 3]);
assert.ok(log.includes("close"));
assert.equal(log.some((e) => e[0] === "notice"), false);  // no banner
assert.equal(calls.length, 1);
assert.deepEqual(calls[0], {
  path: "api/gmail/messages/bulk-delete", uids: [5], folder: "INBOX", keepalive: true,
});
idle().then(() => {
  // The hung request timed out and the retry landed: still gone, reconciled.
  assert.equal(calls.length, 2);
  assert.deepEqual(uids(), [7, 3]);
  assert.equal(state.pendingDeletes.get("5").settledSeq, 0);
  assert.deepEqual(log.at(-1), ["reload", { force: true, background: true }]);
  assert.equal(log.some((e) => e[0] === "error"), false);
});
"""
    )


@pytest.mark.parametrize(
    ("responses", "attempts"),
    [
        ("[500]", 1),  # a definite server refusal is not retried
        ("[0, 0]", 2),  # network error twice
        ("[502, 502]", 2),  # Gmail upstream down twice
        ('["hang", "hang"]', 2),  # timeout twice
    ],
)
def test_failed_delete_puts_the_message_back_in_place(
    responses: str, attempts: int
) -> None:
    _run_node(
        _delete_harness()
        + f"responses = {responses};\n"
        + r"""
deleteMessage();
assert.deepEqual(uids(), [7, 3]);
idle().then(() => {
"""
        + f"  assert.equal(calls.length, {attempts});\n"
        + r"""
  assert.deepEqual(uids(), [7, 5, 3]);         // back at its original index
  assert.equal(state.pendingDeletes.size, 0);
  assert.equal(log.filter((e) => e[0] === "error").length, 1);
  assert.equal(log.some((e) => e[0] === "reload"), false);
});
"""
    )


def test_retry_after_a_network_error_keeps_the_delete() -> None:
    _run_node(
        _delete_harness()
        + r"""
responses = [0, "ok"];
deleteMessage();
idle().then(() => {
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[1].uids, [5]);
  assert.deepEqual(uids(), [7, 3]);
  assert.equal(log.some((e) => e[0] === "error"), false);
});
"""
    )


def test_delete_asks_no_confirmation() -> None:
    _run_node(
        _delete_harness()
        + r"""
window.confirm = () => { throw new Error("no confirm dialog expected"); };
deleteMessage();
assert.deepEqual(uids(), [7, 3]);
state.selectedUids = new Set(["7"]);
deleteSelected();
assert.deepEqual(uids(), [3]);
"""
    )


def test_deletes_made_while_one_is_in_flight_share_one_request() -> None:
    _run_node(
        _delete_harness()
        + r"""
let release;
const first = new Promise((resolve) => { release = resolve; });
const original = request;
let n = 0;
request = (path, options) => (n++ === 0 ? first.then(() => original(path, options)) : original(path, options));
deleteMessage();                       // 5: in flight
state.selectedUid = 7; deleteMessage(); // queued
state.selectedUid = 3; deleteMessage(); // queued
assert.deepEqual(uids(), []);
release();
idle().then(() => {
  assert.deepEqual(calls.map((c) => c.uids), [[5], [7, 3]]);
  assert.equal(log.filter((e) => e[0] === "reload").length, 1);
});
"""
    )


def test_bulk_delete_is_optimistic_and_leaves_selection_mode() -> None:
    _run_node(
        _delete_harness()
        + r"""
state.selectionMode = true;
state.selectedUid = null;
state.selectedUids = new Set(["7", "3"]);
deleteSelected();
assert.deepEqual(uids(), [5]);
assert.equal(state.selectionMode, false);
assert.equal(state.selectedUids.size, 0);
assert.equal(log.some((e) => e[0] === "notice"), false);
idle().then(() => {
  assert.deepEqual(calls.map((c) => c.uids), [[7, 3]]);
});
"""
    )


def test_bulk_delete_failure_restores_every_message_in_order() -> None:
    _run_node(
        _delete_harness()
        + r"""
responses = [500];
state.selectedUid = null;
state.selectedUids = new Set(["7", "3"]);
deleteSelected();
idle().then(() => {
  assert.deepEqual(uids(), [7, 5, 3]);
});
"""
    )


# ── reconciliation in loadMessages ───────────────────────────────────────────

_LOAD_HARNESS = r"""
const assert = require("node:assert/strict");
const FETCH_LIMIT = 200;
const state = {
  configured: true, open: true, folder: "INBOX", uidvalidity: 1,
  messages: [], pageIndex: 2, listSeq: 0, selectedUid: null,
  selectedUids: new Set(["5", "9"]), selectionMode: true,
  pendingDeletes: new Map(),
};
const elements = { listSummary: { textContent: "" } };
const log = [];
let serverUids = [9, 5, 3];
function request(path) {
  log.push(["request", path]);
  return Promise.resolve({ json: async () => ({
    folder: "INBOX", uidvalidity: 1,
    messages: serverUids.map((uid) => ({ uid, subject: "", seen: true })),
  }) });
}
function displayAddress(value) { return value || ""; }
function syncRowSelection() {}
function updateSelectionActions() {}
function setListLoading(loading) { log.push(["loading", loading]); }
function renderList() {}
function renderAccountStatus() {}
function renderListPlaceholder() { log.push("placeholder"); }
function closeReader() { log.push("close"); }
function showNotice() {}
function showError(text) { log.push(["error", text]); }
const uids = () => state.messages.map((m) => m.uid);
"""


def _load_harness() -> str:
    return (
        _LOAD_HARNESS
        + _js_functions("normalizeUid", "sameUid", "uidKey")
        + _async_js_function("loadMessages")
    )


def test_reload_started_before_the_delete_landed_does_not_resurrect_it() -> None:
    _run_node(
        _load_harness()
        + r"""
// The load starts (sequence 1), then the delete of 5 is confirmed while it is
// still in flight: its server snapshot may predate the delete.
const stale = loadMessages({ force: true });
state.pendingDeletes.set("5", { settledSeq: state.listSeq });
stale.then(() => {
  assert.deepEqual(uids(), [9, 3]);
  assert.ok(state.pendingDeletes.has("5"));
});
"""
    )


def test_load_after_the_delete_settled_clears_the_filter() -> None:
    _run_node(
        _load_harness()
        + r"""
serverUids = [9, 3];
state.listSeq = 4;
state.pendingDeletes.set("5", { settledSeq: 4 });
state.pendingDeletes.set("8", { settledSeq: null });   // still in flight
loadMessages({ force: true }).then(() => {
  assert.deepEqual(uids(), [9, 3]);
  assert.equal(state.pendingDeletes.has("5"), false);
  assert.ok(state.pendingDeletes.has("8"));
});
"""
    )


def test_unsettled_delete_stays_hidden_from_any_reload() -> None:
    _run_node(
        _load_harness()
        + r"""
state.listSeq = 10;
state.pendingDeletes.set("5", { settledSeq: null });
loadMessages({ force: true }).then(() => {
  assert.deepEqual(uids(), [9, 3]);
  assert.ok(state.pendingDeletes.has("5"));
});
"""
    )


def test_background_reload_keeps_page_and_selection_quietly() -> None:
    _run_node(
        _load_harness()
        + r"""
serverUids = [9, 3];
loadMessages({ force: true, background: true }).then(() => {
  assert.deepEqual(uids(), [9, 3]);
  assert.equal(state.pageIndex, 2);               // clamped later by renderList
  assert.equal(state.selectionMode, true);
  assert.deepEqual([...state.selectedUids], ["9"]); // 5 is gone: pruned
  assert.equal(elements.listSummary.textContent, "");
  assert.equal(log.some((e) => e[0] === "loading" && e[1] === true), false);
  assert.ok(log[0][1].includes("refresh=1"));
});
"""
    )


def test_background_reload_failure_keeps_the_list() -> None:
    _run_node(
        _load_harness()
        + r"""
state.messages = [{ uid: 9 }, { uid: 3 }];
request = () => Promise.reject(Object.assign(new Error("HTTP 502"), { status: 502 }));
console.debug = () => {};
loadMessages({ background: true }).then(() => {
  assert.deepEqual(uids(), [9, 3]);
  assert.equal(log.some((e) => e[0] === "error" || e === "placeholder"), false);
});
"""
    )


def test_foreground_reload_still_resets_selection_and_page() -> None:
    _run_node(
        _load_harness()
        + r"""
loadMessages({ force: true }).then(() => {
  assert.equal(state.pageIndex, 0);
  assert.equal(state.selectionMode, false);
  assert.equal(state.selectedUids.size, 0);
  assert.deepEqual(log.find((e) => e[0] === "loading"), ["loading", true]);
});
"""
    )


def test_index_and_handlers_wire_the_optimistic_delete() -> None:
    script = _GMAIL_JS_PATH.read_text(encoding="utf-8")
    index = (_ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
    assert "gmail.js?v=29" in index
    assert "    deleteMessage();\n" in script
    assert "    deleteSelected();\n" in script
    assert "bulkDeleting" not in script
    assert "state.deleting" not in script
    assert "window.confirm(" not in script
    assert "spostat" not in script.lower()
