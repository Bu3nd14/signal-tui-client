"""Regression tests for launcher/whatsapp.py (WAHA credentials + lifecycle).

Replaces the WAHA-related portion of tests/test_install_script.py.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from launcher import whatsapp


@pytest.fixture
def project(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(whatsapp, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(whatsapp, "COMPOSE_FILE", tmp_path / "docker-compose.yml")
    return tmp_path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class TestWahaEnv:
    def test_creates_secure_waha_credentials(self, project: Path):
        shutil.copy(PROJECT_ROOT / ".env.example", project / ".env.example")

        whatsapp.ensure_waha_env()

        env_file = project / ".env"
        values = dict(
            line.split("=", 1)
            for line in env_file.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        )
        assert values["WAHA_IMAGE"] in (
            "devlikeapro/waha:arm",
            "devlikeapro/waha:latest",
        )
        assert len(values["WAHA_API_KEY"]) >= 32
        assert values["WAHA_DASHBOARD_USERNAME"] == "admin"
        assert len(values["WAHA_DASHBOARD_PASSWORD"]) >= 32
        assert values["WHATSAPP_SWAGGER_USERNAME"] == "admin"
        assert len(values["WHATSAPP_SWAGGER_PASSWORD"]) >= 32
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600

    def test_preserves_existing_values_and_is_idempotent(self, project: Path):
        env_file = project / ".env"
        env_file.write_text(
            "TELEGRAM_API_ID=12345\nWAHA_API_KEY=existing-key\nWAHA_DASHBOARD_PASSWORD=\n",
            encoding="utf-8",
        )

        whatsapp.ensure_waha_env()
        first_content = env_file.read_text(encoding="utf-8")
        whatsapp.ensure_waha_env()

        assert env_file.read_text(encoding="utf-8") == first_content
        assert "TELEGRAM_API_ID=12345" in first_content
        assert "WAHA_API_KEY=existing-key" in first_content

    @pytest.mark.parametrize(
        ("architecture", "expected"),
        [
            ("arm64", "devlikeapro/waha:arm"),
            ("aarch64", "devlikeapro/waha:arm"),
            ("x86_64", "devlikeapro/waha:latest"),
            ("amd64", "devlikeapro/waha:latest"),
        ],
    )
    def test_recalculates_image_for_current_architecture(
        self, project: Path, monkeypatch, architecture: str, expected: str
    ):
        env_file = project / ".env"
        env_file.write_text(
            "WAHA_IMAGE=devlikeapro/waha:opposite-architecture\n", encoding="utf-8"
        )
        monkeypatch.setattr(whatsapp.platform, "machine", lambda: architecture)

        whatsapp.ensure_waha_env()

        assert f"WAHA_IMAGE={expected}\n" in env_file.read_text(encoding="utf-8")


class TestReadWahaApiKey:
    def test_env_var_wins(self, project: Path, monkeypatch):
        monkeypatch.setenv("WAHA_API_KEY", "from-env")
        assert whatsapp.read_waha_api_key() == "from-env"

    def test_falls_back_to_dotenv_file(self, project: Path, monkeypatch):
        monkeypatch.delenv("WAHA_API_KEY", raising=False)
        (project / ".env").write_text("WAHA_API_KEY=from-dotenv\n", encoding="utf-8")
        assert whatsapp.read_waha_api_key() == "from-dotenv"

    def test_strips_quotes(self, project: Path, monkeypatch):
        monkeypatch.delenv("WAHA_API_KEY", raising=False)
        (project / ".env").write_text('WAHA_API_KEY="quoted-key"\n', encoding="utf-8")
        assert whatsapp.read_waha_api_key() == "quoted-key"

    def test_missing_returns_empty(self, project: Path, monkeypatch):
        monkeypatch.delenv("WAHA_API_KEY", raising=False)
        assert whatsapp.read_waha_api_key() == ""


class TestSetup:
    def test_check_only_does_not_create_env(self, project: Path, monkeypatch):
        monkeypatch.setattr(whatsapp, "command_exists", lambda name: True)
        monkeypatch.setattr(
            whatsapp.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(
                a[0] if a else [], 0, stdout="Docker 1.0"
            ),
        )
        monkeypatch.setattr(whatsapp, "check_port", lambda *a, **k: True)
        monkeypatch.setattr(whatsapp, "check_firewall", lambda *a, **k: None)

        result = whatsapp.setup(should_start=False)

        assert result is True
        assert not (project / ".env").exists()

    def test_start_probes_sessions_endpoint_with_api_key(
        self, project: Path, monkeypatch
    ):
        docker_calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            docker_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="Docker 1.0")

        monkeypatch.setattr(whatsapp, "command_exists", lambda name: True)
        monkeypatch.setattr(whatsapp.subprocess, "run", fake_run)
        monkeypatch.setattr(whatsapp, "check_port", lambda *a, **k: True)
        monkeypatch.setattr(whatsapp, "check_firewall", lambda *a, **k: None)
        monkeypatch.setattr(whatsapp, "read_waha_api_key", lambda: "the-key")

        seen_requests: list[tuple[str, dict]] = []

        def fake_http_get(url, *, headers=None, timeout=5):
            seen_requests.append((url, headers or {}))
            return 200, b"{}"

        monkeypatch.setattr(whatsapp, "http_get", fake_http_get)
        monkeypatch.setattr(whatsapp.time, "sleep", lambda s: None)

        result = whatsapp.setup(should_start=True)

        assert result is True
        assert any("up" in c and "-d" in c for c in docker_calls)
        assert seen_requests, "expected a readiness probe request"
        url, headers = seen_requests[0]
        assert "/api/sessions" in url
        assert headers.get("X-Api-Key") == "the-key"

    def test_docker_missing_fails_cleanly(self, project: Path, monkeypatch):
        monkeypatch.setattr(whatsapp, "command_exists", lambda name: False)
        assert whatsapp.setup(should_start=False) is False


class TestStandaloneStartStop:
    def test_start_no_wait_skips_readiness_probe(self, project: Path, monkeypatch):
        monkeypatch.setattr(whatsapp, "command_exists", lambda name: True)
        monkeypatch.setattr(
            whatsapp.subprocess,
            "run",
            lambda cmd, **k: subprocess.CompletedProcess(cmd, 0),
        )
        called = {"http_get": False}

        def fake_http_get(*a, **k):
            called["http_get"] = True
            return 200, b"{}"

        monkeypatch.setattr(whatsapp, "http_get", fake_http_get)

        assert whatsapp.start(no_wait=True) == 0
        assert called["http_get"] is False

    def test_start_waits_and_reports_timeout(self, project: Path, monkeypatch):
        monkeypatch.setattr(whatsapp, "command_exists", lambda name: True)
        monkeypatch.setattr(
            whatsapp.subprocess,
            "run",
            lambda cmd, **k: subprocess.CompletedProcess(cmd, 0),
        )
        monkeypatch.setattr(whatsapp, "http_get", lambda *a, **k: None)
        monkeypatch.setattr(whatsapp.time, "sleep", lambda s: None)

        assert whatsapp.start(no_wait=False) == 1

    def test_stop_runs_compose_down(self, project: Path, monkeypatch):
        calls: list[list[str]] = []

        def fake_run(cmd, **k):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(whatsapp.subprocess, "run", fake_run)
        assert whatsapp.stop() == 0
        assert any("down" in c for c in calls)
