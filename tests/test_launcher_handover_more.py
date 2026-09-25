"""Additional coverage for launcher/handover.py's remote orchestration."""

from __future__ import annotations

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


class TestStatus:
    def test_prints_local_and_remote_sections(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(server_mod, "LOCK_FILE", tmp_path / "no.lock")
        monkeypatch.setattr(server_mod, "_pgrep", lambda pattern: False)
        monkeypatch.setattr(server_mod, "_docker_container_running", lambda n: False)
        monkeypatch.setattr(
            handover, "_run_remote", lambda script, capture: _cp(["ssh"], 0)
        )
        monkeypatch.setattr(handover, "CONF_FILE", tmp_path / "missing.conf")
        monkeypatch.delenv("HZ_HOST", raising=False)
        monkeypatch.delenv("HZ_USER", raising=False)
        assert handover.status() == 0
        out = capsys.readouterr().out
        assert "LOCALE" in out
        assert "REMOTO" in out


class TestToServer:
    def test_dies_when_remote_start_fails(self, monkeypatch, tmp_path):
        monkeypatch.setattr(server_mod, "_stop_tui", lambda: None)
        monkeypatch.setattr(whatsapp_mod, "COMPOSE_FILE", tmp_path / "c.yml")
        monkeypatch.setattr(handover.subprocess, "run", lambda *a, **k: _cp(a[0], 0))
        monkeypatch.setattr(
            handover,
            "_run_remote",
            lambda script, capture: _cp(["ssh"], 0, stdout="FAIL_TUI_SERVER\n"),
        )
        monkeypatch.setattr(handover, "CONF_FILE", tmp_path / "missing.conf")
        monkeypatch.delenv("HZ_HOST", raising=False)
        with pytest.raises(SystemExit):
            handover.to_server()

    def test_succeeds_when_remote_reports_ok(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(server_mod, "_stop_tui", lambda: None)
        monkeypatch.setattr(whatsapp_mod, "COMPOSE_FILE", tmp_path / "c.yml")
        monkeypatch.setattr(handover.subprocess, "run", lambda *a, **k: _cp(a[0], 0))
        monkeypatch.setattr(
            handover,
            "_run_remote",
            lambda script, capture: _cp(["ssh"], 0, stdout="OK_TUI_SERVER\n"),
        )
        monkeypatch.setattr(handover, "CONF_FILE", tmp_path / "missing.conf")
        monkeypatch.delenv("HZ_HOST", raising=False)
        assert handover.to_server() == 0
        assert "Handover verso il server completato" in capsys.readouterr().out


class TestToLocal:
    def test_dies_when_local_tmux_session_fails(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            handover, "_run_remote", lambda script, capture: _cp(["ssh"], 0)
        )
        monkeypatch.setattr(whatsapp_mod, "start", lambda **k: 0)
        monkeypatch.setattr(handover, "PROJECT_DIR", tmp_path)
        monkeypatch.setattr(handover.time, "sleep", lambda *_: None)

        def _fake_run(cmd, **kwargs):
            if cmd[:2] == ["tmux", "list-sessions"]:
                return _cp(cmd, stdout="")
            return _cp(cmd, 0)

        monkeypatch.setattr(handover.subprocess, "run", _fake_run)
        monkeypatch.setattr(handover, "CONF_FILE", tmp_path / "missing.conf")
        monkeypatch.delenv("HZ_HOST", raising=False)
        with pytest.raises(SystemExit):
            handover.to_local()

    def test_falls_back_to_no_wait_start_when_waha_start_fails(
        self, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setattr(
            handover, "_run_remote", lambda script, capture: _cp(["ssh"], 0)
        )
        starts = []
        monkeypatch.setattr(
            whatsapp_mod,
            "start",
            lambda **k: starts.append(k) or (1 if not k.get("no_wait") else 0),
        )
        monkeypatch.setattr(handover, "PROJECT_DIR", tmp_path)
        monkeypatch.setattr(handover.time, "sleep", lambda *_: None)

        def _fake_run(cmd, **kwargs):
            if cmd[:2] == ["tmux", "list-sessions"]:
                return _cp(cmd, stdout="tui: 1 windows\n")
            return _cp(cmd, 0)

        monkeypatch.setattr(handover.subprocess, "run", _fake_run)
        monkeypatch.setattr(handover, "CONF_FILE", tmp_path / "missing.conf")
        monkeypatch.delenv("HZ_HOST", raising=False)
        assert handover.to_local() == 0
        assert len(starts) == 2  # first wait=True attempt, then no_wait fallback
        assert "Handover verso il locale completato" in capsys.readouterr().out
