"""Phase 5 Gmail HTML rendering tests: foldable quoted text.

The scanner marks the quoted-text containers of common clients with a safe
``data-quote`` attribute (classes and ids are stripped later), and the reader
folds them behind a "•••" button (``collapseQuotes`` run in Node on a small
fake DOM, as in ``test_gmail_images_default.py``).
"""

from __future__ import annotations

import pytest

from server import gmail
from tests.test_gmail_images_default import _js_functions, _run_node


def _sanitize(raw: str) -> str:
    cleaned = gmail.sanitize_html(raw)
    assert gmail.sanitize_html(cleaned) == cleaned
    return cleaned


# ── server: markers ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            '<div class="gmail_quote gmail_quote_container"><p>q</p></div>',
            '<div data-quote="block"><p>q</p></div>',
        ),
        ('<div class="yahoo_quoted">q</div>', '<div data-quote="block">q</div>'),
        (
            '<blockquote type="cite">q</blockquote>',
            '<blockquote data-quote="block">q</blockquote>',
        ),
        (
            '<BLOCKQUOTE TYPE="CITE">q</BLOCKQUOTE>',
            '<blockquote data-quote="block">q</blockquote>',
        ),
        ('<div id="divRplyFwdMsg">Da: x</div>', '<div data-quote="after">Da: x</div>'),
        ('<div id="appendonsend"></div>', '<div data-quote="after"></div>'),
    ],
)
def test_quote_containers_are_marked(raw: str, expected: str) -> None:
    assert _sanitize(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "<blockquote>q</blockquote>",
        '<div class="gmail_quoted">q</div>',
        '<div id="other">q</div>',
        '<p class="gmail_quote">q</p>',
    ],
)
def test_other_elements_are_not_marked(raw: str) -> None:
    assert "data-quote" not in _sanitize(raw)


def test_sender_supplied_marker_values_are_validated() -> None:
    assert (
        _sanitize('<div data-quote="block">q</div>')
        == '<div data-quote="block">q</div>'
    )
    assert _sanitize('<div data-quote="evil">q</div>') == "<div>q</div>"
    assert _sanitize('<p data-quote="block">q</p>') == "<p>q</p>"


def test_marker_is_added_once_and_survives_inlining() -> None:
    raw = (
        "<html><head><style>.gmail_quote{color:#555}</style></head><body>"
        '<p>Risposta</p><div class="gmail_quote" data-quote="after">q</div>'
        "</body></html>"
    )
    cleaned = _sanitize(raw)

    assert cleaned.count("data-quote=") == 1
    assert 'data-quote="after"' in cleaned


# ── client: collapseQuotes in Node ───────────────────────────────────────────

_FAKE_DOM = r"""
const assert = require("node:assert/strict");
class El {
  constructor(tag, attrs = {}, children = []) {
    this.tagName = tag;
    this.attrs = { ...attrs };
    this.children = [];
    this.parentElement = null;
    this.hidden = false;
    this.listeners = {};
    this._text = "";
    this.className = "";
    for (const child of children) this.appendChild(child);
  }
  appendChild(child) {
    if (typeof child === "string") { const t = new El("#text"); t._text = child; child = t; }
    child.parentElement = this;
    this.children.push(child);
    return child;
  }
  get textContent() {
    if (this.tagName === "#text" || !this.children.length) return this._text;
    return this.children.map((c) => c.textContent).join("");
  }
  set textContent(value) { this.children = []; this._text = String(value); }
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  click() { this.listeners.click(); }
  get elements() { return this.children.filter((c) => c.tagName !== "#text"); }
  get nextElementSibling() {
    if (!this.parentElement) return null;
    const siblings = this.parentElement.elements;
    return siblings[siblings.indexOf(this) + 1] || null;
  }
  closest(selector) {
    for (let el = this; el; el = el.parentElement) if ("data-quote" in el.attrs) return el;
    return null;
  }
  querySelectorAll(selector) {
    const found = [];
    const walk = (el) => {
      for (const child of el.elements) {
        if ("data-quote" in child.attrs) found.push(child);
        walk(child);
      }
    };
    walk(this);
    return found;
  }
  before(node) {
    const siblings = this.parentElement.children;
    node.parentElement = this.parentElement;
    siblings.splice(siblings.indexOf(this), 0, node);
  }
}
const document = { createElement: (tag) => new El(tag) };
function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}
const toggles = (root) => {
  const found = [];
  const walk = (el) => {
    for (const child of el.elements) {
      if (child.className === "gmail-quote-toggle") found.push(child);
      walk(child);
    }
  };
  walk(root);
  return found;
};
"""


def _run(body: str) -> None:
    _run_node(_FAKE_DOM + _js_functions("collapseQuotes") + body)


def test_gmail_style_quote_folds_with_nested_quotes() -> None:
    _run(
        r"""
const inner = new El("blockquote", { "data-quote": "block" }, ["citazione più vecchia"]);
const quote = new El("div", { "data-quote": "block" }, ["Il giorno X ha scritto:", inner]);
const root = new El("div", {}, [new El("p", {}, ["Risposta nuova"]), quote]);
collapseQuotes(root);

const [toggle] = toggles(root);
assert.equal(toggles(root).length, 1);
assert.equal(root.elements[1], toggle);
assert.equal(toggle.textContent, "•••");
assert.equal(quote.hidden, true);
assert.equal(inner.hidden, false);
assert.equal(toggle.getAttribute("aria-expanded"), "false");
toggle.click();
assert.equal(quote.hidden, false);
assert.equal(toggle.getAttribute("aria-expanded"), "true");
assert.equal(toggle.title, "Nascondi testo citato");
toggle.click();
assert.equal(quote.hidden, true);
"""
    )


def test_outlook_quote_folds_everything_after_the_marker() -> None:
    _run(
        r"""
const before = new El("p", {}, ["Risposta"]);
const marker = new El("div", { "data-quote": "after" }, []);
const hr = new El("hr");
const header = new El("div", { "data-quote": "after" }, ["Da: Mario"]);
const body = new El("div", {}, ["testo originale"]);
const root = new El("div", {}, [before, marker, hr, header, body]);
collapseQuotes(root);

assert.equal(toggles(root).length, 1);
assert.deepEqual([before, marker, hr, header, body].map((el) => el.hidden),
  [false, true, true, true, true]);
toggles(root)[0].click();
assert.deepEqual([marker, hr, header, body].map((el) => el.hidden),
  [false, false, false, false]);
"""
    )


def test_message_that_is_only_a_quote_is_not_folded() -> None:
    _run(
        r"""
const quote = new El("div", { "data-quote": "block" }, ["Inoltrato: tutto il testo"]);
const root = new El("div", {}, [new El("p", {}, ["  "]), quote]);
collapseQuotes(root);

assert.equal(toggles(root).length, 0);
assert.equal(quote.hidden, false);

const plain = new El("div", {}, [new El("p", {}, ["nessuna citazione"])]);
collapseQuotes(plain);
assert.equal(toggles(plain).length, 0);
"""
    )
