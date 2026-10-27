"""Phase 3 Gmail HTML rendering tests: ``<style>`` blocks inlined.

``<style>`` CSS is reduced to allow-listed declarations, applied as inline
styles with ``css-inline`` and then sanitized by the unchanged pipeline.  Any
failure falls back to the document without inlining.  Every case checks
idempotence and the structural safety helpers of the phase 1 suite.
"""

from __future__ import annotations

import sys
import time

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


def _document(css: str, body: str) -> str:
    return (
        "<!DOCTYPE html><html><head><title>t</title>"
        f'<style type="text/css">{css}</style></head><body>{body}</body></html>'
    )


# ---------------------------------------------------------------------------
# Inlining
# ---------------------------------------------------------------------------


def test_class_rules_are_inlined() -> None:
    cleaned = _sanitize(
        _document(
            ".wrapper{max-width:600px;margin:0 auto;background:#ffffff}"
            ".lead{color:#333;font-size:16px}",
            '<table class="wrapper" align="center"><tr><td>'
            '<p class="lead">Ciao</p></td></tr></table>',
        )
    )

    assert 'align="center"' in cleaned
    assert _style_value(cleaned, "max-width") == "600px"
    assert _style_value(cleaned, "background-color") == "#ffffff"
    assert _style_value(cleaned, "font-size") == "16px"
    assert "class=" not in cleaned


def test_body_rule_reaches_the_body_div() -> None:
    cleaned = _sanitize(_document("body{background-color:#f4f4f4}", "<p>x</p>"))

    assert cleaned.startswith('<div style="background-color:#f4f4f4">')


def test_existing_inline_style_wins_unless_important() -> None:
    cleaned = _sanitize(
        _document(
            "p{color:red;font-weight:bold !important}",
            '<p style="color:blue;font-weight:normal">x</p>',
        )
    )

    assert _style_value(cleaned, "color") == "blue"
    assert _style_value(cleaned, "font-weight") == "bold"


def test_descendant_and_child_selectors_are_inlined() -> None:
    cleaned = _sanitize(
        _document(
            "td > p{color:#111} table a{color:#222}",
            '<table><tr><td><p>x</p><a href="https://e.example">l</a></td></tr></table>',
        )
    )

    assert _style_value(cleaned, "color") == "#111"
    assert "color:#222" in cleaned


@pytest.mark.parametrize(
    "css",
    [
        "@media (max-width:480px){p{color:red}}",
        "@import url(https://evil.example/x.css);",
        "@font-face{font-family:x;src:url(https://evil.example/f.woff)}",
        "@supports (display:grid){p{color:red}}",
        "p:hover{color:red}",
        "p:focus{color:red}",
        "p::before{color:red}",
        "p::first-line{color:red}",
        "p{position:absolute;z-index:9;top:0}",
        "p{background:url(https://evil.example/x.gif)}",
        "p{background-image:url(https://evil.example/x.gif)}",
        "p{color:expression(alert(1))}",
        "p{color:var(--x)}",
        "p{behavior:url(x.htc);-moz-binding:url(x)}",
        "p{width:calc(100% - 2px)}",
        "}}}{{{",
        "p{color:",
    ],
)
def test_unsafe_or_unsupported_rules_are_not_inlined(css: str) -> None:
    cleaned = _sanitize(_document(css, "<p>x</p>"))

    assert cleaned == "<div><p>x</p></div>"


def test_selector_list_keeps_stateless_selectors() -> None:
    cleaned = _sanitize(_document("a:hover, p.x {color:#123}", '<p class="x">x</p>'))

    assert _style_value(cleaned, "color") == "#123"


def test_remote_stylesheet_link_is_never_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access")

    monkeypatch.setattr(socket, "create_connection", no_network)
    raw = (
        '<html><head><link rel="stylesheet" href="https://evil.example/x.css">'
        "<style>p{color:red}</style></head><body><p>x</p></body></html>"
    )

    cleaned = _sanitize(raw)

    assert _style_value(cleaned, "color") == "red"
    assert "evil.example" not in cleaned


def test_style_inside_comment_is_not_applied() -> None:
    cleaned = _sanitize("<!-- <style>p{color:red}</style> --><p>x</p>")

    assert cleaned == "<p>x</p>"


def test_style_block_in_body_is_inlined() -> None:
    cleaned = _sanitize('<div><style>.x{color:#abc}</style><p class="x">x</p></div>')

    assert _style_value(cleaned, "color") == "#abc"


# ---------------------------------------------------------------------------
# Kill switches and fallbacks
# ---------------------------------------------------------------------------


def test_kill_switch_disables_inlining(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GMAIL_HTML_INLINE_CSS", "0")

    cleaned = _sanitize(_document("p{color:red}", '<p style="font-weight:bold">x</p>'))

    assert _style_value(cleaned, "color") is None
    assert _style_value(cleaned, "font-weight") == "bold"


def test_styles_kill_switch_also_disables_inlining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GMAIL_HTML_STYLES", "0")

    assert _style_attributes(_sanitize(_document("p{color:red}", "<p>x</p>"))) == []


def test_missing_css_inline_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "css_inline", None)

    cleaned = _sanitize(_document("p{color:red}", "<p>x</p>"))

    assert cleaned == "<div><p>x</p></div>"


def test_inliner_failure_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    import css_inline

    class Broken:
        def __init__(self, **kwargs: object) -> None:
            pass

        def inline(self, html: str) -> str:
            raise css_inline.InlineError("boom")

    monkeypatch.setattr(css_inline, "CSSInliner", Broken)

    cleaned = _sanitize(_document("p{color:red}", "<p>x</p>"))

    assert cleaned == "<div><p>x</p></div>"


def test_inlined_document_over_budget_falls_back_to_original() -> None:
    declarations = ";".join(
        ["color:red", "font-size:12px", "line-height:1.5", "margin:0", "padding:0"]
        + ["font-weight:bold", "text-align:left", "font-style:normal", "opacity:1"]
    )
    body = '<p style="color:blue">x</p>' + "<p>y</p>" * 1_500
    cleaned = _sanitize(_document(f"p{{{declarations}}}", body))

    assert cleaned.startswith('<div><p style="color:blue">x</p><p>y</p>')
    assert len(_style_attributes(cleaned)) == 1


def test_oversized_stylesheet_is_ignored() -> None:
    padding = "/*" + "x" * gmail._INLINE_CSS_MAX_CHARS + "*/"
    cleaned = _sanitize(_document(padding + "p{color:red}", "<p>x</p>"))

    assert cleaned == "<div><p>x</p></div>"


def test_rule_count_is_capped() -> None:
    css = "".join(
        f".c{index}{{color:red}}" for index in range(gmail._INLINE_CSS_MAX_RULES)
    )
    css += ".last{color:blue}"

    assert ".last" not in gmail._filter_stylesheet(css)
    assert ".c0" in gmail._filter_stylesheet(css)


def test_filter_stylesheet_validates_and_aliases() -> None:
    css = gmail._filter_stylesheet(
        "a > b{background:#FFF;color:red !important;position:fixed;mso-hide:all}"
    )

    assert css == "a > b{background-color:#FFF;color:red !important}"


# ---------------------------------------------------------------------------
# Declaration budget (idempotence) and performance
# ---------------------------------------------------------------------------


def test_declaration_count_uses_decoded_entities() -> None:
    assert gmail._style_declaration_count("color:red&#59;color:blue") == 2
    assert gmail._style_declaration_count("color:red;;  ;") == 1


def test_declaration_budget_boundary_is_idempotent() -> None:
    style = ";".join(["color:red"] * 32)
    accepted = f'<p style="{style}">x</p>' * 400
    rejected = accepted + '<p style="color:red">x</p>'

    cleaned = _sanitize(accepted)
    assert cleaned != ""
    assert gmail.sanitize_html(rejected) == ""


def test_inlining_large_newsletter_is_bounded() -> None:
    css = "\n".join(
        f"td.c{index} p, table.k{index} > p {{color:#123;padding:{index % 9}px}}"
        for index in range(500)
    )
    rows = "".join(
        f'<tr><td class="c{index % 500}"><p>riga {index}</p></td></tr>'
        for index in range(1_200)
    )
    raw = _document(css, f"<table>{rows}</table>")

    started = time.perf_counter()
    cleaned = _sanitize(raw)
    elapsed = time.perf_counter() - started
    print(f"\nInline: input={len(raw)}B output={len(cleaned)}B {elapsed * 1000:.1f}ms")

    assert _style_value(cleaned, "color") == "#123"
    assert elapsed < 3.0
