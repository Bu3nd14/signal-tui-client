"""Tester verification for Gmail images visible by default (design rev. 1).

Covers the client JS (``loadImages``/``cancelPendingImages``/``safeImageUrl``),
the static assets and docs, the Python->JS signed-token contract, the raster
dimension parser and the per-router fetch semaphore.  The tests never touch
production code and use ``node -e``/``vm`` for the frontend, following the
pattern already used by ``test_web_avatar.py``/``test_web_media_version.py``.
"""

from __future__ import annotations

import asyncio
import json
import re
import struct
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from client_harness import api_for
from fastapi import FastAPI

try:  # CI installa httpx2 (requirements-web); httpx può mancare.
    import httpx2 as httpx
except ModuleNotFoundError:  # pragma: no cover - ambiente locale con httpx
    import httpx

import server.link_preview as lp
from server import gmail
from web.gmail_api import create_gmail_image_router

_ROOT = Path(__file__).resolve().parents[1]
_GMAIL_JS_PATH = _ROOT / "web/static/gmail.js"
_INDEX_PATH = _ROOT / "web/static/index.html"
_GMAIL_CSS_PATH = _ROOT / "web/static/gmail.css"
_GMAIL_MAX_PIXELS = 25_000_000
_NEXT_JS_TOP_LEVEL = re.compile(
    r"\n  (?:(?:async )?function |(?:const|let|var) |elements\.|document\.|window\.)"
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _run_node(source: str) -> None:
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def _js_function(source: str, name: str) -> str:
    """Return a top-level two-space-indented function block from gmail.js."""
    marker = f"  function {name}("
    start = source.index(marker)
    following = _NEXT_JS_TOP_LEVEL.search(source, start + len(marker))
    end = following.start() + 1 if following else len(source)
    return source[start:end]


def _js_functions(*names: str) -> str:
    source = _GMAIL_JS_PATH.read_text(encoding="utf-8")
    return "\n".join(_js_function(source, name) for name in names)


def _token(url: str) -> str:
    from urllib.parse import parse_qs, urlsplit

    path = gmail.sign_image_url(url)
    return parse_qs(urlsplit(path).query)["token"][0]


def _png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


def _gif(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00"


def _webp_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        b"RIFF"
        + struct.pack("<I", 4 + 8 + len(payload))
        + b"WEBP"
        + kind
        + struct.pack("<I", len(payload))
        + payload
    )


def _webp_vp8x(width: int, height: int) -> bytes:
    payload = (
        b"\x00\x00\x00\x00"
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )
    return _webp_chunk(b"VP8X", payload)


def _webp_vp8(width: int, height: int) -> bytes:
    payload = (
        b"\x00\x00\x00"
        + b"\x9d\x01\x2a"
        + struct.pack("<H", width & 0x3FFF)
        + struct.pack("<H", height & 0x3FFF)
    )
    return _webp_chunk(b"VP8 ", payload)


def _webp_vp8l(width: int, height: int) -> bytes:
    packed = ((width - 1) & 0x3FFF) | (((height - 1) & 0x3FFF) << 14)
    return _webp_chunk(b"VP8L", b"\x2f" + struct.pack("<I", packed))


def _jpeg_segment(marker: int, payload: bytes) -> bytes:
    return b"\xff" + bytes([marker]) + struct.pack(">H", len(payload) + 2) + payload


def _jpeg_sof(width: int, height: int, marker: int = 0xC0) -> bytes:
    payload = (
        b"\x08"
        + struct.pack(">HH", height, width)
        + b"\x03"
        + b"\x01\x11\x00"
        + b"\x02\x11\x01"
        + b"\x03\x11\x01"
    )
    return _jpeg_segment(marker, payload)


def _jpeg_app1_with_fake_sof(
    width: int, height: int, fake_width: int = 60_000, fake_height: int = 60_000
) -> bytes:
    fake_sof = _jpeg_sof(fake_width, fake_height)
    return (
        b"\xff\xd8"
        + _jpeg_segment(0xE1, b"Exif\x00\x00" + fake_sof)
        + _jpeg_sof(width, height)
        + b"\xff\xd9"
    )


@asynccontextmanager
async def _fake_shared_session():
    yield object()


def _install_fake_fetch(monkeypatch, body: bytes, content_type: str) -> None:
    async def fake_get(session, url, max_bytes):
        return url, content_type, body, None

    monkeypatch.setattr(lp, "shared_session", _fake_shared_session)
    monkeypatch.setattr(lp, "_get", fake_get)


# ── safeImageUrl + Python->JS token contract ─────────────────────────────────


def test_safe_image_url_and_signed_token_contract():
    signed = gmail.sign_image_url("https://image.example/path/a.png?x=1&y=2")
    token = _token("https://image.example/path/a.png?x=1&y=2")
    assert gmail.verify_image_token(token) == (
        "https://image.example/path/a.png?x=1&y=2"
    )

    js = r"""
const assert = require("node:assert/strict");
__SAFE__
const signed = __SIGNED__;
assert.equal(safeImageUrl(signed), signed);
assert.equal(
  safeImageUrl("/gmail-image?token=abc.def-123_456&v=2"),
  "gmail-image?token=abc.def-123_456&v=2",
);
assert.equal(safeImageUrl("data:image/png;base64,AAAA"), "data:image/png;base64,AAAA");
assert.equal(safeImageUrl("data:image/jpeg;base64,AAAA"), "data:image/jpeg;base64,AAAA");
assert.equal(safeImageUrl("data:image/gif;base64,AAAA"), "data:image/gif;base64,AAAA");
assert.equal(safeImageUrl("data:image/webp;base64,AAAA"), "data:image/webp;base64,AAAA");
assert.equal(safeImageUrl("data:image/svg+xml;base64,AAAA"), "");
assert.equal(safeImageUrl("https://evil.example/a.png"), "");
assert.equal(safeImageUrl("//evil.example/a.png"), "");
assert.equal(safeImageUrl("//gmail-image?token=abc.def&v=2"), "");
assert.equal(safeImageUrl("javascript:alert(1)"), "");
assert.equal(safeImageUrl("../gmail-image?token=abc.def&v=2"), "");
assert.equal(safeImageUrl("/gmail-image?token=abc/def&v=2"), "");
assert.equal(safeImageUrl("/gmail-image?token=abc.def&v=x"), "");
assert.equal(safeImageUrl("/gmail-image?token=abc.def&foo=1"), "");
assert.equal(safeImageUrl("/gmail-image?token="), "");
assert.equal(safeImageUrl(""), "");
assert.equal(safeImageUrl(null), "");
"""
    js = js.replace("__SAFE__", _js_function(_read_gmail_js(), "safeImageUrl"))
    js = js.replace("__SIGNED__", json.dumps(signed))
    _run_node(js)


def _read_gmail_js() -> str:
    return _GMAIL_JS_PATH.read_text(encoding="utf-8")


# ── loadImages ───────────────────────────────────────────────────────────────


_LOAD_IMAGES_HARNESS = r"""
const assert = require("node:assert/strict");
const MAX_IMAGES_PER_MESSAGE = 50;
__SAFE__
__CANCEL__
__LOAD__
let notices = [];
function showNotice(message) { notices.push(String(message)); }
const state = { readerSeq: 0 };
let currentImages = [];
const elements = {
  readerBody: {
    querySelectorAll(selector) {
      if (selector === "img[data-src]") {
        return currentImages.filter((img) =>
          Object.prototype.hasOwnProperty.call(img.dataset, "src"));
      }
      if (selector === "img[src]") {
        return currentImages.filter((img) => img.attrs.has("src"));
      }
      if (selector === "img") return currentImages;
      return [];
    },
  },
};
function makeImage(dataSrc, options) {
  const opts = options || {};
  const order = [];
  const classes = new Set();
  const attrs = new Set(opts.src ? ["src"] : []);
  const listeners = {};
  const image = {
    dataset: dataSrc === undefined ? {} : { src: dataSrc },
    order,
    classes,
    attrs,
    listeners,
    _src: opts.src || null,
    _loading: "",
    _decoding: "",
    get src() { return this._src; },
    set src(value) {
      this._src = value;
      this.attrs.add("src");
      this.order.push("src");
    },
    get loading() { return this._loading; },
    set loading(value) {
      this._loading = value;
      this.order.push("loading");
    },
    get decoding() { return this._decoding; },
    set decoding(value) {
      this._decoding = value;
      this.order.push("decoding");
    },
    hasAttribute(name) { return name === "src" ? this.attrs.has("src") : false; },
    removeAttribute(name) {
      if (name === "src") {
        this.attrs.delete("src");
        this._src = null;
      }
    },
    addEventListener(name, callback, config) {
      this.listeners[name] = { callback, config };
    },
    classList: {
      add(name) { classes.add(name); },
      remove(name) { classes.delete(name); },
      contains(name) { return classes.has(name); },
      toggle(name, force) {
        const active = force === undefined ? !classes.has(name) : Boolean(force);
        if (active) classes.add(name); else classes.delete(name);
      },
    },
  };
  return image;
}
function reset(images) {
  currentImages = images;
  notices = [];
  state.readerSeq = 0;
}
function loadedCount() {
  return currentImages.filter((img) => img.attrs.has("src")).length;
}
"""


def test_load_images_sets_hints_before_src_and_skips_existing():
    source = (
        _LOAD_IMAGES_HARNESS.replace(
            "__SAFE__", _js_function(_read_gmail_js(), "safeImageUrl")
        )
        .replace("__CANCEL__", _js_function(_read_gmail_js(), "cancelPendingImages"))
        .replace("__LOAD__", _js_function(_read_gmail_js(), "loadImages"))
    )
    js = (
        source
        + r"""
reset([makeImage("/gmail-image?token=abc.def&v=2")]);
loadImages();
const first = currentImages[0];
assert.deepEqual(first.order, ["loading", "decoding", "src"]);
assert.equal(first.src, "gmail-image?token=abc.def&v=2");
assert.deepEqual(notices, []);

// Idempotente: l'immagine ha già src, non viene riassegnata né reiscritta.
const before = first.order.slice();
loadImages();
assert.deepEqual(first.order, before);
assert.deepEqual(Object.keys(first.listeners).sort(), ["error", "load"]);
assert.equal(first.listeners.load.config.once, true);

// Skip di un src già presente (es. data: URI inline già caricato).
reset([makeImage("/gmail-image?token=abc.def&v=2", {
  src: "/gmail-image?token=old.sig&v=2",
})]);
loadImages();
const skipped = currentImages[0];
assert.equal(skipped.order.length, 0);
assert.equal(skipped.src, "/gmail-image?token=old.sig&v=2");
assert.deepEqual(notices, []);
"""
    )
    _run_node(js)


def test_load_images_cap_and_invalid_urls():
    source = (
        _LOAD_IMAGES_HARNESS.replace(
            "__SAFE__", _js_function(_read_gmail_js(), "safeImageUrl")
        )
        .replace("__CANCEL__", _js_function(_read_gmail_js(), "cancelPendingImages"))
        .replace("__LOAD__", _js_function(_read_gmail_js(), "loadImages"))
    )
    js = (
        source
        + r"""
// 60 immagini valide -> 50 caricate, notice sincrono con il cap.
reset(Array.from({ length: 60 }, (_, index) =>
  makeImage("/gmail-image?token=t" + index + "&v=2")));
loadImages();
assert.equal(loadedCount(), 50);
assert.equal(currentImages.filter((img) => !img.attrs.has("src")).length, 10);
assert.deepEqual(notices, ["Caricate 50 di 60 immagini."]);

// URL invalidi: nessun src, classe is-failed, nessun notice d'errore.
reset([
  makeImage("https://evil.example/a.png"),
  makeImage("javascript:alert(1)"),
  makeImage("/gmail-image?token=bad/slash&v=2"),
  makeImage("data:image/svg+xml;base64,AAAA"),
]);
loadImages();
for (const image of currentImages) {
  assert.equal(image.src, null);
  assert.equal(image.classes.has("is-failed"), true);
}
assert.deepEqual(notices, []);

// Il notice usa il conteggio reale (loaded), non il cap teorico: 20 invalide
// su 60 -> 40 caricate.
const invalid = Array.from({ length: 20 }, (_, index) =>
  makeImage("https://evil.example/" + index + ".png"));
const valid = Array.from({ length: 40 }, (_, index) =>
  makeImage("/gmail-image?token=v" + index + "&v=2"));
reset([...invalid, ...valid]);
loadImages();
assert.equal(loadedCount(), 40);
assert.deepEqual(notices, ["Caricate 40 di 60 immagini."]);
"""
    )
    _run_node(js)


def test_load_images_error_generation_guard_and_no_error_notice():
    source = (
        _LOAD_IMAGES_HARNESS.replace(
            "__SAFE__", _js_function(_read_gmail_js(), "safeImageUrl")
        )
        .replace("__CANCEL__", _js_function(_read_gmail_js(), "cancelPendingImages"))
        .replace("__LOAD__", _js_function(_read_gmail_js(), "loadImages"))
    )
    js = (
        source
        + r"""
reset([makeImage("/gmail-image?token=abc.def&v=2")]);
loadImages();
const failed = currentImages[0];
failed.listeners.error.callback();
assert.equal(failed.classes.has("is-failed"), true);
assert.deepEqual(notices, []);

// Generation stale: il callback error non tocca il DOM corrente.
reset([makeImage("/gmail-image?token=abc.def&v=2")]);
loadImages();
const stale = currentImages[0];
state.readerSeq += 1;
stale.listeners.error.callback();
assert.equal(stale.classes.has("is-failed"), false);
assert.deepEqual(notices, []);
assert.equal(stale.listeners.error.config.once, true);
"""
    )
    _run_node(js)


def test_load_images_late_load_after_cancel_does_not_mark_loaded():
    source = (
        _LOAD_IMAGES_HARNESS.replace(
            "__SAFE__", _js_function(_read_gmail_js(), "safeImageUrl")
        )
        .replace("__CANCEL__", _js_function(_read_gmail_js(), "cancelPendingImages"))
        .replace("__LOAD__", _js_function(_read_gmail_js(), "loadImages"))
    )
    js = (
        source
        + r"""
reset([makeImage("/gmail-image?token=abc.def&v=2")]);
loadImages();
const image = currentImages[0];
assert.deepEqual(Object.keys(image.listeners).sort(), ["error", "load"]);

// cancelPendingImages() rimuove src prima che arrivi il load tardivo.
cancelPendingImages();
assert.equal(image.attrs.has("src"), false);
image.listeners.load.callback();
assert.equal(image.dataset.loaded, undefined);
assert.equal(image.classes.has("is-failed"), false);

// Load valido: marca loaded e rimuove un eventuale is-failed precedente.
reset([makeImage("/gmail-image?token=abc.def&v=2")]);
loadImages();
const loadedImage = currentImages[0];
loadedImage.classes.add("is-failed");
loadedImage.listeners.load.callback();
assert.equal(loadedImage.dataset.loaded, "1");
assert.equal(loadedImage.classes.has("is-failed"), false);
"""
    )
    _run_node(js)


def test_cancel_pending_images_order_and_toggle_reload():
    source = _js_functions(
        "cancelPendingImages",
        "closeReader",
        "showReaderLoading",
        "showReaderError",
        "renderReader",
        "toggleRenderMode",
        "deactivateGmail",
    )
    js = r"""
const assert = require("node:assert/strict");
const RENDER_KEY = "gmail-render-mode";
__FUNCTIONS__
let log = [];
const state = {
  open: false,
  listSeq: 0,
  readerSeq: 0,
  renderMode: "html",
  detail: null,
  selectedUids: new Set(),
  selectionMode: false,
};
function record(value) { log.push(value); }
function makeStubImage(label, loaded) {
  return {
    dataset: loaded ? { loaded: "1" } : {},
    _src: "/gmail-image?token=abc.def&v=2",
    hasAttribute(name) { return name === "src" ? this._src !== null : false; },
    removeAttribute(name) {
      if (name !== "src") return;
      record("cancel:" + label);
      record("cancel-seq:" + state.readerSeq);
      this._src = null;
    },
  };
}
function makeReaderBody() {
  return {
    _text: "",
    querySelectorAll(selector) {
      if (selector === "img[src]") {
        record("query-img-src");
        return [makeStubImage("loaded", true), makeStubImage("pending", false)];
      }
      if (selector === "img") {
        record("query-img");
        return [makeStubImage("loaded", true), makeStubImage("pending", false)];
      }
      return [];
    },
    replaceChildren() { record("replace"); },
    get textContent() { return this._text; },
    set textContent(value) { this._text = value; record("text"); },
    classList: { add() {}, remove() {}, toggle() {} },
  };
}
function makeElement() {
  return {
    hidden: false,
    textContent: "",
    title: "",
    setAttribute() {},
    replaceChildren() { record("aux-replace"); },
    classList: { add() {}, remove() {}, toggle() {} },
  };
}
function makeView() {
  return {
    _hidden: false,
    get hidden() { return this._hidden; },
    set hidden(value) {
      this._hidden = value;
      if (value) record("hide-view");
    },
    classList: { toggle() {} },
  };
}
const elements = {
  readerBody: makeReaderBody(),
  reader: makeElement(),
  detailEmpty: makeElement(),
  attachments: makeElement(),
  attachmentList: makeElement(),
  renderToggle: makeElement(),
  readerSubject: makeElement(),
  readerFrom: makeElement(),
  readerTo: makeElement(),
  readerCcRow: makeElement(),
  readerCc: makeElement(),
  readerDate: makeElement(),
  tab: makeElement(),
  view: makeView(),
  composeDialog: { open: false, close() {} },
};
function setDetailOpen() {}
function renderList() {}
function hideError() {}
function hideNotice() {}
function restoreProtocolTab() {}
function subjectOf(detail) { return (detail && detail.subject) || ""; }
function senderOf(detail) { return (detail && detail.from) || ""; }
function joinAddresses(value) { return value || ""; }
function renderRecipients(element, value) { element.textContent = value || "—"; }
function formatFullDate() { return ""; }
function loadImages() { record("load"); }
function renderAttachments() { record("attachments"); }
function DOMParser() {}
DOMParser.prototype.parseFromString = function () {
  return { body: { childNodes: [] } };
};
const document = {
  body: { classList: { remove() {} } },
  createElement() { return { className: "", append() {} }; },
};
const localStorage = { setItem() {} };
function resetLog() {
  log = [];
  elements.readerBody = makeReaderBody();
  state.readerSeq = 0;
}
function assertCancelBefore(marker) {
  const cancelIndex = log.findIndex((entry) => entry.startsWith("cancel:"));
  const markerIndex = log.indexOf(marker);
  assert.ok(cancelIndex !== -1, "cancel mancante: " + JSON.stringify(log));
  assert.ok(markerIndex !== -1, marker + " mancante: " + JSON.stringify(log));
  assert.ok(cancelIndex < markerIndex, "cancel dopo " + marker + ": " + JSON.stringify(log));
}
const detail = {
  html: "<p>html</p>",
  text: "testo",
  subject: "Oggetto",
  from: "Mario <mario@example.com>",
  to: "user@example.com",
  cc: "",
  date_ts: null,
  attachments: [],
};

resetLog();
closeReader();
assertCancelBefore("replace");
assert.equal(log.includes("cancel:loaded"), true, JSON.stringify(log));

resetLog();
showReaderLoading();
assertCancelBefore("text");
assert.equal(log.includes("cancel:loaded"), true, JSON.stringify(log));

resetLog();
showReaderError("Errore");
assertCancelBefore("text");
assert.equal(log.includes("cancel:loaded"), true, JSON.stringify(log));

resetLog();
state.detail = detail;
state.renderMode = "html";
renderReader(detail);
assertCancelBefore("replace");
assert.equal(log.includes("cancel:loaded"), true, JSON.stringify(log));
assert.equal(log.includes("load"), true);

resetLog();
state.renderMode = "text";
renderReader(detail);
assertCancelBefore("text");
assert.equal(log.includes("cancel:loaded"), true, JSON.stringify(log));
assert.equal(log.includes("load"), false);

// Testo -> HTML ri-pinga: toggleRenderMode richiama renderReader/loadImages.
resetLog();
state.renderMode = "text";
state.detail = detail;
toggleRenderMode();
assert.equal(state.renderMode, "html");
assert.equal(log.includes("load"), true);

// HTML -> Testo non carica immagini.
resetLog();
toggleRenderMode();
assert.equal(state.renderMode, "text");
assert.equal(log.includes("load"), false);

// all=true cancella anche l'immagine già caricata.
resetLog();
cancelPendingImages({ all: true });
assert.equal(log.includes("cancel:loaded"), true, JSON.stringify(log));
assert.equal(log.includes("cancel:pending"), true, JSON.stringify(log));

// Cambio tab: cancel solo delle pending, loaded preservata, prima della view.
resetLog();
state.open = true;
state.listSeq = 4;
state.selectedUids = new Set(["42"]);
state.selectionMode = true;
deactivateGmail();
assertCancelBefore("hide-view");
assert.equal(log.includes("cancel:pending"), true, JSON.stringify(log));
assert.equal(log.includes("cancel:loaded"), false, JSON.stringify(log));
assert.equal(log.includes("cancel-seq:1"), true, JSON.stringify(log));
assert.equal(state.readerSeq, 1);
assert.equal(state.listSeq, 5);
assert.equal(state.selectionMode, false);
assert.equal(state.selectedUids.size, 0);
"""
    js = js.replace("__FUNCTIONS__", source)
    _run_node(js)


# ── static assets, no-JS and docs ────────────────────────────────────────────


def test_index_html_removes_toggle_and_bumps_gmail_js():
    index = _INDEX_PATH.read_text(encoding="utf-8")

    assert "gmail-images-toggle" not in index
    assert "Mostra immagini" not in index
    assert "gmail.js?v=19" in index
    assert "gmail.css?v=19" in index


def test_gmail_js_has_no_orphan_toggle_remnants():
    script = _read_gmail_js()

    for token in (
        "imagesToggle",
        "imagesLoaded",
        "toggleImages",
        "gmail-images-toggle",
    ):
        assert token not in script
    assert "MAX_IMAGES_PER_MESSAGE = 50" in script
    assert "function cancelPendingImages(" in script
    assert "function loadImages(" in script


def test_server_html_keeps_data_src_without_remote_src():
    cleaned = gmail.sanitize_html(
        '<p>ciao</p><img src="https://tracker.example/pixel.gif" alt="pixel">'
    )

    assert "<img" in cleaned
    assert re.search(r'<img\b[^>]*\bdata-src="gmail-image\?token=', cleaned)
    # ``data-src=`` non deve essere scambiato per un attributo ``src=``.
    assert re.search(r"<img\b[^>]*\ssrc=", cleaned) is None
    assert "https://tracker.example" not in cleaned
    css = _GMAIL_CSS_PATH.read_text(encoding="utf-8")
    assert ".gmail-reader-body.is-html img[data-src]:not([src])" in css
    assert ".gmail-reader-body.is-html img.is-failed" in css


def test_docs_no_longer_describe_click_to_load_images():
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    poc = (_ROOT / "docs/DESIGN_GMAIL_POC.md").read_text(encoding="utf-8")
    for text in (readme, poc):
        assert "Mostra immagini" not in text
        assert "not loaded automatically" not in text.lower()
    assert "loaded by default" in readme.lower()
    assert "caricate di default" in poc.lower()
    assert "25 MP" in readme
    assert "25 MP" in poc


# ── _raster_dimensions ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("data", "content_type", "expected"),
    [
        (_png(640, 480), "image/png", (640, 480)),
        (_gif(320, 200), "image/gif", (320, 200)),
        (_webp_vp8x(1920, 1080), "image/webp", (1920, 1080)),
        (_webp_vp8(800, 600), "image/webp", (800, 600)),
        (_webp_vp8l(1024, 768), "image/webp", (1024, 768)),
        (_jpeg_app1_with_fake_sof(5000, 4000), "image/jpeg", (5000, 4000)),
    ],
)
def test_raster_dimensions_parses_raster_headers(data, content_type, expected):
    assert lp._raster_dimensions(data, content_type) == expected


def test_jpeg_parser_ignores_fake_sof_inside_app1():
    # Se il parser non fosse segment-aware leggerebbe il falso SOF 60000x60000
    # dentro APP1 invece del SOF reale 8000x6000.
    data = _jpeg_app1_with_fake_sof(8000, 6000)
    assert lp._raster_dimensions(data, "image/jpeg") == (8000, 6000)


def test_jpeg_parser_stops_at_eoi_before_sof():
    # EOI termina l'immagine: un SOF nei byte successivi non è il SOF reale.
    data = b"\xff\xd8\xff\xd9" + _jpeg_sof(8000, 6000)
    assert lp._jpeg_dimensions(data) is None


def test_jpeg_parser_reads_sof_before_eoi():
    data = b"\xff\xd8" + _jpeg_sof(640, 480) + b"\xff\xd9"
    assert lp._jpeg_dimensions(data) == (640, 480)


@pytest.mark.parametrize("marker", [0xD0, 0xD7])
def test_jpeg_parser_skips_restart_markers_before_sof(marker):
    # RST0/RST7 sono marker senza lunghezza: il parser deve saltarli e
    # continuare fino al SOF.
    data = b"\xff\xd8\xff" + bytes([marker]) + _jpeg_sof(800, 600) + b"\xff\xd9"
    assert lp._jpeg_dimensions(data) == (800, 600)


@pytest.mark.parametrize(
    "data",
    [
        b"\xff\xd8\x00\x00",  # byte non-FF dopo SOI
        b"\xff\xd8\xff",  # FF a fine buffer
        b"\xff\xd8\xff\xff",  # fill bytes fino a EOF
    ],
)
def test_jpeg_dimensions_rejects_non_ff_and_truncated_marker(data):
    assert lp._jpeg_dimensions(data) is None


def test_jpeg_dimensions_skips_stuffed_zero_marker():
    # FF 00 prima di EOI: il marker 0x00 va ignorato e il parsing prosegue.
    assert lp._jpeg_dimensions(b"\xff\xd8\xff\x00\xff\xd9") is None


@pytest.mark.parametrize("length", range(2, 7))
def test_jpeg_sof_with_too_short_length_returns_none(length):
    data = b"\xff\xd8\xff\xc0" + struct.pack(">H", length) + b"\x00" * length
    assert lp._jpeg_dimensions(data) is None


@pytest.mark.parametrize(
    "data",
    [
        _webp_chunk(b"VP8X", b"\x00" * 9),  # chunk VP8X < 10
        _webp_chunk(b"VP8 ", b"\x00" * 10),  # start code VP8 errato
        _webp_chunk(b"VP8L", b"\x00" + struct.pack("<I", 0)),  # signature errata
        _webp_chunk(b"JUNK", b"\x00" * 8),  # chunk kind sconosciuto
        b"RIFF\x00\x00\x00\x00WEBP",  # header troncato (<20)
        b"RIFF\x00\x00\x00\x00WEBX" + b"\x00" * 4,  # WEBP errato
        _webp_chunk(b"VP8X", b"\x00" * 10)[:20],  # chunk troncato
    ],
)
def test_webp_dimensions_invalid_headers_return_none(data):
    assert lp._webp_dimensions(data) is None


def test_raster_dimensions_unknown_content_type_and_empty_headers():
    assert lp._raster_dimensions(b"", "image/avif") is None
    assert lp._raster_dimensions(b"", "image/png") is None
    assert lp._raster_dimensions(b"", "image/gif") is None
    assert lp._raster_dimensions(b"", "image/webp") is None
    assert lp._raster_dimensions(b"", "image/jpeg") is None


@pytest.mark.parametrize(
    ("data", "content_type"),
    [
        (b"", "image/png"),
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + b"JUNK" + b"\x00" * 8, "image/png"),
        (b"GIF89a", "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBP", "image/webp"),
        (_webp_vp8x(100, 100)[:20], "image/webp"),
        (b"\xff\xd8", "image/jpeg"),
        (b"\xff\xd8\xff\xda\x00\x02", "image/jpeg"),
        (b"\xff\xd8\xff\xe0\x00\x01\xff\xc0\x00\x07\x08\x00\x01\x00\x01", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 24, "image/avif"),
    ],
)
def test_raster_dimensions_truncated_or_malformed_returns_none(data, content_type):
    assert lp._raster_dimensions(data, content_type) is None


# ── fetch_external_image pixel cap ──────────────────────────────────────────


async def test_fetch_external_image_applies_pixel_cap(monkeypatch):
    under = _png(100, 100)
    _install_fake_fetch(monkeypatch, under, "image/png")
    assert await lp.fetch_external_image(
        "https://images.example/under.png", max_pixels=_GMAIL_MAX_PIXELS
    ) == (under, "image/png")

    over = _png(10_000, 10_000)
    _install_fake_fetch(monkeypatch, over, "image/png")
    assert (
        await lp.fetch_external_image(
            "https://images.example/over.png", max_pixels=_GMAIL_MAX_PIXELS
        )
        is None
    )

    at_cap = _png(5000, 5000)  # 25 MP esatti: il cap è ">", non ">=".
    _install_fake_fetch(monkeypatch, at_cap, "image/png")
    assert await lp.fetch_external_image(
        "https://images.example/at-cap.png", max_pixels=_GMAIL_MAX_PIXELS
    ) == (at_cap, "image/png")


async def test_fetch_external_image_rejects_non_raster_body(monkeypatch):
    _install_fake_fetch(monkeypatch, b"<html>not an image</html>", "text/html")
    assert await lp.fetch_external_image("https://images.example/page") is None


async def test_fetch_external_image_pixel_cap_fails_open(monkeypatch):
    # Header non parsabile: il pixel cap è best-effort, resta il byte cap.
    malformed = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + b"JUNK" + b"\x00" * 8
    _install_fake_fetch(monkeypatch, malformed, "image/png")
    assert await lp.fetch_external_image(
        "https://images.example/bad.png", max_pixels=1
    ) == (malformed, "image/png")

    # Default None: le link preview restano invariate (nessun pixel cap).
    over = _png(10_000, 10_000)
    _install_fake_fetch(monkeypatch, over, "image/png")
    assert await lp.fetch_external_image("https://images.example/over.png") == (
        over,
        "image/png",
    )


# ── semaphore (4 per router/app/loop) ───────────────────────────────────────


def _gmail_image_app() -> FastAPI:
    app = FastAPI()
    app.state.api = api_for(SimpleNamespace())
    app.include_router(create_gmail_image_router())
    return app


async def test_image_proxy_limits_concurrent_fetches_to_four(monkeypatch):
    active = 0
    max_active = 0
    calls = 0

    async def fake_fetch(url, max_bytes, max_pixels=None):
        nonlocal active, max_active, calls
        assert max_pixels == _GMAIL_MAX_PIXELS
        calls += 1
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.03)
            return b"\x89PNG\r\n\x1a\nfake", "image/png"
        finally:
            active -= 1

    monkeypatch.setattr("server.link_preview.fetch_external_image", fake_fetch)
    app = _gmail_image_app()
    # Distinct URLs: requests for the same URL share one fetch (and the cache).
    tokens = [_token(f"https://images.example/{index}.png") for index in range(10)]

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = await asyncio.gather(
            *(client.get("/gmail-image", params={"token": token}) for token in tokens)
        )

    assert calls == 10
    assert all(response.status_code == 200 for response in responses)
    assert max_active == 4


def test_semaphore_is_not_loop_bound_across_routers(monkeypatch):
    async def fake_fetch(url, max_bytes, max_pixels=None):
        await asyncio.sleep(0.01)
        return b"\x89PNG\r\n\x1a\nfake", "image/png"

    monkeypatch.setattr("server.link_preview.fetch_external_image", fake_fetch)
    token = _token("https://images.example/a.png")

    for _ in range(2):
        app = _gmail_image_app()

        async def scenario(app=app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                responses = await asyncio.gather(
                    *(
                        client.get("/gmail-image", params={"token": token})
                        for _ in range(6)
                    )
                )
            assert all(response.status_code == 200 for response in responses)

        # Due asyncio.run() creano due loop distinti e sequenziali. Con un
        # semaforo module-level il secondo loop solleverebbe RuntimeError.
        asyncio.run(scenario())
