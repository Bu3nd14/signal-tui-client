"""
Regression tests for protocols/config.py.

Covers the env-var / config.json / .env resolution order, the invalid-input
fallbacks (default ports / empty dicts), and the socket reachability check.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from protocols import config


class TestLoadDotenv:
    def test_load_dotenv_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        assert config._load_dotenv() == {}

    def test_load_dotenv_oserror(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        (tmp_path / ".env").mkdir()  # directory -> open() raises OSError
        assert config._load_dotenv() == {}


class TestLoadConfig:
    def test_load_config_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        assert config._load_config() == {}

    def test_load_config_invalid_json(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        (tmp_path / "config.json").write_text("{not valid json")
        assert config._load_config() == {}

    def test_load_config_oserror(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        (tmp_path / "config.json").mkdir()  # directory -> open() raises OSError
        assert config._load_config() == {}


class TestGetPrecedence:
    def test_get_env_precedence(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.setenv("WHATSAPP_SESSION_NAME", "custom")
        assert config.get_whatsapp_session_name() == "custom"


class TestWhatsAppApiPort:
    def test_api_port_invalid(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_API_PORT", "abc")
        assert config.get_whatsapp_api_port() == 3005


class TestLocalWahaReachable:
    def test_unreachable(self, monkeypatch):
        def _raise_oserror(*args, **kwargs):
            raise OSError("connection refused")

        monkeypatch.setattr(config.socket, "create_connection", _raise_oserror)
        assert config._local_waha_reachable() is False


class TestWebhookPort:
    def test_valid(self, monkeypatch):
        monkeypatch.setenv("CLIENT_WEBHOOK_PORT", "9999")
        assert config.get_whatsapp_webhook_port() == 9999

    def test_invalid(self, monkeypatch):
        monkeypatch.setenv("CLIENT_WEBHOOK_PORT", "xyz")
        assert config.get_whatsapp_webhook_port() == 8088

    def test_default(self, monkeypatch):
        monkeypatch.delenv("CLIENT_WEBHOOK_PORT", raising=False)
        assert config.get_whatsapp_webhook_port() == 8088


class TestWebhookUrl:
    def test_explicit(self, monkeypatch):
        monkeypatch.setenv("WAHA_WEBHOOK_URL", "http://example.com/")
        assert config.get_whatsapp_webhook_url() == "http://example.com"

    def test_default(self, monkeypatch):
        monkeypatch.delenv("WAHA_WEBHOOK_URL", raising=False)
        monkeypatch.setenv("CLIENT_WEBHOOK_PORT", "8088")
        assert (
            config.get_whatsapp_webhook_url()
            == "http://host.docker.internal:8088/webhook"
        )


class TestTelegramApiId:
    def test_env_invalid(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_API_ID", "abc")
        assert config.get_telegram_api_id() == 0

    def test_config_int(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
        (tmp_path / "config.json").write_text(json.dumps({"telegram_api_id": 12345}))
        assert config.get_telegram_api_id() == 12345

    def test_config_invalid(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
        (tmp_path / "config.json").write_text(
            json.dumps({"telegram_api_id": "not-a-number"})
        )
        assert config.get_telegram_api_id() == 0

    def test_dotenv_invalid(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
        (tmp_path / ".env").write_text("TELEGRAM_API_ID=xyz\n")
        assert config.get_telegram_api_id() == 0


class TestTelegramApiHash:
    def test_config_str(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
        (tmp_path / "config.json").write_text(
            json.dumps({"telegram_api_hash": "abc123"})
        )
        assert config.get_telegram_api_hash() == "abc123"
