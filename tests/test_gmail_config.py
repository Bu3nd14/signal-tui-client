"""Tests for the Gmail configuration getters in ``protocols.config``."""

from __future__ import annotations

import json

import pytest

from protocols import config

_ENV_VARS = (
    "GMAIL_ADDRESS",
    "GMAIL_APP_PASSWORD",
    "GMAIL_APP_PASSWORD_FILE",
    "GMAIL_APP_PASSWORD_CMD",
    "GMAIL_IMAP_HOST",
    "GMAIL_IMAP_PORT",
    "GMAIL_SMTP_HOST",
    "GMAIL_SMTP_PORT",
    "GMAIL_FOLDER",
    "GMAIL_TIMEOUT_S",
)


@pytest.fixture(autouse=True)
def isolate_config(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_defaults():
    assert config.get_gmail_address() == ""
    assert config.get_gmail_app_password() == ""
    assert config.get_gmail_imap_host() == "imap.gmail.com"
    assert config.get_gmail_imap_port() == 993
    assert config.get_gmail_smtp_host() == "smtp.gmail.com"
    assert config.get_gmail_smtp_port() == 587
    assert config.get_gmail_folder() == "INBOX"
    assert config.get_gmail_timeout_s() == 30
    assert config.gmail_enabled() is False


def test_env_precedence(monkeypatch, tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "gmail_address": "config@example.com",
                "gmail_app_password": "config-secret",
                "gmail_folder": "Archive",
            }
        )
    )
    (tmp_path / ".env").write_text(
        "GMAIL_ADDRESS=dotenv@example.com\nGMAIL_APP_PASSWORD=dotenv-secret\n"
    )
    monkeypatch.setenv("GMAIL_ADDRESS", "env@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "env-secret")
    monkeypatch.setenv("GMAIL_FOLDER", "Sent")

    assert config.get_gmail_address() == "env@example.com"
    assert config.get_gmail_app_password() == "env-secret"
    assert config.get_gmail_folder() == "Sent"
    assert config.gmail_enabled() is True


def test_app_password_file_preferred(monkeypatch, tmp_path):
    secret = tmp_path / "gmail-secret"
    secret.write_text("file-secret\n")
    secret.chmod(0o600)
    monkeypatch.setenv("GMAIL_APP_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "env-secret")

    assert config.get_gmail_app_password() == "file-secret"


def test_app_password_file_relative_to_project(monkeypatch, tmp_path):
    (tmp_path / "secrets").mkdir()
    secret = tmp_path / "secrets" / "gmail"
    secret.write_text("relative-secret")
    monkeypatch.setenv("GMAIL_APP_PASSWORD_FILE", "secrets/gmail")

    assert config.get_gmail_app_password() == "relative-secret"


def test_app_password_file_missing_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAIL_APP_PASSWORD_FILE", str(tmp_path / "missing"))
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "env-secret")

    assert config.get_gmail_app_password() == "env-secret"


def test_app_password_command(monkeypatch):
    monkeypatch.setenv("GMAIL_APP_PASSWORD_CMD", "/bin/echo cmd-secret")

    assert config.get_gmail_app_password() == "cmd-secret"


def test_app_password_command_failure_falls_back(monkeypatch):
    monkeypatch.setenv("GMAIL_APP_PASSWORD_CMD", "/bin/false")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "env-secret")

    assert config.get_gmail_app_password() == "env-secret"


def test_app_password_file_precedes_command(monkeypatch, tmp_path):
    secret = tmp_path / "gmail-secret"
    secret.write_text("file-secret")
    monkeypatch.setenv("GMAIL_APP_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("GMAIL_APP_PASSWORD_CMD", "/bin/echo cmd-secret")

    assert config.get_gmail_app_password() == "file-secret"


def test_config_json_fallback(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "gmail_address": "config@example.com",
                "gmail_app_password": "config-secret",
                "gmail_imap_host": "imap.example.com",
                "gmail_imap_port": 1993,
                "gmail_smtp_host": "smtp.example.com",
                "gmail_smtp_port": 1587,
                "gmail_folder": "Archive",
                "gmail_timeout_s": 10,
            }
        )
    )

    assert config.get_gmail_address() == "config@example.com"
    assert config.get_gmail_imap_host() == "imap.example.com"
    assert config.get_gmail_imap_port() == 1993
    assert config.get_gmail_smtp_host() == "smtp.example.com"
    assert config.get_gmail_smtp_port() == 1587
    assert config.get_gmail_folder() == "Archive"
    assert config.get_gmail_timeout_s() == 10


def test_dotenv_fallback(tmp_path):
    (tmp_path / ".env").write_text(
        "GMAIL_ADDRESS=dotenv@example.com\n"
        "GMAIL_APP_PASSWORD=dotenv-secret\n"
        "GMAIL_FOLDER=Archive\n"
    )

    assert config.get_gmail_address() == "dotenv@example.com"
    assert config.get_gmail_app_password() == "dotenv-secret"
    assert config.get_gmail_folder() == "Archive"
    assert config.gmail_enabled() is True


def test_blank_values_fall_through(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAIL_ADDRESS", "   ")
    (tmp_path / ".env").write_text("GMAIL_ADDRESS=dotenv@example.com\n")
    assert config.get_gmail_address() == "dotenv@example.com"


def test_enabled_requires_both_credentials(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "user@example.com")
    assert config.gmail_enabled() is False
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "")
    assert config.gmail_enabled() is False
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "secret")
    assert config.gmail_enabled() is True


def test_invalid_ports_and_timeout_fall_back(monkeypatch):
    monkeypatch.setenv("GMAIL_IMAP_PORT", "not-a-port")
    monkeypatch.setenv("GMAIL_SMTP_PORT", "99999")
    monkeypatch.setenv("GMAIL_TIMEOUT_S", "-5")
    assert config.get_gmail_imap_port() == 993
    assert config.get_gmail_smtp_port() == 587
    assert config.get_gmail_timeout_s() == 30


def test_folder_defaults_when_blank(monkeypatch):
    monkeypatch.setenv("GMAIL_FOLDER", "   ")
    assert config.get_gmail_folder() == "INBOX"
