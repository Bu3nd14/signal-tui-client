"""Layering rules (design §4.4): who may import whom.

* ``server/`` (the core) never imports a UI or an adapter (``tui``, ``web``,
  Textual, FastAPI); ``server/main.py`` alone hosts the web adapter, lazily.
* ``protocols/`` never imports ``web``.
* ``web/`` (an adapter) talks to the core only through the operation API:
  it imports no ``protocols``, ``tui``, ``transcription`` internals or other
  ``server`` modules than the allowlist below.
* ``tui/`` and the root UI modules never import ``protocols`` or ``web``; the
  composition root ``signal_tui.py`` (runs before the core exists) may import
  ``protocols.config`` and ``web``.
"""

from __future__ import annotations

import ast
import sys
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


def _violations(
    package: str, forbidden: set[str], exempt: dict[str, set[str]] | None = None
) -> list[str]:
    found = []
    for path in sorted((ROOT / package).rglob("*.py")):
        allowed = (exempt or {}).get(path.name, set())
        bad = _imported_roots(path) & (forbidden - allowed)
        if bad:
            found.append(f"{path.relative_to(ROOT)}: {sorted(bad)}")
    return found


def test_server_imports_no_ui_or_web():
    # ``server/main.py`` hosts the web adapter in serve mode and imports
    # ``web`` lazily; it still must never touch Textual or the TUI.
    assert (
        _violations(
            "server",
            {"textual", "fastapi", "starlette", "tui", "web"},
            exempt={"main.py": {"fastapi", "starlette", "web"}},
        )
        == []
    )


def test_serve_path_has_no_textual_or_tui():
    path = ROOT / "server" / "main.py"
    assert _imported_roots(path) & {"textual", "tui"} == set()


def test_protocols_import_no_web():
    assert _violations("protocols", {"web"}) == []


def test_server_modules_import_without_ui_modules_loaded():
    """Importing the core in a fresh interpreter must not pull in the UIs."""
    import subprocess
    import sys

    code = (
        "import sys, server.core, server.bus, server.domain, server.main, "
        "server.lock;"
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


# -- adapters and UIs only see the API ----------------------------------------


def _imported_modules(path: Path) -> set[str]:
    """Every absolute module name *path* imports (function-level included).

    ``from server import x`` also yields ``server.x``.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
            if node.module == "server":
                names.update(f"server.{alias.name}" for alias in node.names)
    return names


#: ``server`` modules ``web/`` may import, each for a reason:
#:
#: * ``server.api``       - ``ChatAPI`` / ``ApiError``: the operation registry itself.
#: * ``server.models_api`` - ``AttachmentIn``, the blob handle the HTTP adapter fills
#:                           from multipart uploads.
#: * ``server.bus``       - ``Event`` / ``EventBus``: the ``/ws`` and ``/ws/v2``
#:                           streams subscribe to the core bus.
#: * ``server.uploads``   - upload policy constants and ``upload_directory`` (pure
#:                           policy: the adapter stores the bytes, the core validates).
#:
#: ``server.ops`` is deliberately absent: ``ChatAPI`` registers the operations
#: itself, an adapter never needs to.
WEB_SERVER_ALLOWLIST = {
    "server.api",
    "server.models_api",
    "server.bus",
    "server.uploads",
}

#: Top-level packages/modules no adapter or UI may import.
WEB_FORBIDDEN_ROOTS = {"protocols", "tui", "transcription", "signal_tui", "textual"}


def _web_violations() -> list[str]:
    found = []
    for path in sorted((ROOT / "web").rglob("*.py")):
        bad = set()
        for name in _imported_modules(path):
            root = name.split(".")[0]
            if (
                root in WEB_FORBIDDEN_ROOTS
                or root == "server"
                and name not in WEB_SERVER_ALLOWLIST
            ):
                bad.add(name)
        if bad:
            found.append(f"{path.relative_to(ROOT)}: {sorted(bad)}")
    return found


def test_web_only_imports_the_api_surface():
    assert _web_violations() == []


def test_web_allowlist_has_no_unused_entries():
    used: set[str] = set()
    for path in (ROOT / "web").rglob("*.py"):
        used |= _imported_modules(path)
    assert WEB_SERVER_ALLOWLIST <= used, sorted(WEB_SERVER_ALLOWLIST - used)


def test_web_rule_detects_violations(tmp_path, monkeypatch):
    """The checker itself: a forbidden import in a fake ``web/`` is reported."""
    fake = tmp_path / "web"
    fake.mkdir()
    (fake / "bad.py").write_text(
        "from server import core\n"
        "import protocols.db\n"
        "def f():\n    from server.messages import x\n    import transcription\n"
    )
    (fake / "good.py").write_text("from server.api import ChatAPI\n")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    found = _web_violations()
    assert len(found) == 1 and "bad.py" in found[0]
    for name in ("server.core", "protocols.db", "server.messages", "transcription"):
        assert name in found[0]


UI_FORBIDDEN_ROOTS = {"protocols", "web"}
UI_ROOT_MODULES = (
    "contact_picker.py",
    "emoji_picker.py",
    "device_link_screen.py",
    "ui_components.py",
)


def test_tui_and_ui_modules_never_import_protocols_or_web():
    found = []
    paths = sorted((ROOT / "tui").rglob("*.py")) + [ROOT / n for n in UI_ROOT_MODULES]
    for path in paths:
        bad = _imported_roots(path) & UI_FORBIDDEN_ROOTS
        if bad:
            found.append(f"{path.relative_to(ROOT)}: {sorted(bad)}")
    assert found == []


def test_composition_root_exception_is_exactly_protocols_config_and_web():
    """``signal_tui.py`` may import ``protocols.config`` and ``web``; nothing
    else of ``protocols``."""
    protocols = {
        name
        for name in _imported_modules(ROOT / "signal_tui.py")
        if name.split(".")[0] == "protocols"
    }
    assert protocols <= {"protocols.config"}, protocols


def test_server_package_does_not_import_tui_or_web_except_main():
    # Same rule as test_server_imports_no_ui_or_web, stated for the adapters
    # introduced in P1.7: nothing but ``main.py`` mentions ``web``.
    offenders = [
        str(p.relative_to(ROOT))
        for p in sorted((ROOT / "server").rglob("*.py"))
        if p.name != "main.py" and _imported_roots(p) & {"web", "tui"}
    ]
    assert offenders == []


def test_web_modules_import_without_protocols_loaded():
    """Importing every web module in a fresh interpreter loads neither the
    protocol backends, the TUI, Textual nor the transcription package."""
    import subprocess

    code = (
        "import sys, web.server, web.api, web.ws, web.auth, web.ops, web.uploads,"
        " web.legacy_events;"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'protocols','tui','textual','transcription','signal_tui'});"
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


def test_ui_modules_import_without_protocols_or_web_loaded():
    import subprocess

    code = (
        "import sys, contact_picker, emoji_picker, device_link_screen, ui_components;"
        "import tui.blobs, tui.chat_view, tui.contacts, tui.download, tui.edit,"
        " tui.events, tui.pickers, tui.polling, tui.send, tui.backend_connect;"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'web','fastapi'});"
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


def test_tui_has_no_test_only_plumbing():
    """Tests drive the TUI through the bus (``tests/event_harness.feed``); the
    production ``tui/`` carries no entry point or skip list made for tests."""
    offenders = [
        f"{path.relative_to(ROOT)}: {name}"
        for path in sorted((ROOT / "tui").rglob("*.py"))
        for name in ("_handle_event", "_compat_payload_ids")
        if name in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_nobody_imports_the_removed_web_bridge():
    """``web.bridge`` (a process-global bus) is gone: ``server/`` and ``tui/``
    never import it, and web code publishes through ``ChatAPI``."""
    assert not (ROOT / "web" / "bridge.py").exists()
    offenders = []
    for package in ("server", "tui", "web"):
        for path in sorted((ROOT / package).rglob("*.py")):
            modules = _imported_modules(path)
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.module == "web":
                    modules.update(f"web.{alias.name}" for alias in node.names)
            if any(m == "web.bridge" or m.startswith("web.bridge.") for m in modules):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
