"""Tests for the guided Gmail setup script helpers (no network)."""

from __future__ import annotations

import importlib.util
import stat
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "gmail_setup", PROJECT_DIR / "scripts" / "gmail_setup.py"
)
assert _SPEC and _SPEC.loader
gmail_setup = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gmail_setup)


def test_normalize_password_strips_display_spaces():
    assert gmail_setup.normalize_password("bcmi ieth occw hvye\n") == "bcmiiethoccwhvye"


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
        gmail_setup.getpass, "getpass", lambda prompt: "bcmi ieth occw hvye"
    )
    monkeypatch.setattr(gmail_setup, "test_credentials", lambda address, password: True)

    assert gmail_setup.main(["--address", "user@example.com", "--force"]) == 0

    assert secret.read_text() == "bcmiiethoccwhvye\n"
    content = env.read_text()
    assert "GMAIL_ADDRESS=user@example.com" in content
    assert f"GMAIL_APP_PASSWORD_FILE={secret}" in content
