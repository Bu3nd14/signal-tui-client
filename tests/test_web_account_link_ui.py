"""The "Collega account" dialog flow and the link-required banner (node).

The block between the ``account-link`` markers of ``app.js`` runs under node
with stubbed ``document`` / ``apiFetch`` / timers.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

PRELUDE = r"""
const assert = require("node:assert/strict")
const fs = require("node:fs")
const vm = require("node:vm")
const app = fs.readFileSync("./web/static/app.js", "utf8")
const start = app.indexOf("// --- account-link:begin")
const end = app.indexOf("// --- account-link:end")
assert.ok(start > 0 && end > start)
const block = app.slice(start, end)

function node(extra = {}) {
  return {
    hidden: false, disabled: false, textContent: "", src: "", className: "",
    dataset: {}, children: [], open: false,
    append(...c) { this.children.push(...c) },
    replaceChildren() { this.children = [] },
    addEventListener() {},
    showModal() { this.open = true },
    close() { this.open = false },
    ...extra,
  }
}
const names = ["accountLinkDialog", "accountLinkChooser", "accountLinkFlow",
  "accountLinkSpinner", "accountLinkQr", "accountLinkText", "accountLinkHint",
  "accountLinkStatus", "accountLinkRetry", "accountLinkCancel",
  "linkRequiredBanner", "linkRequiredText"]
const elements = Object.fromEntries(names.map((n) => [n, node()]))
const state = { linkRequired: new Set(), linkRequiredDismissed: new Set() }
const calls = []
const timers = []
let now = 1_000_000
let routes = {}
let contactsLoads = 0
const fakeSetTimeout = (fn, ms) => { timers.push({ fn, ms, live: true }); return timers.length - 1 }
const fakeClearTimeout = (id) => { if (timers[id]) timers[id].live = false }
async function runTimer() {
  const t = timers.filter((x) => x.live).pop()
  assert.ok(t, "no live timer")
  t.live = false
  await t.fn()
  await flush()
}
const flush = () => new Promise((r) => setImmediate(r))
function apiFetch(path, options = {}) {
  const body = options.body ? JSON.parse(options.body) : null
  calls.push([path, body])
  const handler = routes[path]
  if (!handler) return Promise.reject(Object.assign(new Error("HTTP 404"), { status: 404 }))
  const out = typeof handler === "function" ? handler(body) : handler
  if (out instanceof Error) return Promise.reject(out)
  return Promise.resolve({ json: async () => out })
}
function closeEmojiPicker() {}
function loadContacts() { contactsLoads += 1 }
const png = (data) => ({ kind: "png", data })
const paths = () => calls.map((c) => c[0])
const ctx = {
  assert, elements, state, apiFetch, closeEmojiPicker, loadContacts, console,
  document: { createElement: () => node() }, setTimeout: fakeSetTimeout,
  clearTimeout: fakeClearTimeout, Date: { now: () => now },
  setImmediate, Promise, JSON, Error, Object, Set,
}
vm.createContext(ctx)
vm.runInContext(block + `
this.api = { accountLink, LINK_PROTOCOLS, startLinkFlow, pollLinkFlow,
  closeAccountLinkDialog, openAccountLinkDialog, stopLinkFlow, setLinkRequired,
  loadLinkRequired, renderLinkChooser }`, ctx)
const { api } = ctx
const WA_STATUS = { protocols: { whatsapp: { available: true }, signal: { available: false } } };
"""


def _run_node(body: str) -> None:
    completed = subprocess.run(
        [
            "node",
            "-e",
            PRELUDE
            + "\n(async () => {\n"
            + body
            + "\n})().catch((e) => { console.error(e); process.exit(1) })",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_start_shows_png_and_poll_replaces_it():
    _run_node(r"""
routes = {
  "api/link/start": { qr: png("AAA") },
  "api/link/poll": { state: "waiting", needs_2fa: false, qr: null },
}
await api.startLinkFlow("whatsapp")
assert.equal(elements.accountLinkQr.src, "data:image/png;base64,AAA")
assert.equal(elements.accountLinkQr.hidden, false)
assert.equal(elements.accountLinkSpinner.hidden, true)
assert.equal(elements.accountLinkHint.textContent, api.LINK_PROTOCOLS.whatsapp.hint)
assert.equal(timers.filter((t) => t.live)[0].ms, 2000)
await runTimer()  // poll without qr: image unchanged, polling continues
assert.equal(elements.accountLinkQr.src, "data:image/png;base64,AAA")
routes["api/link/poll"] = { state: "waiting", needs_2fa: false, qr: png("BBB") }
await runTimer()
assert.equal(elements.accountLinkQr.src, "data:image/png;base64,BBB")
assert.ok(timers.some((t) => t.live), "polling goes on")
""")


def test_done_reconnects_closes_and_reloads_contacts():
    _run_node(r"""
routes = {
  "api/link/start": { qr: png("AAA") },
  "api/link/poll": { state: "done", needs_2fa: false, qr: null },
  "api/link/reconnect": { ok: true },
  "api/link/cancel": { ok: true },
}
elements.accountLinkDialog.open = true
await api.startLinkFlow("whatsapp")
await runTimer()
assert.equal(elements.accountLinkStatus.textContent, "✅ Collegato")
const reconnect = calls.find((c) => c[0] === "api/link/reconnect")
assert.deepEqual(reconnect[1], { protocols: ["whatsapp"] })
assert.equal(elements.accountLinkDialog.open, true)
await runTimer()  // the ~1.5 s close timer
assert.equal(timers[timers.length - 1].ms, 1500)
assert.equal(elements.accountLinkDialog.open, false)
assert.equal(contactsLoads, 1)
assert.equal(api.accountLink.protocol, null)
""")


def test_closing_mid_flow_cancels_once_and_stops_polling():
    _run_node(r"""
routes = {
  "api/link/start": { qr: png("AAA") },
  "api/link/cancel": { ok: true },
}
elements.accountLinkDialog.open = true
await api.startLinkFlow("whatsapp")
api.closeAccountLinkDialog()
api.closeAccountLinkDialog()
await flush()
assert.equal(paths().filter((p) => p === "api/link/cancel").length, 1)
assert.deepEqual(calls.find((c) => c[0] === "api/link/cancel")[1], { protocol: "whatsapp" })
assert.equal(timers.filter((t) => t.live).length, 0)
assert.equal(elements.accountLinkDialog.open, false)
""")


def test_close_without_started_flow_does_not_cancel():
    _run_node(r"""
routes = { "api/link/status": WA_STATUS }
await api.openAccountLinkDialog()
api.closeAccountLinkDialog()
await flush()
assert.ok(!paths().includes("api/link/cancel"))
""")


def test_info_kind_ends_with_message():
    _run_node(r"""
routes = {
  "api/link/start": { qr: { kind: "info", data: "WhatsApp is already linked" } },
  "api/link/cancel": { ok: true },
}
await api.startLinkFlow("whatsapp")
assert.equal(elements.accountLinkText.textContent, "WhatsApp is already linked")
assert.equal(elements.accountLinkQr.hidden, true)
assert.match(elements.accountLinkStatus.textContent, /Già collegato/)
assert.equal(timers.filter((t) => t.live).length, 0)
assert.equal(api.accountLink.protocol, null)
assert.ok(paths().includes("api/link/cancel"))  // frees the server session
""")


def test_start_failure_offers_retry_and_poll_409_ends_flow():
    _run_node(r"""
routes = {
  "api/link/start": () => Object.assign(new Error("HTTP 503"), {
    status: 503, response: { json: async () => ({ detail: "WAHA down" }) } }),
}
await api.startLinkFlow("whatsapp")
assert.equal(elements.accountLinkStatus.textContent, "WAHA down")
assert.equal(elements.accountLinkRetry.hidden, false)
routes = {
  "api/link/start": { qr: png("AAA") },
  "api/link/poll": () => Object.assign(new Error("HTTP 409"), { status: 409 }),
}
await api.startLinkFlow("whatsapp")
assert.equal(elements.accountLinkRetry.hidden, true)
await runTimer()
assert.match(elements.accountLinkStatus.textContent, /non è più in corso/)
assert.equal(elements.accountLinkRetry.hidden, false)
assert.equal(timers.filter((t) => t.live).length, 0)
""")


def test_poll_failure_keeps_waiting_and_timeout_ends():
    _run_node(r"""
routes = {
  "api/link/start": { qr: png("AAA") },
  "api/link/poll": () => Object.assign(new Error("HTTP 502"), { status: 502 }),
  "api/link/cancel": { ok: true },
}
await api.startLinkFlow("whatsapp")
await runTimer()
assert.equal(api.accountLink.protocol, "whatsapp")
assert.equal(timers.filter((t) => t.live).length, 1)
now += 5 * 60 * 1000 + 1
await runTimer()
assert.match(elements.accountLinkStatus.textContent, /Tempo scaduto/)
assert.equal(elements.accountLinkRetry.hidden, false)
assert.ok(paths().includes("api/link/cancel"))
assert.equal(timers.filter((t) => t.live).length, 0)
""")


def test_only_one_flow_at_a_time_and_chooser_state():
    _run_node(r"""
routes = {
  "api/link/status": WA_STATUS,
  "api/link/start": { qr: png("AAA") },
}
await api.openAccountLinkDialog()
assert.equal(elements.accountLinkChooser.children.length, 1)
assert.equal(elements.accountLinkChooser.children[0].disabled, false)
await api.startLinkFlow("whatsapp")
await api.startLinkFlow("whatsapp")
assert.equal(paths().filter((p) => p === "api/link/start").length, 1)
assert.equal(elements.accountLinkChooser.children[0].disabled, true)
""")


def test_unavailable_protocol_is_disabled_and_banner_opens_on_protocol():
    _run_node(r"""
routes = {
  "api/link/status": { protocols: { whatsapp: { available: false } } },
}
await api.openAccountLinkDialog("whatsapp")
const button = elements.accountLinkChooser.children[0]
assert.equal(button.disabled, true)
assert.equal(button.children[1].textContent, "non configurato")
assert.ok(!paths().includes("api/link/start"))
routes = { "api/link/status": WA_STATUS, "api/link/start": { qr: png("QQ") } }
await api.openAccountLinkDialog("whatsapp")
assert.equal(elements.accountLinkQr.src, "data:image/png;base64,QQ")
""")


def test_link_required_frame_toggles_banner():
    _run_node(r"""
api.setLinkRequired("whatsapp", true)
assert.equal(elements.linkRequiredBanner.hidden, false)
assert.match(elements.linkRequiredText.textContent, /WhatsApp richiede un nuovo collegamento/)
assert.equal(elements.linkRequiredBanner.dataset.protocol, "whatsapp")
api.setLinkRequired("whatsapp", false)
assert.equal(elements.linkRequiredBanner.hidden, true)
routes = { "api/link/required": { protocols: { whatsapp: true } } }
await api.loadLinkRequired()
assert.equal(elements.linkRequiredBanner.hidden, false)
// dismissed: stays hidden on a repeated "required", back after a clear
state.linkRequiredDismissed.add("whatsapp")
api.setLinkRequired("whatsapp", true)
assert.equal(elements.linkRequiredBanner.hidden, true)
api.setLinkRequired("whatsapp", false)
api.setLinkRequired("whatsapp", true)
assert.equal(elements.linkRequiredBanner.hidden, false)
""")


def test_app_wires_socket_frame_and_startup_fetch():
    app = Path("web/static/app.js").read_text(encoding="utf-8")
    assert 'case "link_required":' in app
    onopen = app.index("socket.onopen")
    assert "loadLinkRequired();" in app[onopen : onopen + 700]
