"""Phase 2 Gmail HTML rendering tests: wider allow-list.

* ``<body>`` becomes a ``<div>`` that keeps ``bgcolor``/``text``/``style``;
* ``<font color face size>`` and the global ``dir`` attribute;
* named colors in legacy ``bgcolor``;
* new CSS properties (``max-width``, ``background`` color-only, ``opacity``…)
  and bounded font-relative ``em``/``%`` sizes.

Every case also checks idempotence and the structural safety helpers of the
phase 1 suite.
"""

from __future__ import annotations

import pytest

from server import gmail
from tests.test_gmail_html_phase1 import (
    _assert_document_safe,
    _style_attributes,
    _style_value,
)


def _sanitize(raw: str) -> str:
    cleaned = gmail.sanitize_html(raw)
    assert gmail.sanitize_html(cleaned) == cleaned
    _assert_document_safe(cleaned)
    return cleaned


# ---------------------------------------------------------------------------
# <body> -> <div>
# ---------------------------------------------------------------------------


def test_body_colors_survive_as_div_style() -> None:
    cleaned = _sanitize(
        "<html><head><title>t</title></head>"
        '<body bgcolor="#F4F4F4" text="#333" style="margin:0"><p>x</p></body></html>'
    )

    assert cleaned.startswith("<div ")
    assert "<body" not in cleaned
    assert _style_value(cleaned, "background-color") == "#F4F4F4"
    assert _style_value(cleaned, "color") == "#333"
    assert _style_value(cleaned, "margin") == "0"


def test_body_explicit_style_wins_over_bgcolor() -> None:
    cleaned = _sanitize(
        '<body bgcolor="#000" style="background-color:#fff"><p>x</p></body>'
    )

    (style,) = _style_attributes(cleaned)
    assert style.index("#000") < style.index("#fff")


@pytest.mark.parametrize(
    "attribute",
    [
        'bgcolor="javascript:alert(1)"',
        'bgcolor="url(x)"',
        'text="#fff;position:fixed"',
        'bgcolor="&quot;onload=x"',
        'onload="alert(1)"',
    ],
)
def test_body_bad_attributes_are_dropped(attribute: str) -> None:
    cleaned = _sanitize(f"<body {attribute}><p>x</p></body>")

    assert cleaned == "<div><p>x</p></div>"


def test_body_quoted_style_is_preserved() -> None:
    cleaned = _sanitize(
        "<body style='font-family:\"Helvetica Neue\", Arial'><p>x</p></body>"
    )

    value = _style_value(cleaned, "font-family")
    assert value is not None
    assert '"Helvetica Neue"' in value
    assert "Arial" in value


def test_body_added_style_counts_towards_style_cap() -> None:
    cells = '<p style="color:red">x</p>' * gmail._HTML_MAX_STYLE_ATTRS
    assert gmail.sanitize_html(f"<body>{cells}</body>") != ""
    assert gmail.sanitize_html(f'<body bgcolor="#fff">{cells}</body>') == ""


# ---------------------------------------------------------------------------
# <font>, dir, legacy colors
# ---------------------------------------------------------------------------


def test_font_tag_keeps_valid_attributes() -> None:
    cleaned = _sanitize(
        '<font color="#FF0000" face="Arial, Helvetica" size="+2">f</font>'
    )

    assert cleaned == '<font color="#ff0000" face="Arial, Helvetica" size="+2">f</font>'


@pytest.mark.parametrize(
    "attribute",
    [
        'color="javascript:x"',
        'color="url(x)"',
        'face="a;b"',
        'face="x\'y"',
        'face="' + ",".join(["Arial"] * 9) + '"',
        'size="8"',
        'size="2px"',
        'onclick="x"',
    ],
)
def test_font_tag_drops_invalid_attributes(attribute: str) -> None:
    assert _sanitize(f"<font {attribute}>f</font>") == "<font>f</font>"


@pytest.mark.parametrize("value", ["ltr", "rtl", "auto", "RTL"])
def test_dir_attribute_is_allowed_on_any_tag(value: str) -> None:
    cleaned = _sanitize(f'<p dir="{value}">a</p>')

    assert cleaned == f'<p dir="{value.lower()}">a</p>'


def test_dir_attribute_invalid_is_dropped() -> None:
    assert _sanitize('<p dir="evil">a</p>') == "<p>a</p>"


def test_legacy_bgcolor_accepts_named_colors() -> None:
    cleaned = _sanitize(
        '<table bgcolor="White"><tr><td bgcolor="red">x</td></tr></table>'
    )

    assert 'bgcolor="white"' in cleaned
    assert 'bgcolor="red"' in cleaned


# ---------------------------------------------------------------------------
# CSS additions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declaration", "name", "expected"),
    [
        ("background:#ffffff", "background-color", "#ffffff"),
        ("background:transparent", "background-color", "transparent"),
        ("background:rgb(1,2,3)", "background-color", "rgb(1, 2, 3)"),
        ("max-width:600px", "max-width", "600px"),
        ("max-width:100%", "max-width", "100%"),
        ("max-width:none", "max-width", "none"),
        ("max-height:0", "max-height", "0"),
        ("min-width:320px", "min-width", "320px"),
        ("min-height:auto", "min-height", "auto"),
        ("text-indent:-10px", "text-indent", "-10px"),
        ("list-style-type:lower-roman", "list-style-type", "lower-roman"),
        ("direction:rtl", "direction", "rtl"),
        ("overflow:hidden", "overflow", "hidden"),
        ("visibility:hidden", "visibility", "hidden"),
        ("opacity:0", "opacity", "0"),
        ("opacity:0.5", "opacity", "0.5"),
        ("opacity:50%", "opacity", "50%"),
        ("font-size:100%", "font-size", "100%"),
        ("font-size:1.2em", "font-size", "1.2em"),
        ("line-height:150%", "line-height", "150%"),
        ("line-height:1.4em", "line-height", "1.4em"),
    ],
)
def test_css_phase2_properties_are_preserved(
    declaration: str, name: str, expected: str
) -> None:
    cleaned = _sanitize(f'<div style="{declaration}">x</div>')

    assert _style_value(cleaned, name) == expected


@pytest.mark.parametrize(
    "declaration",
    [
        "background:url(https://evil.example/x)",
        "background:#fff url(https://evil.example/x)",
        "background:#fff no-repeat",
        "background:none",
        "max-width:1601px",
        "max-width:10em",
        "min-width:auto auto",
        "text-indent:257px",
        "text-indent:2em",
        "list-style-type:url(x)",
        "list-style-type:'x'",
        "direction:evil",
        "overflow:scroll",
        "visibility:collapse",
        "opacity:2",
        "opacity:-1",
        "opacity:101%",
        "font-size:0.4em",
        "font-size:2.6em",
        "font-size:49%",
        "font-size:251%",
        "font-size:1.2rem",
        "line-height:99%",
        "line-height:301%",
        "line-height:0.5em",
    ],
)
def test_css_phase2_rejects_unsafe_or_overbound(declaration: str) -> None:
    cleaned = _sanitize(f'<p style="{declaration}">x</p>')

    assert _style_attributes(cleaned) == []
    assert cleaned == "<p>x</p>"


def test_background_alias_keeps_declaration_order() -> None:
    cleaned = _sanitize('<p style="background-color:red;background:#00f">x</p>')

    (style,) = _style_attributes(cleaned)
    assert style == "background-color:red;background-color:#00f"


def test_hidden_preheader_pattern_survives() -> None:
    cleaned = _sanitize(
        '<div style="display:none;max-height:0;overflow:hidden;opacity:0;'
        'mso-hide:all">Anteprima</div><p>Corpo</p>'
    )

    assert _style_value(cleaned, "display") == "none"
    assert _style_value(cleaned, "max-height") == "0"
    assert _style_value(cleaned, "overflow") == "hidden"
    assert "mso-hide" not in cleaned
