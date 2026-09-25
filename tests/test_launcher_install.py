"""Regression tests for launcher/install.py — the Python installer.

Replaces the install.sh portion of tests/test_install_script.py. Pure Python
functions are exercised directly (monkeypatching PROJECT_DIR/BIN_DIR and
subprocess/urllib calls) instead of spawning bash with a fake PATH.
"""

from __future__ import annotations

import io
import json
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

from launcher import install


def _fake_signal_cli(bin_dir: Path, version: str) -> Path:
    exe = bin_dir / f"signal-cli-{version}" / "bin" / "signal-cli"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\necho fake-signal-cli\n", encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    (exe.parent.parent / "lib").mkdir(parents=True, exist_ok=True)
    return exe


def _fake_tarball(path: Path, version: str) -> None:
    with tarfile.open(path, "w:gz") as tar:
        bin_info = tarfile.TarInfo(f"signal-cli-{version}/bin/signal-cli")
        bin_data = b"#!/bin/sh\necho fake\n"
        bin_info.size = len(bin_data)
        bin_info.mode = 0o755
        tar.addfile(bin_info, io.BytesIO(bin_data))
        lib_info = tarfile.TarInfo(f"signal-cli-{version}/lib/placeholder.jar")
        lib_data = b"fake-jar"
        lib_info.size = len(lib_data)
        tar.addfile(lib_info, io.BytesIO(lib_data))


@pytest.fixture
def project(tmp_path: Path, monkeypatch):
    """Redirect install.py's PROJECT_DIR/BIN_DIR to an isolated tmp_path."""
    monkeypatch.setattr(install, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(install, "BIN_DIR", tmp_path / "bin")
    return tmp_path


class TestInstalledVersionDetection:
    def test_installed_version_detected(self, project: Path):
        _fake_signal_cli(install.BIN_DIR, "0.14.6")
        assert install.get_installed_version() == "0.14.6"

    def test_no_installed_version(self, project: Path):
        install.BIN_DIR.mkdir()
        assert install.get_installed_version() == ""

    def test_no_bin_dir_at_all(self, project: Path):
        assert install.get_installed_version() == ""


class TestRemoveOldVersions:
    def test_removes_all_but_kept_version(self, project: Path):
        _fake_signal_cli(install.BIN_DIR, "0.14.5")
        _fake_signal_cli(install.BIN_DIR, "0.14.6")
        install.remove_old_versions("0.14.6")
        assert not (install.BIN_DIR / "signal-cli-0.14.5").exists()
        assert (install.BIN_DIR / "signal-cli-0.14.6").exists()

    def test_noop_without_bin_dir(self, project: Path):
        install.remove_old_versions("0.14.6")  # must not raise


class TestVersionLookup:
    def test_get_latest_version_parses_tag(self, project: Path, monkeypatch):
        body = json.dumps({"tag_name": "v0.14.7"}).encode()
        monkeypatch.setattr(install, "http_get", lambda *a, **k: (200, body))
        assert install.get_latest_version() == "0.14.7"

    def test_get_latest_version_dies_on_transport_error(
        self, project: Path, monkeypatch
    ):
        monkeypatch.setattr(install, "http_get", lambda *a, **k: None)
        with pytest.raises(SystemExit):
            install.get_latest_version()

    def test_get_latest_version_dies_on_non_200(self, project: Path, monkeypatch):
        monkeypatch.setattr(install, "http_get", lambda *a, **k: (404, b""))
        with pytest.raises(SystemExit):
            install.get_latest_version()


class TestDownloadSignalCli:
    def test_download_creates_correct_structure(self, project: Path, monkeypatch):
        def fake_urlretrieve(url, dest):
            _fake_tarball(Path(dest), "0.14.7")

        monkeypatch.setattr(install.urllib.request, "urlretrieve", fake_urlretrieve)
        install.download_signal_cli("0.14.7")
        exe = install.BIN_DIR / "signal-cli-0.14.7" / "bin" / "signal-cli"
        assert exe.is_file()
        assert exe.stat().st_mode & stat.S_IEXEC
        assert (install.BIN_DIR / "signal-cli-0.14.7" / "lib").is_dir()

    def test_download_failure_dies(self, project: Path, monkeypatch):
        def fake_urlretrieve(url, dest):
            raise OSError("boom")

        monkeypatch.setattr(install.urllib.request, "urlretrieve", fake_urlretrieve)
        with pytest.raises(SystemExit):
            install.download_signal_cli("0.14.7")

    def test_unexpected_structure_dies(self, project: Path, monkeypatch):
        def fake_urlretrieve(url, dest):
            # Tarball without the expected bin/signal-cli entry.
            with tarfile.open(dest, "w:gz") as tar:
                info = tarfile.TarInfo("signal-cli-0.14.7/README")
                data = b"not the binary"
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

        monkeypatch.setattr(install.urllib.request, "urlretrieve", fake_urlretrieve)
        with pytest.raises(SystemExit):
            install.download_signal_cli("0.14.7")


class TestPrerequisites:
    def test_check_python_ok_for_current_interpreter(self, project: Path):
        install.check_python()  # running interpreter satisfies the repo's own floor

    def test_check_java_missing(self, project: Path, monkeypatch):
        monkeypatch.setattr(install, "command_exists", lambda name: False)
        assert install.check_java() is False

    def test_check_java_too_old_warns_not_dies(self, project: Path, monkeypatch):
        monkeypatch.setattr(install, "command_exists", lambda name: True)

        def fake_run(*a, **k):
            return subprocess.CompletedProcess(
                a[0], 0, stdout='openjdk version "17.0.1" 2021-10-19\n', stderr=""
            )

        monkeypatch.setattr(install.subprocess, "run", fake_run)
        assert install.check_java() is False

    def test_check_java_current_ok(self, project: Path, monkeypatch):
        monkeypatch.setattr(install, "command_exists", lambda name: True)

        def fake_run(*a, **k):
            return subprocess.CompletedProcess(
                a[0], 0, stdout='openjdk version "25.0.1" 2025-04-15\n', stderr=""
            )

        monkeypatch.setattr(install.subprocess, "run", fake_run)
        assert install.check_java() is True


class TestWebConfig:
    def test_creates_enabled_web_config_with_secure_token(self, project: Path):
        install.ensure_web_config()
        config_file = project / "config.json"
        config = json.loads(config_file.read_text(encoding="utf-8"))
        assert config["web"]["enabled"] is True
        assert config["web"]["host"] == "127.0.0.1"
        assert config["web"]["port"] == 4242
        assert len(config["web"]["token"]) >= 32
        assert stat.S_IMODE(config_file.stat().st_mode) == 0o600

    def test_preserves_existing_config_and_is_idempotent(self, project: Path):
        config_file = project / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "user_number": "+391234",
                    "web": {
                        "enabled": False,
                        "host": "0.0.0.0",
                        "port": 5000,
                        "token": "existing-token",
                    },
                }
            ),
            encoding="utf-8",
        )

        install.ensure_web_config()
        first_content = config_file.read_text(encoding="utf-8")
        install.ensure_web_config()

        assert config_file.read_text(encoding="utf-8") == first_content
        config = json.loads(first_content)
        assert config["user_number"] == "+391234"
        assert config["web"] == {
            "enabled": False,
            "host": "0.0.0.0",
            "port": 5000,
            "token": "existing-token",
        }

    def test_rejects_invalid_config_without_overwriting_it(self, project: Path):
        config_file = project / "config.json"
        config_file.write_text("{invalid", encoding="utf-8")

        with pytest.raises(SystemExit):
            install.ensure_web_config()
        assert config_file.read_text(encoding="utf-8") == "{invalid"


class TestInstallPythonDeps:
    def test_no_venv_installs_with_system_python(self, project: Path, monkeypatch):
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(install.subprocess, "run", fake_run)
        result = install.install_python_deps(do_venv=False, do_web=True)

        assert result is True
        joined = [" ".join(c) for c in calls]
        assert any("requirements.txt" in c for c in joined)
        assert any("requirements-web.txt" in c for c in joined)
        assert not (project / ".venv").exists()

    def test_no_web_flag_skips_web_deps(self, project: Path, monkeypatch):
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(install.subprocess, "run", fake_run)
        install.install_python_deps(do_venv=False, do_web=False)

        joined = [" ".join(c) for c in calls]
        assert any("requirements.txt" in c for c in joined)
        assert not any("requirements-web.txt" in c for c in joined)

    def test_web_deps_failure_is_soft(self, project: Path, monkeypatch):
        def fake_run(cmd, **kwargs):
            if "requirements-web.txt" in " ".join(cmd):
                return subprocess.CompletedProcess(cmd, 1)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(install.subprocess, "run", fake_run)
        result = install.install_python_deps(do_venv=False, do_web=True)
        assert result is False  # web disabled, but no exception raised

    def test_requirements_failure_dies(self, project: Path, monkeypatch):
        def fake_run(cmd, **kwargs):
            if "requirements.txt" in " ".join(cmd) and "web" not in " ".join(cmd):
                return subprocess.CompletedProcess(cmd, 1)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(install.subprocess, "run", fake_run)
        with pytest.raises(SystemExit):
            install.install_python_deps(do_venv=False, do_web=True)
