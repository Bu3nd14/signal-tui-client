from __future__ import annotations

import subprocess


def _run_node(source: str) -> None:
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_linkify_makes_http_urls_clickable_and_trims_punctuation():
    _run_node(r"""
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const start = app.indexOf("function linkifyText(");
const end = app.indexOf("\nfunction timestampMilliseconds", start);
const block = app.slice(start, end);
globalThis.document = {
  createElement: (tag) => ({
    tag,
    children: [],
    textContent: "",
    append(...kids) { this.children.push(...kids); },
    setAttribute(name, value) { this[name] = value; },
  }),
  createDocumentFragment: () => ({
    tag: "#fragment",
    children: [],
    append(...kids) { this.children.push(...kids); },
  }),
  createTextNode: (text) => ({ tag: "#text", text }),
};
vm.runInThisContext(block);

const root = linkifyText("guarda https://example.com/foo. e (vedi https://esempio.it/pag?x=1&y=2)");
assert.equal(root.children.length, 6);

const text = (child) => (child.tag === "#text" ? child.text : child.textContent);
const links = root.children.filter((c) => c.tag === "a");
assert.equal(links.length, 2);
assert.equal(links[0].href, "https://example.com/foo");
assert.equal(links[1].href, "https://esempio.it/pag?x=1&y=2");
assert.equal(links[0].target, "_blank");
assert.equal(links[0].rel, "noopener noreferrer");

const joined = root.children.map(text).join("");
assert.equal(joined, "guarda https://example.com/foo. e (vedi https://esempio.it/pag?x=1&y=2)");

const punctuated = linkifyText("https://example.com/foo.)");
assert.equal(punctuated.children[0].href, "https://example.com/foo");
assert.equal(punctuated.children[1].text, ".)");

const plain = linkifyText("nessun link");
assert.equal(plain.children.filter((c) => c.tag === "a").length, 0);
assert.equal(plain.children[0].tag, "#text");

const backslash = linkifyText("https://example.com/dir\\");
assert.equal(backslash.children[0].href, "https://example.com/dir\\");

const unsupported = linkifyText("javascript:alert(1) ftp://x");
assert.equal(unsupported.children.length, 1);
assert.equal(unsupported.children[0].text, "javascript:alert(1) ftp://x");

const angleBracket = linkifyText("https://a.b/c<d");
assert.equal(angleBracket.children[0].href, "https://a.b/c");
assert.equal(angleBracket.children[1].text, "<d");

const port = linkifyText("http://127.0.0.1:8080/foo,");
assert.equal(port.children[0].href, "http://127.0.0.1:8080/foo");
assert.equal(port.children[1].text, ",");

const empty = linkifyText("");
assert.equal(empty.tag, "#fragment");
assert.equal(empty.children.length, 0);
""")


def test_linkify_keeps_wikipedia_parentheses_urls():
    _run_node(r"""
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const start = app.indexOf("function linkifyText(");
const end = app.indexOf("\nfunction timestampMilliseconds", start);
globalThis.document = {
  createElement: (tag) => ({ tag, children: [], textContent: "", append(...k) { this.children.push(...k); }, setAttribute(n, v) { this[n] = v; } }),
  createDocumentFragment: () => ({ tag: "#fragment", children: [], append(...k) { this.children.push(...k); } }),
  createTextNode: (text) => ({ tag: "#text", text }),
};
vm.runInThisContext(app.slice(start, end));

const root = linkifyText("https://it.wikipedia.org/wiki/Python_(linguaggio)");
const links = root.children.filter((c) => c.tag === "a");
assert.equal(links.length, 1);
assert.equal(links[0].href, "https://it.wikipedia.org/wiki/Python_(linguaggio)");
""")
