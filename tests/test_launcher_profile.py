"""Coverage for launcher/profile.py's guard clauses and strace log parsing."""

from __future__ import annotations

from launcher import profile


class TestRunPyspy:
    def test_missing_pyspy_returns_1(self, monkeypatch, capsys):
        monkeypatch.setattr(profile, "_pyspy_bin", lambda: None)
        assert profile.run_pyspy(10) == 1
        assert "py-spy is not installed" in capsys.readouterr().out

    def test_missing_app_returns_1(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(profile, "_pyspy_bin", lambda: "/usr/bin/py-spy")
        monkeypatch.setattr(profile, "APP_PATH", tmp_path / "does-not-exist.py")
        assert profile.run_pyspy(10) == 1
        assert "App not found" in capsys.readouterr().out

    def test_app_exits_immediately_returns_1(self, monkeypatch, tmp_path, capsys):
        app = tmp_path / "signal_tui.py"
        app.write_text("", encoding="utf-8")
        monkeypatch.setattr(profile, "_pyspy_bin", lambda: "/usr/bin/py-spy")
        monkeypatch.setattr(profile, "APP_PATH", app)
        monkeypatch.setattr(profile, "OUTPUT_DIR", tmp_path / "output")
        monkeypatch.setattr(profile.time, "sleep", lambda *_: None)

        class _DeadProc:
            pid = 4242

            def poll(self):
                return 1

        monkeypatch.setattr(profile.subprocess, "Popen", lambda *a, **k: _DeadProc())
        assert profile.run_pyspy(10) == 1
        assert "failed to start" in capsys.readouterr().out


class TestRunStrace:
    def test_missing_strace_returns_1(self, monkeypatch, capsys):
        monkeypatch.setattr(profile.shutil, "which", lambda name: None)
        assert profile.run_strace(10) == 1
        assert "strace is not installed" in capsys.readouterr().out

    def test_missing_timeout_returns_1(self, monkeypatch, capsys):
        monkeypatch.setattr(
            profile.shutil,
            "which",
            lambda name: "/usr/bin/strace" if name == "strace" else None,
        )
        assert profile.run_strace(10) == 1
        assert "'timeout'" in capsys.readouterr().out

    def test_missing_app_returns_1(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(profile.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(profile, "APP_PATH", tmp_path / "missing.py")
        assert profile.run_strace(10) == 1
        assert "App not found" in capsys.readouterr().out

    def test_summary_written_with_file_and_syscall_counts(
        self, monkeypatch, tmp_path, capsys
    ):
        app = tmp_path / "signal_tui.py"
        app.write_text("", encoding="utf-8")
        output_dir = tmp_path / "output"
        monkeypatch.setattr(profile.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(profile, "APP_PATH", app)
        monkeypatch.setattr(profile, "OUTPUT_DIR", output_dir)
        monkeypatch.setattr(profile.time, "sleep", lambda *_: None)

        strace_log_content = (
            'openat(AT_FDCWD, "/etc/passwd", O_RDONLY) = 3\n'
            'openat(AT_FDCWD, "/etc/passwd", O_RDONLY) = 3\n'
            "% time     seconds  usecs/call     calls    errors syscall\n"
            "------ ----------- ----------- --------- --------- ----------------\n"
            " 50.00    0.001000         500         2           read\n"
        )

        class _FakeProc:
            def __init__(self):
                self.pid = 999

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                pass

        def _fake_popen(cmd, **kwargs):
            if cmd[0] == "strace":
                (output_dir).mkdir(parents=True, exist_ok=True)
                (output_dir / "strace.log").write_text(
                    strace_log_content, encoding="utf-8"
                )
            return _FakeProc()

        monkeypatch.setattr(profile.subprocess, "Popen", _fake_popen)

        assert profile.run_strace(5) == 0
        summary = (output_dir / "strace_summary.txt").read_text(encoding="utf-8")
        assert "/etc/passwd" in summary
        assert "2 /etc/passwd" in summary
        assert "SYSCALL SUMMARY" in summary
        out = capsys.readouterr().out
        assert "Summary saved to" in out
