"""Additional coverage for launcher/server.py's start/stop/status orchestration."""

from __future__ import annotations

import subprocess

import pytest

from launcher import server
from launcher import whatsapp as whatsapp_mod


def _cp(cmd, returncode=0, stdout=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout)


class TestLocalIp:
    def test_returns_a_string(self):
        # Best-effort: either a real LAN IP or the 127.0.0.1 fallback.
        assert isinstance(server._local_ip(), str)

    def test_falls_back_on_os_error(self, monkeypatch):
        class _BoomSocket:
            def __init__(self, *a, **k):
                raise OSError("no network")

        monkeypatch.setattr(server.socket, "socket", _BoomSocket)
        assert server._local_ip() == "127.0.0.1"


class TestDockerContainerRunning:
    def test_true_when_name_listed(self, monkeypatch):
        monkeypatch.setattr(server, "command_exists", lambda name: True)
        monkeypatch.setattr(
            server.subprocess,
            "run",
            lambda *a, **k: _cp(a[0], stdout="signal-tui-whatsapp\nother\n"),
        )
        assert server._docker_container_running("signal-tui-whatsapp") is True

    def test_false_when_name_absent(self, monkeypatch):
        monkeypatch.setattr(server, "command_exists", lambda name: True)
        monkeypatch.setattr(
            server.subprocess, "run", lambda *a, **k: _cp(a[0], stdout="other\n")
        )
        assert server._docker_container_running("signal-tui-whatsapp") is False


class TestPgrep:
    def test_true_when_process_found(self, monkeypatch):
        monkeypatch.setattr(server, "command_exists", lambda name: True)
        monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: _cp(a[0], 0))
        assert server._pgrep("anything") is True

    def test_false_when_process_missing(self, monkeypatch):
        monkeypatch.setattr(server, "command_exists", lambda name: True)
        monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: _cp(a[0], 1))
        assert server._pgrep("anything") is False


class TestStatus:
    def test_reports_all_off(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(server, "LOCK_FILE", tmp_path / "no.lock")
        monkeypatch.setattr(server, "_pgrep", lambda pattern: False)
        monkeypatch.setattr(server, "_docker_container_running", lambda name: False)
        monkeypatch.setattr(server, "port_listening", lambda port: False)
        assert server.status() == 0
        out = capsys.readouterr().out
        assert "TUI: spenta" in out
        assert "daemon signal-cli: spento" in out
        assert "WAHA: spento" in out
        assert "Web UI: spenta" in out

    def test_reports_all_on(self, monkeypatch, tmp_path, capsys):
        lock = tmp_path / "signal-tui.lock"
        lock.write_text("4242", encoding="utf-8")
        monkeypatch.setattr(server, "LOCK_FILE", lock)
        monkeypatch.setattr(server, "_pgrep", lambda pattern: True)
        monkeypatch.setattr(server, "_docker_container_running", lambda name: True)
        monkeypatch.setattr(server, "port_listening", lambda port: True)
        assert server.status() == 0
        out = capsys.readouterr().out
        assert "TUI: ATTIVA (pid 4242)" in out
        assert "daemon signal-cli: attivo" in out
        assert "WAHA: attivo" in out
        assert "in ascolto su 4242" in out


class TestStopAll:
    def test_reports_waha_stopped(self, monkeypatch, capsys):
        monkeypatch.setattr(server, "_stop_tui", lambda: None)
        monkeypatch.setattr(whatsapp_mod, "COMPOSE_FILE", "compose.yml")
        monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: _cp(a[0], 0))
        assert server.stop_all() == 0
        assert "WAHA fermato" in capsys.readouterr().out

    def test_reports_waha_was_not_running(self, monkeypatch, capsys):
        monkeypatch.setattr(server, "_stop_tui", lambda: None)
        monkeypatch.setattr(whatsapp_mod, "COMPOSE_FILE", "compose.yml")
        monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: _cp(a[0], 1))
        assert server.stop_all() == 0
        assert "WAHA non era attivo" in capsys.readouterr().out


class TestStartAll:
    def test_dies_when_tui_never_comes_up(self, monkeypatch, tmp_path):
        monkeypatch.setattr(whatsapp_mod, "start", lambda **k: 1)
        monkeypatch.setattr(server, "LOCK_FILE", tmp_path / "signal-tui.lock")
        monkeypatch.setattr(server, "_kill_tmux_session", lambda name="tui": None)
        monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: _cp(a[0], 0))
        monkeypatch.setattr(server.time, "sleep", lambda *_: None)
        monkeypatch.setattr(server, "port_listening", lambda port: False)
        with pytest.raises(SystemExit):
            server.start_all()

    def test_reports_success_with_token_and_url(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(whatsapp_mod, "start", lambda **k: 0)
        monkeypatch.setattr(server, "LOCK_FILE", tmp_path / "signal-tui.lock")
        monkeypatch.setattr(server, "_kill_tmux_session", lambda name="tui": None)
        monkeypatch.setattr(server, "_read_web_token", lambda: "tok123")
        monkeypatch.setattr(server, "_local_ip", lambda: "10.0.0.5")
        monkeypatch.setattr(server.time, "sleep", lambda *_: None)
        monkeypatch.setattr(server, "port_listening", lambda port: True)

        def _fake_run(cmd, **kwargs):
            if cmd[:2] == ["tmux", "list-sessions"]:
                return _cp(cmd, stdout="tui: 1 windows\n")
            return _cp(cmd, 0)

        monkeypatch.setattr(server.subprocess, "run", _fake_run)
        assert server.start_all() == 0
        out = capsys.readouterr().out
        assert "10.0.0.5:4242" in out
        assert "tok123" in out
