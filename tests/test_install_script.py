"""
Regression tests for install.sh — the automatic installation script.

The script is a bash script, so these tests execute it as a subprocess in an
isolated temporary directory (tmp_path). Network calls (curl/wget) and system
commands (python3, java, pip) are mocked with stub executables placed in a fake
PATH, so no real downloads or installs happen.
"""

from __future__ import annotations

import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SCRIPT = PROJECT_ROOT / "install.sh"
REAL_PYTHON = sys.executable


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _make_executable(path: Path) -> None:
    """Make a file executable."""
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _write_stub(path: Path, content: str) -> None:
    """Write an executable stub script."""
    path.write_text(content, encoding="utf-8")
    _make_executable(path)


def _create_fake_signal_cli(bin_dir: Path, version: str) -> Path:
    """Create a fake signal-cli install with the expected structure.

    Returns the path to the fake executable.
    """
    exe = bin_dir / f"signal-cli-{version}" / "bin" / "signal-cli"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\necho fake-signal-cli\n", encoding="utf-8")
    _make_executable(exe)
    # Add a lib/ dir to mimic the JVM build
    (exe.parent.parent / "lib").mkdir(parents=True, exist_ok=True)
    return exe


def _create_fake_tarball(path: Path, version: str) -> None:
    """Create a fake signal-cli tarball with the expected structure."""
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


def _extract_function(script: Path, func_name: str) -> str:
    """Extract a single bash function definition from install.sh.

    Returns the function body (without the surrounding main script), so it can
    be sourced without executing the whole installer.
    """
    lines = script.read_text(encoding="utf-8").splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith(f"{func_name}()"):
            start = i
            break
    if start is None:
        raise ValueError(f"Function {func_name} not found")
    # Find the closing brace at column 0
    for i in range(start, len(lines)):
        if lines[i].strip() == "}":
            return "\n".join(lines[start : i + 1])
    raise ValueError(f"Function {func_name} not closed")


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    """Create a fake bin/ directory with a stub signal-cli install."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _create_fake_signal_cli(bin_dir, "0.14.6")
    return bin_dir


@pytest.fixture
def fake_path(tmp_path: Path, monkeypatch) -> Path:
    """Create a fake PATH with stub executables for curl, wget, python3, java, pip.

    Returns the fake bin directory.
    """
    fake_bin_dir = tmp_path / "fakebin"
    fake_bin_dir.mkdir()

    # curl stub: if the URL contains "releases/latest", print the latest version;
    # otherwise download a fake tarball using the real python.
    curl_stub = f"""#!/bin/sh
if echo "$*" | grep -q "releases/latest"; then
    echo '{{"tag_name": "v0.14.7"}}'
    exit 0
fi
out=""
prev=""
for a in "$@"; do
    if [ "$prev" = "-o" ]; then out="$a"; fi
    prev="$a"
done
if [ -n "$out" ]; then
    version="$(echo "$out" | sed 's/.*signal-cli-\\([0-9.]*\\)\\.tar\\.gz/\\1/')"
    {REAL_PYTHON} -c "import sys; sys.path.insert(0, '{tmp_path}'); from test_helpers import make_tarball; make_tarball('$out', '$version')"
    exit 0
fi
exit 1
"""
    _write_stub(fake_bin_dir / "curl", curl_stub)

    # wget stub: same behaviour as curl.
    wget_stub = f"""#!/bin/sh
if echo "$*" | grep -q "releases/latest"; then
    echo '{{"tag_name": "v0.14.7"}}'
    exit 0
fi
out=""
prev=""
for a in "$@"; do
    if [ "$prev" = "-O" ]; then out="$a"; fi
    prev="$a"
done
if [ -n "$out" ]; then
    version="$(echo "$out" | sed 's/.*signal-cli-\\([0-9.]*\\)\\.tar\\.gz/\\1/')"
    {REAL_PYTHON} -c "import sys; sys.path.insert(0, '{tmp_path}'); from test_helpers import make_tarball; make_tarball('$out', '$version')"
    exit 0
fi
exit 1
"""
    _write_stub(fake_bin_dir / "wget", wget_stub)

    # python3 stub: supports --version, -c (version detection), -m venv, -m pip.
    python_stub = """#!/bin/sh
if [ "$1" = "--version" ]; then
    echo "Python 3.12.3"
    exit 0
fi
if [ "$1" = "-c" ]; then
    echo "3.12"
    exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
    mkdir -p "$3/bin"
    touch "$3/bin/pip" "$3/bin/python"
    chmod +x "$3/bin/pip" "$3/bin/python"
    exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then
    exit 0
fi
exit 0
"""
    _write_stub(fake_bin_dir / "python3", python_stub)

    # java stub: reports Java 25.
    java_stub = """#!/bin/sh
echo 'openjdk version "25.0.1" 2025-04-15'
exit 0
"""
    _write_stub(fake_bin_dir / "java", java_stub)

    # pip stub: no-op.
    pip_stub = """#!/bin/sh
exit 0
"""
    _write_stub(fake_bin_dir / "pip", pip_stub)
    _write_stub(fake_bin_dir / "pip3", pip_stub)

    # tar is real (needed to extract the fake tarball).
    monkeypatch.setenv("PATH", f"{fake_bin_dir}:{os.environ['PATH']}")
    return fake_bin_dir


@pytest.fixture
def test_helpers(tmp_path: Path) -> Path:
    """Create a test_helpers.py module in tmp_path for the curl/wget stubs."""
    helpers = tmp_path / "test_helpers.py"
    helpers.write_text(
        "import tarfile, io\n"
        "def make_tarball(path, version):\n"
        "    with tarfile.open(path, 'w:gz') as tar:\n"
        "        bin_info = tarfile.TarInfo(f'signal-cli-{version}/bin/signal-cli')\n"
        "        data = b'#!/bin/sh\\necho fake\\n'\n"
        "        bin_info.size = len(data)\n"
        "        bin_info.mode = 0o755\n"
        "        tar.addfile(bin_info, io.BytesIO(data))\n"
        "        lib_info = tarfile.TarInfo(f'signal-cli-{version}/lib/placeholder.jar')\n"
        "        lib_data = b'fake-jar'\n"
        "        lib_info.size = len(lib_data)\n"
        "        tar.addfile(lib_info, io.BytesIO(lib_data))\n",
        encoding="utf-8",
    )
    return helpers


def _run_install(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    """Copy install.sh into tmp_path and run it with the given args."""
    script = tmp_path / "install.sh"
    shutil.copy(INSTALL_SCRIPT, script)
    _make_executable(script)
    return subprocess.run(  # noqa: PLW1510 — return code inspected by each test
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=60,
    )


# ─── Tests ────────────────────────────────────────────────────────────────────


class TestInstallScriptHelp:
    """📖 Help e parsing argomenti."""

    def test_help_flag(self, tmp_path: Path):
        """--help mostra l'uso ed esce con 0."""
        result = _run_install(tmp_path, "--help")
        assert result.returncode == 0
        assert "install.sh" in result.stdout
        assert "--update" in result.stdout

    def test_unknown_flag(self, tmp_path: Path):
        """Flag sconosciuto → errore, exit ≠ 0."""
        result = _run_install(tmp_path, "--bogus")
        assert result.returncode != 0
        assert "Opzione sconosciuta" in result.stderr

    def test_missing_version_arg(self, tmp_path: Path):
        """--version senza argomento → errore."""
        result = _run_install(tmp_path, "--version")
        assert result.returncode != 0
        assert "--version richiede" in result.stderr


class TestInstalledVersionDetection:
    """🔍 Rilevamento versione installata."""

    def test_installed_version_detected(self, tmp_path: Path, fake_bin: Path):
        """Rileva la versione da bin/signal-cli-*/."""
        func = _extract_function(INSTALL_SCRIPT, "get_installed_version")
        script = tmp_path / "detect.sh"
        script.write_text(
            f"{func}\nBIN_DIR='{fake_bin}'\necho \"$(get_installed_version)\"\n",
            encoding="utf-8",
        )
        result = subprocess.run(  # noqa: PLW1510 — return code asserted below
            ["bash", str(script)], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "0.14.6"

    def test_no_installed_version(self, tmp_path: Path):
        """Nessuna versione in bin/ → stringa vuota."""
        empty_bin = tmp_path / "bin"
        empty_bin.mkdir()
        func = _extract_function(INSTALL_SCRIPT, "get_installed_version")
        script = tmp_path / "detect.sh"
        script.write_text(
            f"{func}\nBIN_DIR='{empty_bin}'\necho \"[$(get_installed_version)]\"\n",
            encoding="utf-8",
        )
        result = subprocess.run(  # noqa: PLW1510 — return code asserted below
            ["bash", str(script)], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "[]"


class TestUpdateFlag:
    """🔄 Aggiornamento signal-cli."""

    def test_update_already_latest(
        self, tmp_path: Path, fake_path: Path, test_helpers: Path
    ):
        """--update con versione già aggiornata → nessun download."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _create_fake_signal_cli(bin_dir, "0.14.7")

        result = _run_install(tmp_path, "--update")
        assert result.returncode == 0
        assert "già all'ultima versione" in result.stdout
        assert "Scaricamento signal-cli" not in result.stdout

    def test_update_downloads_new(
        self, tmp_path: Path, fake_path: Path, test_helpers: Path
    ):
        """--update con versione vecchia → scarica la nuova e rimuove la vecchia."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _create_fake_signal_cli(bin_dir, "0.14.6")

        result = _run_install(tmp_path, "--update")
        assert result.returncode == 0
        assert "Scaricamento signal-cli v0.14.7" in result.stdout
        assert "Rimozione vecchia versione" in result.stdout
        assert (bin_dir / "signal-cli-0.14.6").exists() is False
        assert (bin_dir / "signal-cli-0.14.7").exists() is True


class TestSkipSignalCli:
    """⏭️ Flag --skip-signal-cli."""

    def test_skip_no_version(self, tmp_path: Path, fake_path: Path, test_helpers: Path):
        """--skip-signal-cli senza versioni → warning."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        result = _run_install(tmp_path, "--skip-signal-cli", "--no-venv")
        assert result.returncode == 0
        assert "Nessuna versione di signal-cli trovata" in result.stdout

    def test_skip_with_version(
        self, tmp_path: Path, fake_path: Path, test_helpers: Path
    ):
        """--skip-signal-cli con versione presente → OK."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _create_fake_signal_cli(bin_dir, "0.14.6")
        result = _run_install(tmp_path, "--skip-signal-cli", "--no-venv")
        assert result.returncode == 0
        assert "signal-cli v0.14.6 trovato" in result.stdout


class TestDownload:
    """⬇️ Download di signal-cli."""

    def test_download_specific_version(
        self, tmp_path: Path, fake_path: Path, test_helpers: Path
    ):
        """--version X.Y.Z → scarica quella versione specifica."""
        result = _run_install(tmp_path, "--version", "0.14.7", "--no-venv")
        assert result.returncode == 0
        assert "Scaricamento signal-cli v0.14.7" in result.stdout
        assert (tmp_path / "bin" / "signal-cli-0.14.7" / "bin" / "signal-cli").exists()

    def test_download_creates_correct_structure(
        self, tmp_path: Path, fake_path: Path, test_helpers: Path
    ):
        """Dopo il download, la struttura bin/signal-cli-*/bin/signal-cli è corretta."""
        result = _run_install(tmp_path, "--version", "0.14.7", "--no-venv")
        assert result.returncode == 0
        exe = tmp_path / "bin" / "signal-cli-0.14.7" / "bin" / "signal-cli"
        assert exe.exists()
        assert os.access(exe, os.X_OK)
        assert (tmp_path / "bin" / "signal-cli-0.14.7" / "lib").is_dir()

    def test_remove_old_versions(
        self, tmp_path: Path, fake_path: Path, test_helpers: Path
    ):
        """Le versioni vecchie vengono rimosse dopo il download."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _create_fake_signal_cli(bin_dir, "0.14.5")
        result = _run_install(tmp_path, "--version", "0.14.7", "--no-venv")
        assert result.returncode == 0
        assert (bin_dir / "signal-cli-0.14.5").exists() is False
        assert (bin_dir / "signal-cli-0.14.7").exists() is True


class TestPrerequisites:
    """🧪 Verifica prerequisiti."""

    def test_python_version_check(self, tmp_path: Path, fake_path: Path):
        """Python troppo vecchio → errore."""
        fake_bin = fake_path
        python_stub = """#!/bin/sh
if [ "$1" = "-c" ]; then
    echo "3.8"
    exit 0
fi
exit 0
"""
        (fake_bin / "python3").write_text(python_stub, encoding="utf-8")
        _make_executable(fake_bin / "python3")

        result = _run_install(tmp_path, "--skip-signal-cli", "--no-venv")
        assert result.returncode != 0
        assert "Python 3.10+ richiesto" in result.stderr

    def test_java_old_warns(self, tmp_path: Path, fake_path: Path):
        """Java troppo vecchio → warning ma non blocca."""
        fake_bin = fake_path
        java_stub = """#!/bin/sh
echo 'openjdk version "17.0.1" 2021-10-19'
exit 0
"""
        (fake_bin / "java").write_text(java_stub, encoding="utf-8")
        _make_executable(fake_bin / "java")

        result = _run_install(tmp_path, "--skip-signal-cli", "--no-venv")
        assert result.returncode == 0
        assert "Java 17 trovato" in result.stdout
        assert "richiede Java 25" in result.stdout


class TestVenv:
    """📦 Gestione virtualenv."""

    def test_venv_created(self, tmp_path: Path, fake_path: Path, test_helpers: Path):
        """Crea .venv quando DO_VENV=1."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _create_fake_signal_cli(bin_dir, "0.14.6")
        result = _run_install(tmp_path, "--skip-signal-cli")
        assert result.returncode == 0
        assert (tmp_path / ".venv").is_dir()

    def test_no_venv_flag(self, tmp_path: Path, fake_path: Path, test_helpers: Path):
        """--no-venv non crea .venv."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _create_fake_signal_cli(bin_dir, "0.14.6")
        result = _run_install(tmp_path, "--skip-signal-cli", "--no-venv")
        assert result.returncode == 0
        assert (tmp_path / ".venv").exists() is False
