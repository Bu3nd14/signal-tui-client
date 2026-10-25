"""Layering rules: the core is UI-free and protocols never reach into web."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _violations(package: str, forbidden: set[str]) -> list[str]:
    found = []
    for path in sorted((ROOT / package).rglob("*.py")):
        bad = _imported_roots(path) & forbidden
        if bad:
            found.append(f"{path.relative_to(ROOT)}: {sorted(bad)}")
    return found


def test_server_imports_no_ui_or_web():
    assert (
        _violations("server", {"textual", "fastapi", "starlette", "tui", "web"}) == []
    )


def test_protocols_import_no_web():
    assert _violations("protocols", {"web"}) == []


def test_server_modules_import_without_ui_modules_loaded():
    """Importing the core in a fresh interpreter must not pull in the UIs."""
    import subprocess
    import sys

    code = (
        "import sys, server.core, server.bus, server.domain;"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'textual','fastapi','tui','web'});"
        "print(bad); sys.exit(1 if bad else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
