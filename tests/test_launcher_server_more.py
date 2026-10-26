"""Additional coverage for launcher/server.py's start/stop/status orchestration."""

from __future__ import annotations

import os
import signal
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
        lock.write_text(str(os.getpid()), encoding="utf-8")
        monkeypatch.setattr(server, "LOCK_FILE", lock)
        monkeypatch.setattr(server, "_pgrep", lambda pattern: True)
        monkeypatch.setattr(server, "_docker_container_running", lambda name: True)
        monkeypatch.setattr(server, "port_listening", lambda port: True)
        monkeypatch.setattr(whatsapp_mod, "session_status", lambda timeout=3: "WORKING")
        assert server.status() == 0
        out = capsys.readouterr().out
        assert f"Server: ATTIVO (pid {os.getpid()})" in out
        assert "daemon signal-cli: attivo" in out
        assert "WAHA: attivo" in out
        assert "in ascolto su 4242" in out

    def test_reports_session_state_when_waha_is_active(
        self, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setattr(server, "LOCK_FILE", tmp_path / "no.lock")
        monkeypatch.setattr(server, "_pgrep", lambda pattern: False)
        monkeypatch.setattr(server, "_docker_container_running", lambda name: True)
        monkeypatch.setattr(server, "port_listening", lambda port: False)
        seen = {}

        def fake_status(timeout=3):
            seen["timeout"] = timeout
            return "SCAN_QR_CODE"

        monkeypatch.setattr(whatsapp_mod, "session_status", fake_status)
        assert server.status() == 0
        out = capsys.readouterr().out
        assert "WAHA: attivo" in out
        assert "in attesa di associazione" in out
        assert seen["timeout"] == 2


class TestStopAll:
    def test_reports_waha_stopped(self, monkeypatch, capsys):
        monkeypatch.setattr(server, "stop_server", lambda: True)
        monkeypatch.setattr(whatsapp_mod, "COMPOSE_FILE", "compose.yml")
        monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: _cp(a[0], 0))
        assert server.stop_all() == 0
        assert "WAHA fermato" in capsys.readouterr().out

    def test_reports_waha_was_not_running(self, monkeypatch, capsys):
        monkeypatch.setattr(server, "stop_server", lambda: True)
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
    monkeypatch.setattr(whatsapp_mod, "session_status", lambda timeout=3: "WORKING")
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
        assert "Sessione WhatsApp connessa." in out

    def test_skips_token_when_print_token_is_false(
        self, monkeypatch, start_env, capsys
    ):
        monkeypatch.setattr(server, "_read_web_token", lambda: "tok123")
        monkeypatch.setattr(server, "_local_ip", lambda: "10.0.0.5")
        monkeypatch.setattr(server, "port_listening", lambda port: True)
        assert server.start_all(print_token=False) == 0
        out = capsys.readouterr().out
        assert "Token Web UI" not in out
        assert "10.0.0.5:4242" in out

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
        monkeypatch.setattr(server, "stop_server", lambda: order.append("stop") or True)
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


class TestStatusStaleLock:
    def test_dead_lock_holder_is_not_running(self, monkeypatch, tmp_path, capsys):
        lock = tmp_path / "signal-tui.lock"
        lock.write_text("4242", encoding="utf-8")
        monkeypatch.setattr(server, "LOCK_FILE", lock)
        monkeypatch.setattr(server, "_pid_alive", lambda pid: False)
        monkeypatch.setattr(server, "_pgrep", lambda pattern: False)
        monkeypatch.setattr(server, "_docker_container_running", lambda name: False)
        monkeypatch.setattr(server, "port_listening", lambda port: False)
        server.status()
        assert "Server: spento" in capsys.readouterr().out


class TestStopServer:
    @pytest.fixture
    def lock(self, monkeypatch, tmp_path):
        lock = tmp_path / "signal-tui.lock"
        monkeypatch.setattr(server, "LOCK_FILE", lock)
        monkeypatch.setattr(server.time, "sleep", lambda *_: None)
        self.tmux = []
        monkeypatch.setattr(
            server, "_kill_tmux_session", lambda name="tui": self.tmux.append(name)
        )
        return lock

    def test_sigint_then_waits_for_exit_and_clears_lock(self, monkeypatch, lock):
        lock.write_text("31337", encoding="utf-8")
        alive = {"v": True}
        sent = []

        def _kill(pid, sig):
            if sig == 0:
                if not alive["v"]:
                    raise ProcessLookupError
                return
            sent.append((pid, sig))

        polls = {"n": 0}

        def _sleep(_):
            polls["n"] += 1
            if polls["n"] >= 3:
                alive["v"] = False

        monkeypatch.setattr(server.os, "kill", _kill)
        monkeypatch.setattr(server.time, "sleep", _sleep)
        assert server.stop_server() is True
        assert sent == [(31337, signal.SIGINT)]
        assert polls["n"] == 3  # waited for the pid, not just the lock file
        assert not lock.exists()
        assert self.tmux == ["tui"]  # legacy session still cleaned up

    def test_process_that_survives_keeps_the_lock(self, monkeypatch, lock):
        lock.write_text("31337", encoding="utf-8")
        monkeypatch.setattr(server.os, "kill", lambda pid, sig: None)  # immortal
        assert server.stop_server() is False
        assert lock.read_text() == "31337"  # never free the lock under a live pid
        assert self.tmux == []

    def test_stale_lock_is_removed_without_signalling(self, monkeypatch, lock):
        lock.write_text("31337", encoding="utf-8")
        calls = []

        def _kill(pid, sig):
            calls.append(sig)
            raise ProcessLookupError

        monkeypatch.setattr(server.os, "kill", _kill)
        assert server.stop_server() is True
        assert calls == [0]  # liveness probe only, no SIGINT
        assert not lock.exists()

    def test_garbage_lock_is_removed(self, monkeypatch, lock):
        lock.write_text("not-a-pid", encoding="utf-8")
        assert server.stop_server() is True
        assert not lock.exists()


class TestStartAllStuckServer:
    def test_does_not_spawn_next_to_a_server_that_will_not_stop(
        self, monkeypatch, start_env
    ):
        monkeypatch.setattr(server, "stop_server", lambda: False)
        monkeypatch.setattr(server, "server_pid", lambda: 99)
        with pytest.raises(SystemExit):
            server.start_all()
        assert start_env == []  # nothing spawned

    def test_stop_all_keeps_waha_when_server_will_not_stop(self, monkeypatch, tmp_path):
        monkeypatch.setattr(server, "stop_server", lambda: False)
        monkeypatch.setattr(server, "server_pid", lambda: 99)
        ran = []
        monkeypatch.setattr(
            server.subprocess, "run", lambda *a, **k: ran.append(a) or _cp(a[0], 0)
        )
        with pytest.raises(SystemExit):
            server.stop_all()
        assert ran == []

    def test_never_starts_a_tmux_session(self, monkeypatch, start_env):
        ran = []
        monkeypatch.setattr(
            server.subprocess, "run", lambda *a, **k: ran.append(a[0]) or _cp(a[0], 0)
        )
        monkeypatch.setattr(server, "port_listening", lambda port: True)
        monkeypatch.setattr(server, "stop_server", lambda: True)
        assert server.start_all() == 0
        assert not any(c[:2] == ["tmux", "new-session"] for c in ran)
