"""The TUI reaches backends only through the server API (operations/events).

The import rule itself lives in ``test_server_import_rules.py``; this adds the
attribute-level guard: no backend attributes (``manager``, ``*_backend``) on the
app, and no ``protocols`` imports in ``tui/`` or the root UI modules.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = sorted((ROOT / "tui").glob("*.py")) + [
    ROOT / name
    for name in (
        "contact_picker.py",
        "emoji_picker.py",
        "device_link_screen.py",
        "ui_components.py",
    )
]

BACKEND_ATTRS = {"manager", "signal_backend", "whatsapp_backend", "telegram_backend"}
#: (file, attribute access) still allowed, with the reason.
ALLOWED: set[tuple[str, str]] = set()


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def test_no_protocols_imports():
    offenders = []
    for path in FILES:
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            offenders += [
                f"{path.name}:{node.lineno} {n}"
                for n in names
                if n == "protocols" or n.startswith("protocols.")
            ]
    assert offenders == []


def test_no_backend_attributes():
    offenders = []
    for path in FILES:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr in BACKEND_ATTRS:
                dotted = _dotted(node)
                if (path.name, dotted.removeprefix("self.")) not in ALLOWED:
                    offenders.append(f"{path.name}:{node.lineno} {dotted}")
    assert offenders == []
