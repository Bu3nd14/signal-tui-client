"""Gmail reader layout: compact recipients, attachment chips, inline images.

Server side, ``inline`` flags image parts the HTML shows via ``cid:``.  Client
side, the real ``gmail.js`` functions run in Node against a tiny fake DOM,
following ``test_gmail_images_default.py``.
"""

from __future__ import annotations

import json
from email.message import EmailMessage

from server import gmail
from tests.test_gmail_images_default import (
    _GMAIL_CSS_PATH,
    _INDEX_PATH,
    _js_functions,
    _run_node,
)

# ── server: inline flag ──────────────────────────────────────────────────────


def test_mark_inline_images_requires_a_cid_reference_and_an_image() -> None:
    attachments = [
        {"content_id": "logo@x", "content_type": "image/png"},
        {"content_id": "Firma%40X", "content_type": "image/jpeg"},
        {"content_id": "unused@x", "content_type": "image/png"},
        {"content_id": "doc@x", "content_type": "application/pdf"},
        {"content_id": None, "content_type": "image/png"},
    ]
    raw_html = (
        "<img src=\"cid:logo@x\"><img src=CID:firma%40x><a href='cid:doc@x'>doc</a>"
    )

    gmail._mark_inline_images(attachments, raw_html)

    assert [item["inline"] for item in attachments] == [
        True,
        False,  # the Content-ID itself is not percent-decoded
        False,
        False,
        False,
    ]


def test_parse_message_flags_inline_image_and_keeps_regular_attachment() -> None:
    message = EmailMessage()
    message["From"] = "a@example.com"
    message["To"] = "b@example.com"
    message["Subject"] = "s"
    message.set_content("testo")
    message.add_alternative('<p>ciao</p><img src="cid:logo123">', subtype="html")
    message.get_payload()[1].add_related(
        b"\x89PNG\r\n\x1a\n", maintype="image", subtype="png", cid="<logo123>"
    )
    message.add_attachment(
        b"%PDF", maintype="application", subtype="pdf", filename="fattura.pdf"
    )

    parsed = gmail.parse_message(message.as_bytes())
    public = gmail.public_attachments(parsed["attachments"])

    flags = {item["content_type"]: item["inline"] for item in public}
    assert flags == {"image/png": True, "application/pdf": False}


# ── static markup / css ──────────────────────────────────────────────────────


def test_reader_markup_has_sticky_actions_and_attachments_before_body() -> None:
    index = _INDEX_PATH.read_text(encoding="utf-8")
    reader = index[index.index('id="gmail-reader"') : index.index("</article>")]

    actions = reader.index('class="gmail-reader-actions"')
    header = reader.index('class="gmail-reader-header"')
    attachments = reader.index('id="gmail-attachments"')
    body = reader.index('id="gmail-reader-body"')
    assert actions < header < attachments < body
    assert 'id="gmail-attachments-toggle"' in reader

    css = _GMAIL_CSS_PATH.read_text(encoding="utf-8")
    assert "max-height: 34%" not in css
    assert "max-height: 38%" not in css


# ── client: recipients and attachments in Node ───────────────────────────────

_FAKE_DOM = r"""
const assert = require("node:assert/strict");
class FakeElement {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.className = "";
    this.hidden = false;
    this.title = "";
    this.type = "";
    this.disabled = false;
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this._text = "";
    this.classList = {
      add: (name) => { this.className = (this.className + " " + name).trim(); },
      remove() {},
      toggle() {},
    };
  }
  get textContent() {
    return this.children.length
      ? this.children.map((child) => child.textContent).join("")
      : this._text;
  }
  set textContent(value) { this.children = []; this._text = String(value); }
  get childElementCount() { return this.children.length; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; this._text = ""; }
  remove() { this.removed = true; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  click() { this.listeners.click(); }
  querySelector(selector) {
    const wanted = selector.replace(/^\./, "");
    const walk = (element) => {
      for (const child of element.children) {
        if (child.className.split(" ").includes(wanted)) return child;
        const found = walk(child);
        if (found) return found;
      }
      return null;
    };
    return walk(this);
  }
}
const document = { createElement: (tag) => new FakeElement(tag) };
function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}
function displayAddress(value) { return value ? String(value) : ""; }
function joinAddresses(value) { return displayAddress(value).trim(); }
"""


def _run(body: str, *functions: str) -> None:
    js = _FAKE_DOM + _js_functions(*functions) + body
    _run_node(js)


def test_split_addresses_keeps_commas_inside_quotes_and_brackets() -> None:
    _run(
        r"""
const parts = splitAddresses(
  '"Rossi, Mario" <mario@example.com>, anna@example.com,  <x,y@example.com> , '
);
assert.deepEqual(parts, [
  '"Rossi, Mario" <mario@example.com>',
  "anna@example.com",
  "<x,y@example.com>",
]);
assert.equal(addressName(parts[0]), "Rossi, Mario");
assert.equal(addressName(parts[1]), "anna@example.com");
assert.equal(addressName("<solo@example.com>"), "solo@example.com");
""",
        "splitAddresses",
        "addressName",
    )


def test_render_recipients_collapses_long_lists() -> None:
    addresses = ", ".join(f"Persona {i} <p{i}@example.com>" for i in range(7))
    _run(
        f"""
const RECIPIENTS_PREVIEW = 3;
const element = document.createElement("dd");
renderRecipients(element, {json.dumps(addresses)});
const [list, more] = element.children;
const visible = () => list.children.filter((item) => !item.hidden).length;
assert.equal(list.children.length, 7);
assert.equal(visible(), 3);
assert.equal(list.children[0].textContent, "Persona 0");
assert.equal(list.children[0].title, "Persona 0 <p0@example.com>");
assert.equal(more.textContent, "e altri 4");
more.click();
assert.equal(visible(), 7);
assert.equal(more.getAttribute("aria-expanded"), "true");
more.click();
assert.equal(visible(), 3);

const short = document.createElement("dd");
renderRecipients(short, "a@example.com, b@example.com");
assert.equal(short.children.length, 1);

const empty = document.createElement("dd");
renderRecipients(empty, "");
assert.equal(empty.textContent, "—");
""",
        "splitAddresses",
        "addressName",
        "renderRecipients",
    )


def _attachments_js(attachments: list[dict[str, object]]) -> str:
    return f"""
const ATTACHMENTS_OPEN_MAX = 4;
const state = {{ selectedUid: 7 }};
const downloads = [];
function safeFilename(value, fallback) {{ return value || fallback; }}
function formatBytes(value) {{ return value + " B"; }}
function downloadAttachment(uid, index) {{ downloads.push([uid, index]); }}
const elements = {{
  attachments: document.createElement("section"),
  attachmentList: document.createElement("div"),
  attachmentsToggle: document.createElement("button"),
}};
renderAttachments({{ uid: 9, attachments: {json.dumps(attachments)} }});
const list = elements.attachmentList;
const toggle = elements.attachmentsToggle;
const chips = list.children.filter((item) => item.className.includes("gmail-attachment-chip"));
"""


def test_attachments_render_as_chips_and_collapse_when_many() -> None:
    few = [
        {"index": i, "filename": f"f{i}.pdf", "size": 10, "inline": False}
        for i in range(2)
    ]
    _run(
        _attachments_js(few)
        + r"""
assert.equal(elements.attachments.hidden, false);
assert.equal(chips.length, 2);
assert.equal(toggle.textContent, "Allegati (2)");
assert.equal(toggle.getAttribute("aria-expanded"), "true");
assert.equal(list.hidden, false);
chips[1].click();
assert.deepEqual(downloads, [[9, 1]]);
toggleAttachments();
assert.equal(list.hidden, true);
assert.equal(toggle.getAttribute("aria-expanded"), "false");
""",
        "renderAttachments",
        "setAttachmentsExpanded",
        "toggleAttachments",
    )
    many = [
        {"index": i, "filename": f"f{i}.pdf", "size": 10, "inline": False}
        for i in range(5)
    ]
    _run(
        _attachments_js(many)
        + r"""
assert.equal(toggle.textContent, "Allegati (5)");
assert.equal(list.hidden, true);
""",
        "renderAttachments",
        "setAttachmentsExpanded",
        "toggleAttachments",
    )


def test_inline_images_are_grouped_behind_a_link() -> None:
    mixed = [
        {"index": 0, "filename": "logo.png", "inline": True},
        {"index": 1, "filename": "fattura.pdf", "inline": False},
        {"index": 2, "filename": "firma.png", "inline": True},
    ]
    _run(
        _attachments_js(mixed)
        + r"""
assert.equal(toggle.textContent, "Allegati (1)");
assert.deepEqual(chips.map((chip) => chip.hidden), [false, true, true]);
assert.equal(chips[0].children[0].textContent, "fattura.pdf");
const more = list.children[list.children.length - 1];
assert.equal(more.textContent, "+ 2 immagini nel testo");
more.click();
assert.deepEqual(chips.map((chip) => chip.hidden), [false, false, false]);
assert.equal(more.removed, true);
""",
        "renderAttachments",
        "setAttachmentsExpanded",
        "toggleAttachments",
    )
    only_inline = [{"index": 0, "filename": "logo.png", "inline": True}]
    _run(
        _attachments_js(only_inline)
        + r"""
assert.equal(toggle.textContent, "Immagini nel testo (1)");
assert.equal(list.hidden, true);
assert.equal(chips[0].hidden, false);
""",
        "renderAttachments",
        "setAttachmentsExpanded",
        "toggleAttachments",
    )
