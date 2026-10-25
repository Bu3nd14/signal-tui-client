"""Phase 1 Gmail HTML rendering tests (design rev. 6).

The tests are grouped by the design sections:

* safe inline CSS preservation and unsafe CSS rejection;
* CSS parser bypasses / fail-closed behaviour;
* legacy table attributes and image proxy idempotence;
* scanner complexity caps and output/entity caps;
* kill switch, dependency/validator failures;
* integration with ``_extract_html`` / ``fetch_message``;
* P1/P2/P3 performance and a Gmail-shaped corpus.

Assertions parse the emitted ``style`` values with ``tinycss2`` instead of
relying on global substrings, so entity escaping does not hide a failed
declaration.
"""

from __future__ import annotations

import html
import re
import sys
import time
from contextlib import contextmanager
from email.message import EmailMessage
from urllib.parse import parse_qs, urlsplit

import pytest
import tinycss2

from web import gmail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STYLE_ATTR_RE = re.compile(r'style="([^"]*)"')
_IMG_DATA_SRC_RE = re.compile(r'<img\b[^>]*\bdata-src="([^"]*)"')

_FORBIDDEN_PROPERTIES = {
    "background",
    "background-image",
    "border-image",
    "list-style",
    "cursor",
    "content",
    "filter",
    "mask",
    "position",
    "z-index",
    "inset",
    "top",
    "right",
    "bottom",
    "left",
    "src",
}
_FORBIDDEN_TOKEN_TYPES = {
    "at-keyword",
    "bad-string",
    "bad-url",
    "url",
    "[] block",
    "{} block",
}
_GLOBAL_KEYWORDS = {"inherit", "initial", "unset", "revert"}
_ALLOWED_COLOR_FUNCTIONS = {"rgb", "rgba", "hsl", "hsla"}


def _sanitize(raw: str) -> str:
    return gmail.sanitize_html(raw)


def _style_attributes(cleaned: str) -> list[str]:
    return [html.unescape(value) for value in _STYLE_ATTR_RE.findall(cleaned)]


def _declarations(cleaned: str) -> list[tinycss2.ast.Declaration]:
    declarations: list[tinycss2.ast.Declaration] = []
    for style in _style_attributes(cleaned):
        for item in tinycss2.parse_declaration_list(
            style, skip_comments=True, skip_whitespace=False
        ):
            if item.type == "declaration":
                declarations.append(item)
    return declarations


def _style_value(cleaned: str, name: str) -> str | None:
    for declaration in _declarations(cleaned):
        if declaration.lower_name == name:
            return tinycss2.serialize(declaration.value).strip()
    return None


def _image_sources(cleaned: str) -> list[str]:
    return _IMG_DATA_SRC_RE.findall(cleaned)


def _unescaped_image_sources(cleaned: str) -> list[str]:
    return [html.unescape(value) for value in _image_sources(cleaned)]


def _token_of(path: str) -> str:
    return parse_qs(urlsplit(path).query)["token"][0]


def _walk_css_tokens(tokens: list[object]) -> None:
    for token in tokens:
        token_type = token.type  # type: ignore[attr-defined]
        assert token_type not in _FORBIDDEN_TOKEN_TYPES, token_type
        if token_type == "function":
            name = token.lower_name  # type: ignore[attr-defined]
            assert name in _ALLOWED_COLOR_FUNCTIONS, name
            _walk_css_tokens(token.arguments)  # type: ignore[attr-defined]
        elif token_type == "ident":
            value = token.lower_value  # type: ignore[attr-defined]
            assert value not in _GLOBAL_KEYWORDS, value
            assert not value.startswith("--"), value
        elif token_type == "literal":
            assert token.value not in {"!", "<", ">", ";"}  # type: ignore[attr-defined]


def _assert_css_safe(cleaned: str) -> None:
    """Structural CSS safety assertions used by the corpus/fuzz checks."""
    for declaration in _declarations(cleaned):
        name = declaration.lower_name
        assert declaration.important is False, name
        assert name not in _FORBIDDEN_PROPERTIES, name
        assert not name.startswith("--"), name
        assert name in gmail._STYLE_VALIDATORS, name
        _walk_css_tokens(declaration.value)


def _assert_document_safe(cleaned: str) -> None:
    lowered = cleaned.lower()
    assert "<style" not in lowered
    assert "<link" not in lowered
    assert " class=" not in lowered
    assert " id=" not in lowered
    assert 'style=""' not in cleaned
    _assert_css_safe(cleaned)


def _wire_bytes(message: EmailMessage) -> bytes:
    return message.as_bytes()


def _email_with_html(html_body: str, text_body: str = "testo") -> EmailMessage:
    message = EmailMessage()
    message["From"] = "Mario <mario@example.com>"
    message["To"] = "user@example.com"
    message["Subject"] = "Fase 1"
    message["Date"] = "Fri, 23 Oct 2026 10:00:00 +0000"
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    return message


# ---------------------------------------------------------------------------
# 1. Safe inline CSS is preserved (design §7.1/§7.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declaration", "name", "expected"),
    [
        ("color:#fff", "color", "#fff"),
        ("color:#ffffff", "color", "#ffffff"),
        ("color:#abcd", "color", "#abcd"),
        ("color:red", "color", "red"),
        ("color:rebeccapurple", "color", "rebeccapurple"),
        ("color:rgb(1,2,3)", "color", "rgb(1, 2, 3)"),
        ("color:rgba(0,0,0,.5)", "color", "rgba(0, 0, 0, 0.5)"),
        ("color:hsl(0,100%,50%)", "color", "hsl(0, 100%, 50%)"),
        ("color:hsla(210,50%,50%,.3)", "color", "hsla(210, 50%, 50%, 0.3)"),
        ("color:transparent", "color", "transparent"),
        ("color:currentColor", "color", "currentColor"),
        ("background-color:#00ff00", "background-color", "#00ff00"),
        ("background-color:currentColor", "background-color", "currentColor"),
    ],
)
def test_css_preserves_safe_colors(declaration: str, name: str, expected: str) -> None:
    cleaned = _sanitize(f'<p style="{declaration}">x</p>')

    assert _style_value(cleaned, name) == expected


def test_css_preserves_multivalue_font_family() -> None:
    cleaned = _sanitize('<p style="font-family:Times New Roman, Arial">x</p>')

    value = _style_value(cleaned, "font-family")
    assert value is not None
    # nh3 re-serializes the value with a cosmetic space before the comma.
    assert re.sub(r"\s*,\s*", ", ", value) == "Times New Roman, Arial"


def test_css_preserves_quoted_font_family() -> None:
    cleaned = _sanitize("<p style=\"font-family:'Times New Roman', Arial\">x</p>")

    value = _style_value(cleaned, "font-family")
    assert value is not None
    assert "Times New Roman" in value
    assert "Arial" in value
    assert value.count('"') == 2


def test_css_font_family_caps() -> None:
    eight = ", ".join(["Arial"] * 8)
    nine = ", ".join(["Arial"] * 9)
    ident_at_limit = "a" * 128
    ident_over_limit = "a" * 129
    quoted_127 = "'" + "a" * 127 + "'"
    quoted_128 = "'" + "a" * 128 + "'"
    quoted_129 = "'" + "a" * 129 + "'"

    assert _style_value(
        _sanitize(f'<p style="font-family:{eight}">x</p>'), "font-family"
    )
    assert _style_attributes(_sanitize(f'<p style="font-family:{nine}">x</p>')) == []
    assert _style_value(
        _sanitize(f'<p style="font-family:{ident_at_limit}">x</p>'), "font-family"
    )
    assert (
        _style_attributes(_sanitize(f'<p style="font-family:{ident_over_limit}">x</p>'))
        == []
    )
    # The 128-char cap applies to the unquoted family value.
    for quoted in (quoted_127, quoted_128):
        assert _style_value(
            _sanitize(f'<p style="font-family:{quoted}">x</p>'), "font-family"
        )
    assert (
        _style_attributes(_sanitize(f'<p style="font-family:{quoted_129}">x</p>')) == []
    )


@pytest.mark.parametrize("name_length", [127, 128])
def test_quoted_font_family_at_limit_is_idempotent(name_length: int) -> None:
    quoted = "'" + "a" * name_length + "'"
    raw = f'<p style="font-family:{quoted}">x</p>'

    cleaned = _sanitize(raw)

    assert _style_value(cleaned, "font-family") is not None
    assert _sanitize(cleaned) == cleaned


def test_quoted_font_family_over_limit_is_idempotent() -> None:
    quoted = "'" + "a" * 129 + "'"
    raw = f'<p style="font-family:{quoted}">x</p>'

    cleaned = _sanitize(raw)

    assert _style_attributes(cleaned) == []
    assert _sanitize(cleaned) == cleaned


@pytest.mark.parametrize(
    ("declaration", "name", "expected"),
    [
        ("font-size:12px", "font-size", "12px"),
        ("font-size:xx-large", "font-size", "xx-large"),
        ("font-weight:bold", "font-weight", "bold"),
        ("font-weight:700", "font-weight", "700"),
        ("font-weight:bolder", "font-weight", "bolder"),
        ("font-style:italic", "font-style", "italic"),
        ("font-variant:small-caps", "font-variant", "small-caps"),
        ("line-height:1.5", "line-height", "1.5"),
        ("line-height:20px", "line-height", "20px"),
        ("line-height:normal", "line-height", "normal"),
        ("letter-spacing:8px", "letter-spacing", "8px"),
        ("letter-spacing:normal", "letter-spacing", "normal"),
        ("text-align:center", "text-align", "center"),
        ("text-transform:uppercase", "text-transform", "uppercase"),
        ("white-space:pre-wrap", "white-space", "pre-wrap"),
        ("word-break:break-all", "word-break", "break-all"),
        ("overflow-wrap:anywhere", "overflow-wrap", "anywhere"),
        ("margin:0 auto", "margin", "0 auto"),
        ("margin:1px 2px 3px 4px", "margin", "1px 2px 3px 4px"),
        ("margin-top:-10px", "margin-top", "-10px"),
        ("padding:8px 12px", "padding", "8px 12px"),
        ("padding:0", "padding", "0"),
        ("border:1px solid #ccc", "border", "1px solid #ccc"),
        ("border:1px", "border", "1px"),
        ("border:red solid 2px", "border", "red solid 2px"),
        ("border-spacing:2px 4px", "border-spacing", "2px 4px"),
        ("border-spacing:2px", "border-spacing", "2px"),
        ("text-decoration:underline overline", "text-decoration", "underline overline"),
        ("text-decoration:none", "text-decoration", "none"),
        ("table-layout:fixed", "table-layout", "fixed"),
        ("border-collapse:collapse", "border-collapse", "collapse"),
        ("vertical-align:top", "vertical-align", "top"),
        ("width:100%", "width", "100%"),
        ("height:auto", "height", "auto"),
        ("display:block", "display", "block"),
        ("display:table-cell", "display", "table-cell"),
        ("float:left", "float", "left"),
    ],
)
def test_css_preserves_safe_properties(
    declaration: str, name: str, expected: str
) -> None:
    cleaned = _sanitize(f'<p style="{declaration}">x</p>')

    assert _style_value(cleaned, name) == expected


@pytest.mark.parametrize(
    ("declaration", "expected"),
    [
        ("border-radius:4px 8px", "4px 8px"),
        ("border-radius:4px 8px / 4px 8px", "4px 8px / 4px 8px"),
        ("border-radius:10px / 20px", "10px / 20px"),
        ("border-radius:10px/20px", "10px / 20px"),
        ("border-radius:0", "0"),
        ("border-radius:100%", "100%"),
    ],
)
def test_css_preserves_border_radius_shorthand(declaration: str, expected: str) -> None:
    cleaned = _sanitize(f'<p style="{declaration}">x</p>')

    assert _style_value(cleaned, "border-radius") == expected


@pytest.mark.parametrize(
    ("declaration", "expected"),
    [
        ("border-top-left-radius:10px 20px", "10px 20px"),
        ("border-top-right-radius:10%", "10%"),
        ("border-bottom-right-radius:0 0", "0 0"),
        ("border-bottom-left-radius:4px", "4px"),
    ],
)
def test_css_preserves_border_radius_longhand(declaration: str, expected: str) -> None:
    cleaned = _sanitize(f'<p style="{declaration}">x</p>')

    name, _ = declaration.split(":", 1)
    assert _style_value(cleaned, name) == expected


def test_css_canonical_output_can_be_longer_than_input() -> None:
    cleaned = _sanitize('<p style="color:rgb(255,0,0)">x</p>')

    assert _style_value(cleaned, "color") == "rgb(255, 0, 0)"
    assert len(_style_attributes(cleaned)[0]) > len("color:rgb(255,0,0)")


# ---------------------------------------------------------------------------
# 2. Unsafe / over-bound CSS is rejected (design §7.2, §8.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "declaration",
    [
        # Resource loading / functions.
        "color:url(https://evil.example/x)",
        "background:url(https://evil.example/x)",
        "background:red",
        "background-image:url(https://evil.example/x)",
        "background-image:none",
        "border-image:url(https://evil.example/x)",
        "color:image-set(url(x) 1x)",
        "color:expression(alert(1))",
        "color:var(--x)",
        "width:env(safe-area-inset-top)",
        "color:attr(data-x)",
        "width:calc(100% - 2px)",
        "--x:1",
        "list-style:url(x)",
        "cursor:url(x)",
        "content:'x'",
        "filter:url(x)",
        "mask:url(x)",
        "color:color(display-p3 1 0 0)",
        "color:hwb(0 0% 0%)",
        "color:lab(50% 0 0)",
        "color:oklab(50% 0 0)",
        # Positioning / overlays.
        "position:absolute",
        "position:relative",
        "z-index:999",
        "inset:0",
        "top:0",
        "right:0",
        "bottom:0",
        "left:0",
        # Relative units and font-size abuse.
        "width:10em",
        "width:10rem",
        "width:10ex",
        "width:10ch",
        "width:10vw",
        "width:10vmin",
        "font-size:100%",
        "font-size:larger",
        "font-size:smaller",
        "font-size:400%",
        # Global keywords.
        "color:inherit",
        "color:initial",
        "color:unset",
        "color:revert",
        "font-family:inherit",
        # Over-bound values.
        "width:1601px",
        "height:1601px",
        "font-size:49px",
        "padding:257px",
        "margin:257px",
        "letter-spacing:9px",
        "line-height:0.5",
        "line-height:4",
        "border-radius:65px",
        "border-radius:1px 2px 3px 4px 5px",
        "border-radius:10px / 20px / 30px",
        "border-top-left-radius:10px / 20px",
        "border-top-left-radius:1px 2px 3px",
        "border-spacing:33px",
        "border-width:9px",
        "white-space:nowrap",
        "white-space:pre",
        "word-break:keep-all",
        "overflow-wrap:normal",
        # Non-finite values.
        "width:1e999px",
        "width:NaNpx",
    ],
)
def test_css_rejects_unsafe_or_overbound(declaration: str) -> None:
    cleaned = _sanitize(f'<p style="{declaration}">x</p>')

    assert _style_attributes(cleaned) == []
    assert cleaned == "<p>x</p>"


@pytest.mark.parametrize(
    "declaration",
    [
        "@import url(https://evil.example/x)",
        "@font-face{font-family:x}",
        "@media screen{color:red}",
        "@supports (display:block){color:red}",
    ],
)
def test_css_rejects_at_rules(declaration: str) -> None:
    cleaned = _sanitize(f'<p style="{declaration}">x</p>')

    assert _style_attributes(cleaned) == []
    assert cleaned == "<p>x</p>"


def test_css_removes_important_flag_and_rejects_bang_values() -> None:
    cleaned = _sanitize('<p style="color:red !important">x</p>')
    assert _style_value(cleaned, "color") == "red"
    assert "!" not in _style_attributes(cleaned)[0]

    cleaned = _sanitize('<p style="color:!garbage">x</p>')
    assert _style_attributes(cleaned) == []


def test_css_keeps_valid_declarations_when_mixed_with_rejected_ones() -> None:
    cleaned = _sanitize(
        '<p style="color:red;position:absolute;background:url(x);'
        '--x:1;@import url(x);width:1px">x</p>'
    )

    assert _style_value(cleaned, "color") == "red"
    assert _style_value(cleaned, "width") == "1px"
    assert _style_value(cleaned, "position") is None
    assert _style_value(cleaned, "background") is None
    assert _style_value(cleaned, "--x") is None


def test_css_empty_or_only_invalid_style_never_emits_empty_attribute() -> None:
    for raw in (
        '<p style="">x</p>',
        '<p style="   ">x</p>',
        '<p style=";/* comment */;">x</p>',
        '<p style="background:url(x)">x</p>',
    ):
        cleaned = _sanitize(raw)
        assert "style=" not in cleaned
        assert cleaned == "<p>x</p>"


# ---------------------------------------------------------------------------
# 3. CSS bypasses, escapes, controls, whitespace, fail-closed (design §4.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "declaration",
    [
        r"c\6flor:red",
        r"\63 olor:red",
        r"c\6f lor:red",
        r"color:r\65 d",
    ],
)
def test_css_resolves_escaped_safe_declarations(declaration: str) -> None:
    cleaned = _sanitize(f'<p style="{declaration}">x</p>')

    assert _style_value(cleaned, "color") == "red"


def test_css_rejects_escaped_unsafe_declarations() -> None:
    for declaration in (
        r"color:u\72 l(https://evil.example/x)",
        r"color:\75 rl(https://evil.example/x)",
        r"color:\65 xpression(alert(1))",
        r"color:\76 ar(--x)",
        r"width:c\61 lc(100% - 1px)",
        r"color:\69 nherit",
        r"font-family:\75 rl(https://evil.example/x)",
    ):
        cleaned = _sanitize(f'<p style="{declaration}">x</p>')
        assert _style_attributes(cleaned) == [], declaration


def test_css_rejects_surrogate_escape() -> None:
    cleaned = _sanitize("<p style=\"font-family:'\\D800'\">x</p>")

    assert _style_attributes(cleaned) == []


def test_css_rejects_control_and_noncharacter_values() -> None:
    for style in (
        "color:red\x01",
        "color:red\x7f",
        "font-family:'a\ufdd0b'",
        "color:red\ufdd0",
    ):
        cleaned = _sanitize(f'<p style="{style}">x</p>')
        assert _style_attributes(cleaned) == [], repr(style)


def test_css_skips_comments_and_normalizes_whitespace() -> None:
    cleaned = _sanitize(
        '<p style="color:/*x*/red; background-color:blue; margin:0    auto">x</p>'
    )

    assert _style_value(cleaned, "color") == "red"
    assert _style_value(cleaned, "background-color") == "blue"
    assert _style_value(cleaned, "margin") == "0 auto"


def test_css_decodes_html_entities_and_double_encoding() -> None:
    assert _style_value(_sanitize('<p style="color:&#114;ed">x</p>'), "color") == "red"
    # Percent-encoding is not a CSS escape and must not turn into a color.
    assert _style_attributes(_sanitize('<p style="color:%72ed">x</p>')) == []


def test_css_property_names_are_case_insensitive() -> None:
    cleaned = _sanitize('<p style="COLOR:RED">x</p>')

    assert _style_value(cleaned, "color") == "RED"


@pytest.mark.parametrize(
    "value",
    [
        "color:red</style><script>x</script>",
        "color:red; width:1px < 2px",
        "color:red; width:1px > 2px",
    ],
)
def test_css_rejects_style_breakout_payloads(value: str) -> None:
    cleaned = _sanitize(f'<p style="{value}">x</p>')

    assert _style_attributes(cleaned) == []
    assert "<script" not in cleaned


def test_lone_surrogates_fail_closed() -> None:
    assert _sanitize("\ud800") == ""
    assert _sanitize('<p style="color:red\ud800">x</p>') == ""
    assert _sanitize('<img alt="\ud800" src="https://e/x">') == ""
    assert gmail._sanitize_style("color:red\ud800") is None


def test_non_finite_numbers_are_rejected() -> None:
    for style in ("width:1e999px", "width:NaNpx", "padding:1e999px"):
        cleaned = _sanitize(f'<p style="{style}">x</p>')
        assert _style_attributes(cleaned) == []


# ---------------------------------------------------------------------------
# 4. Legacy table attributes (design §6)
# ---------------------------------------------------------------------------


def test_legacy_table_attributes_are_preserved() -> None:
    raw = (
        '<table width="600" height="400" bgcolor="#FFF" align="center" '
        'border="1" cellpadding="5" cellspacing="2">'
        '<tr bgcolor="#abc" height="10" align="left" valign="middle">'
        '<td colspan="2" rowspan="3" width="50%" height="99%" '
        'bgcolor="#ABCDEF" align="right" valign="baseline">x</td>'
        "</tr></table>"
    )
    cleaned = _sanitize(raw)

    for expected in (
        'width="600"',
        'height="400"',
        'bgcolor="#fff"',
        'align="center"',
        'border="1"',
        'cellpadding="5"',
        'cellspacing="2"',
        'bgcolor="#abc"',
        'height="10"',
        'align="left"',
        'valign="middle"',
        'colspan="2"',
        'rowspan="3"',
        'width="50%"',
        'height="99%"',
        'bgcolor="#abcdef"',
        'align="right"',
        'valign="baseline"',
    ):
        assert expected in cleaned, expected


def test_legacy_table_scope_colgroup_lists_and_images() -> None:
    raw = (
        '<table><thead><tr><th scope="col" width="10">h</th></tr></thead>'
        '<colgroup span="2"><col span="3" width="20" bgcolor="#fff" '
        'align="center" valign="top"></colgroup>'
        "<tbody><tr><td>x</td></tr></tbody></table>"
        '<ol start="-5"><li value="7">x</li></ol>'
        '<img width="100" height="50" align="middle" alt="x">'
    )
    cleaned = _sanitize(raw)

    for expected in (
        'scope="col"',
        'span="2"',
        'span="3"',
        'width="20"',
        'bgcolor="#fff"',
        'align="center"',
        'valign="top"',
        'start="-5"',
        'value="7"',
        'width="100"',
        'height="50"',
        'align="middle"',
        'alt="x"',
    ):
        assert expected in cleaned, expected


def test_legacy_invalid_and_over_bound_values_are_removed() -> None:
    raw = (
        '<table width="-5" height="9999" bgcolor="javascript:alert(1)" '
        'border="999" cellpadding="-1" cellspacing="51" align="evil">'
        '<tr height="9999" bgcolor="#12345" valign="evil">'
        '<td colspan="0" rowspan="999" width="abc" height="9999" '
        'bgcolor="red" align="evil" valign="evil">x</td>'
        '<th scope="evil">h</th></tr></table>'
        '<ol start="99999"><li value="-99999">x</li></ol>'
        '<img width="10001" height="2001" align="evil">'
    )
    cleaned = _sanitize(raw)

    for forbidden in (
        "-5",
        "9999",
        "javascript",
        'border="999"',
        'cellpadding="-1"',
        'cellspacing="51"',
        'align="evil"',
        'valign="evil"',
        'colspan="0"',
        'rowspan="999"',
        'width="abc"',
        'bgcolor="red"',
        'scope="evil"',
        'start="99999"',
        'value="-99999"',
        'width="10001"',
        'height="2001"',
    ):
        assert forbidden not in cleaned, forbidden


def test_legacy_nowrap_is_never_emitted() -> None:
    raw = "<table nowrap><tr><td nowrap>x</td></tr></table>"

    cleaned = _sanitize(raw)

    assert "nowrap" not in cleaned.lower()
    assert "<table>" in cleaned


# ---------------------------------------------------------------------------
# 5. Image proxy tokens and idempotence (design §5.3)
# ---------------------------------------------------------------------------


def test_remote_image_src_is_replaced_by_signed_data_src() -> None:
    url = "https://image.example/path/a.png?x=1&y=2"

    cleaned = _sanitize(f'<img src="{url}">')

    sources = _unescaped_image_sources(cleaned)
    assert len(sources) == 1
    assert sources[0].startswith("/gmail-image?token=")
    assert gmail.verify_image_token(_token_of(sources[0])) == url
    assert "https://image.example" not in cleaned


def test_remote_image_data_src_is_signed() -> None:
    url = "https://image.example/path/a.png"

    cleaned = _sanitize(f'<img data-src="{url}">')

    sources = _unescaped_image_sources(cleaned)
    assert len(sources) == 1
    assert gmail.verify_image_token(_token_of(sources[0])) == url


def test_signed_image_path_is_preserved_unchanged() -> None:
    path = gmail.sign_image_url("https://image.example/a.png")

    cleaned = _sanitize(f'<img data-src="{path}">')

    assert _unescaped_image_sources(cleaned) == [path]


def test_signed_image_path_with_amp_entity_is_preserved() -> None:
    path = gmail.sign_image_url("https://image.example/a.png")
    escaped_path = path.replace("&", "&amp;")

    cleaned = _sanitize(f'<img data-src="{escaped_path}">')

    assert _unescaped_image_sources(cleaned) == [path]


def test_tampered_or_multi_dot_signed_tokens_are_removed() -> None:
    path = gmail.sign_image_url("https://image.example/a.png")
    token = _token_of(path)
    replacement = "1" if token[-1] != "1" else "2"
    tampered = path.replace(token, token[:-1] + replacement, 1)
    multi_dot = f"/gmail-image?token={token}.a.b"

    for candidate in (tampered, multi_dot):
        cleaned = _sanitize(f'<img data-src="{candidate}">')
        assert _image_sources(cleaned) == [], candidate


def test_unsafe_image_sources_are_removed() -> None:
    raw = (
        '<img src="javascript:alert(1)">'
        '<img src="cid:inline-image">'
        '<img src="data:image/svg+xml;base64,PHN2Zz4=">'
        '<img src="https://e/x" data-src="javascript:alert(1)">'
    )

    cleaned = _sanitize(raw)

    assert _image_sources(cleaned) == []
    assert "javascript:" not in cleaned
    assert "cid:" not in cleaned
    assert "image/svg+xml" not in cleaned


def test_safe_data_uri_is_preserved() -> None:
    raw = '<img src="data:image/png;base64,iVBORw0KGgo=">'

    cleaned = _sanitize(raw)

    assert _unescaped_image_sources(cleaned) == ["data:image/png;base64,iVBORw0KGgo="]


def test_image_alt_with_gt_and_url_with_gt_are_escaped() -> None:
    raw = '<img src="https://e/x?a=>b" alt="a > b">'

    cleaned = _sanitize(raw)

    assert 'alt="a &gt; b"' in cleaned
    assert _unescaped_image_sources(cleaned)
    assert _sanitize(cleaned) == cleaned


def test_src_inside_style_is_not_an_attack_surface() -> None:
    cleaned = _sanitize('<img style="src:https://evil.example/x">')

    assert cleaned == "<img>"
    assert "style=" not in cleaned
    assert "evil.example" not in cleaned


def test_image_handling_is_idempotent() -> None:
    path = gmail.sign_image_url("https://image.example/a.png")
    inputs = [
        '<img src="https://image.example/a.png">',
        '<img data-src="https://image.example/a.png">',
        f'<img data-src="{path}">',
        f'<img src="{path}">',
        '<img src="data:image/png;base64,AAAA">',
        '<img src="https://e/x" alt="a > b">',
        '<img src="https://e/x" alt="a &amp; b">',
        '<img src="https://e/x" src="https://track.example/y">',
    ]
    for raw in inputs:
        first = _sanitize(raw)
        assert _sanitize(first) == first, raw


def test_large_signed_token_is_preserved_above_url_length_cap() -> None:
    long_url = "https://image.example/" + "a" * 5000 + ".png"
    path = gmail.sign_image_url(long_url)
    token = _token_of(path)

    assert len(token) > gmail._IMAGE_URL_MAX_LEN
    cleaned = _sanitize(f'<img data-src="{path}">')

    assert _unescaped_image_sources(cleaned) == [path]
    assert _sanitize(cleaned) == cleaned


def test_unsigned_remote_url_above_cap_is_removed() -> None:
    long_url = "https://e/" + "a" * 4100

    cleaned = _sanitize(f'<img src="{long_url}">')

    assert _image_sources(cleaned) == []


def test_uppercase_signed_digest_is_rejected() -> None:
    path = gmail.sign_image_url("https://image.example/a.png")
    token = _token_of(path)
    base, digest = token.rsplit(".", 1)
    uppercase = f"/gmail-image?token={base}.{digest.upper()}"

    cleaned = _sanitize(f'<img data-src="{uppercase}">')

    assert _image_sources(cleaned) == []


# ---------------------------------------------------------------------------
# 6. Scanner and complexity caps (design §5.1/§5.2)
# ---------------------------------------------------------------------------


def test_tag_cap_boundary() -> None:
    assert _sanitize("<br>" * gmail._HTML_MAX_TAGS) != ""
    assert _sanitize("<br>" * (gmail._HTML_MAX_TAGS + 1)) == ""


def test_style_attribute_cap_boundary() -> None:
    accepted = '<p style="color:red">x</p>' * gmail._HTML_MAX_STYLE_ATTRS
    excessive = '<p style="color:red">x</p>' * (gmail._HTML_MAX_STYLE_ATTRS + 1)

    assert _sanitize(accepted) != ""
    assert _sanitize(excessive) == ""


def test_depth_cap_boundary() -> None:
    at_limit = "<div>" * gmail._HTML_MAX_DEPTH + "x" + "</div>" * gmail._HTML_MAX_DEPTH
    over_limit = (
        "<div>" * (gmail._HTML_MAX_DEPTH + 1)
        + "x"
        + "</div>" * (gmail._HTML_MAX_DEPTH + 1)
    )

    assert _sanitize(at_limit) != ""
    assert _sanitize(over_limit) == ""


def test_tbody_is_excluded_from_tag_and_depth_caps() -> None:
    raw = "<table>" + "<tbody>" * 9000 + "<tr><td>x</td></tr>" + "</table>"

    cleaned = _sanitize(raw)

    assert cleaned != ""
    assert "<table>" in cleaned


def test_attribute_count_cap_boundary() -> None:
    at_limit = "<p " + " ".join(f"data-x{i}='1'" for i in range(256)) + ">x</p>"
    over_limit = "<p " + " ".join(f"data-x{i}='1'" for i in range(257)) + ">x</p>"

    assert _sanitize(at_limit) != ""
    assert _sanitize(over_limit) == ""


def test_attribute_length_cap_boundary() -> None:
    # attr_length = len("title") + 1 + len(value).
    at_limit = '<p title="' + "a" * 16378 + '">x</p>'
    over_limit = '<p title="' + "a" * 16379 + '">x</p>'

    assert _sanitize(at_limit) != ""
    assert _sanitize(over_limit) == ""


def test_text_cap_boundary_is_independent_of_input_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gmail, "_HTML_MAX_TEXT_CHARS", 8)

    assert _sanitize("<p>" + "a" * 8 + "</p>") != ""
    assert _sanitize("<p>" + "a" * 9 + "</p>") == ""


def test_void_and_raw_text_tags_do_not_count_raw_content() -> None:
    comment = "<!--" + "<div>" * 9000 + "-->"
    raw_text = "<style>" + "<div>" * 9000 + "</style>"

    assert _sanitize(comment + "<p>x</p>") == "<p>x</p>"
    assert _sanitize(raw_text + "<p>x</p>") == "<p>x</p>"


def test_unquoted_attributes_and_case_are_handled() -> None:
    cleaned = _sanitize("<IMG SRC=https://e/x ALT=hello>")

    assert _unescaped_image_sources(cleaned)
    assert 'alt="hello"' in cleaned

    cleaned = _sanitize('<P STYLE="COLOR:RED">X</P>')
    assert _style_value(cleaned, "color") == "RED"


def test_cdata_and_comments_are_stable() -> None:
    for raw in (
        "<![CDATA[ <img src='https://e/x'> ]]><p>x</p>",
        "<!-- <img src='https://e/x'> --><p>x</p>",
    ):
        first = _sanitize(raw)
        assert "<img" not in first
        assert _sanitize(first) == first


def test_cap_violations_are_fail_closed_and_idempotent() -> None:
    over_tag_cap = "<br>" * (gmail._HTML_MAX_TAGS + 1)

    assert _sanitize(over_tag_cap) == ""
    assert _sanitize(_sanitize(over_tag_cap)) == ""


def test_idempotence_at_cap_boundaries() -> None:
    payloads = [
        "<br>" * gmail._HTML_MAX_TAGS,
        '<p style="color:red">x</p>' * gmail._HTML_MAX_STYLE_ATTRS,
        "<table>" + "<tr><td>x</td></tr>" * 2666 + "</table>",
        "a" * 999_000,
    ]

    for raw in payloads:
        first = _sanitize(raw)
        assert first != ""
        assert _sanitize(first) == first


@pytest.mark.parametrize("tag", sorted(gmail._OPTIONAL_END_TAGS))
def test_scanner_treats_every_optional_end_tag_as_flat(tag: str) -> None:
    raw = f"<{tag}>" * (gmail._HTML_MAX_DEPTH + 1)

    assert gmail._pre_scan_html(raw) != ""


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("p", "<p>x" * (gmail._HTML_MAX_DEPTH + 1)),
        ("li", "<ul>" + "<li>x" * (gmail._HTML_MAX_DEPTH + 1) + "</ul>"),
        (
            "tr-td",
            "<table>" + "<tr><td>x" * (gmail._HTML_MAX_DEPTH + 1) + "</table>",
        ),
    ],
)
def test_optional_end_tags_without_closers_do_not_trigger_depth_cap(
    label: str, payload: str
) -> None:
    """Optional-end tags are flat in HTML and must not consume the depth cap."""
    cleaned = _sanitize(payload)

    assert cleaned != "", label
    assert _sanitize(cleaned) == cleaned, label


def test_nested_optional_end_tags_below_tag_cap_are_accepted() -> None:
    payload = (
        "<div>" * 10
        + "<p>x" * (gmail._HTML_MAX_DEPTH + 1)
        + "<ul>"
        + "<li>x" * (gmail._HTML_MAX_DEPTH + 1)
        + "</ul>"
        + "<table>"
        + "<tr><td>x" * (gmail._HTML_MAX_DEPTH + 1)
        + "</table>"
    )
    assert payload.count("<") < gmail._HTML_MAX_TAGS

    cleaned = _sanitize(payload)

    assert cleaned != ""
    assert _sanitize(cleaned) == cleaned


def test_129_nested_div_still_triggers_depth_cap() -> None:
    raw = "<div>" * (gmail._HTML_MAX_DEPTH + 1)

    assert _sanitize(raw) == ""
    assert _sanitize(_sanitize(raw)) == ""


# ---------------------------------------------------------------------------
# 7. Output cap / entity-heavy input (design §5.2b, §8.1)
# ---------------------------------------------------------------------------


def test_entity_heavy_text_is_idempotent() -> None:
    raw = "&" * 100_000

    cleaned = _sanitize(raw)

    assert cleaned != ""
    assert len(cleaned) <= gmail._HTML_MAX_OUTPUT_CHARS
    assert _sanitize(cleaned) == cleaned


def test_quote_heavy_text_is_idempotent() -> None:
    raw = '"' * 100_000

    cleaned = _sanitize(raw)

    assert cleaned != ""
    assert _sanitize(cleaned) == cleaned


def test_style_with_many_quotes_plus_entity_heavy_text() -> None:
    families = ", ".join(f'"Family {index} ' + "x" * 80 + '"' for index in range(8))
    raw = f"<p style='font-family:{families}'>" + "&" * 100_000 + "</p>"

    cleaned = _sanitize(raw)

    assert cleaned != ""
    assert _style_value(cleaned, "font-family") is not None
    assert len(cleaned) <= gmail._HTML_MAX_OUTPUT_CHARS
    assert _sanitize(cleaned) == cleaned


def test_entity_expansion_output_cap_boundary() -> None:
    at_cap = "&" * 200_000
    over_cap = "&" * 200_001

    accepted = _sanitize(at_cap)
    assert accepted != ""
    assert len(accepted) <= gmail._HTML_MAX_OUTPUT_CHARS
    assert _sanitize(accepted) == accepted

    assert _sanitize(over_cap) == ""


def test_output_cap_is_enforced_after_nh3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gmail, "_HTML_MAX_OUTPUT_CHARS", 32)

    assert _sanitize("<p>" + "a" * 10 + "</p>") != ""
    assert _sanitize("<p>" + "a" * 40 + "</p>") == ""


def test_style_serialized_cap_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gmail, "_STYLE_SERIALIZED_MAX", 16)

    assert _sanitize('<p style="color:red">x</p>') == '<p style="color:red">x</p>'
    assert _sanitize('<p style="color:red;background-color:blue">x</p>') == "<p>x</p>"


def test_style_declaration_cap_is_fail_closed() -> None:
    at_limit = ";".join(["color:red"] * gmail._STYLE_MAX_DECLS)
    over_limit = ";".join(["color:red"] * (gmail._STYLE_MAX_DECLS + 1))

    assert _sanitize(f'<p style="{at_limit}">x</p>') != "<p>x</p>"
    assert _sanitize(f'<p style="{over_limit}">x</p>') == "<p>x</p>"


def _long_font_family_style(name_width: int) -> str:
    families = ", ".join("'" + "x" * name_width + "'" for _ in range(8))
    return "font-family:" + families


def test_style_input_length_cap_is_enforced() -> None:
    accepted = _long_font_family_style(119)  # 994 chars, below _STYLE_MAX_LEN
    rejected = _long_font_family_style(124)  # 1034 chars, above _STYLE_MAX_LEN

    assert len(accepted) <= gmail._STYLE_MAX_LEN
    assert len(rejected) > gmail._STYLE_MAX_LEN
    assert _sanitize(f'<p style="{accepted}">x</p>') != "<p>x</p>"
    assert _sanitize(f'<p style="{rejected}">x</p>') == "<p>x</p>"


def test_style_attribute_escaping_stays_below_hard_attribute_cap() -> None:
    # 8 * 100 ampersands are valid font-family strings (max 8 families,
    # <=128 chars each).  nh3 expands each "&" to "&amp;" (5x), so the final
    # attribute is larger than _STYLE_SERIALIZED_MAX but must stay below the
    # 16 KiB hard cap and remain idempotent.
    families = ", ".join("'" + "&" * 100 + "'" for _ in range(8))
    cleaned = _sanitize(f'<p style="font-family:{families}">x</p>')

    assert cleaned != ""
    raw_style_value = _STYLE_ATTR_RE.findall(cleaned)[0]
    assert len(raw_style_value) > gmail._STYLE_SERIALIZED_MAX
    assert len(raw_style_value) <= gmail._HTML_MAX_ATTR_LEN
    assert _sanitize(cleaned) == cleaned


# ---------------------------------------------------------------------------
# 8. Kill switch (design §4.3)
# ---------------------------------------------------------------------------


def test_kill_switch_removes_styles_but_keeps_legacy_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GMAIL_HTML_STYLES", "0")
    raw = (
        '<table width="600" bgcolor="#fff">'
        '<tr><td style="color:red;background-color:blue" colspan="2">x</td>'
        "</tr></table>"
        '<p style="font-weight:bold">y</p>'
    )

    cleaned = _sanitize(raw)

    assert "style=" not in cleaned
    assert 'width="600"' in cleaned
    assert 'bgcolor="#fff"' in cleaned
    assert 'colspan="2"' in cleaned
    assert "<p>y</p>" in cleaned


def test_kill_switch_accepts_values_other_than_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GMAIL_HTML_STYLES", " 1 ")
    cleaned = _sanitize('<p style="color:red">x</p>')
    assert _style_value(cleaned, "color") == "red"

    monkeypatch.setenv("GMAIL_HTML_STYLES", " 0 ")
    cleaned = _sanitize('<p style="color:red">x</p>')
    assert "style=" not in cleaned


# ---------------------------------------------------------------------------
# 9. Fail-closed: dependency / callback / validator failures (design §4.1)
# ---------------------------------------------------------------------------


def test_missing_tinycss2_removes_styles_but_keeps_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "tinycss2", None)

    cleaned = _sanitize(
        '<table width="600"><tr><td style="color:red">x</td></tr></table>'
    )

    assert "style=" not in cleaned
    assert 'width="600"' in cleaned
    assert "x" in cleaned


def test_missing_nh3_disables_html_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "nh3", None)

    assert _sanitize("<p>x</p>") == ""


def test_nh3_exception_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    import nh3

    def boom(*args: object, **kwargs: object) -> str:
        raise RuntimeError("clean failed")

    monkeypatch.setattr(nh3, "clean", boom)

    assert _sanitize("<p>x</p>") == ""


def test_pre_scan_exception_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(raw: str) -> str:
        raise RuntimeError("scan failed")

    monkeypatch.setattr(gmail, "_pre_scan_html", boom)

    assert _sanitize("<p>x</p>") == ""


def test_style_callback_exception_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(value: str) -> str | None:
        raise RuntimeError("style failed")

    monkeypatch.setattr(gmail, "_sanitize_style", boom)

    cleaned = _sanitize(
        '<table width="600"><tr><td style="color:red">x</td></tr></table>'
    )

    assert "style=" not in cleaned
    assert 'width="600"' in cleaned


def test_style_validator_exception_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(tokens: list[object]) -> str | None:
        raise RuntimeError("validator failed")

    monkeypatch.setitem(gmail._STYLE_VALIDATORS, "color", boom)

    cleaned = _sanitize('<p style="color:red">x</p>')

    assert "style=" not in cleaned
    assert cleaned == "<p>x</p>"


def test_legacy_validator_exception_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(value: str) -> str | None:
        raise RuntimeError("legacy failed")

    monkeypatch.setitem(gmail._LEGACY_ATTR_VALIDATORS, ("table", "width"), boom)

    cleaned = _sanitize('<table width="600"><tr><td>x</td></tr></table>')

    assert 'width="600"' not in cleaned
    assert "<table>" in cleaned


def test_public_sanitizer_api_contract() -> None:
    assert gmail.sanitize_html("") == ""
    assert gmail.sanitize_html(None) == ""  # type: ignore[arg-type]
    assert gmail.sanitize_html("<p>x</p>") == "<p>x</p>"
    assert isinstance(gmail.sanitize_html("<p>x</p>"), str)


def test_output_is_csp_attribute_only() -> None:
    raw = (
        "<style>p{color:red}</style>"
        '<p class="x" id="y" onclick="steal()" style="color:red">x</p>'
        '<a href="https://e/x" class="z">link</a>'
    )

    cleaned = _sanitize(raw)

    _assert_document_safe(cleaned)
    assert "<style" not in cleaned
    assert "class=" not in cleaned
    assert "id=" not in cleaned
    assert "onclick" not in cleaned


# ---------------------------------------------------------------------------
# 10. Regression: extraction and fetch detail integration
# ---------------------------------------------------------------------------


def test_extract_html_from_multipart_alternative_is_sanitized() -> None:
    message = _email_with_html(
        '<p style="color:#333;position:absolute">Ciao <b>HTML</b></p>'
        "<script>alert(1)</script>"
        '<img src="https://track.example/pixel.gif">'
    )

    cleaned = gmail._extract_html(message)

    assert "<p" in cleaned
    assert "<b>HTML</b>" in cleaned
    assert "<script" not in cleaned
    assert "position" not in " ".join(_style_attributes(cleaned))
    assert _unescaped_image_sources(cleaned)
    assert _sanitize(cleaned) == cleaned


def test_parse_message_returns_sanitized_html_with_inline_cid() -> None:
    message = EmailMessage()
    message["From"] = "Mario <mario@example.com>"
    message["To"] = "user@example.com"
    message["Subject"] = "Inline"
    message["Date"] = "Fri, 23 Oct 2026 10:00:00 +0000"
    message.set_content(
        '<p style="color:red">Ciao</p><img src="cid:logo@example.com">',
        subtype="html",
    )
    message.add_related(
        b"PNGDATA", maintype="image", subtype="png", cid="<logo@example.com>"
    )

    parsed = gmail.parse_message(_wire_bytes(message))

    assert "Ciao" in parsed["html"]
    assert _style_value(parsed["html"], "color") == "red"
    assert "cid:" not in parsed["html"]
    assert _image_sources(parsed["html"]) == []
    assert len(parsed["attachments"]) == 1


def test_fetch_message_detail_applies_sanitizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html_body = (
        '<p style="color:#333;position:absolute">Ciao</p>'
        "<script>alert(1)</script>"
        '<img src="https://track.example/pixel.gif">'
    )
    raw = _wire_bytes(_email_with_html(html_body, "testo"))

    class _Connection:
        def select(
            self, mailbox: str, readonly: bool = False
        ) -> tuple[str, list[bytes]]:
            return "OK", [b"1"]

        def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
            if command == "FETCH":
                meta = f"1 (UID 1 FLAGS () BODY[] {{{len(raw)}}}".encode()
                return "OK", [(meta, raw)]
            return "NO", []

    @contextmanager
    def _connect(settings: object = None):
        yield _Connection()

    settings = gmail.GmailSettings(
        address="sender@example.com",
        password="not-a-real-secret",
        imap_host="imap.example.com",
        imap_port=993,
        smtp_host="smtp.example.com",
        smtp_port=587,
        folder="INBOX",
        timeout_s=30,
    )
    monkeypatch.setattr(gmail, "load_settings", lambda: settings)
    monkeypatch.setattr(gmail, "imap_connect", _connect)
    monkeypatch.setattr(gmail, "_PARTIAL_FETCH_ENABLED", False)

    detail = gmail.fetch_message(1)

    assert detail["html"]
    assert "Ciao" in detail["html"]
    assert "<script" not in detail["html"]
    assert _style_value(detail["html"], "position") is None
    assert _unescaped_image_sources(detail["html"])
    assert _sanitize(detail["html"]) == detail["html"]


# ---------------------------------------------------------------------------
# 11. Performance P1/P2/P3 (design §9.3/§10) with generous CI budgets
# ---------------------------------------------------------------------------


def _build_p1_payload() -> str:
    """~950 KB reference: ~4k tags, 250 style attrs, 50 images, ~200 KB text."""
    style = (
        "color:#333;font-family:Arial, Helvetica, sans-serif;font-size:14px;"
        "font-weight:normal;line-height:1.5;text-align:left;margin:0 auto;"
        "padding:8px 12px;border:1px solid #ccc;border-radius:4px;"
        "background-color:#fafafa;vertical-align:top"
    )
    text = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 8
    parts: list[str] = []
    tags = 0
    for _ in range(250):
        parts.append(f'<p style="{style}">{text}</p>')
        tags += 1
    for index in range(50):
        parts.append(f'<img src="https://images.example/{index}.png" alt="img{index}">')
        tags += 1
    long_url = "https://track.example.com/click/" + "a" * 615
    for index in range(1000):
        parts.append(f'<a href="{long_url}{index}">link{index}</a>')
        tags += 2
    for _ in range(200):
        parts.append(f"<p>{text}</p>")
        tags += 1
    filler = max(0, 4000 - tags)
    if filler:
        parts.append("<br>" * filler)
    return "".join(parts)


def _build_p2_payload() -> str:
    """Cap-bounded worst case: 8000 tags, 400 style attrs, ~200 KB text."""
    declarations = (
        "color:#333;background-color:#fff;font-size:12px;font-weight:normal;"
        "line-height:1.5;text-align:left;margin:0 auto;padding:1px 2px;"
        "border:1px solid #ccc;border-radius:2px;width:100%;height:auto;"
        "display:block;float:none;vertical-align:top;letter-spacing:0;"
        "font-style:normal;font-variant:normal;text-transform:none;"
        "white-space:normal;word-break:normal;overflow-wrap:anywhere;"
        "border-collapse:collapse;table-layout:fixed;border-spacing:0;"
        "margin-top:0;margin-bottom:0;padding-left:0;padding-right:0;"
        "border-top:0;border-bottom:0;text-decoration:none"
    )
    text = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 8
    parts: list[str] = []
    tags = 0
    for _ in range(200):
        parts.append(f'<table style="{declarations}"><tr><td>{text}</td></tr></table>')
        tags += 3
    for _ in range(200):
        parts.append(f'<p style="{declarations}">{text}</p>')
        tags += 1
    for _ in range(50):
        parts.append(f"<p>{text}</p>")
        tags += 1
    filler = max(0, 8000 - tags)
    if filler:
        parts.append("<br>" * filler)
    return "".join(parts)


def test_p1_950kb_reference_is_correct_and_bounded() -> None:
    payload = _build_p1_payload()
    assert len(payload) <= gmail._MAX_HTML_CHARS
    assert payload.count("style=") == 250
    assert payload.count("<img") == 50

    started = time.perf_counter()
    cleaned = _sanitize(payload)
    elapsed = time.perf_counter() - started
    print(f"\nP1: input={len(payload)}B output={len(cleaned)}B {elapsed * 1000:.1f}ms")

    assert cleaned != ""
    assert len(cleaned) <= gmail._HTML_MAX_OUTPUT_CHARS
    assert _sanitize(cleaned) == cleaned
    # Design target: <=400 ms.  A generous CI budget avoids flakiness.
    assert elapsed < 2.0


def test_p2_400_style_worst_case_is_correct_and_bounded() -> None:
    payload = _build_p2_payload()
    assert len(payload) <= gmail._MAX_HTML_CHARS
    assert payload.count("style=") == 400

    started = time.perf_counter()
    cleaned = _sanitize(payload)
    elapsed = time.perf_counter() - started
    print(f"\nP2: input={len(payload)}B output={len(cleaned)}B {elapsed * 1000:.1f}ms")

    assert cleaned != ""
    assert len(cleaned) <= gmail._HTML_MAX_OUTPUT_CHARS
    assert _sanitize(cleaned) == cleaned
    # Design target: <=800 ms.  A generous CI budget avoids flakiness.
    assert elapsed < 3.0


@pytest.mark.parametrize(
    "payload",
    [
        "<img>" * 100_000,
        "<div>" * 20_000,
        '<p style="color:red">x</p>' * 401,
    ],
)
def test_p3_adversarial_payloads_are_rejected_quickly(payload: str) -> None:
    started = time.perf_counter()
    cleaned = _sanitize(payload)
    elapsed = time.perf_counter() - started
    print(f"\nP3: input={len(payload)}B {elapsed * 1000:.1f}ms")

    assert cleaned == ""
    # Design target: <50 ms.  Keep a generous CI budget.
    assert elapsed < 1.0


# ---------------------------------------------------------------------------
# 12. Gmail-shaped corpus (design §9.4)
# ---------------------------------------------------------------------------


def _corpus() -> list[tuple[str, bytes]]:
    marketing = (
        "<style>@import url(https://evil.example/x.css);"
        "p{position:absolute;color:red}</style>"
        '<table width="600" bgcolor="#f4f4f4" align="center" '
        'border="0" cellpadding="0" cellspacing="0">'
        '<tr bgcolor="#ffffff"><td width="50%" valign="top" '
        'style="color:#333;font-family:Arial, Helvetica, sans-serif;'
        "font-size:14px;line-height:1.5;margin:0 auto;padding:8px 12px;"
        'border:1px solid #ccc;border-radius:4px;background:url(https://t/p)">'
        'Ciao &amp; benvenuto &lt;amico&gt; "quoted"'
        '<img src="https://track.example/pixel.gif" alt="pixel">'
        "</td></tr></table>"
        '<p style="color:red !important;position:absolute">'
        "<script>alert(1)</script>fine</p>"
    )
    return [
        ("mixed", _wire_bytes(_email_with_html(marketing, "plain"))),
        (
            "html-only",
            _wire_bytes(_html_only_message(marketing)),
        ),
        ("inline-cid", _wire_bytes(_inline_cid_message())),
        ("entity-heavy", _wire_bytes(_email_with_html(_entity_heavy_html(), "text"))),
    ]


def _html_only_message(html_body: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = "Mario <mario@example.com>"
    message["To"] = "user@example.com"
    message["Subject"] = "HTML only"
    message["Date"] = "Fri, 23 Oct 2026 10:00:00 +0000"
    message.set_content(html_body, subtype="html")
    return message


def _inline_cid_message() -> EmailMessage:
    message = EmailMessage()
    message["From"] = "Mario <mario@example.com>"
    message["To"] = "user@example.com"
    message["Subject"] = "Inline"
    message["Date"] = "Fri, 23 Oct 2026 10:00:00 +0000"
    message.set_content(
        '<p style="color:#123456">Ciao</p><img src="cid:logo@example.com">',
        subtype="html",
    )
    message.add_related(
        b"PNGDATA", maintype="image", subtype="png", cid="<logo@example.com>"
    )
    return message


def _entity_heavy_html() -> str:
    entities = "&amp;" * 1000 + "&lt;tag&gt;" * 1000 + "&quot;" * 1000
    return (
        '<p style="color:#333;font-family:Times New Roman, Arial">' + entities + "</p>"
        '<table width="100%" bgcolor="#fff"><tr><td colspan="2">'
        "àèìòù €</td></tr></table>"
    )


_CORPUS = _corpus()


@pytest.mark.parametrize("name,raw", _CORPUS, ids=[name for name, _ in _CORPUS])
def test_gmail_shaped_corpus_stays_safe_and_idempotent(name: str, raw: bytes) -> None:
    parsed = gmail.parse_message(raw)
    cleaned = parsed["html"]

    assert cleaned != "", name
    _assert_document_safe(cleaned)
    styles = " ".join(_style_attributes(cleaned))
    lowered = styles.lower()
    for forbidden in (
        "url(",
        "image-set(",
        "expression(",
        "var(",
        "env(",
        "attr(",
        "calc(",
        "@import",
        "@font-face",
        "!important",
        "position:",
        "z-index:",
        "inset:",
    ):
        assert forbidden not in lowered, (name, forbidden)
    assert _sanitize(cleaned) == cleaned
