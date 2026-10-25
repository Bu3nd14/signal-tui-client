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
        assert "Server: spento" in out
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
        assert "Server: ATTIVO (pid 4242)" in out
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


class _FakeProc:
    def __init__(self, returncode=None):
        self.returncode = returncode

    def poll(self):
        return self.returncode


@pytest.fixture
def start_env(monkeypatch, tmp_path):
    """Isolate start_all from tmux, the real lock, the log and real sleeping."""
    monkeypatch.setattr(whatsapp_mod, "start", lambda **k: 0)
    monkeypatch.setattr(server, "LOCK_FILE", tmp_path / "signal-tui.lock")
    monkeypatch.setattr(server, "_kill_tmux_session", lambda name="tui": None)
    monkeypatch.setattr(server, "SERVER_LOG", tmp_path / "serve.log")
    monkeypatch.setattr(server.time, "sleep", lambda *_: None)
    spawned = []

    def _popen(cmd, **kwargs):
        spawned.append((cmd, kwargs))
        return _FakeProc()

    monkeypatch.setattr(server.subprocess, "Popen", _popen)
    return spawned


class TestStartAll:
    def test_dies_when_server_never_comes_up(self, monkeypatch, start_env):
        monkeypatch.setattr(server, "port_listening", lambda port: False)
        with pytest.raises(SystemExit):
            server.start_all()

    def test_dies_early_when_server_process_exits(self, monkeypatch, start_env):
        monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: _FakeProc(1))
        monkeypatch.setattr(server, "port_listening", lambda port: True)
        with pytest.raises(SystemExit):
            server.start_all()

    def test_reports_success_with_token_and_url(self, monkeypatch, start_env, capsys):
        monkeypatch.setattr(server, "_read_web_token", lambda: "tok123")
        monkeypatch.setattr(server, "_local_ip", lambda: "10.0.0.5")
        monkeypatch.setattr(server, "port_listening", lambda port: True)
        assert server.start_all() == 0
        out = capsys.readouterr().out
        assert "10.0.0.5:4242" in out
        assert "tok123" in out

    def test_starts_detached_serve_process(self, monkeypatch, start_env):
        monkeypatch.setattr(server, "port_listening", lambda port: True)
        assert server.start_all() == 0
        ((cmd, kwargs),) = start_env
        assert cmd[0].endswith(".venv/bin/python")
        assert cmd[1:] == [
            "megachat.py",
            "serve",
            "--web-port",
            "4242",
            "--web-host",
            "0.0.0.0",
        ]
        assert kwargs["start_new_session"] is True
        assert kwargs["cwd"] == server.PROJECT_DIR

    def test_stops_previous_instance_before_starting(self, monkeypatch, start_env):
        order = []
        monkeypatch.setattr(server, "_stop_tui", lambda: order.append("stop"))
        monkeypatch.setattr(server, "port_listening", lambda port: True)
        real_popen = server.subprocess.Popen
        monkeypatch.setattr(
            server.subprocess,
            "Popen",
            lambda *a, **k: order.append("spawn") or real_popen(*a, **k),
        )
        server.start_all()
        assert order == ["stop", "spawn"]


class TestKillTmuxSession:
    def test_tolerates_missing_tmux(self, monkeypatch):
        def _boom(*a, **k):
            raise FileNotFoundError("tmux")

        monkeypatch.setattr(server.subprocess, "run", _boom)
        server._kill_tmux_session()  # must not raise
