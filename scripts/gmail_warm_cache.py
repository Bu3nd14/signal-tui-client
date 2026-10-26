#!/usr/bin/env python3
"""Scalda la cache gpg-agent del segreto Gmail prima di avviare il server.

Se il segreto è configurato come comando (``GMAIL_APP_PASSWORD_CMD``) e la
cache gpg-agent è fredda, il comando apre pinentry sul terminale dell'utente
e attende la passphrase senza timeout.  Qualsiasi errore è non fatale: si
stampa un avviso su stderr e si restituisce sempre ``0``.

    python3 scripts/gmail_warm_cache.py
"""

from __future__ import annotations

import contextlib
import os
import shlex
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


def _pil_available() -> bool:
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


def _venv_python() -> Path | None:
    """Path del python del venv di progetto, se esiste ed è un altro interprete."""
    candidate = PROJECT_DIR / ".venv" / "bin" / "python"
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        return None
    with contextlib.suppress(OSError):
        if Path(sys.executable).resolve() == candidate.resolve():
            return None
    return candidate


def _ensure_venv() -> None:
    if _pil_available():
        return
    candidate = _venv_python()
    if candidate is not None:
        print(
            f"🔁 Dipendenze non nel Python corrente; riavvio con: {candidate}",
            flush=True,
        )
        os.execv(str(candidate), [str(candidate), *sys.argv])
    print(
        "Dipendenze mancanti: usa .venv/bin/python oppure crea il venv con "
        "`python3 -m venv .venv` e `pip install -r requirements.txt`.",
        file=sys.stderr,
    )
    raise SystemExit(1)


if __name__ == "__main__":
    _ensure_venv()

from protocols import config  # import dopo il guard del venv

_WARNING = (
    "⚠️  Gmail: impossibile sbloccare il segreto (cache GPG fredda?). "
    "Esegui: bash scripts/gmail_gpg_init.sh (o gpg --decrypt <file>)"
)


def _warn() -> None:
    print(_WARNING, file=sys.stderr)


def _export_gpg_tty() -> None:
    if os.environ.get("GPG_TTY"):
        return
    with contextlib.suppress(OSError, ValueError):
        os.environ.setdefault("GPG_TTY", os.ttyname(0))


def main() -> int:
    if not config.get_gmail_address():
        return 0
    if config.get_gmail_app_password_file():
        return 0

    cmd = config.get_gmail_app_password_cmd()
    if not cmd:
        return 0

    try:
        argv = shlex.split(cmd)
    except ValueError:
        _warn()
        return 0
    if not argv:
        _warn()
        return 0

    _export_gpg_tty()

    try:
        if sys.stdin.isatty():
            result = subprocess.run(argv, capture_output=True, text=True, check=False)
        else:
            result = subprocess.run(
                argv, capture_output=True, text=True, check=False, timeout=5
            )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        _warn()
        return 0

    if (result.stdout or "").strip():
        return 0

    _warn()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
