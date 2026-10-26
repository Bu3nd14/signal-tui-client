"""The SPA must never request a root-absolute URL.

The web UI can be served under a reverse-proxy path prefix that is stripped
before forwarding (e.g. ``https://dash.example.com/proxy/wa/``).  Every URL the
page uses must therefore be relative to the page (always the index, ``./``) so
it resolves under whatever prefix the page was loaded from.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "web" / "static"

# src="/..." / href="/..." (not protocol-relative "//host").
_ATTR_RE = re.compile(r"""\b(?:src|href)\s*=\s*["'`]/(?!/)""", re.IGNORECASE)
# fetch("/...") / apiFetch(`/...`) / request('/...') and the like.
_FETCH_RE = re.compile(r"""\b(?:fetch|apiFetch|request)\(\s*["'`]/(?!/)""")
# Any string literal starting with a server route.
_LITERAL_RE = re.compile(r"""["'`]/(?:api|ws|health|gmail-image)\b""")
# JSON values such as "start_url": "/" or "src": "/icon.svg".
_JSON_RE = re.compile(r""":\s*"/(?!/)""")


def _static_files() -> list[Path]:
    files = sorted(
        path
        for pattern in ("*.html", "*.js", "*.json")
        for path in STATIC.glob(pattern)
    )
    assert files, f"no static files found in {STATIC}"
    return files


def _strip_line_comment(line: str, suffix: str) -> str:
    # Comments may legitimately mention routes ("/ws", "/api/...").
    if suffix == ".js" and line.lstrip().startswith(("//", "*", "/*")):
        return ""
    return line


@pytest.mark.parametrize("path", _static_files(), ids=lambda p: p.name)
def test_static_file_has_no_root_absolute_urls(path: Path) -> None:
    patterns = [_ATTR_RE, _FETCH_RE, _LITERAL_RE]
    if path.suffix == ".json":
        patterns.append(_JSON_RE)
    offenders = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = _strip_line_comment(raw, path.suffix)
        if any(pattern.search(line) for pattern in patterns):
            offenders.append(f"{path.name}:{number}: {raw.strip()}")
    assert not offenders, "root-absolute URLs break under a path prefix:\n" + "\n".join(
        offenders
    )


def test_scanner_catches_root_absolute_urls() -> None:
    samples = [
        '<link rel="stylesheet" href="/style.css?v=1">',
        '<script src="/app.js" defer></script>',
        'await apiFetch("/api/status");',
        "fetch(`/api/media/${id}`)",
        'const url = "/gmail-image?token=x";',
        'new WebSocket("/ws")',
        "fetch('/health')",
    ]
    for sample in samples:
        assert any(
            pattern.search(sample) for pattern in (_ATTR_RE, _FETCH_RE, _LITERAL_RE)
        ), sample
    assert _JSON_RE.search('"start_url": "/"')
    for ok in (
        'href="style.css?v=1"',
        'apiFetch("api/status")',
        'src="https://example.com/x.png"',
        'href="//cdn.example.com/x"',
    ):
        assert not any(
            pattern.search(ok) for pattern in (_ATTR_RE, _FETCH_RE, _LITERAL_RE)
        ), ok


def test_websocket_url_is_built_from_the_page_url() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "location.host" not in app
    assert 'new URL("ws", location.href)' in app
