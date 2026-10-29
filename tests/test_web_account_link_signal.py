"""Signal in the "Collega account" dialog: form, finalize step (node)."""

from __future__ import annotations

from tests.test_web_account_link_ui import _run_node


def test_form_values_are_sent_to_start():
    _run_node(
        r"""
routes = { "api/link/start": { qr: png("SIG") } }
const flow = api.startLinkFlow("signal")
await flush()
assert.equal(elements.accountLinkForm.hidden, false)
assert.equal(elements.accountLinkDeviceName.value, "Signal-TUI-Client")
assert.ok(!paths().includes("api/link/start"))
elements.accountLinkPhone.value = "  +39123  "
elements.accountLinkDeviceName.value = "  Web  "
api.submitLinkForm(true)
await flow
assert.equal(elements.accountLinkForm.hidden, true)
assert.deepEqual(calls.find((c) => c[0] === "api/link/start")[1],
  { protocol: "signal", phone: "+39123", device_name: "Web" })
assert.equal(elements.accountLinkQr.src, "data:image/png;base64,SIG")
assert.equal(elements.accountLinkHint.textContent, api.LINK_PROTOCOLS.signal.hint)
"""
    )


def test_empty_device_name_falls_back_to_default():
    _run_node(
        r"""
routes = { "api/link/start": { qr: png("SIG") } }
const flow = api.startLinkFlow("signal")
await flush()
elements.accountLinkDeviceName.value = "   "
api.submitLinkForm(true)
await flow
assert.deepEqual(calls[0][1],
  { protocol: "signal", phone: "", device_name: "Signal-TUI-Client" })
"""
    )


def test_annulla_aborts_without_start():
    _run_node(
        r"""
routes = {}
const flow = api.startLinkFlow("signal")
await flush()
api.submitLinkForm(false)
await flow
assert.equal(calls.length, 0)
assert.equal(elements.accountLinkForm.hidden, true)
assert.equal(elements.accountLinkStatus.textContent, "Annullato.")
assert.equal(api.accountLink.protocol, null)
// closing the dialog while the form is open aborts as well
const flow2 = api.startLinkFlow("signal")
await flush()
api.closeAccountLinkDialog()
await flow2
assert.equal(calls.length, 0)
assert.equal(elements.accountLinkForm.hidden, true)
"""
    )


def test_waiting_poll_continues_and_finalize_completes():
    _run_node(
        r"""
routes = {
  "api/link/start": { qr: png("SIG") },
  "api/link/poll": { state: "waiting", needs_2fa: false, qr: null },
  "api/link/finalize": { account: "+39123" },
  "api/link/reconnect": { ok: true },
  "api/link/cancel": { ok: true },
}
elements.accountLinkDialog.open = true
const flow = api.startLinkFlow("signal")
await flush()
elements.accountLinkPhone.value = "+39123"
api.submitLinkForm(true)
await flow
await runTimer()
assert.ok(!paths().includes("api/link/finalize"))
assert.equal(timers.filter((t) => t.live).length, 1, "keeps polling while waiting")
routes["api/link/poll"] = { state: "finalize", needs_2fa: false, qr: null }
await runTimer()
assert.deepEqual(calls.find((c) => c[0] === "api/link/finalize")[1], { phone: "+39123" })
assert.equal(elements.accountLinkStatus.textContent, "✅ Collegato: +39123")
assert.deepEqual(calls.find((c) => c[0] === "api/link/reconnect")[1], { protocols: ["signal"] })
assert.ok(paths().includes("api/link/cancel"))
assert.equal(timers.filter((t) => t.live && t.ms === 2000).length, 0, "polling stopped")
await runTimer()  // close timer
assert.equal(elements.accountLinkDialog.open, false)
assert.equal(contactsLoads, 1)
"""
    )


def test_finalize_error_shows_message_and_retry():
    _run_node(
        r"""
routes = {
  "api/link/start": { qr: png("SIG") },
  "api/link/poll": { state: "finalize", needs_2fa: false, qr: null },
  "api/link/finalize": () => Object.assign(new Error("HTTP 502"), {
    status: 502, response: { json: async () => ({ detail: "nessun account" }) } }),
  "api/link/cancel": { ok: true },
}
const flow = api.startLinkFlow("signal")
await flush()
api.submitLinkForm(true)
await flow
await runTimer()
assert.equal(elements.accountLinkStatus.textContent, "nessun account")
assert.equal(elements.accountLinkRetry.hidden, false)
assert.equal(api.accountLink.protocol, null)
assert.equal(timers.filter((t) => t.live).length, 0)
assert.ok(!paths().includes("api/link/reconnect"))
"""
    )


def test_whatsapp_flow_has_no_form():
    _run_node(
        r"""
routes = { "api/link/start": { qr: png("WA") } }
elements.accountLinkForm.hidden = true
await api.startLinkFlow("whatsapp")
assert.deepEqual(calls[0][1], { protocol: "whatsapp" })
assert.equal(elements.accountLinkForm.hidden, true)
"""
    )
