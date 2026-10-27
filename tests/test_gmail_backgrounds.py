"""Newsletter backgrounds and fixed-width mails (Lufthansa WorldShop case).

Hero banners drawn as cell backgrounds (``<td background>`` /
``background-image:url()``) become an inert ``data-bg`` signed for the image
proxy, applied by the client; fixed-width mails wider than the reader are
zoomed down to fit.
"""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import parse_qs, urlsplit

import pytest

from server import gmail
from tests.test_gmail_html_phase1 import _style_value
from tests.test_gmail_images_default import _js_functions, _run_node


def _sanitize(raw: str) -> str:
    cleaned = gmail.sanitize_html(raw)
    assert gmail.sanitize_html(cleaned) == cleaned
    return cleaned


def _data_bg_urls(cleaned: str) -> list[str]:
    urls = []
    for source in re.findall(r'data-bg="([^"]+)"', cleaned):
        token = parse_qs(urlsplit(unescape(source)).query)["token"][0]
        urls.append(gmail.verify_image_token(token))
    return urls


def _cell(attrs: str) -> str:
    return f"<table><tr><td {attrs}>x</td></tr></table>"


# ── server ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "url"),
    [
        (
            _cell('height="466" background="https://news.example/hero.jpg"'),
            "https://news.example/hero.jpg",
        ),
        (
            _cell("style=\"background-image:url('https://news.example/a.png')\""),
            "https://news.example/a.png",
        ),
        (
            _cell('style="background:#000 url(http://news.example/b.gif) no-repeat"'),
            "http://news.example/b.gif",
        ),
        (
            '<div style="background-image: url(&quot;https://n.example/c.png&quot;)">y</div>',
            None,
        ),
        (
            '<table background="https://news.example/t.png"><tr><td>x</td></tr></table>',
            "https://news.example/t.png",
        ),
    ],
)
def test_backgrounds_are_deferred_through_the_proxy(raw, url) -> None:
    cleaned = _sanitize(raw)

    assert "background=" not in cleaned
    assert "url(" not in cleaned
    if url is None:
        # Entity-encoded quotes inside url() are not a plain URL: dropped.
        assert "data-bg" not in cleaned
    else:
        assert _data_bg_urls(cleaned) == [url]


@pytest.mark.parametrize(
    "attrs",
    [
        'background="javascript:alert(1)"',
        'background="data:text/html,x"',
        'background="//evil.example/x.png"',
        'background="/relative.png"',
        "style=\"background-image:url('javascript:alert(1)')\"",
    ],
)
def test_unsafe_backgrounds_are_dropped(attrs) -> None:
    cleaned = _sanitize(_cell(attrs))

    assert "data-bg" not in cleaned
    assert "javascript" not in cleaned


def test_only_layout_tags_get_a_background() -> None:
    cleaned = _sanitize('<p background="https://news.example/p.png">x</p>')

    assert "data-bg" not in cleaned


def test_sender_data_bg_is_signed_and_not_duplicated() -> None:
    cleaned = _sanitize(
        _cell('data-bg="https://x.example/a.png" background="https://y.example/b.png"')
    )

    assert _data_bg_urls(cleaned) == ["https://x.example/a.png"]


@pytest.mark.parametrize(
    ("declaration", "name", "expected"),
    [
        ("background-repeat:no-repeat", "background-repeat", "no-repeat"),
        ("background-repeat:repeat-x", "background-repeat", "repeat-x"),
        ("background-repeat:no-repeat repeat", "background-repeat", "no-repeat repeat"),
        ("background-position:center", "background-position", "center"),
        ("background-position:top left", "background-position", "top left"),
        ("background-position:50% 10px", "background-position", "50% 10px"),
        ("background-size:contain", "background-size", "contain"),
        ("background-size:cover", "background-size", "cover"),
        ("background-size:100% auto", "background-size", "100% auto"),
        ("background-size:600px 300px", "background-size", "600px 300px"),
    ],
)
def test_background_layout_properties_are_kept(declaration, name, expected) -> None:
    cleaned = _sanitize(f'<div style="{declaration}">x</div>')

    assert _style_value(cleaned, name) == expected


@pytest.mark.parametrize(
    "declaration",
    [
        "background-repeat:repeat-x repeat",
        "background-repeat:url(x)",
        "background-position:center center center",
        "background-position:9999px",
        "background-size:contain cover",
        "background-size:-5px",
        "background-size:calc(100% - 1px)",
    ],
)
def test_background_layout_properties_are_validated(declaration) -> None:
    assert _sanitize(f'<div style="{declaration}">x</div>') == "<div>x</div>"


# ── client ───────────────────────────────────────────────────────────────────


def test_load_images_applies_backgrounds_and_counts_them() -> None:
    _run_node(
        r"""
const assert = require("node:assert/strict");
const MAX_IMAGES_PER_MESSAGE = 2;
const notices = [];
function showNotice(message) { notices.push(message); }
function scheduleHtmlFit() {}
const state = { readerSeq: 0 };
const cell = (bg) => ({ dataset: { bg }, style: { backgroundImage: "" } });
const backgrounds = [
  cell("gmail-image?token=abc.0123&v=2"),
  cell("https://evil.example/x.png"),  // not a proxy URL: ignored
  cell("gmail-image?token=def.4567&v=2"),
];
const image = {
  dataset: { src: "gmail-image?token=ghi.89ab&v=2" }, attrs: new Set(), classList: { add() {}, remove() {} },
  hasAttribute(name) { return this.attrs.has(name); },
  addEventListener() {},
  set src(value) { this.attrs.add("src"); this._src = value; },
};
const elements = { readerBody: { querySelectorAll(selector) {
  if (selector === "[data-bg]") return backgrounds;
  if (selector === "img[data-src]") return [image];
  return [];
} } };
"""
        + _js_functions("safeImageUrl", "loadImages")
        + r"""
loadImages();
assert.equal(backgrounds[0].style.backgroundImage, 'url("gmail-image?token=abc.0123&v=2")');
assert.equal(backgrounds[1].style.backgroundImage, "");
assert.equal(backgrounds[2].style.backgroundImage, 'url("gmail-image?token=def.4567&v=2")');
// The cap (2 here) counts backgrounds too: the <img> is left for later.
assert.equal(image._src, undefined);
assert.deepEqual(notices, ["Caricate 2 di 4 immagini."]);
"""
    )


@pytest.mark.parametrize(
    ("available", "needed", "zoom"),
    [(520, 520, ""), (520, 521, ""), (490, 700, "0.7"), (300, 1000, "0.5")],
)
def test_fit_zooms_wide_mails_down(available, needed, zoom) -> None:
    _run_node(
        f"""
const assert = require("node:assert/strict");
const HTML_FIT_MIN_ZOOM = 0.5;
const fit = {{
  style: {{ zoom: "0.3" }},
  clientWidth: {available},
  get scrollWidth() {{ return this.style.zoom ? 0 : {needed}; }},
}};
const elements = {{ readerBody: {{ querySelector: () => fit }} }};
"""
        + _js_functions("fitHtmlToWidth")
        + f"""
fitHtmlToWidth();
assert.equal(fit.style.zoom, {zoom!r});
"""
    )
