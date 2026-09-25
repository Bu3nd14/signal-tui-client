"""Coverage for launcher/test.py's regression-suite runner."""

from __future__ import annotations

import subprocess

from launcher import test as launcher_test


class TestVenvPython:
    def test_none_when_venv_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(launcher_test, "VENV_DIR", tmp_path / "no-venv")
        assert launcher_test._venv_python() is None

    def test_finds_unix_layout(self, tmp_path, monkeypatch):
        venv = tmp_path / ".venv-test"
        python_bin = venv / "bin" / "python"
        python_bin.parent.mkdir(parents=True)
        python_bin.write_text("", encoding="utf-8")
        monkeypatch.setattr(launcher_test, "VENV_DIR", venv)
        assert launcher_test._venv_python() == python_bin


class TestRun:
    def test_venv_creation_failure_returns_1(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(launcher_test, "PROJECT_DIR", tmp_path)
        monkeypatch.setattr(launcher_test, "VENV_DIR", tmp_path / ".venv-test")

        def _fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1)

        monkeypatch.setattr(launcher_test.subprocess, "run", _fake_run)
        assert launcher_test.run() == 1
        assert "Impossibile creare il virtualenv" in capsys.readouterr().out

    def test_missing_python_after_creation_returns_1(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(launcher_test, "PROJECT_DIR", tmp_path)
        monkeypatch.setattr(launcher_test, "VENV_DIR", tmp_path / ".venv-test")

        def _fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(launcher_test.subprocess, "run", _fake_run)
        monkeypatch.setattr(launcher_test, "_venv_python", lambda: None)
        assert launcher_test.run() == 1
        assert "Impossibile trovare il Python" in capsys.readouterr().out

    def test_happy_path_runs_pytest_and_returns_its_code(
        self, tmp_path, monkeypatch, capsys
    ):
        venv = tmp_path / ".venv-test"
        (venv / "bin").mkdir(parents=True)
        python_bin = venv / "bin" / "python"
        python_bin.write_text("", encoding="utf-8")
        monkeypatch.setattr(launcher_test, "PROJECT_DIR", tmp_path)
        monkeypatch.setattr(launcher_test, "VENV_DIR", venv)
        (tmp_path / "requirements.txt").write_text("", encoding="utf-8")

        calls = []

        def _fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:3] == [str(python_bin), "-m", "pytest"]:
                return subprocess.CompletedProcess(cmd, 0)
            if cmd[2:5] == ["pip", "show", "pytest"]:
                return subprocess.CompletedProcess(cmd, 0)  # already installed
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(launcher_test.subprocess, "run", _fake_run)
        assert launcher_test.run() == 0
        out = capsys.readouterr().out
        assert "TUTTI I TEST SUPERATI" in out
        assert any(c[:3] == [str(python_bin), "-m", "pytest"] for c in calls)

    def test_pytest_failure_propagates_return_code(self, tmp_path, monkeypatch, capsys):
        venv = tmp_path / ".venv-test"
        (venv / "bin").mkdir(parents=True)
        python_bin = venv / "bin" / "python"
        python_bin.write_text("", encoding="utf-8")
        monkeypatch.setattr(launcher_test, "PROJECT_DIR", tmp_path)
        monkeypatch.setattr(launcher_test, "VENV_DIR", venv)

        def _fake_run(cmd, **kwargs):
            if cmd[:3] == [str(python_bin), "-m", "pytest"]:
                return subprocess.CompletedProcess(cmd, 1)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(launcher_test.subprocess, "run", _fake_run)
        assert launcher_test.run() == 1
        assert "QUALCHE TEST" in capsys.readouterr().out
