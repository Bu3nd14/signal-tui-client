"""Entry-point dispatch: ``megachat serve`` vs the TUI, ``python -m server``."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import megachat

ROOT = Path(__file__).resolve().parent.parent


def test_serve_dispatches_to_server_main():
    with patch("server.main.serve", return_value=7) as serve:
        rc = megachat.main(["serve", "--no-web", "--debug"])
    assert rc == 7
    serve.assert_called_once_with(["--no-web", "--debug"])


def test_other_args_go_to_the_tui():
    with (
        patch("signal_tui.main", return_value=3) as tui,
        patch("server.main.serve") as serve,
    ):
        rc = megachat.main(["--web", "--web-port", "4242"])
    assert rc == 3
    tui.assert_called_once_with(["--web", "--web-port", "4242"])
    serve.assert_not_called()


def test_no_args_start_the_tui_with_sys_argv(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["megachat", "--debug"])
    with patch("signal_tui.main", return_value=0) as tui:
        assert megachat.main() == 0
    tui.assert_called_once_with(["--debug"])


def test_serve_is_only_the_first_argument():
    with patch("signal_tui.main", return_value=0) as tui:
        megachat.main(["--debug", "serve"])
    tui.assert_called_once_with(["--debug", "serve"])


def test_tui_main_refuses_when_lock_is_held(capsys):
    import signal_tui

    with (
        patch("signal_tui.logging.basicConfig"),
        patch("signal_tui._acquire_lock", return_value=False),
    ):
        assert signal_tui.main([]) == 1
    assert "already running" in capsys.readouterr().err


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_python_m_server_dispatches_to_serve():
    result = _run("-m", "server", "--help")
    assert result.returncode == 0
    assert "--no-web" in result.stdout


def test_python_m_server_rejects_bad_port():
    result = _run("-m", "server", "--web-port", "70000")
    assert result.returncode == 2


def test_megachat_script_serve_help():
    result = _run("megachat.py", "serve", "--help")
    assert result.returncode == 0
    assert "--web-no-auth" in result.stdout


def test_signal_tui_script_and_module_help_still_work():
    for args in (("signal_tui.py", "--help"), ("-m", "signal_tui", "--help")):
        result = _run(*args)
        assert result.returncode == 0, result.stderr
        assert "--web-no-auth" in result.stdout
