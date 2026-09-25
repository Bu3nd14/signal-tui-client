"""In-process coverage for launcher.py's CLI dispatch (``main``/``build_parser``).

``launcher.py`` (top-level script) and the ``launcher/`` package share the
same import name — Python resolves ``import launcher`` to the *package*, so
the only way to exercise the script's own code (not just the package it
delegates to) is to load it directly by file path via ``importlib``, the
same way ``python3 launcher.py ...`` does when run as a script.

tests/test_launcher_cli.py already covers argparse wiring via subprocess
(--help, missing subcommands, etc.) but that runs launcher.py in a *separate*
process, which coverage.py doesn't track — hence launcher.py showing 0%
coverage despite being fully argparse-tested. These tests load it in-process
instead and monkeypatch every underlying command function, so both the
argparse wiring AND the dispatch branches in main() are actually measured.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_launcher_script():
    spec = importlib.util.spec_from_file_location(
        "launcher_cli_script", PROJECT_ROOT / "launcher.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def launcher_cli():
    return _load_launcher_script()


class TestBuildParser:
    def test_parses_install_flags(self, launcher_cli):
        parser = launcher_cli.build_parser()
        args = parser.parse_args(
            ["install", "--no-venv", "--version", "1.2.3", "--whatsapp"]
        )
        assert args.command == "install"
        assert args.no_venv is True
        assert args.version == "1.2.3"
        assert args.whatsapp is True

    def test_parses_whatsapp_start_with_no_docker_limits(self, launcher_cli):
        parser = launcher_cli.build_parser()
        args = parser.parse_args(["whatsapp", "start", "--no-docker-limits"])
        assert args.command == "whatsapp"
        assert args.wa_command == "start"
        assert args.no_docker_limits is True

    def test_parses_profile_pyspy_duration(self, launcher_cli):
        parser = launcher_cli.build_parser()
        args = parser.parse_args(["profile", "pyspy", "30"])
        assert args.profile_command == "pyspy"
        assert args.duration == 30

    def test_profile_duration_defaults_to_120(self, launcher_cli):
        parser = launcher_cli.build_parser()
        args = parser.parse_args(["profile", "strace"])
        assert args.duration == 120


class TestMainDispatch:
    def test_install_dispatches_to_install_run(self, launcher_cli, monkeypatch):
        calls = []
        monkeypatch.setattr(
            launcher_cli.install, "run", lambda args: calls.append(args) or 0
        )
        assert launcher_cli.main(["install"]) == 0
        assert len(calls) == 1

    def test_aliases_dispatches_to_run_aliases_only(self, launcher_cli, monkeypatch):
        monkeypatch.setattr(launcher_cli.install, "run_aliases_only", lambda: 0)
        assert launcher_cli.main(["aliases"]) == 0

    def test_whatsapp_start_dispatches_with_docker_limits(
        self, launcher_cli, monkeypatch
    ):
        calls = []
        monkeypatch.setattr(
            launcher_cli.whatsapp,
            "start",
            lambda **kwargs: calls.append(kwargs) or 0,
        )
        assert launcher_cli.main(["whatsapp", "start"]) == 0
        assert calls == [{"no_wait": False, "docker_limits": True}]

    def test_whatsapp_start_no_docker_limits_flag(self, launcher_cli, monkeypatch):
        calls = []
        monkeypatch.setattr(
            launcher_cli.whatsapp,
            "start",
            lambda **kwargs: calls.append(kwargs) or 0,
        )
        launcher_cli.main(["whatsapp", "start", "--no-docker-limits", "--no-wait"])
        assert calls == [{"no_wait": True, "docker_limits": False}]

    def test_whatsapp_stop_dispatches(self, launcher_cli, monkeypatch):
        monkeypatch.setattr(launcher_cli.whatsapp, "stop", lambda: 0)
        assert launcher_cli.main(["whatsapp", "stop"]) == 0

    def test_backend_restart_dispatches(self, launcher_cli, monkeypatch):
        calls = []
        monkeypatch.setattr(
            launcher_cli.backend, "run", lambda **kwargs: calls.append(kwargs) or 0
        )
        launcher_cli.main(["backend-restart", "--no-wait"])
        assert calls == [{"no_wait": True, "docker_limits": True}]

    def test_server_start_dispatches(self, launcher_cli, monkeypatch):
        calls = []
        monkeypatch.setattr(
            launcher_cli.server,
            "start_all",
            lambda **kwargs: calls.append(kwargs) or 0,
        )
        launcher_cli.main(["server", "start"])
        assert calls == [{"docker_limits": True}]

    def test_server_stop_dispatches(self, launcher_cli, monkeypatch):
        monkeypatch.setattr(launcher_cli.server, "stop_all", lambda: 0)
        assert launcher_cli.main(["server", "stop"]) == 0

    def test_server_status_dispatches(self, launcher_cli, monkeypatch):
        monkeypatch.setattr(launcher_cli.server, "status", lambda: 0)
        assert launcher_cli.main(["server", "status"]) == 0

    def test_handover_to_server_dispatches(self, launcher_cli, monkeypatch):
        calls = []
        monkeypatch.setattr(
            launcher_cli.handover,
            "to_server",
            lambda **kwargs: calls.append(kwargs) or 0,
        )
        launcher_cli.main(["handover", "to-server", "--no-docker-limits"])
        assert calls == [{"docker_limits": False}]

    def test_handover_to_local_dispatches(self, launcher_cli, monkeypatch):
        calls = []
        monkeypatch.setattr(
            launcher_cli.handover,
            "to_local",
            lambda **kwargs: calls.append(kwargs) or 0,
        )
        launcher_cli.main(["handover", "to-local"])
        assert calls == [{"docker_limits": True}]

    def test_handover_status_dispatches(self, launcher_cli, monkeypatch):
        monkeypatch.setattr(launcher_cli.handover, "status", lambda: 0)
        assert launcher_cli.main(["handover", "status"]) == 0

    def test_profile_pyspy_dispatches_with_duration(self, launcher_cli, monkeypatch):
        calls = []
        monkeypatch.setattr(
            launcher_cli.profile_mod,
            "run_pyspy",
            lambda duration: calls.append(duration) or 0,
        )
        launcher_cli.main(["profile", "pyspy", "45"])
        assert calls == [45]

    def test_profile_strace_dispatches_with_duration(self, launcher_cli, monkeypatch):
        calls = []
        monkeypatch.setattr(
            launcher_cli.profile_mod,
            "run_strace",
            lambda duration: calls.append(duration) or 0,
        )
        launcher_cli.main(["profile", "strace"])
        assert calls == [120]

    def test_test_command_dispatches(self, launcher_cli, monkeypatch):
        monkeypatch.setattr(launcher_cli.test, "run", lambda: 0)
        assert launcher_cli.main(["test"]) == 0

    def test_propagates_nonzero_return_codes(self, launcher_cli, monkeypatch):
        monkeypatch.setattr(launcher_cli.whatsapp, "stop", lambda: 1)
        assert launcher_cli.main(["whatsapp", "stop"]) == 1
