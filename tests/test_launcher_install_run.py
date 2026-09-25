"""Coverage for launcher/install.py's run() — the full installer orchestration.

The individual helpers (download_signal_cli, check_java, ensure_web_config,
etc.) are already covered in tests/test_launcher_install.py; here we drive
run(args) end-to-end with every helper mocked, to cover its branch wiring
(--update, --skip-signal-cli, --whatsapp/--check-whatsapp, --aliases, the
final next-steps banner).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from launcher import install


def _args(**overrides):
    defaults = {
        "no_venv": False,
        "skip_signal_cli": False,
        "no_web": False,
        "whatsapp": False,
        "check_whatsapp": False,
        "no_docker_limits": False,
        "version": "",
        "aliases": False,
        "update": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture
def wired(monkeypatch):
    """Stub every side-effecting helper run() calls, tracking what fired."""
    calls: dict[str, object] = {}
    monkeypatch.setattr(install, "command_exists", lambda name: True)
    monkeypatch.setattr(
        install, "check_python", lambda: calls.setdefault("python", True)
    )
    monkeypatch.setattr(install, "check_java", lambda: calls.setdefault("java", True))
    monkeypatch.setattr(install, "get_latest_version", lambda: "0.14.7")
    monkeypatch.setattr(install, "get_installed_version", lambda: "")
    monkeypatch.setattr(
        install, "download_signal_cli", lambda v: calls.setdefault("downloaded", v)
    )
    monkeypatch.setattr(
        install, "remove_old_versions", lambda v: calls.setdefault("removed", v)
    )
    monkeypatch.setattr(install, "install_python_deps", lambda venv, web: web)
    monkeypatch.setattr(
        install, "ensure_web_config", lambda: calls.setdefault("web_config", True)
    )
    monkeypatch.setattr(
        install.whatsapp_mod,
        "setup",
        lambda **k: calls.setdefault("whatsapp_setup", k),
    )
    monkeypatch.setattr(install, "install_aliases", lambda: True)
    return calls


class TestAliasesShortCircuit:
    def test_aliases_flag_only_runs_aliases(self, wired, monkeypatch):
        monkeypatch.setattr(install, "run_aliases_only", lambda: 0)
        assert install.run(_args(aliases=True)) == 0
        assert "python" not in wired  # rest of the installer never ran


class TestMissingTar:
    def test_dies_without_tar(self, wired, monkeypatch):
        monkeypatch.setattr(install, "command_exists", lambda name: False)
        with pytest.raises(SystemExit):
            install.run(_args())


class TestUpdateMode:
    def test_updates_when_newer_version_available(self, wired):
        assert install.run(_args(update=True)) == 0
        assert wired["downloaded"] == "0.14.7"
        assert wired["removed"] == "0.14.7"

    def test_noop_when_already_latest(self, wired, monkeypatch):
        monkeypatch.setattr(install, "get_installed_version", lambda: "0.14.7")
        assert install.run(_args(update=True)) == 0
        assert "downloaded" not in wired


class TestSignalCliInstall:
    def test_skips_signal_cli_download_when_flagged(self, wired, monkeypatch, capsys):
        monkeypatch.setattr(install, "get_installed_version", lambda: "")
        assert install.run(_args(skip_signal_cli=True)) == 0
        assert "downloaded" not in wired
        assert "non funzionerà" in capsys.readouterr().out

    def test_skips_download_when_already_installed_at_same_version(
        self, wired, monkeypatch
    ):
        monkeypatch.setattr(install, "get_installed_version", lambda: "0.14.7")
        assert install.run(_args(version="0.14.7")) == 0
        assert "downloaded" not in wired

    def test_downloads_specified_version(self, wired):
        assert install.run(_args(version="0.14.7")) == 0
        assert wired["downloaded"] == "0.14.7"
        assert wired["removed"] == "0.14.7"


class TestWhatsAppFlags:
    def test_check_whatsapp_calls_setup_without_starting(self, wired):
        install.run(_args(check_whatsapp=True))
        assert wired["whatsapp_setup"] == {
            "should_start": False,
            "docker_limits": True,
        }

    def test_whatsapp_flag_calls_setup_and_starts(self, wired):
        install.run(_args(whatsapp=True, no_docker_limits=True))
        assert wired["whatsapp_setup"] == {
            "should_start": True,
            "docker_limits": False,
        }

    def test_neither_flag_skips_whatsapp_setup(self, wired):
        install.run(_args())
        assert "whatsapp_setup" not in wired


class TestAliasesSoftFailure:
    def test_warns_but_completes_when_aliases_fail(self, wired, monkeypatch, capsys):
        monkeypatch.setattr(install, "install_aliases", lambda: False)
        assert install.run(_args()) == 0
        assert "alias shell non riuscita" in capsys.readouterr().out


class TestFinalBanner:
    def test_suggests_docker_whatsapp_when_docker_present_and_unused(
        self, wired, capsys
    ):
        install.run(_args())
        out = capsys.readouterr().out
        assert "Hai Docker installato" in out
        assert "--whatsapp" in out

    def test_no_docker_suggestion_when_whatsapp_already_requested(self, wired, capsys):
        install.run(_args(whatsapp=True))
        out = capsys.readouterr().out
        assert "Hai Docker installato" not in out

    def test_mentions_whatsapp_pairing_step_when_requested(self, wired, capsys):
        install.run(_args(whatsapp=True))
        out = capsys.readouterr().out
        assert "link_whatsapp.py" in out
