"""Tests for the Gmail configuration getters in ``protocols.config``."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess

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
    config.clear_gmail_secret_cache()
    yield tmp_path
    config.clear_gmail_secret_cache()


def _fake_runner(monkeypatch, stdout="cmd-secret", returncode=0):
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, returncode, stdout)

    monkeypatch.setattr(config.subprocess, "run", run)
    return calls


def _clock(monkeypatch, start: float = 1000.0) -> dict[str, float]:
    now = {"value": start}
    monkeypatch.setattr(config, "_now", lambda: now["value"])
    return now


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


def test_source_switch_file_command_env(monkeypatch, tmp_path):
    secret = tmp_path / "gmail-secret"
    secret.write_text("file-secret")
    monkeypatch.setenv("GMAIL_APP_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("GMAIL_APP_PASSWORD_CMD", "/bin/secret-helper")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "env-secret")
    _fake_runner(monkeypatch)

    assert config.get_gmail_app_password() == "file-secret"

    monkeypatch.delenv("GMAIL_APP_PASSWORD_FILE")
    assert config.get_gmail_app_password() == "cmd-secret"

    monkeypatch.delenv("GMAIL_APP_PASSWORD_CMD")
    assert config.get_gmail_app_password() == "env-secret"


def test_empty_file_falls_back_to_command(monkeypatch, tmp_path):
    secret = tmp_path / "gmail-secret"
    secret.write_text("  \n")
    monkeypatch.setenv("GMAIL_APP_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("GMAIL_APP_PASSWORD_CMD", "/bin/secret-helper")
    _fake_runner(monkeypatch)

    assert config.get_gmail_app_password() == "cmd-secret"


def test_command_output_is_cached(monkeypatch):
    monkeypatch.setenv("GMAIL_APP_PASSWORD_CMD", "/bin/secret-helper")
    calls = _fake_runner(monkeypatch)

    assert config.get_gmail_app_password() == "cmd-secret"
    assert config.get_gmail_app_password() == "cmd-secret"

    assert calls == [["/bin/secret-helper"]]


def test_command_cache_key_is_sha256_hex_string():
    key = config._gmail_secret_cache_key("gpg --decrypt secret.gpg")
    other = config._gmail_secret_cache_key("gpg --decrypt secret2.gpg")

    assert isinstance(key, str)
    assert key == hashlib.sha256(b"gpg --decrypt secret.gpg").hexdigest()
    assert len(key) == 64
    assert key != other


def test_command_cache_uses_fixed_ttl(monkeypatch):
    clock = _clock(monkeypatch)
    monkeypatch.setenv("GMAIL_APP_PASSWORD_CMD", "/bin/secret-helper")
    calls = _fake_runner(monkeypatch)

    assert config.get_gmail_app_password() == "cmd-secret"
    clock["value"] += 299
    assert config.get_gmail_app_password() == "cmd-secret"
    assert len(calls) == 1

    clock["value"] += 2
    assert config.get_gmail_app_password() == "cmd-secret"
    assert len(calls) == 2


def test_clear_command_cache_forces_a_new_decrypt(monkeypatch):
    monkeypatch.setenv("GMAIL_APP_PASSWORD_CMD", "/bin/secret-helper")
    calls = _fake_runner(monkeypatch)

    assert config.get_gmail_app_password() == "cmd-secret"
    config.clear_gmail_secret_cache()
    assert config.get_gmail_app_password() == "cmd-secret"

    assert len(calls) == 2


def test_clear_during_decrypt_discards_the_in_flight_put(monkeypatch):
    monkeypatch.setenv("GMAIL_APP_PASSWORD_CMD", "/bin/secret-helper")
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        if len(calls) == 1:
            config.clear_gmail_secret_cache()
        return subprocess.CompletedProcess(argv, 0, "cmd-secret")

    monkeypatch.setattr(config.subprocess, "run", run)

    assert config.get_gmail_app_password() == "cmd-secret"
    assert config._gmail_secret_cache == {}

    assert config.get_gmail_app_password() == "cmd-secret"
    assert len(calls) == 2


def test_failed_command_is_not_cached_and_falls_back(monkeypatch):
    monkeypatch.setenv("GMAIL_APP_PASSWORD_CMD", "/bin/secret-helper")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "env-secret")
    calls = _fake_runner(monkeypatch, returncode=1)

    assert config.get_gmail_app_password() == "env-secret"
    assert config.get_gmail_app_password() == "env-secret"

    assert len(calls) == 2
    assert config._gmail_secret_cache == {}


def test_empty_command_output_is_not_cached(monkeypatch):
    monkeypatch.setenv("GMAIL_APP_PASSWORD_CMD", "/bin/secret-helper")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "env-secret")
    calls = _fake_runner(monkeypatch, stdout="   \n")

    assert config.get_gmail_app_password() == "env-secret"
    assert config.get_gmail_app_password() == "env-secret"

    assert len(calls) == 2
    assert config._gmail_secret_cache == {}


def test_changing_command_prunes_the_expired_entry(monkeypatch):
    clock = _clock(monkeypatch)
    monkeypatch.setenv("GMAIL_APP_PASSWORD_CMD", "/bin/first-helper")
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, f"secret-{argv[0]}")

    monkeypatch.setattr(config.subprocess, "run", run)

    first = config.get_gmail_app_password()
    first_key = config._gmail_secret_cache_key("/bin/first-helper")
    monkeypatch.setenv("GMAIL_APP_PASSWORD_CMD", "/bin/second-helper")
    second = config.get_gmail_app_password()

    assert first == "secret-/bin/first-helper"
    assert second == "secret-/bin/second-helper"
    assert len(config._gmail_secret_cache) == 2

    clock["value"] += 301
    assert config.get_gmail_app_password() == "secret-/bin/second-helper"
    assert first_key not in config._gmail_secret_cache
    assert len(config._gmail_secret_cache) == 1


def test_file_secret_is_reread_even_with_same_size_and_mtime(monkeypatch, tmp_path):
    secret = tmp_path / "gmail-secret"
    secret.write_text("aaaa")
    stamp = secret.stat()
    os.utime(secret, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    monkeypatch.setenv("GMAIL_APP_PASSWORD_FILE", str(secret))

    assert config.get_gmail_app_password() == "aaaa"

    before = secret.stat()
    secret.write_text("bbbb")
    os.utime(secret, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = secret.stat()
    assert after.st_size == before.st_size
    assert after.st_mtime_ns == before.st_mtime_ns

    assert config.get_gmail_app_password() == "bbbb"


def test_file_source_is_not_cached(monkeypatch, tmp_path):
    secret = tmp_path / "gmail-secret"
    secret.write_text("file-secret")
    monkeypatch.setenv("GMAIL_APP_PASSWORD_FILE", str(secret))
    calls: list[str] = []
    original = config._gmail_secret_from_file

    def spy(raw):
        calls.append(raw)
        return original(raw)

    monkeypatch.setattr(config, "_gmail_secret_from_file", spy)

    assert config.get_gmail_app_password() == "file-secret"
    assert config.get_gmail_app_password() == "file-secret"
    assert len(calls) == 2


def test_malformed_quotes_do_not_leak_the_command(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "env-secret")

    assert config._gmail_secret_from_command("hunter2 'unterminated") == ""
    assert "ValueError" in caplog.text
    assert "Gmail secret command failed" in caplog.text
    assert "hunter2" not in caplog.text


def test_missing_command_does_not_leak_the_command(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "env-secret")

    assert config._gmail_secret_from_command("/definitely/missing/hunter2") == ""
    assert "OSError" in caplog.text
    assert "Gmail secret command failed" in caplog.text
    assert "hunter2" not in caplog.text
    assert "/definitely/missing" not in caplog.text


def test_timeout_does_not_leak_the_command(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)

    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd="hunter2", timeout=10)

    monkeypatch.setattr(config.subprocess, "run", run)

    assert config._gmail_secret_from_command("hunter2 --secret") == ""
    assert "TimeoutExpired" in caplog.text
    assert "TimeoutExpired" in caplog.text
    assert "hunter2" not in caplog.text


def test_nonzero_returncode_does_not_leak_the_command(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    _fake_runner(monkeypatch, returncode=1)

    assert config._gmail_secret_from_command("hunter2") == ""
    assert "returncode" in caplog.text
    assert "Gmail secret command failed" in caplog.text
    assert "hunter2" not in caplog.text


def test_empty_output_does_not_leak_the_command(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    _fake_runner(monkeypatch, stdout=" \n")

    assert config._gmail_secret_from_command("hunter2") == ""
    assert "empty output" in caplog.text
    assert "hunter2" not in caplog.text


def test_empty_command_is_rejected_before_subprocess(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)

    def run(*args, **kwargs):
        raise AssertionError("subprocess must not run for an empty command")

    monkeypatch.setattr(config.subprocess, "run", run)

    assert config._gmail_secret_from_command("   ") == ""
    assert "empty command" in caplog.text
    assert "Gmail secret command failed" in caplog.text


def test_value_error_from_run_is_sanitized(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)

    def run(argv, **kwargs):
        raise ValueError("embedded null byte: hunter2")

    monkeypatch.setattr(config.subprocess, "run", run)

    assert config._gmail_secret_from_command("hunter2") == ""
    assert "ValueError" in caplog.text
    assert "hunter2" not in caplog.text
    assert "embedded null byte" not in caplog.text


def test_unicode_decode_error_is_sanitized(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)

    def run(argv, **kwargs):
        raise UnicodeDecodeError("utf-8", b"\xffhunter2", 0, 1, "invalid start")

    monkeypatch.setattr(config.subprocess, "run", run)

    assert config._gmail_secret_from_command("hunter2") == ""
    assert "UnicodeDecodeError" in caplog.text
    assert "hunter2" not in caplog.text
    assert "invalid start" not in caplog.text


def test_file_oserror_does_not_leak_the_path(tmp_path, caplog):
    caplog.set_level(logging.WARNING)

    assert config._gmail_secret_from_file(str(tmp_path / "hunter2")) == ""
    assert "OSError" in caplog.text
    assert "hunter2" not in caplog.text
    assert str(tmp_path) not in caplog.text


def test_file_unicode_decode_error_does_not_leak_content(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    secret = tmp_path / "gmail-secret"
    secret.write_bytes(b"\xff\xfehunter2")

    assert config._gmail_secret_from_file(str(secret)) == ""
    assert "UnicodeDecodeError" in caplog.text
    assert "hunter2" not in caplog.text
    assert str(tmp_path) not in caplog.text
