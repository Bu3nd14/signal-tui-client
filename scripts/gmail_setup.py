#!/usr/bin/env python3
"""Guided Gmail setup for the optional Web UI.

Prompts for the Gmail address and Google App Password, verifies IMAP/SMTP,
stores the secret in a ``600`` file under ``~/.config/signal-tui`` and updates
``.env``.  The secret never goes through the browser.

Run it from the project directory:

    python3 scripts/gmail_setup.py

or:

    ./install.sh --gmail
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import imaplib
import os
import smtplib
import ssl
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from protocols import config

CONFIG_ROOT = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
SECRET_DIR = CONFIG_ROOT / "signal-tui"
SECRET_FILE = SECRET_DIR / "gmail-app-password"
ENV_FILE = PROJECT_DIR / ".env"


def normalize_password(value: str) -> str:
    """App Passwords are displayed in groups of four; spaces are cosmetic."""
    return "".join(value.split())


def write_secret(path: Path, password: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    # Intentional fallback: the file is created with 600 permissions, never
    # committed, and can be upgraded to GPG/TPM with the helper scripts.
    # codeql[py/clear-text-storage-sensitive-data]
    path.write_text(password + "\n", encoding="utf-8")
    path.chmod(0o600)


def update_env(env_path: Path, address: str, secret_path: Path) -> None:
    """Replace Gmail secret settings, preserving every other .env line."""
    lines = (
        env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    )
    prefixes = (
        "GMAIL_ADDRESS=",
        "GMAIL_APP_PASSWORD=",
        "GMAIL_APP_PASSWORD_FILE=",
        "GMAIL_APP_PASSWORD_CMD=",
    )
    out = [line for line in lines if not line.startswith(prefixes)]
    out.append(f"GMAIL_ADDRESS={address}")
    out.append(f"GMAIL_APP_PASSWORD_FILE={secret_path}")
    # Only the secret file *path* is written to .env, not the App Password.
    # codeql[py/clear-text-storage-sensitive-data]
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    env_path.chmod(0o600)


def test_credentials(address: str, password: str) -> bool:
    """Verify IMAP and SMTP login without sending any message."""
    timeout = config.get_gmail_timeout_s()
    ok = True

    imap = None
    try:
        imap = imaplib.IMAP4_SSL(
            config.get_gmail_imap_host(),
            config.get_gmail_imap_port(),
            timeout=timeout,
        )
        imap.login(address, password)
        imap.select("INBOX", readonly=True)
        print("IMAP: login OK")
    except Exception as exc:  # noqa: BLE001 - never print the secret
        ok = False
        print(f"IMAP: errore ({type(exc).__name__})", file=sys.stderr)
    finally:
        if imap is not None:
            with contextlib.suppress(Exception):
                imap.logout()

    smtp = None
    try:
        smtp = smtplib.SMTP(
            config.get_gmail_smtp_host(),
            config.get_gmail_smtp_port(),
            timeout=timeout,
        )
        smtp.ehlo()
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
        smtp.login(address, password)
        print("SMTP: login OK")
    except Exception as exc:  # noqa: BLE001 - never print the secret
        ok = False
        print(f"SMTP: errore ({type(exc).__name__})", file=sys.stderr)
    finally:
        if smtp is not None:
            with contextlib.suppress(Exception):
                smtp.quit()
            with contextlib.suppress(Exception):
                smtp.close()

    return ok


def _existing_configuration() -> bool:
    if config.get_gmail_address():
        return True
    if ENV_FILE.exists():
        return any(
            line.startswith("GMAIL_APP_PASSWORD")
            for line in ENV_FILE.read_text(encoding="utf-8").splitlines()
        )
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Configura Gmail per la Web UI (memorizza l'App Password in un file 600).",
    )
    parser.add_argument("--address", help="indirizzo Gmail (altrimenti viene chiesto)")
    parser.add_argument(
        "--no-test",
        action="store_true",
        help="salva senza verificare il login IMAP/SMTP",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="sovrascrive una configurazione Gmail esistente senza chiedere",
    )
    args = parser.parse_args(argv)

    if _existing_configuration() and not args.force:
        print(
            "Attenzione: la configurazione Gmail esistente (inclusi eventuali "
            "GPG/TPM) verrà sostituita da un file 600."
        )
        answer = input("Sovrascrivere? [y/N] ").strip()
        if answer.lower() not in ("y", "yes", "s", "si", "sì"):
            print("Nessuna modifica.")
            return 1

    default_address = config.get_gmail_address()
    address = (args.address or "").strip()
    if not address:
        prompt = (
            f"Indirizzo Gmail [{default_address}]: "
            if default_address
            else "Indirizzo Gmail: "
        )
        address = input(prompt).strip() or default_address
    if "@" not in address:
        print("Indirizzo Gmail non valido.", file=sys.stderr)
        return 1

    try:
        password = normalize_password(
            getpass.getpass("App Password Google (16 caratteri, input nascosto): ")
        )
    except (EOFError, KeyboardInterrupt):
        print("\nAnnullato.", file=sys.stderr)
        return 1
    if not password:
        print("App Password vuota.", file=sys.stderr)
        return 1
    if len(password) != 16:
        print(
            "Attenzione: l'App Password Google è di solito di 16 caratteri; proseguo comunque.",
            file=sys.stderr,
        )

    if not args.no_test and not test_credentials(address, password):
        print(
            "Login IMAP/SMTP fallito: configurazione non salvata. "
            "Controlla l'App Password o usa --no-test per salvarla comunque.",
            file=sys.stderr,
        )
        return 1

    write_secret(SECRET_FILE, password)
    update_env(ENV_FILE, address, SECRET_FILE)

    print()
    print("Gmail configurato.")
    print("  Segreto salvato in un file con permessi 600.")
    print(f"  Config:  {ENV_FILE} (600)")
    print()
    print(
        "Ricarica la tab Gmail nella Web UI: il server rilegge .env a ogni richiesta."
    )
    print("Cifratura opzionale:")
    print("  bash scripts/gmail_gpg_init.sh   # GPG con passphrase")
    print("  bash scripts/gmail_tpm_init.sh   # TPM2 (richiede il gruppo tss)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
