"""Tests for scripts/gmail_warm_cache.py (no real gpg invocation)."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "gmail_warm_cache", PROJECT_DIR / "scripts" / "gmail_warm_cache.py"
)
assert _SPEC and _SPEC.loader
gmail_warm_cache = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gmail_warm_cache)


def _patch_sources(monkeypatch, address="user@example.com", file_path="", cmd="gpg"):
    monkeypatch.setattr(gmail_warm_cache.config, "get_gmail_address", lambda: address)
    monkeypatch.setattr(
        gmail_warm_cache.config, "get_gmail_app_password_file", lambda: file_path
    )
    monkeypatch.setattr(
        gmail_warm_cache.config, "get_gmail_app_password_cmd", lambda: cmd
    )


def _no_run(monkeypatch):
    def run(*args, **kwargs):
        raise AssertionError("subprocess.run must not be called")

    monkeypatch.setattr(gmail_warm_cache.subprocess, "run", run)


class _Stdin:
    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _patch_stdin(monkeypatch, tty: bool) -> None:
    monkeypatch.setattr(gmail_warm_cache.sys, "stdin", _Stdin(tty))


def test_skips_when_gmail_is_not_configured(monkeypatch):
    _patch_sources(monkeypatch, address="")
    _no_run(monkeypatch)

    assert gmail_warm_cache.main() == 0


def test_skips_when_secret_comes_from_a_file(monkeypatch):
    _patch_sources(monkeypatch, file_path="/tmp/gmail-secret")
    _no_run(monkeypatch)

    assert gmail_warm_cache.main() == 0


def test_skips_when_secret_command_is_empty(monkeypatch):
    _patch_sources(monkeypatch, cmd="")
    _no_run(monkeypatch)

    assert gmail_warm_cache.main() == 0


def test_tty_success_is_silent_and_has_no_timeout(monkeypatch, capsys):
    calls: list[dict] = []
    _patch_sources(monkeypatch, cmd="gpg --quiet --decrypt secret.gpg")
    _patch_stdin(monkeypatch, True)
    monkeypatch.setattr(gmail_warm_cache.os, "ttyname", lambda fd: "/dev/pts/0")
    monkeypatch.delenv("GPG_TTY", raising=False)

    def run(argv, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(argv, 0, "secret-value\n")

    monkeypatch.setattr(gmail_warm_cache.subprocess, "run", run)

    assert gmail_warm_cache.main() == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert calls == [
        {"capture_output": True, "text": True, "check": False},
    ]
    assert gmail_warm_cache.os.environ["GPG_TTY"] == "/dev/pts/0"


def test_tty_empty_output_warns_with_manual_command(monkeypatch, capsys):
    _patch_sources(monkeypatch, cmd="gpg --quiet --decrypt secret.gpg")
    _patch_stdin(monkeypatch, True)
    monkeypatch.setattr(gmail_warm_cache.os, "ttyname", lambda fd: "/dev/pts/0")
    monkeypatch.setattr(
        gmail_warm_cache.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "  \n"),
    )

    assert gmail_warm_cache.main() == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "gmail_gpg_init.sh" in captured.err
    assert "secret-value" not in captured.err


def test_non_tty_timeout_warns_without_crashing(monkeypatch, capsys):
    _patch_sources(monkeypatch, cmd="gpg --quiet --decrypt secret.gpg")
    _patch_stdin(monkeypatch, False)
    monkeypatch.setattr(gmail_warm_cache.os, "ttyname", lambda fd: "not-a-tty")

    def run(argv, **kwargs):
        assert kwargs["timeout"] == 5
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(gmail_warm_cache.subprocess, "run", run)

    assert gmail_warm_cache.main() == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "gmail_gpg_init.sh" in captured.err
