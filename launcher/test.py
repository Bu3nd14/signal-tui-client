"""Regression test suite runner (isolated ``.venv-test``).

Ported from ``tests/run_regression_tests.sh``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .common import PROJECT_DIR

VENV_DIR = PROJECT_DIR / ".venv-test"


def _venv_python() -> Path | None:
    candidate = VENV_DIR / "bin" / "python"
    if candidate.is_file():
        return candidate
    candidate = VENV_DIR / "Scripts" / "python.exe"  # Windows venv layout
    return candidate if candidate.is_file() else None


def run() -> int:
    print()
    print("=== Signal TUI Client — Regression Test Suite ===")
    print()
    print(f"Project: {PROJECT_DIR}")
    print()

    if not VENV_DIR.is_dir():
        print("📦 Creazione virtual environment...")
        result = subprocess.run(
            [sys.executable, "-m", "venv", str(VENV_DIR)], check=False
        )
        if result.returncode != 0:
            print("❌ Impossibile creare il virtualenv.")
            return 1
        print(f"   ✅ Creato {VENV_DIR}")

    python_bin = _venv_python()
    if python_bin is None:
        print("❌ Impossibile trovare il Python del virtualenv.")
        return 1

    print("📦 Installazione dipendenze...")
    subprocess.run(
        [str(python_bin), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
        capture_output=True,
        check=False,
    )
    has_pytest = (
        subprocess.run(
            [str(python_bin), "-m", "pip", "show", "pytest"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )
    if not has_pytest:
        subprocess.run(
            [str(python_bin), "-m", "pip", "install", "--quiet", "pytest"],
            capture_output=True,
            check=False,
        )
        print("   ✅ pytest installato")

    requirements = PROJECT_DIR / "requirements.txt"
    if requirements.is_file():
        subprocess.run(
            [
                str(python_bin),
                "-m",
                "pip",
                "install",
                "--quiet",
                "-r",
                str(requirements),
            ],
            capture_output=True,
            check=False,
        )
        print("   ✅ Dipendenze progetto installate")

    print()
    print("🧪 Esecuzione test...")
    print()

    result = subprocess.run(
        [str(python_bin), "-m", "pytest", "tests/", "-v", "--tb=short", "--no-header"],
        cwd=PROJECT_DIR,
        check=False,
    )

    print()
    if result.returncode == 0:
        print("✅ TUTTI I TEST SUPERATI")
        print("   Il client è pronto per il rilascio.")
    else:
        print("❌ QUALCHE TEST È FALLITO")
        print("   Controlla l'output sopra per i dettagli.")
    print()
    return result.returncode
