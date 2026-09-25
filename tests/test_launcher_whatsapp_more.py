"""Additional coverage for launcher/whatsapp.py: port/firewall probes and
the failure branches of setup()/start() not exercised by
tests/test_launcher_whatsapp.py.
"""

from __future__ import annotations

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


def _cp(cmd, returncode=0, stdout=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout)


class TestListeningPortOwner:
    def test_none_when_ss_missing(self, monkeypatch):
        monkeypatch.setattr(whatsapp, "command_exists", lambda name: False)
        assert whatsapp._listening_port_owner(3005) == (None, None)

    def test_parses_pid_and_process_name(self, monkeypatch):
        monkeypatch.setattr(whatsapp, "command_exists", lambda name: True)
        ss_output = 'LISTEN 0 128 127.0.0.1:3005 0.0.0.0:* users:(("docker-proxy",pid=4242,fd=7))\n'
        monkeypatch.setattr(
            whatsapp.subprocess, "run", lambda *a, **k: _cp(a[0], stdout=ss_output)
        )
        assert whatsapp._listening_port_owner(3005) == ("4242", "docker-proxy")

    def test_none_when_port_not_found(self, monkeypatch):
        monkeypatch.setattr(whatsapp, "command_exists", lambda name: True)
        monkeypatch.setattr(
            whatsapp.subprocess, "run", lambda *a, **k: _cp(a[0], stdout="")
        )
        assert whatsapp._listening_port_owner(3005) == (None, None)

    def test_none_on_subprocess_error(self, monkeypatch):
        monkeypatch.setattr(whatsapp, "command_exists", lambda name: True)

        def _raise(*a, **k):
            raise subprocess.SubprocessError("boom")

        monkeypatch.setattr(whatsapp.subprocess, "run", _raise)
        assert whatsapp._listening_port_owner(3005) == (None, None)


class TestCheckPort:
    def test_available_when_nothing_listens(self, monkeypatch):
        monkeypatch.setattr(
            whatsapp, "_listening_port_owner", lambda port: (None, None)
        )
        monkeypatch.setattr(whatsapp, "port_listening", lambda port: False)
        assert whatsapp.check_port(3005, "WAHA API") is True

    def test_webhook_port_owned_by_python_is_ok(self, monkeypatch):
        monkeypatch.setattr(
            whatsapp, "_listening_port_owner", lambda port: ("123", "python")
        )
        monkeypatch.setattr(whatsapp, "port_listening", lambda port: True)
        assert whatsapp.check_port(whatsapp.WEBHOOK_PORT, "webhook") is True

    def test_in_use_by_other_process_warns_false(self, monkeypatch):
        monkeypatch.setattr(
            whatsapp, "_listening_port_owner", lambda port: ("999", "nginx")
        )
        monkeypatch.setattr(whatsapp, "port_listening", lambda port: True)
        assert whatsapp.check_port(3005, "WAHA API") is False

    def test_in_use_with_unknown_owner_warns_false(self, monkeypatch):
        monkeypatch.setattr(
            whatsapp, "_listening_port_owner", lambda port: (None, None)
        )
        monkeypatch.setattr(whatsapp, "port_listening", lambda port: True)
        assert whatsapp.check_port(3005, "WAHA API") is False


class TestCheckFirewall:
    def test_no_firewall_tools_present(self, monkeypatch):
        monkeypatch.setattr(whatsapp, "command_exists", lambda name: False)
        whatsapp.check_firewall(3005, "WAHA API")  # must not raise

    def test_ufw_active_and_port_allowed(self, monkeypatch):
        monkeypatch.setattr(whatsapp, "command_exists", lambda name: name == "ufw")
        monkeypatch.setattr(
            whatsapp.subprocess,
            "run",
            lambda *a, **k: _cp(a[0], stdout="Status: active\n3005 ALLOW Anywhere\n"),
        )
        whatsapp.check_firewall(3005, "WAHA API")

    def test_ufw_active_and_port_not_allowed(self, monkeypatch):
        monkeypatch.setattr(whatsapp, "command_exists", lambda name: name == "ufw")
        monkeypatch.setattr(
            whatsapp.subprocess,
            "run",
            lambda *a, **k: _cp(a[0], stdout="Status: active\n"),
        )
        whatsapp.check_firewall(3005, "WAHA API")

    def test_iptables_allows_port(self, monkeypatch):
        monkeypatch.setattr(whatsapp, "command_exists", lambda name: name == "iptables")
        monkeypatch.setattr(
            whatsapp.subprocess,
            "run",
            lambda *a, **k: _cp(a[0], stdout="ACCEPT tcp -- anywhere dpt:3005\n"),
        )
        whatsapp.check_firewall(3005, "WAHA API")

    def test_iptables_drop_policy_warns(self, monkeypatch):
        monkeypatch.setattr(whatsapp, "command_exists", lambda name: name == "iptables")
        monkeypatch.setattr(
            whatsapp.subprocess,
            "run",
            lambda *a, **k: _cp(a[0], stdout="DROP all -- anywhere anywhere\n"),
        )
        whatsapp.check_firewall(3005, "WAHA API")


class TestSetupFailureBranches:
    def test_docker_compose_missing_returns_false(self, project, monkeypatch):
        monkeypatch.setattr(whatsapp, "command_exists", lambda name: True)
        monkeypatch.setattr(
            whatsapp.subprocess,
            "run",
            lambda cmd, **k: (
                _cp(cmd, returncode=1)
                if cmd[:2] == ["docker", "compose"]
                else _cp(cmd, stdout="Docker version X")
            ),
        )
        assert whatsapp.setup(should_start=False) is False

    def test_start_up_failure_returns_false(self, project, monkeypatch):
        monkeypatch.setattr(whatsapp, "command_exists", lambda name: True)
        monkeypatch.setattr(whatsapp, "check_port", lambda port, label: True)
        monkeypatch.setattr(whatsapp, "check_firewall", lambda port, label: None)
        monkeypatch.setattr(whatsapp, "ensure_waha_env", lambda: None)

        def _fake_run(cmd, **k):
            if cmd[:2] == ["docker", "compose"] and "up" in cmd:
                return _cp(cmd, returncode=1)
            if cmd[:2] == ["docker", "compose"]:
                return _cp(cmd, returncode=0)
            return _cp(cmd, stdout="Docker version X")

        monkeypatch.setattr(whatsapp.subprocess, "run", _fake_run)
        assert whatsapp.setup(should_start=True) is False

    def test_start_not_ready_after_timeout_still_returns_true(
        self, project, monkeypatch
    ):
        monkeypatch.setattr(whatsapp, "command_exists", lambda name: True)
        monkeypatch.setattr(whatsapp, "check_port", lambda port, label: True)
        monkeypatch.setattr(whatsapp, "check_firewall", lambda port, label: None)
        monkeypatch.setattr(whatsapp, "ensure_waha_env", lambda: None)
        monkeypatch.setattr(whatsapp.time, "sleep", lambda *_: None)
        monkeypatch.setattr(whatsapp, "http_get", lambda *a, **k: None)

        def _fake_run(cmd, **k):
            if cmd[:2] == ["docker", "compose"]:
                return _cp(cmd, returncode=0)
            return _cp(cmd, stdout="Docker version X")

        monkeypatch.setattr(whatsapp.subprocess, "run", _fake_run)
        assert whatsapp.setup(should_start=True) is True


class TestStandaloneStartFailureBranches:
    def test_docker_missing_returns_1(self, project, monkeypatch):
        monkeypatch.setattr(whatsapp, "command_exists", lambda name: False)
        assert whatsapp.start(no_wait=True) == 1

    def test_up_failure_returns_its_code(self, project, monkeypatch):
        monkeypatch.setattr(whatsapp, "command_exists", lambda name: True)
        monkeypatch.setattr(
            whatsapp.subprocess, "run", lambda cmd, **k: _cp(cmd, returncode=3)
        )
        assert whatsapp.start(no_wait=True) == 3

    def test_waits_and_reports_ready(self, project, monkeypatch):
        monkeypatch.setattr(whatsapp, "command_exists", lambda name: True)
        monkeypatch.setattr(
            whatsapp.subprocess, "run", lambda cmd, **k: _cp(cmd, returncode=0)
        )
        monkeypatch.setattr(whatsapp, "http_get", lambda *a, **k: (200, b"ok"))
        assert whatsapp.start(no_wait=False) == 0


class TestStop:
    def test_prints_on_success(self, project, monkeypatch, capsys):
        monkeypatch.setattr(
            whatsapp.subprocess, "run", lambda cmd, **k: _cp(cmd, returncode=0)
        )
        assert whatsapp.stop() == 0
        assert "WAHA stopped" in capsys.readouterr().out
