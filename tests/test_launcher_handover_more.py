"""Additional coverage for launcher/handover.py's remote orchestration."""

from __future__ import annotations

import shlex
import subprocess

import pytest

from launcher import handover
from launcher import server as server_mod
from launcher import whatsapp as whatsapp_mod


def _cp(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


class TestLoadConf:
    def test_ignores_comments_and_blank_lines(self, tmp_path, monkeypatch):
        conf = tmp_path / "handover.conf"
        conf.write_text(
            "# comment\n\nHZ_HOST=1.2.3.4\nnot-a-kv-line\n", encoding="utf-8"
        )
        monkeypatch.setattr(handover, "CONF_FILE", conf)
        assert handover._load_conf() == {"HZ_HOST": "1.2.3.4"}

    def test_strips_quotes(self, tmp_path, monkeypatch):
        conf = tmp_path / "handover.conf"
        conf.write_text("HZ_HOST=\"1.2.3.4\"\nHZ_USER='deploy'\n", encoding="utf-8")
        monkeypatch.setattr(handover, "CONF_FILE", conf)
        assert handover._load_conf() == {"HZ_HOST": "1.2.3.4", "HZ_USER": "deploy"}


class TestRunRemote:
    def test_dies_without_ssh(self, monkeypatch):
        monkeypatch.setattr(handover, "command_exists", lambda name: False)
        with pytest.raises(SystemExit):
            handover._run_remote("echo hi", capture=False)

    def test_runs_ssh_with_script_as_stdin(self, monkeypatch, tmp_path):
        monkeypatch.setattr(handover, "command_exists", lambda name: True)
        monkeypatch.setattr(handover, "CONF_FILE", tmp_path / "missing.conf")
        monkeypatch.delenv("HZ_HOST", raising=False)
        monkeypatch.delenv("HZ_USER", raising=False)
        captured = {}

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["input"] = kwargs.get("input")
            return _cp(cmd, 0)

        monkeypatch.setattr(handover.subprocess, "run", _fake_run)
        result = handover._run_remote("echo hi", capture=True)
        assert result.returncode == 0
        assert captured["cmd"][0] == "ssh"
        assert captured["input"] == "echo hi"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """No test may touch the real lock, config, log or sleep."""
    monkeypatch.setattr(server_mod, "LOCK_FILE", tmp_path / "signal-tui.lock")
    monkeypatch.setattr(server_mod, "SERVER_LOG", tmp_path / "serve.log")
    monkeypatch.setattr(handover, "CONF_FILE", tmp_path / "missing.conf")
    monkeypatch.setattr(server_mod.time, "sleep", lambda *_: None)
    monkeypatch.delenv("HZ_HOST", raising=False)
    monkeypatch.delenv("HZ_USER", raising=False)


class TestRemoteScripts:
    def test_scripts_are_valid_bash(self):
        for script in (
            handover.REMOTE_STATUS,
            handover.REMOTE_STOP,
            handover._remote_start_script(),
            handover._remote_start_script(docker_limits=False),
        ):
            result = subprocess.run(
                ["bash", "-n"],
                input=script,
                text=True,
                capture_output=True,
                check=False,
            )
            assert result.returncode == 0, result.stderr

    def test_start_runs_detached_serve_and_never_tmux_new_session(self):
        script = handover._remote_start_script()
        serve = shlex.join([".venv/bin/python", *server_mod.SERVE_ARGS])
        assert f"nohup {serve} </dev/null" in script
        assert "--web-host 0.0.0.0" in script
        assert "tmux new-session" not in script
        assert "signal_tui" not in script
        assert "OK_TUI_SERVER" in script and "FAIL_TUI_SERVER" in script
        assert "--no-docker-limits" not in script
        assert "--no-docker-limits" in handover._remote_start_script(
            docker_limits=False
        )

    def test_start_stops_previous_instance_before_spawning(self):
        script = handover._remote_start_script()
        assert script.index("stop_serve ||") < script.index("nohup")

    def test_only_the_lock_variable_is_removed(self):
        for script in (handover.REMOTE_STOP, handover._remote_start_script()):
            rms = [ln.strip() for ln in script.splitlines() if "rm " in ln]
            assert rms == ['rm -f "$LOCK"']

    def test_stop_signals_lock_pid_waits_for_exit_keeps_legacy_tmux_kill(self):
        script = handover.REMOTE_STOP
        assert 'kill -INT "$pid"' in script
        assert 'kill -0 "$pid"' in script
        assert "tmux kill-session -t tui" in script
        assert "tmux new-session" not in script

    def test_status_reports_server_not_tmux(self):
        assert "Server: ATTIVO" in handover.REMOTE_STATUS
        assert "tmux" not in handover.REMOTE_STATUS


class TestStatus:
    def test_prints_local_and_remote_sections(self, monkeypatch, capsys):
        monkeypatch.setattr(server_mod, "_pgrep", lambda pattern: False)
        monkeypatch.setattr(server_mod, "_docker_container_running", lambda n: False)
        monkeypatch.setattr(
            handover, "_run_remote", lambda script, capture: _cp(["ssh"], 0)
        )
        assert handover.status() == 0
        out = capsys.readouterr().out
        assert "LOCALE" in out
        assert "REMOTO" in out
        assert "Server: spento" in out

    def test_local_running_means_live_lock_holder(self, monkeypatch, capsys):
        monkeypatch.setattr(server_mod, "server_pid", lambda: 4321)
        monkeypatch.setattr(server_mod, "_pgrep", lambda pattern: False)
        monkeypatch.setattr(server_mod, "_docker_container_running", lambda n: False)
        monkeypatch.setattr(
            handover, "_run_remote", lambda script, capture: _cp(["ssh"], 0)
        )
        handover.status()
        assert "Server: ATTIVO (lock pid 4321)" in capsys.readouterr().out


class TestToServer:
    def _common(self, monkeypatch, tmp_path):
        monkeypatch.setattr(server_mod, "stop_server", lambda: True)
        monkeypatch.setattr(whatsapp_mod, "COMPOSE_FILE", tmp_path / "c.yml")
        monkeypatch.setattr(handover.subprocess, "run", lambda *a, **k: _cp(a[0], 0))

    def test_dies_when_remote_start_fails(self, monkeypatch, tmp_path):
        self._common(monkeypatch, tmp_path)
        monkeypatch.setattr(
            handover,
            "_run_remote",
            lambda script, capture: _cp(["ssh"], 0, stdout="FAIL_TUI_SERVER\n"),
        )
        with pytest.raises(SystemExit):
            handover.to_server()

    def test_succeeds_when_remote_reports_ok(self, monkeypatch, tmp_path, capsys):
        self._common(monkeypatch, tmp_path)
        monkeypatch.setattr(
            handover,
            "_run_remote",
            lambda script, capture: _cp(["ssh"], 0, stdout="OK_TUI_SERVER\n"),
        )
        assert handover.to_server() == 0
        out = capsys.readouterr().out
        assert "Handover verso il server completato" in out
        assert "megachat.py" in out  # how to open the TUI on the server

    def test_stuck_local_server_aborts_before_touching_the_remote(
        self, monkeypatch, tmp_path
    ):
        self._common(monkeypatch, tmp_path)
        monkeypatch.setattr(server_mod, "stop_server", lambda: False)
        remote = []
        monkeypatch.setattr(
            handover, "_run_remote", lambda script, capture: remote.append(script)
        )
        with pytest.raises(SystemExit):
            handover.to_server()
        assert remote == []  # never two servers, never none by surprise

    def test_stops_local_before_starting_remote(self, monkeypatch, tmp_path):
        order = []
        self._common(monkeypatch, tmp_path)
        monkeypatch.setattr(
            server_mod, "stop_server", lambda: order.append("local-stop") or True
        )
        monkeypatch.setattr(
            handover,
            "_run_remote",
            lambda script, capture: (
                order.append("remote-start")
                or _cp(["ssh"], 0, stdout="OK_TUI_SERVER\n")
            ),
        )
        handover.to_server()
        assert order == ["local-stop", "remote-start"]


class TestToLocal:
    def _stubs(self, monkeypatch, tmp_path, *, remote_rc=0, stops=True, up=True):
        events = []
        monkeypatch.setattr(
            handover,
            "_run_remote",
            lambda script, capture: (
                events.append("remote-stop") or _cp(["ssh"], remote_rc)
            ),
        )
        monkeypatch.setattr(
            whatsapp_mod, "start", lambda **k: events.append("waha") or 0
        )
        monkeypatch.setattr(handover, "PROJECT_DIR", tmp_path)
        monkeypatch.setattr(
            server_mod, "stop_server", lambda: events.append("local-stop") or stops
        )
        monkeypatch.setattr(
            server_mod, "spawn_and_wait", lambda: events.append("spawn") or up
        )
        return events

    def test_dies_when_local_server_does_not_come_up(self, monkeypatch, tmp_path):
        self._stubs(monkeypatch, tmp_path, up=False)
        with pytest.raises(SystemExit):
            handover.to_local()

    def test_happy_path_order_and_message(self, monkeypatch, tmp_path, capsys):
        events = self._stubs(monkeypatch, tmp_path)
        assert handover.to_local() == 0
        assert events == ["remote-stop", "waha", "local-stop", "spawn"]
        out = capsys.readouterr().out
        assert "Handover verso il locale completato" in out
        assert "megachat.py" in out

    def test_never_starts_tmux(self, monkeypatch, tmp_path):
        self._stubs(monkeypatch, tmp_path)
        ran = []
        monkeypatch.setattr(
            handover.subprocess, "run", lambda cmd, **k: ran.append(cmd) or _cp(cmd, 0)
        )
        handover.to_local()
        assert not any(c[0] == "tmux" for c in ran)

    def test_remote_that_will_not_stop_aborts_before_local_start(
        self, monkeypatch, tmp_path
    ):
        events = self._stubs(monkeypatch, tmp_path, remote_rc=3)
        with pytest.raises(SystemExit):
            handover.to_local()
        assert events == ["remote-stop"]  # no second server

    def test_falls_back_to_no_wait_start_when_waha_start_fails(
        self, monkeypatch, tmp_path, capsys
    ):
        self._stubs(monkeypatch, tmp_path)
        starts = []
        monkeypatch.setattr(
            whatsapp_mod,
            "start",
            lambda **k: starts.append(k) or (1 if not k.get("no_wait") else 0),
        )
        assert handover.to_local() == 0
        assert len(starts) == 2  # first wait=True attempt, then no_wait fallback
        assert "Handover verso il locale completato" in capsys.readouterr().out
