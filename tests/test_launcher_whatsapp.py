"""Regression tests for launcher/whatsapp.py (WAHA credentials + lifecycle).

Replaces the WAHA-related portion of tests/test_install_script.py.
"""

from __future__ import annotations

import os
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
    monkeypatch.setattr(
        whatsapp, "RESOURCES_FILE", tmp_path / "docker-compose.resources.yml"
    )
    return tmp_path


class TestComposeArgs:
    def test_default_layers_resources_overlay(self, project: Path):
        args = whatsapp.compose_args()
        assert args == [
            "-f",
            str(project / "docker-compose.yml"),
            "-f",
            str(project / "docker-compose.resources.yml"),
        ]

    def test_no_docker_limits_omits_resources_overlay(self, project: Path):
        args = whatsapp.compose_args(False)
        assert args == ["-f", str(project / "docker-compose.yml")]

    @pytest.mark.parametrize("value", ["0", "false", "off", "NO"])
    def test_waha_docker_limits_0_in_dotenv_omits_overlay(
        self, project: Path, monkeypatch, value
    ):
        monkeypatch.delenv("WAHA_DOCKER_LIMITS", raising=False)
        (project / ".env").write_text(f"WAHA_DOCKER_LIMITS={value}\n")
        assert whatsapp.compose_args() == ["-f", str(project / "docker-compose.yml")]
        assert not whatsapp.limits_enabled()

    def test_waha_docker_limits_1_keeps_overlay(self, project: Path, monkeypatch):
        monkeypatch.setenv("WAHA_DOCKER_LIMITS", "1")
        assert str(project / "docker-compose.resources.yml") in whatsapp.compose_args()

    def test_extra_files_are_layered_last_in_order(self, project: Path, monkeypatch):
        monkeypatch.setenv(
            "WAHA_COMPOSE_FILES", f"local.yml{os.pathsep}/abs/other.yml{os.pathsep}"
        )
        assert whatsapp.compose_args() == [
            "-f",
            str(project / "docker-compose.yml"),
            "-f",
            str(project / "docker-compose.resources.yml"),
            "-f",
            str(project / "local.yml"),
            "-f",
            "/abs/other.yml",
        ]

    def test_extra_files_from_dotenv(self, project: Path, monkeypatch):
        monkeypatch.delenv("WAHA_COMPOSE_FILES", raising=False)
        (project / ".env").write_text('WAHA_COMPOSE_FILES="mine.yml"\n')
        assert whatsapp.compose_args(False)[-2:] == ["-f", str(project / "mine.yml")]

    def test_compose_file_flag_appends_after_dotenv(
        self, project: Path, monkeypatch, tmp_path
    ):
        # add_compose_files writes os.environ directly: register it for restore.
        monkeypatch.setenv("WAHA_COMPOSE_FILES", "")
        (project / ".env").write_text("WAHA_COMPOSE_FILES=mine.yml\n")
        monkeypatch.chdir(tmp_path)
        whatsapp.add_compose_files(["cli.yml"])
        assert whatsapp.compose_args(False)[2:] == [
            "-f",
            str(project / "mine.yml"),
            "-f",
            str((tmp_path / "cli.yml").resolve()),
        ]

    def test_stop_uses_the_extra_files(self, project: Path, monkeypatch):
        monkeypatch.setenv("WAHA_COMPOSE_FILES", "/abs/other.yml")
        calls = []
        monkeypatch.setattr(
            whatsapp.subprocess,
            "run",
            lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
        )
        assert whatsapp.stop() == 0
        assert calls[0][-3:] == ["-f", "/abs/other.yml", "down"]


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

    def test_start_default_layers_resources_overlay(self, project: Path, monkeypatch):
        docker_calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            docker_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="Docker 1.0")

        monkeypatch.setattr(whatsapp, "command_exists", lambda name: True)
        monkeypatch.setattr(whatsapp.subprocess, "run", fake_run)
        monkeypatch.setattr(whatsapp, "check_port", lambda *a, **k: True)
        monkeypatch.setattr(whatsapp, "check_firewall", lambda *a, **k: None)
        monkeypatch.setattr(whatsapp, "http_get", lambda *a, **k: (200, b"{}"))
        monkeypatch.setattr(whatsapp.time, "sleep", lambda s: None)

        whatsapp.setup(should_start=True)

        up_call = next(c for c in docker_calls if "up" in c and "-d" in c)
        assert str(whatsapp.RESOURCES_FILE) in up_call

    def test_start_no_docker_limits_skips_resources_overlay(
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
        monkeypatch.setattr(whatsapp, "http_get", lambda *a, **k: (200, b"{}"))
        monkeypatch.setattr(whatsapp.time, "sleep", lambda s: None)

        whatsapp.setup(should_start=True, docker_limits=False)

        up_call = next(c for c in docker_calls if "up" in c and "-d" in c)
        assert str(whatsapp.RESOURCES_FILE) not in up_call


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

    def test_start_default_layers_resources_overlay(self, project: Path, monkeypatch):
        docker_calls: list[list[str]] = []
        monkeypatch.setattr(whatsapp, "command_exists", lambda name: True)
        monkeypatch.setattr(
            whatsapp.subprocess,
            "run",
            lambda cmd, **k: (
                docker_calls.append(cmd),
                subprocess.CompletedProcess(cmd, 0),
            )[1],
        )

        whatsapp.start(no_wait=True)

        up_call = next(c for c in docker_calls if "up" in c and "-d" in c)
        assert str(whatsapp.RESOURCES_FILE) in up_call

    def test_start_no_docker_limits_skips_resources_overlay(
        self, project: Path, monkeypatch
    ):
        docker_calls: list[list[str]] = []
        monkeypatch.setattr(whatsapp, "command_exists", lambda name: True)
        monkeypatch.setattr(
            whatsapp.subprocess,
            "run",
            lambda cmd, **k: (
                docker_calls.append(cmd),
                subprocess.CompletedProcess(cmd, 0),
            )[1],
        )

        whatsapp.start(no_wait=True, docker_limits=False)

        up_call = next(c for c in docker_calls if "up" in c and "-d" in c)
        assert str(whatsapp.RESOURCES_FILE) not in up_call

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
