"""Sanitizer tests for the Gmail HTML reader."""

from __future__ import annotations

import re
from email.message import EmailMessage
from urllib.parse import parse_qs, urlsplit

from web import gmail


def _image_sources(cleaned: str) -> list[str]:
    return re.findall(r'<img\b[^>]*\bdata-src="([^"]*)"', cleaned)


def test_sanitize_strips_scripts_and_event_handlers():
    raw = (
        '<p onclick="steal()">Ciao <script>alert(1)</script>'
        '<img src="https://tracker.example/pixel" onerror="alert(1)">'
        "<b>grassetto</b></p>"
    )

    cleaned = gmail.sanitize_html(raw)

    assert "<script" not in cleaned
    assert "onclick" not in cleaned
    assert "onerror" not in cleaned
    assert "<img" in cleaned
    sources = _image_sources(cleaned)
    assert len(sources) == 1
    assert sources[0].startswith("/gmail-image?token=")
    assert "<b>grassetto</b>" in cleaned
    assert "Ciao" in cleaned


def test_sanitize_keeps_safe_links_and_drops_javascript():
    raw = (
        '<a href="https://example.com/x" onclick="steal()">ok</a>'
        '<a href="javascript:alert(1)">bad</a>'
        '<a href="/relative">rel</a>'
    )

    cleaned = gmail.sanitize_html(raw)

    assert 'href="https://example.com/x"' in cleaned
    assert "javascript:" not in cleaned
    assert "/relative" not in cleaned
    assert "onclick" not in cleaned
    assert 'rel="noopener noreferrer"' in cleaned


def test_sanitize_strips_styles_and_remote_resources():
    raw = (
        "<style>@import url(https://evil.example/x.css)</style>"
        '<p style="background:url(https://evil.example/pixel)">testo</p>'
        '<iframe src="https://evil.example/frame"></iframe>'
        '<img src="https://evil.example/remote.png" onerror="alert(1)">'
    )

    cleaned = gmail.sanitize_html(raw)

    assert "<style" not in cleaned
    assert "style=" not in cleaned
    assert "background" not in cleaned
    assert "<iframe" not in cleaned
    sources = _image_sources(cleaned)
    assert len(sources) == 1
    assert sources[0].startswith("/gmail-image?token=")
    assert "onerror" not in cleaned
    assert "testo" in cleaned


def test_sanitize_keeps_basic_tables():
    raw = '<table><tr><td colspan="2">cella</td></tr></table>'

    cleaned = gmail.sanitize_html(raw)

    assert "<table>" in cleaned
    assert 'colspan="2"' in cleaned
    assert "cella" in cleaned


def test_sanitize_rejects_oversized_html(monkeypatch):
    monkeypatch.setattr(gmail, "_MAX_HTML_CHARS", 8)

    assert gmail.sanitize_html("<p>troppo lungo</p>") == ""


def test_parse_message_returns_sanitized_html():
    message = EmailMessage()
    message["From"] = "Mario <mario@example.com>"
    message["To"] = "user@example.com"
    message["Subject"] = "HTML"
    message["Date"] = "Fri, 23 Oct 2026 10:00:00 +0000"
    message.set_content("versione testo")
    message.add_alternative(
        "<p>versione <b>HTML</b><script>alert(1)</script></p>",
        subtype="html",
    )

    parsed = gmail.parse_message(message.as_bytes())

    assert parsed["text"] == "versione testo"
    assert "<p>" in parsed["html"]
    assert "<b>HTML</b>" in parsed["html"]
    assert "<script" not in parsed["html"]


def test_sanitize_keeps_safe_layout_table_attributes():
    raw = (
        '<table width="600" align="center"><tr>'
        '<td width="50%" align="left" valign="top">cella</td>'
        "</tr></table>"
    )

    cleaned = gmail.sanitize_html(raw)

    assert 'width="600"' in cleaned
    assert 'align="center"' in cleaned
    assert 'width="50%"' in cleaned
    assert 'align="left"' in cleaned
    assert 'valign="top"' in cleaned


def test_sanitize_rejects_unsafe_image_sources():
    raw = (
        '<img src="javascript:alert(1)">'
        '<img src="cid:inline-image">'
        '<img src="data:image/svg+xml;base64,PHN2Zz4=">'
        '<img src="data:image/png;base64,iVBORw0KGgo=">'
    )

    cleaned = gmail.sanitize_html(raw)

    sources = _image_sources(cleaned)
    assert len(sources) == 1
    assert sources[0] == "data:image/png;base64,iVBORw0KGgo="
    assert "javascript:" not in cleaned
    assert "cid:" not in cleaned
    assert "image/svg+xml" not in cleaned


def test_image_proxy_token_roundtrip_and_tamper():
    url = "https://image.example/path/a.png?x=1&y=2"
    path = gmail.sign_image_url(url)
    token = parse_qs(urlsplit(path).query)["token"][0]

    assert gmail.verify_image_token(token) == url
    replacement = "1" if token[-1] != "1" else "2"
    assert gmail.verify_image_token(token[:-1] + replacement) == ""
