"""Lighter regression coverage for launcher/{backend,server,handover}.py.

These wrap process management / SSH, so tests focus on the pure/mockable
helper functions rather than spawning real daemons or containers.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from launcher import backend, handover, install, server

# ─── backend.py ──────────────────────────────────────────────────────────────


class TestSignalNumber:
    def test_env_var_wins(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(backend, "PROJECT_DIR", tmp_path)
        monkeypatch.setenv("SIGNAL_USER_NUMBER", "+1000")
        assert backend._signal_number() == "+1000"

    def test_falls_back_to_config_json(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(backend, "PROJECT_DIR", tmp_path)
        monkeypatch.delenv("SIGNAL_USER_NUMBER", raising=False)
        (tmp_path / "config.json").write_text(
            json.dumps({"user_number": "+2000"}), encoding="utf-8"
        )
        assert backend._signal_number() == "+2000"

    def test_missing_everything_returns_empty(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(backend, "PROJECT_DIR", tmp_path)
        monkeypatch.delenv("SIGNAL_USER_NUMBER", raising=False)
        assert backend._signal_number() == ""


class TestFindSignalCli:
    def test_finds_executable_binary(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(backend, "PROJECT_DIR", tmp_path)
        exe = tmp_path / "bin" / "signal-cli-0.14.7" / "bin" / "signal-cli"
        exe.parent.mkdir(parents=True)
        exe.write_text("#!/bin/sh\n", encoding="utf-8")
        exe.chmod(0o755)
        assert backend._find_signal_cli() == str(exe)

    def test_none_when_absent(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(backend, "PROJECT_DIR", tmp_path)
        assert backend._find_signal_cli() is None


class TestIsConfiguredSignalDaemon:
    def test_own_process_without_matching_cmdline_is_false(self):
        # This test process itself is owned by us but isn't a signal-cli daemon.
        assert backend._is_configured_signal_daemon(os.getpid(), "+1000") is False

    def test_nonexistent_pid_is_false(self):
        assert backend._is_configured_signal_daemon(2**30, "+1000") is False


# ─── server.py ───────────────────────────────────────────────────────────────


class TestServerHelpers:
    def test_read_web_token(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(server, "PROJECT_DIR", tmp_path)
        (tmp_path / "config.json").write_text(
            json.dumps({"web": {"token": "abc123"}}), encoding="utf-8"
        )
        assert server._read_web_token() == "abc123"

    def test_read_web_token_missing_file(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(server, "PROJECT_DIR", tmp_path)
        assert server._read_web_token() == ""

    def test_stop_tui_without_lock_file_is_noop(self, tmp_path: Path, monkeypatch):
        fake_lock = tmp_path / "signal-tui.lock"
        monkeypatch.setattr(server, "LOCK_FILE", fake_lock)
        monkeypatch.setattr(server, "_kill_tmux_session", lambda name="tui": None)
        server._stop_tui()  # must not raise

    def test_docker_container_running_false_without_docker(self, monkeypatch):
        monkeypatch.setattr(server, "command_exists", lambda name: False)
        assert server._docker_container_running("signal-tui-whatsapp") is False

    def test_pgrep_false_without_pgrep(self, monkeypatch):
        monkeypatch.setattr(server, "command_exists", lambda name: False)
        assert server._pgrep("anything") is False


# ─── handover.py ─────────────────────────────────────────────────────────────


class TestHandoverConfig:
    def test_defaults_when_no_conf_file_or_env(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(handover, "CONF_FILE", tmp_path / "missing.conf")
        monkeypatch.delenv("HZ_HOST", raising=False)
        monkeypatch.delenv("HZ_USER", raising=False)
        assert handover._hz_host() == "167.233.140.207"
        assert handover._hz_user() == "root"

    def test_env_var_overrides_conf_file(self, tmp_path: Path, monkeypatch):
        conf = tmp_path / "handover.conf"
        conf.write_text("HZ_HOST=10.0.0.1\n", encoding="utf-8")
        monkeypatch.setattr(handover, "CONF_FILE", conf)
        monkeypatch.setenv("HZ_HOST", "10.0.0.2")
        assert handover._hz_host() == "10.0.0.2"

    def test_conf_file_used_when_no_env(self, tmp_path: Path, monkeypatch):
        conf = tmp_path / "handover.conf"
        conf.write_text('HZ_HOST="10.0.0.1"\nHZ_USER=deploy\n', encoding="utf-8")
        monkeypatch.setattr(handover, "CONF_FILE", conf)
        monkeypatch.delenv("HZ_HOST", raising=False)
        monkeypatch.delenv("HZ_USER", raising=False)
        assert handover._hz_host() == "10.0.0.1"
        assert handover._hz_user() == "deploy"

    def test_ssh_base_uses_accept_new_host_key_policy(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setattr(handover, "CONF_FILE", tmp_path / "missing.conf")
        monkeypatch.delenv("HZ_HOST", raising=False)
        monkeypatch.delenv("HZ_USER", raising=False)
        assert handover._ssh_base() == [
            "ssh",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "root@167.233.140.207",
        ]


# ─── install.py: --aliases mode integration ──────────────────────────────────


class TestRunAliasesOnly:
    def test_generates_web_config_and_aliases(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(install, "PROJECT_DIR", tmp_path)
        from launcher import aliases

        monkeypatch.setattr(aliases, "PROJECT_DIR", tmp_path)
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        monkeypatch.setenv("SHELL", "/bin/bash")

        assert install.run_aliases_only() == 0
        config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert config["web"]["enabled"] is True
        assert config["web"]["token"]
        assert (home / ".bashrc").exists()
