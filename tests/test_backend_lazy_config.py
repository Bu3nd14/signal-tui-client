"""
Regression tests for lazy configuration in ``protocols.rpc``.

Ensures ``protocols.rpc`` (and therefore ``protocols``) imports cleanly without
``config.json`` or ``bin/`` present, and that the canonical RuntimeError /
FileNotFoundError are raised only at the point of use.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

import protocols.rpc as backend_rpc
from protocols import SignalBackend


def test_import_clean_without_config_in_ci_environment(tmp_path: Path) -> None:
    """Importing protocols.rpc in a clean environment never raises.

    Copies the ``protocols/`` package and its local module dependencies into a
    temp dir (leaving out ``config.json`` and ``bin/``) and runs a fresh
    interpreter with ``PYTHONPATH`` pointing at that copy. This mirrors CI,
    where the real project root's ``config.json`` and ``bin/`` do not exist,
    and asserts the import-time defaults:
    ``USER_NUMBER == ""`` and ``SIGNAL_CLI_PATH is None``.
    """
    shutil.copytree(
        PROJECT_ROOT / "protocols",
        tmp_path / "protocols",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copy2(PROJECT_ROOT / "filename_utils.py", tmp_path)
    shutil.copy2(PROJECT_ROOT / "models.py", tmp_path)

    code = (
        "import protocols.rpc as r; print(repr(r.USER_NUMBER), repr(r.SIGNAL_CLI_PATH))"
    )
    env = {k: v for k, v in os.environ.items() if k != "SIGNAL_USER_NUMBER"}
    env["PYTHONPATH"] = str(tmp_path)

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "'' None" in result.stdout, result.stdout
    assert "Traceback" not in result.stderr


def test_get_user_number_best_effort_and_require(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_get_user_number is best-effort; _require_user_number raises lazily."""
    monkeypatch.delenv("SIGNAL_USER_NUMBER", raising=False)
    monkeypatch.setattr(backend_rpc, "PROJECT_DIR", tmp_path)

    assert backend_rpc._get_user_number() == ""
    with pytest.raises(RuntimeError, match="Signal phone number not configured"):
        backend_rpc._require_user_number()


def test_find_signal_cli_best_effort_and_find(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_find_signal_cli is best-effort; find_signal_cli raises lazily."""
    monkeypatch.setattr(backend_rpc, "PROJECT_DIR", tmp_path)

    assert backend_rpc._find_signal_cli() is None
    with pytest.raises(FileNotFoundError, match="signal-cli not found"):
        backend_rpc.find_signal_cli()


def test_signal_backend_instantiates_without_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Instantiating SignalBackend never touches config; connecting does."""
    monkeypatch.delenv("SIGNAL_USER_NUMBER", raising=False)
    monkeypatch.setattr(backend_rpc, "PROJECT_DIR", tmp_path)

    # Instantiating (even with the import-time default) must not raise.
    SignalBackend()

    # With an explicitly empty number (the value USER_NUMBER takes when no
    # config is present), connecting must raise the canonical RuntimeError.
    with pytest.raises(RuntimeError, match="not configured"):
        SignalBackend(user_number="")._connect_sync()


def test_run_subprocess_raises_lazy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_run_subprocess raises RuntimeError first, then FileNotFoundError."""
    monkeypatch.delenv("SIGNAL_USER_NUMBER", raising=False)
    monkeypatch.setattr(backend_rpc, "PROJECT_DIR", tmp_path)

    with pytest.raises(RuntimeError):
        backend_rpc._run_subprocess(["listContacts"])

    monkeypatch.setattr(backend_rpc, "_require_user_number", lambda: "+39")
    with pytest.raises(FileNotFoundError):
        backend_rpc._run_subprocess(["listContacts"])
