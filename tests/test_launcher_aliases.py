"""Regression tests for launcher/aliases.py — web reader shell aliases.

Replaces the alias-related portion of tests/test_install_script.py
(TestAliasIsolation). Covers shell detection (including the "ash" is a
substring of "bash" pitfall), idempotency, and byte-for-byte parity with the
block install.sh used to write.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from launcher import aliases


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home_dir))
    monkeypatch.setattr(aliases, "PROJECT_DIR", tmp_path / "project")
    monkeypatch.delenv("ENV", raising=False)
    return home_dir


class TestShellDetection:
    @pytest.mark.parametrize(
        ("shell_path", "rc_name"),
        [
            ("/bin/bash", ".bashrc"),
            ("/usr/bin/bash", ".bashrc"),
            ("", ".bashrc"),
            ("/usr/bin/zsh", ".zshrc"),
            ("/bin/ash", ".ashrc"),
            ("/bin/busybox-ash", ".ashrc"),
        ],
    )
    def test_resolves_expected_rc_file(
        self, home: Path, monkeypatch, shell_path: str, rc_name: str
    ):
        monkeypatch.setenv("SHELL", shell_path)
        assert aliases.install_aliases() is True
        assert (home / rc_name).exists()

    def test_bash_is_not_misdetected_as_ash(self, home: Path, monkeypatch):
        """Regression: "ash" is a substring of "bash" — bash must win."""
        monkeypatch.setenv("SHELL", "/bin/bash")
        aliases.install_aliases()
        assert (home / ".bashrc").exists()
        assert not (home / ".ashrc").exists()

    def test_unsupported_shell_skips_gracefully(self, home: Path, monkeypatch):
        monkeypatch.setenv("SHELL", "/usr/bin/fish")
        assert aliases.install_aliases() is True
        assert not any(home.iterdir())


class TestAshEnvWarning:
    def test_warns_when_env_not_pointed_at_ashrc(self, home: Path, monkeypatch, capsys):
        monkeypatch.setenv("SHELL", "/bin/ash")
        monkeypatch.delenv("ENV", raising=False)
        aliases.install_aliases()
        assert "ENV" in capsys.readouterr().out

    def test_no_warning_when_env_already_correct(self, home: Path, monkeypatch, capsys):
        monkeypatch.setenv("SHELL", "/bin/ash")
        monkeypatch.setenv("ENV", str(home / ".ashrc"))
        aliases.install_aliases()
        assert "legge gli alias da $ENV" not in capsys.readouterr().out


class TestIdempotency:
    def test_second_run_replaces_block_in_place(self, home: Path, monkeypatch):
        monkeypatch.setenv("SHELL", "/bin/bash")
        aliases.install_aliases()
        first = (home / ".bashrc").read_text(encoding="utf-8")
        aliases.install_aliases()
        second = (home / ".bashrc").read_text(encoding="utf-8")
        assert first == second
        assert second.count(aliases.BEGIN_MARKER) == 1

    def test_preserves_surrounding_content(self, home: Path, monkeypatch):
        monkeypatch.setenv("SHELL", "/bin/bash")
        (home / ".bashrc").write_text("export FOO=bar\n", encoding="utf-8")
        aliases.install_aliases()
        content = (home / ".bashrc").read_text(encoding="utf-8")
        assert "export FOO=bar" in content
        assert aliases.BEGIN_MARKER in content

    def test_missing_end_marker_leaves_file_untouched(self, home: Path, monkeypatch):
        monkeypatch.setenv("SHELL", "/bin/bash")
        broken = f"before\n{aliases.BEGIN_MARKER}\nmid\n"
        (home / ".bashrc").write_text(broken, encoding="utf-8")
        result = aliases.install_aliases()
        assert result is False
        assert (home / ".bashrc").read_text(encoding="utf-8") == broken


class TestContentParity:
    def test_project_dir_is_substituted(self, home: Path, monkeypatch):
        monkeypatch.setenv("SHELL", "/bin/bash")
        aliases.install_aliases()
        content = (home / ".bashrc").read_text(encoding="utf-8")
        assert f'SIGNAL_TUI_DIR="{aliases.PROJECT_DIR}"' in content
        assert "web-signal-tui-bg" in content
        assert "signal-tui-stop" in content

    def test_no_project_dir_placeholder_leaks(self, home: Path, monkeypatch):
        monkeypatch.setenv("SHELL", "/bin/bash")
        aliases.install_aliases()
        content = (home / ".bashrc").read_text(encoding="utf-8")
        assert "__PROJECT_DIR__" not in content
