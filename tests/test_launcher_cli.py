"""CLI-level regression tests for launcher.py (argparse wiring).

Replaces the argument-parsing portion of the old tests/test_install_script.py
(bash --help/unknown-flag/missing-arg tests) — the actual command logic is
tested in tests/test_launcher_install.py, tests/test_launcher_whatsapp.py and
tests/test_launcher_aliases.py.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = PROJECT_ROOT / "launcher.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: PLW1510
        [sys.executable, str(LAUNCHER), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_top_level_help():
    result = _run("--help")
    assert result.returncode == 0
    assert "install" in result.stdout
    assert "whatsapp" in result.stdout
    assert "handover" in result.stdout


def test_install_help_lists_all_flags():
    result = _run("install", "--help")
    assert result.returncode == 0
    for flag in (
        "--no-venv",
        "--version",
        "--skip-signal-cli",
        "--update",
        "--whatsapp",
        "--check-whatsapp",
        "--no-web",
        "--aliases",
    ):
        assert flag in result.stdout


def test_unknown_top_level_command():
    result = _run("bogus-command")
    assert result.returncode != 0
    assert "invalid choice" in result.stderr


def test_install_missing_version_arg():
    result = _run("install", "--version")
    assert result.returncode != 0
    assert "--version" in result.stderr


def test_whatsapp_requires_subcommand():
    result = _run("whatsapp")
    assert result.returncode != 0


def test_server_requires_subcommand():
    result = _run("server")
    assert result.returncode != 0


def test_handover_requires_subcommand():
    result = _run("handover")
    assert result.returncode != 0


def test_profile_requires_subcommand():
    result = _run("profile")
    assert result.returncode != 0
