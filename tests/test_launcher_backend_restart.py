"""Coverage for launcher/backend.py's WAHA restart orchestration."""

from __future__ import annotations

import subprocess

import pytest

from launcher import backend, common
from launcher import whatsapp as whatsapp_mod


def _cp(cmd, returncode=0, stdout=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout)


class TestRestartWaha:
    def test_dies_without_docker(self, monkeypatch):
        monkeypatch.setattr(backend, "command_exists", lambda name: False)
        with pytest.raises(SystemExit):
            backend._restart_waha(True, 5)

    def test_dies_without_compose_plugin(self, monkeypatch):
        monkeypatch.setattr(backend, "command_exists", lambda name: True)
        monkeypatch.setattr(
            backend.subprocess, "run", lambda *a, **k: _cp(a[0], returncode=1)
        )
        with pytest.raises(SystemExit):
            backend._restart_waha(True, 5)

    def test_dies_when_compose_file_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(backend, "command_exists", lambda name: True)
        monkeypatch.setattr(
            backend.subprocess, "run", lambda *a, **k: _cp(a[0], returncode=0)
        )
        monkeypatch.setattr(whatsapp_mod, "COMPOSE_FILE", tmp_path / "missing.yml")
        with pytest.raises(SystemExit):
            backend._restart_waha(True, 5)

    def test_dies_when_service_not_defined(self, monkeypatch, tmp_path):
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("services: {}\n", encoding="utf-8")
        monkeypatch.setattr(backend, "command_exists", lambda name: True)
        monkeypatch.setattr(whatsapp_mod, "COMPOSE_FILE", compose_file)

        def _fake_run(cmd, **kwargs):
            if cmd[-2:] == ["config", "--services"]:
                return _cp(cmd, stdout="other-service\n")
            return _cp(cmd)

        monkeypatch.setattr(backend.subprocess, "run", _fake_run)
        with pytest.raises(SystemExit):
            backend._restart_waha(True, 5)

    def test_restarts_existing_service_and_waits_for_api(self, monkeypatch, tmp_path):
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("services:\n  whatsapp: {}\n", encoding="utf-8")
        monkeypatch.setattr(backend, "command_exists", lambda name: True)
        monkeypatch.setattr(whatsapp_mod, "COMPOSE_FILE", compose_file)
        monkeypatch.setattr(whatsapp_mod, "read_waha_api_key", lambda: "")

        calls = []

        def _fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[-2:] == ["config", "--services"]:
                return _cp(cmd, stdout="whatsapp\n")
            if cmd[-3:] == ["ps", "-aq", "whatsapp"]:
                return _cp(cmd, stdout="abc123\n")
            return _cp(cmd)

        monkeypatch.setattr(backend.subprocess, "run", _fake_run)
        monkeypatch.setattr(common, "http_get", lambda *a, **k: (200, b"ok"))
        backend._restart_waha(True, 5)
        assert any("restart" in c for c in calls)

    def test_starts_up_when_service_does_not_exist_yet(self, monkeypatch, tmp_path):
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("services:\n  whatsapp: {}\n", encoding="utf-8")
        monkeypatch.setattr(backend, "command_exists", lambda name: True)
        monkeypatch.setattr(whatsapp_mod, "COMPOSE_FILE", compose_file)

        def _fake_run(cmd, **kwargs):
            if cmd[-2:] == ["config", "--services"]:
                return _cp(cmd, stdout="whatsapp\n")
            if cmd[-3:] == ["ps", "-aq", "whatsapp"]:
                return _cp(cmd, stdout="")  # nothing yet
            return _cp(cmd)

        monkeypatch.setattr(backend.subprocess, "run", _fake_run)
        backend._restart_waha(False, 5)  # no_wait path: skip API poll

    def test_dies_when_up_fails(self, monkeypatch, tmp_path):
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("services:\n  whatsapp: {}\n", encoding="utf-8")
        monkeypatch.setattr(backend, "command_exists", lambda name: True)
        monkeypatch.setattr(whatsapp_mod, "COMPOSE_FILE", compose_file)

        def _fake_run(cmd, **kwargs):
            if cmd[-2:] == ["config", "--services"]:
                return _cp(cmd, stdout="whatsapp\n")
            if cmd[-3:] == ["ps", "-aq", "whatsapp"]:
                return _cp(cmd, stdout="")
            if "up" in cmd:
                return _cp(cmd, returncode=1)
            return _cp(cmd)

        monkeypatch.setattr(backend.subprocess, "run", _fake_run)
        with pytest.raises(SystemExit):
            backend._restart_waha(True, 5)

    def test_dies_on_api_wait_timeout(self, monkeypatch, tmp_path):
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("services:\n  whatsapp: {}\n", encoding="utf-8")
        monkeypatch.setattr(backend, "command_exists", lambda name: True)
        monkeypatch.setattr(whatsapp_mod, "COMPOSE_FILE", compose_file)
        monkeypatch.setattr(whatsapp_mod, "read_waha_api_key", lambda: "")

        def _fake_run(cmd, **kwargs):
            if cmd[-2:] == ["config", "--services"]:
                return _cp(cmd, stdout="whatsapp\n")
            if cmd[-3:] == ["ps", "-aq", "whatsapp"]:
                return _cp(cmd, stdout="abc123\n")
            return _cp(cmd)

        monkeypatch.setattr(backend.subprocess, "run", _fake_run)
        monkeypatch.setattr(common, "http_get", lambda *a, **k: None)
        monkeypatch.setattr(backend.time, "sleep", lambda *_: None)
        # Force the deadline to already be in the past.
        times = iter([0, 100])
        monkeypatch.setattr(backend.time, "monotonic", lambda: next(times, 100))
        with pytest.raises(SystemExit):
            backend._restart_waha(True, 5)


class TestRun:
    def test_wires_timeouts_and_docker_limits(self, monkeypatch):
        calls = {}

        def _fake_restart_signal(wait, timeout):
            calls["signal"] = (wait, timeout)

        def _fake_restart_waha(wait, timeout, *, docker_limits=True):
            calls["waha"] = (wait, timeout, docker_limits)

        monkeypatch.setattr(backend, "_restart_signal_daemon", _fake_restart_signal)
        monkeypatch.setattr(backend, "_restart_waha", _fake_restart_waha)
        monkeypatch.delenv("SIGNAL_DAEMON_TIMEOUT_SECONDS", raising=False)
        monkeypatch.delenv("WAHA_API_TIMEOUT_SECONDS", raising=False)

        assert backend.run(no_wait=True, docker_limits=False) == 0
        assert calls["signal"] == (False, 30)
        assert calls["waha"] == (False, 120, False)

    def test_reads_custom_timeouts_from_env(self, monkeypatch):
        calls = {}
        monkeypatch.setattr(
            backend, "_restart_signal_daemon", lambda wait, t: calls.update(signal=t)
        )
        monkeypatch.setattr(
            backend,
            "_restart_waha",
            lambda wait, t, *, docker_limits=True: calls.update(waha=t),
        )
        monkeypatch.setenv("SIGNAL_DAEMON_TIMEOUT_SECONDS", "5")
        monkeypatch.setenv("WAHA_API_TIMEOUT_SECONDS", "9")
        backend.run(no_wait=False)
        assert calls == {"signal": 5, "waha": 9}


class TestConfiguredDaemonPids:
    def test_empty_when_no_matching_process(self):
        assert backend._configured_daemon_pids("+1000000000") == []
