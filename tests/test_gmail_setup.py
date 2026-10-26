"""Tests for the guided Gmail setup script helpers (no network)."""

from __future__ import annotations

import importlib.util
import stat
import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "gmail_setup", PROJECT_DIR / "scripts" / "gmail_setup.py"
)
assert _SPEC and _SPEC.loader
gmail_setup = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gmail_setup)


def test_normalize_password_strips_display_spaces():
    assert gmail_setup.normalize_password("abcd efgh ijkl mnop\n") == "abcdefghijklmnop"


def test_write_secret_uses_private_permissions(tmp_path):
    secret = tmp_path / "config" / "gmail-app-password"

    gmail_setup.write_secret(secret, "secret-value")

    assert secret.read_text() == "secret-value\n"
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert stat.S_IMODE(secret.parent.stat().st_mode) == 0o700


def test_update_env_replaces_gmail_lines_and_keeps_the_rest(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# existing\n"
        "WAHA_API_KEY=keep-me\n"
        "GMAIL_ADDRESS=old@example.com\n"
        "GMAIL_APP_PASSWORD=old-secret\n"
        "GMAIL_FOLDER=Archive\n"
    )

    gmail_setup.update_env(env, "new@example.com", tmp_path / "gmail-secret")

    content = env.read_text()
    assert "WAHA_API_KEY=keep-me" in content
    assert "GMAIL_FOLDER=Archive" in content
    assert "GMAIL_ADDRESS=new@example.com" in content
    assert f"GMAIL_APP_PASSWORD_FILE={tmp_path / 'gmail-secret'}" in content
    assert "GMAIL_APP_PASSWORD=old-secret" not in content
    assert "old@example.com" not in content
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_update_env_creates_missing_file(tmp_path):
    env = tmp_path / ".env"

    gmail_setup.update_env(env, "new@example.com", tmp_path / "gmail-secret")

    assert "GMAIL_ADDRESS=new@example.com" in env.read_text()
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_existing_configuration_detects_secret_lines(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    monkeypatch.setattr(gmail_setup, "ENV_FILE", env)
    monkeypatch.setattr(gmail_setup.config, "get_gmail_address", lambda: "")

    assert gmail_setup._existing_configuration() is False
    env.write_text("GMAIL_APP_PASSWORD_FILE=/tmp/secret\n")
    assert gmail_setup._existing_configuration() is True


def test_main_saves_secret_and_updates_env(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    secret = tmp_path / "gmail-secret"
    monkeypatch.setattr(gmail_setup, "ENV_FILE", env)
    monkeypatch.setattr(gmail_setup, "SECRET_FILE", secret)
    monkeypatch.setattr(gmail_setup.config, "get_gmail_address", lambda: "")
    monkeypatch.setattr(
        gmail_setup.getpass, "getpass", lambda prompt: "abcd efgh ijkl mnop"
    )
    monkeypatch.setattr(gmail_setup, "test_credentials", lambda address, password: True)

    assert gmail_setup.main(["--address", "user@example.com", "--force"]) == 0

    assert secret.read_text() == "abcdefghijklmnop\n"
    content = env.read_text()
    assert "GMAIL_ADDRESS=user@example.com" in content
    assert f"GMAIL_APP_PASSWORD_FILE={secret}" in content


def test_ensure_venv_is_noop_when_dependencies_available(monkeypatch):
    calls = []
    monkeypatch.setattr(gmail_setup, "_pil_available", lambda: True)
    monkeypatch.setattr(gmail_setup.os, "execv", lambda *args: calls.append(args))

    gmail_setup._ensure_venv()

    assert calls == []


def test_ensure_venv_reexecutes_with_venv_python(monkeypatch, tmp_path):
    calls = []
    fake_python = tmp_path / ".venv" / "bin" / "python"

    def fake_execv(path, argv):
        calls.append((path, argv))
        raise SystemExit(0)

    monkeypatch.setattr(gmail_setup, "_pil_available", lambda: False)
    monkeypatch.setattr(gmail_setup, "_venv_python", lambda: fake_python)
    monkeypatch.setattr(gmail_setup.os, "execv", fake_execv)

    with pytest.raises(SystemExit) as excinfo:
        gmail_setup._ensure_venv()

    assert excinfo.value.code == 0
    assert calls == [(str(fake_python), [str(fake_python), *sys.argv])]


def test_ensure_venv_exits_when_no_venv_python(monkeypatch, capsys):
    monkeypatch.setattr(gmail_setup, "_pil_available", lambda: False)
    monkeypatch.setattr(gmail_setup, "_venv_python", lambda: None)

    with pytest.raises(SystemExit) as excinfo:
        gmail_setup._ensure_venv()

    assert excinfo.value.code == 1
    assert ".venv/bin/python" in capsys.readouterr().err


def test_venv_python_missing_candidate_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(gmail_setup, "PROJECT_DIR", tmp_path)
    assert not (tmp_path / ".venv" / "bin" / "python").exists()

    assert gmail_setup._venv_python() is None


def test_encryption_hint_mentions_tpm_only_on_linux(monkeypatch):
    monkeypatch.setattr(gmail_setup, "_platform", lambda: "linux")

    output = "\n".join(gmail_setup.encryption_hint())

    assert "gmail_tpm_init.sh" in output
    assert "non disponibili" not in output
    assert "gmail_gpg_init.sh" in output
    assert "README.md#gmail-optional" in output


def test_encryption_hint_skips_tpm_elsewhere(monkeypatch):
    monkeypatch.setattr(gmail_setup, "_platform", lambda: "darwin")

    output = "\n".join(gmail_setup.encryption_hint())

    assert "gmail_tpm_init.sh" not in output
    assert "TPM2/systemd-creds non disponibili" in output
    assert "gmail_gpg_init.sh" in output
    assert "README.md#gmail-optional" in output
