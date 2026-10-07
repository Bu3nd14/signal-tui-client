"""Frontend voice recorder UI (design v2, gating R2).

Test JS stile ``node -e`` (pattern di tests/test_web_multi_attachment_ui.py):
selezione del mime, registrazione tap-per-fermare con staging del file,
annullamento con rilascio del microfono, auto-stop a 5 minuti, guardie R2 di
``submitMessage`` e presenza degli asset statici.

Node 18: ``File`` non e' globale, quindi viene preso da ``node:buffer``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest


def _run_node(source: str) -> None:
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


_LOAD_VOICE = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
function setGlobal(name, value) {
  try {
    Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
  } catch {
    globalThis[name] = value;
  }
}
setGlobal("File", globalThis.File || require("node:buffer").File);
const app = fs.readFileSync("./web/static/app.js", "utf8");
const voice = app.slice(
  app.indexOf("const VOICE_MAX_DURATION_MS"),
  app.indexOf("// ── Outbox:"),
);
vm.runInThisContext(voice);
"""

LOAD_SUBMIT = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
function setGlobal(name, value) {
  try {
    Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
  } catch {
    globalThis[name] = value;
  }
}
setGlobal("File", globalThis.File || require("node:buffer").File);
const app = fs.readFileSync("./web/static/app.js", "utf8");
const helper = app.slice(
  app.indexOf("function mediaKindFromMime("),
  app.indexOf("\nfunction clearStagedAttachments"),
);
const submit = helper + "\n" + app.slice(
  app.indexOf("async function submitMessage("),
  app.indexOf("\nfunction encodeToken"),
);
vm.runInThisContext(submit);
"""

_VOICE_STUBS = r"""
let __staged = [];
let __stopped = 0;
const __stream = { getTracks: () => [{ stop: () => { __stopped += 1; } }] };
const __recorderInstances = [];
class __FakeMediaRecorder {
  static isTypeSupported(type) {
    return (globalThis.__supported || []).includes(type);
  }
  constructor(stream, options) {
    this.stream = stream;
    this.mimeType = (options && options.mimeType) || "";
    this.state = "inactive";
    this.onstop = null;
    this.ondataavailable = null;
    this.onerror = null;
    __recorderInstances.push(this);
  }
  start() { this.state = "recording"; }
  stop() { this.state = "inactive"; if (this.onstop) this.onstop(); }
}
function __makeNode(tag) {
  return {
    tag, id: "", className: "", hidden: false, _html: "", children: [],
    textContent: "",
    set innerHTML(value) { this._html = value; },
    get innerHTML() { return this._html; },
    before() {}, append() {}, replaceChildren() {},
    addEventListener() {}, querySelector() { return { addEventListener() {} }; },
  };
}
function setGlobal(name, value) {
  try {
    Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
  } catch {
    globalThis[name] = value;
  }
}
function installCommon() {
  setGlobal("MediaRecorder", __FakeMediaRecorder);
  setGlobal("document", {
    querySelector: () => null,
    createElement: (tag) => __makeNode(tag),
    documentElement: { classList: { toggle() {} } },
  });
  globalThis.elements = { attachmentsPreview: { before() {} } };
  setGlobal("URL", { createObjectURL: () => "blob:stub", revokeObjectURL() {} });
  globalThis.updateComposer = () => {};
  globalThis.closeComposerMenu = () => {};
  globalThis.stageAttachments = async (files) => { __staged.push(...files); };
  globalThis.showError = (message) => {
    throw new Error("unexpected showError: " + message);
  };
}
"""


def _voice_source(body: str) -> str:
    return _LOAD_VOICE + _VOICE_STUBS + body


def _recording_state() -> str:
    return (
        "globalThis.state = { voiceRecorder: null, voiceStarting: false, "
        "voiceGeneration: 0, editing: null, editSending: false, sending: 0, "
        "stagedAttachments: [] };"
    )


def _async(body: str, *, on_error: str = "") -> str:
    return (
        "\n(async () => {\n"
        + body
        + "\n})().catch((error) => { "
        + (on_error if on_error else "")
        + " console.error(error); process.exitCode = 1; });\n"
    )


# ── _selectVoiceMime / helper puri ────────────────────────────────────────────


def test_select_voice_mime_prefers_mp4_then_webm_then_empty():
    _run_node(
        _voice_source(
            r"""
installCommon();
globalThis.__supported = ["audio/mp4", "audio/webm"];
assert.equal(_selectVoiceMime(), "audio/mp4");
globalThis.__supported = ["audio/webm"];
assert.equal(_selectVoiceMime(), "audio/webm");
globalThis.__supported = [];
assert.equal(_selectVoiceMime(), "");
setGlobal("MediaRecorder", undefined);
assert.equal(_selectVoiceMime(), "");
setGlobal("MediaRecorder", {});
assert.equal(_selectVoiceMime(), "");
"""
        )
    )


def test_mime_extension_and_duration_helpers():
    _run_node(
        _voice_source(
            r"""
installCommon();
assert.equal(mimeTypeToExtension("audio/mp4"), "m4a");
assert.equal(mimeTypeToExtension("audio/mp4;codecs=mp4a.40.2"), "m4a");
assert.equal(mimeTypeToExtension("audio/webm;codecs=opus"), "webm");
assert.equal(mimeTypeToExtension(""), "webm");
assert.equal(formatVoiceDuration(0), "00:00");
assert.equal(formatVoiceDuration(65_000), "01:05");
assert.equal(formatVoiceDuration(5 * 60 * 1000), "05:00");
"""
        )
    )


# ── startVoiceRecording → stop → stage ────────────────────────────────────────


def test_start_stop_stages_m4a_file():
    _run_node(
        _voice_source(
            _recording_state()
            + r"""
installCommon();
globalThis.__supported = ["audio/mp4"];
setGlobal("window", { matchMedia: () => ({ matches: true, addEventListener() {} }) });
setGlobal("navigator", { mediaDevices: { getUserMedia: async () => __stream } });
"""
            + _async(
                r"""
  await startVoiceRecording();
  assert.equal(state.voiceStarting, false);
  assert.ok(state.voiceRecorder, "recorder expected");
  const rec = state.voiceRecorder;
  assert.equal(__recorderInstances.length, 1);
  assert.equal(rec.mediaRecorder.mimeType, "audio/mp4");
  rec.mediaRecorder.ondataavailable({
    data: new Blob(["voice-bytes"], { type: "audio/mp4" }),
  });
  rec.mediaRecorder.stop();
  assert.equal(__staged.length, 1);
  assert.match(__staged[0].name, /^voice-\d+\.m4a$/);
  assert.equal(__staged[0].type, "audio/mp4");
  assert.equal(state.voiceRecorder, null);
  assert.equal(state.voiceStarting, false);
  assert.equal(__stopped, 1);
"""
            )
        )
    )


def test_start_stop_stages_webm_file_when_mp4_unsupported():
    _run_node(
        _voice_source(
            _recording_state()
            + r"""
installCommon();
globalThis.__supported = ["audio/webm"];
setGlobal("window", { matchMedia: () => ({ matches: true }) });
setGlobal("navigator", { mediaDevices: { getUserMedia: async () => __stream } });
"""
            + _async(
                r"""
  await startVoiceRecording();
  const rec = state.voiceRecorder;
  assert.equal(rec.mediaRecorder.mimeType, "audio/webm");
  rec.mediaRecorder.ondataavailable({
    data: new Blob(["voice-bytes"], { type: "audio/webm" }),
  });
  rec.mediaRecorder.stop();
  assert.equal(__staged.length, 1);
  assert.match(__staged[0].name, /^voice-\d+\.webm$/);
  assert.equal(__staged[0].type, "audio/webm");
  assert.equal(__stopped, 1);
"""
            )
        )
    )


# ── cancelVoiceRecording ──────────────────────────────────────────────────────


def test_cancel_releases_mic_and_discards_without_staging():
    _run_node(
        _voice_source(
            _recording_state()
            + r"""
installCommon();
globalThis.__supported = ["audio/mp4"];
setGlobal("window", { matchMedia: () => ({ matches: true }) });
setGlobal("navigator", { mediaDevices: { getUserMedia: async () => __stream } });
"""
            + _async(
                r"""
  await startVoiceRecording();
  const rec = state.voiceRecorder;
  assert.ok(rec, "recorder expected");
  cancelVoiceRecording();
  assert.equal(rec.discarded, true);
  assert.equal(state.voiceRecorder, null);
  assert.equal(state.voiceStarting, false);
  assert.equal(__staged.length, 0);
  assert.equal(state.stagedAttachments.length, 0);
  assert.equal(__stopped, 1, "il microfono deve essere rilasciato una volta");
  // Handler azzerati: un onstop tardivo non deve accodare nulla.
  assert.equal(rec.mediaRecorder.ondataavailable, null);
  assert.equal(rec.mediaRecorder.onstop, null);
  assert.equal(rec.mediaRecorder.onerror, null);
"""
            )
        )
    )


def test_cancel_while_get_user_media_in_flight_stops_late_stream():
    _run_node(
        _voice_source(
            _recording_state()
            + r"""
installCommon();
globalThis.__supported = ["audio/mp4"];
setGlobal("window", { matchMedia: () => ({ matches: true }) });
let resolveGum;
setGlobal("navigator", {
  mediaDevices: { getUserMedia: () => new Promise((resolve) => { resolveGum = resolve; }) },
});
"""
            + _async(
                r"""
  const pending = startVoiceRecording();
  assert.equal(state.voiceStarting, true);
  cancelVoiceRecording();
  assert.equal(state.voiceStarting, false);
  assert.equal(state.voiceGeneration, 2);
  resolveGum(__stream);
  await pending;
  assert.equal(state.voiceRecorder, null);
  assert.equal(__staged.length, 0);
  assert.equal(__stopped, 1);
"""
            )
        )
    )


# ── Desktop / errori di disponibilità ─────────────────────────────────────────


def test_desktop_does_not_start_recording():
    _run_node(
        _voice_source(
            _recording_state()
            + r"""
installCommon();
globalThis.__supported = ["audio/mp4"];
setGlobal("window", { matchMedia: () => ({ matches: false }) });
let gumCalled = false;
setGlobal("navigator", {
  mediaDevices: { getUserMedia: async () => { gumCalled = true; return __stream; } },
});
"""
            + _async(
                r"""
  await startVoiceRecording();
  assert.equal(gumCalled, false, "desktop non deve chiedere il microfono");
  assert.equal(state.voiceRecorder, null);
  assert.equal(state.voiceStarting, false);
  assert.equal(__staged.length, 0);
"""
            )
        )
    )


def test_permission_denied_shows_clear_error_without_crash():
    _run_node(
        _voice_source(
            _recording_state()
            + r"""
installCommon();
globalThis.__supported = ["audio/mp4"];
setGlobal("window", { matchMedia: () => ({ matches: true }) });
setGlobal("navigator", {
  mediaDevices: { getUserMedia: async () => { throw new Error("NotAllowedError"); } },
});
const errors = [];
globalThis.showError = (message) => errors.push(message);
"""
            + _async(
                r"""
  await startVoiceRecording();
  assert.deepEqual(errors, ["Impossibile accedere al microfono."]);
  assert.equal(state.voiceStarting, false);
  assert.equal(state.voiceRecorder, null);
  assert.equal(__staged.length, 0);
"""
            )
        )
    )


def test_missing_media_devices_shows_https_error():
    _run_node(
        _voice_source(
            _recording_state()
            + r"""
installCommon();
globalThis.__supported = ["audio/mp4"];
setGlobal("window", { matchMedia: () => ({ matches: true }) });
setGlobal("navigator", {});
const errors = [];
globalThis.showError = (message) => errors.push(message);
"""
            + _async(
                r"""
  await startVoiceRecording();
  assert.deepEqual(errors, [
    "Registrazione vocale non disponibile: serve una connessione sicura (HTTPS).",
  ]);
  assert.equal(state.voiceRecorder, null);
"""
            )
        )
    )


def test_media_recorder_constructor_failure_releases_stream():
    _run_node(
        _voice_source(
            _recording_state()
            + r"""
installCommon();
setGlobal("MediaRecorder", class {
  static isTypeSupported() { return true; }
  constructor() { throw new Error("boom"); }
});
setGlobal("window", { matchMedia: () => ({ matches: true }) });
setGlobal("navigator", { mediaDevices: { getUserMedia: async () => __stream } });
const errors = [];
globalThis.showError = (message) => errors.push(message);
"""
            + _async(
                r"""
  await startVoiceRecording();
  assert.deepEqual(errors, ["Impossibile avviare la registrazione."]);
  assert.equal(state.voiceRecorder, null);
  assert.equal(state.voiceStarting, false);
  assert.equal(__stopped, 1);
"""
            )
        )
    )


# ── Auto-stop 5 minuti ────────────────────────────────────────────────────────


def test_auto_stop_after_five_minutes():
    _run_node(
        _voice_source(
            _recording_state()
            + r"""
installCommon();
globalThis.__supported = ["audio/mp4"];
setGlobal("window", { matchMedia: () => ({ matches: true }) });
setGlobal("navigator", { mediaDevices: { getUserMedia: async () => __stream } });
let intervalCb = null;
globalThis.setInterval = (callback) => { intervalCb = callback; return 99; };
globalThis.clearInterval = () => { intervalCb = null; };
const errors = [];
globalThis.showError = (message) => errors.push(message);
const realNow = Date.now;
let now = 1_000_000;
Date.now = () => now;
"""
            + _async(
                r"""
  await startVoiceRecording();
  assert.ok(intervalCb, "il timer deve essere registrato");
  const rec = state.voiceRecorder;
  rec.mediaRecorder.ondataavailable({
    data: new Blob(["voice-bytes"], { type: "audio/mp4" }),
  });
  now += 5 * 60 * 1000;
  intervalCb();
  assert.deepEqual(errors, ["Registrazione fermata automaticamente dopo 5 minuti."]);
  assert.equal(rec.mediaRecorder.state, "inactive");
  assert.equal(__staged.length, 1);
""",
                on_error="Date.now = realNow;",
            )
        )
    )


# ── R2: guardie e ritenzione staging in submitMessage ─────────────────────────


def test_submit_guard_attachment_sending_blocks_second_send():
    _run_node(
        LOAD_SUBMIT
        + r"""
globalThis.state = {
  active: { id: "alice", protocol: "signal" },
  attachmentSending: true, voiceRecorder: null, voiceStarting: false,
  stagedAttachments: [], sending: 0, messages: [], optimistic: [],
  optimisticSequence: 0, replyTo: null, editing: null, editSending: false,
};
globalThis.elements = { messageInput: { value: "ciao", focus() {} } };
let calls = 0;
globalThis.apiFetch = async () => { calls += 1; return { status: 200 }; };
"""
        + _async(
            r"""
  await submitMessage();
  assert.equal(calls, 0, "attachmentSending deve bloccare il doppio invio");
"""
        )
    )


def test_submit_guard_active_voice_recorder_blocks_send():
    _run_node(
        LOAD_SUBMIT
        + r"""
globalThis.state = {
  active: { id: "alice", protocol: "signal" },
  attachmentSending: false,
  voiceRecorder: { mediaRecorder: {} }, voiceStarting: false,
  stagedAttachments: [], sending: 0, messages: [], optimistic: [],
  optimisticSequence: 0, replyTo: null, editing: null, editSending: false,
};
globalThis.elements = { messageInput: { value: "ciao", focus() {} } };
let calls = 0;
globalThis.apiFetch = async () => { calls += 1; return { status: 200 }; };
"""
        + _async(
            r"""
  await submitMessage();
  assert.equal(calls, 0, "il registratore attivo deve bloccare l'invio");
"""
        )
    )


def test_submit_error_retains_staged_attachments():
    _run_node(
        LOAD_SUBMIT
        + r"""
const attachment = {
  file: new Blob(["voice"], { type: "audio/mp4" }), filename: "voice.m4a",
  previewUrl: null, previewWidth: null, previewHeight: null,
  attachmentId: "voice.m4a[0]",
};
globalThis.state = {
  active: { id: "alice", protocol: "signal" },
  attachmentSending: false, voiceRecorder: null, voiceStarting: false,
  stagedAttachments: [attachment], sending: 0, messages: [], optimistic: [],
  optimisticSequence: 0, replyTo: null, editing: null, editSending: false,
};
globalThis.elements = { messageInput: { value: "nota", focus() {} } };
setGlobal("window", { SignalTuiReconcile: { messageIdentity: (m) => m.id } });
globalThis.resizeComposer = () => {};
globalThis.updateComposer = () => {};
globalThis.renderMessages = () => {};
globalThis.cacheMedia = () => {};
const errors = [];
globalThis.showError = (message) => errors.push(message);
let clearOptions = null;
globalThis.clearStagedAttachments = (options) => {
  clearOptions = options;
  state.stagedAttachments = [];
};
let calls = 0;
globalThis.apiFetch = async () => { calls += 1; throw new Error("network down"); };
"""
        + _async(
            r"""
  await submitMessage();
  assert.equal(calls, 1);
  assert.equal(state.stagedAttachments.length, 1, "staging NON svuotato su errore");
  assert.equal(clearOptions, null);
  assert.equal(state.attachmentSending, false);
  assert.equal(state.optimistic[0].optimisticStatus, "failed");
  assert.equal(state.optimistic[0].attachment.media_kind, "audio");
  assert.deepEqual(errors, ["Impossibile inviare il messaggio."]);
"""
        )
    )


def test_submit_success_clears_staging_without_revoke():
    _run_node(
        LOAD_SUBMIT
        + r"""
const attachment = {
  file: new Blob(["voice"], { type: "audio/mp4" }), filename: "voice.m4a",
  previewUrl: null, previewWidth: null, previewHeight: null,
  attachmentId: "voice.m4a[0]",
};
globalThis.state = {
  active: { id: "alice", protocol: "signal" },
  attachmentSending: false, voiceRecorder: null, voiceStarting: false,
  stagedAttachments: [attachment], sending: 0, messages: [], optimistic: [],
  optimisticSequence: 0, replyTo: null, editing: null, editSending: false,
};
globalThis.elements = { messageInput: { value: "nota", focus() {} } };
setGlobal("window", { SignalTuiReconcile: { messageIdentity: (m) => m.id } });
globalThis.resizeComposer = () => {};
globalThis.updateComposer = () => {};
globalThis.renderMessages = () => {};
globalThis.cacheMedia = () => {};
globalThis.showError = assert.fail;
let clearOptions = null;
globalThis.clearStagedAttachments = (options) => {
  clearOptions = options;
  state.stagedAttachments = [];
};
globalThis.apiFetch = async () => ({ status: 200 });
"""
        + _async(
            r"""
  await submitMessage();
  assert.equal(state.stagedAttachments.length, 0);
  assert.deepEqual(clearOptions, { revoke: false });
  assert.equal(state.attachmentSending, false);
  assert.equal(state.optimistic[0].optimisticStatus, "sent");
"""
        )
    )


# ── Asset statici ─────────────────────────────────────────────────────────────


def test_static_assets_declare_voice_record_ui():
    index = Path("web/static/index.html").read_text(encoding="utf-8")
    assert 'id="voice-record"' in index
    assert "style.css?v=68" in index
    assert "app.js?v=121" in index
    menu = re.search(r'<div id="composer-menu".*?</div>', index, re.DOTALL)
    assert menu is not None
    assert 'id="voice-record"' in menu.group(0)

    css = Path("web/static/style.css").read_text(encoding="utf-8")
    assert "#voice-record { display: none; }" in css
    assert ".is-mobile #voice-record" in css
    assert ".is-mobile .voice-recorder" in css


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
