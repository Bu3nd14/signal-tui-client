#!/usr/bin/env python3
"""Standalone CLI utility to pair a WhatsApp session with a Baileys-based API.

Calls the WhatsApp HTTP API's session endpoint to obtain a QR code string,
renders it as ASCII in the terminal (using the ``qrcode`` library), then polls
the session status until the phone has scanned it and the session is connected.

Mirrors ``link_account.py`` (the Signal linking flow).

Usage:
    python3 link_whatsapp.py
"""

from __future__ import annotations

import logging
import os
import sys


# ─── Auto-run inside the project virtualenv ───────────────────────────────────
# `qrcode`, `websocket-client` etc. are installed in the project's `.venv`
# (created by install.sh).  If this script is launched with the system python
# (e.g. `python3 link_whatsapp.py` without activating the venv), re-execute
# ourselves under the venv interpreter so it "just works".
def _ensure_venv() -> None:
    try:
        import qrcode  # noqa: F401

        return
    except ImportError:
        pass

    project_dir = os.path.dirname(os.path.abspath(__file__))
    venv_python = os.path.join(project_dir, ".venv", "bin", "python")
    if os.path.exists(venv_python):
        print(f"🔁 Dipendenze non nel Python corrente; riavvio con: {venv_python}")
        os.execv(venv_python, [venv_python] + sys.argv)


_ensure_venv()

import time

logger = logging.getLogger(__name__)

from protocols.config import (
    get_whatsapp_session_name,
    resolve_whatsapp_api_url,
)
from protocols.whatsapp import WhatsAppRESTClient
from qr_utils import print_qr_code, qr_png_to_ascii


def main() -> None:
    # Use the configured URL if set, otherwise the local WAHA default (port 3005).
    api_url = resolve_whatsapp_api_url()
    if not api_url:
        print("❌ WhatsApp API URL not configured.", file=sys.stderr)
        print(
            "   Set WHATSAPP_API_URL or add 'whatsapp_api_url' to config.json.",
            file=sys.stderr,
        )
        sys.exit(1)

    session_name = get_whatsapp_session_name()
    client = WhatsAppRESTClient(api_url)

    print("=" * 60)
    print("  🔗 Link WhatsApp session")
    print("  📱 Scan the QR code with WhatsApp on your phone")
    print("=" * 60)
    print(f"✅ API: {api_url}")
    print(f"✅ Session: {session_name}")
    print()

    print("⏳ Creating a fresh session / requesting a valid QR...")
    # Sempre un QR nuovo e valido: abbatte la vecchia sessione (un QR scaduto è il
    # motivo più comune del "non puoi collegare nuovi dispositivi") e ne chiede uno
    # appena generato.
    qr = client.get_fresh_pairing_qr(reset=True)
    if not qr:
        if client.last_status == 401:
            print(
                "❌ WhatsApp API ha rifiutato la richiesta (401 Unauthorized).",
                file=sys.stderr,
            )
            print(
                "   La WAHA richiede una API key via header X-Api-Key.", file=sys.stderr
            )
            print(
                "   Controlla che il file .env contenga WAHA_API_KEY e che config.json",
                file=sys.stderr,
            )
            print(
                "   o la variabile WHATSAPP_API_KEY siano allineati.", file=sys.stderr
            )
        else:
            print("❌ Could not obtain a pairing QR from the API.", file=sys.stderr)
            print(
                "   Es: docker compose up -d  (o ./scripts/start_whatsapp.sh)",
                file=sys.stderr,
            )
            print("   poi conferma che l'API risponda:", file=sys.stderr)
            print(
                f"       curl -H 'X-Api-Key: <chiave>' {api_url}/api/version",
                file=sys.stderr,
            )
        sys.exit(1)

    _save_qr_png(qr, session_name)
    _print_qr(qr)

    print("⏳ Waiting for scan from phone...")
    print("   (press Ctrl+C to cancel — il QR viene rigenerato quando scade)")
    print("=" * 60)

    # Poll the session status; regenerate the QR when it expires so the code on
    # screen stays scannable until the phone connects.
    deadline = time.time() + 300
    qr_age = 0.0
    while time.time() < deadline:
        status = client.get_session_status() or {}
        s = str(status.get("status") or "").lower()
        if s in ("connected", "authenticated", "ready", "open", "working"):
            print()
            print("✅ WhatsApp session linked and connected!")
            # The session is now saved on the API side; nothing else to write
            # locally (the backend reads session + media config from env/config).
            sys.exit(0)

        # WhatsApp QRs expire quickly (~60-90s).  Re-request a fresh one so the
        # displayed code never goes stale.
        qr_age += 2.0
        if s in ("scan_qr", "scan_qr_code", "unpaired", "pending") and qr_age >= 60:
            print()
            print("⟳ Il QR è scaduto: ne genero uno nuovo...")
            new_qr = client.get_fresh_pairing_qr(reset=False)
            if new_qr:
                _save_qr_png(new_qr, session_name)
                _print_qr(new_qr)
            qr_age = 0.0

        time.sleep(2)

    print()
    print("❌ Timed out waiting for the phone to scan the QR code.", file=sys.stderr)
    sys.exit(1)


def _save_qr_png(qr, session_name: str) -> str:
    """Persist a bytes-typed QR PNG to a writable location; return its path.

    Script-only helper (unused by the TUI backend), so it lives next to the
    link script rather than in the client.
    """
    if not isinstance(qr, bytes):
        return ""
    project_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(project_dir, "whatsapp-data"),
        os.path.join(os.path.expanduser("~"), ".cache", "signal-tui-client"),
        os.path.join(project_dir, ".docker"),
        "/tmp",
    ]
    for base in candidates:
        try:
            os.makedirs(base, exist_ok=True)
        except OSError:
            continue
        candidate = os.path.join(base, f"link-{session_name}.png")
        try:
            with open(candidate, "wb") as f:
                f.write(qr)
            return candidate
        except OSError:
            continue
    print(
        "❌ Impossibile salvare il QR PNG (nessuna cartella scrivibile).",
        file=sys.stderr,
    )
    sys.exit(1)


def _print_qr(qr) -> None:
    """Show the QR on the terminal: ASCII for text, PNG→ASCII for binary."""
    if isinstance(qr, bytes):
        try:
            print()
            print("📸 SCAN THIS QR CODE WITH WHATSAPP:")
            print()
            print(qr_png_to_ascii(qr))
            print()
        except Exception as _e:
            # Fallback: instruct the user to open the saved PNG instead.
            logger.debug("Failed to render QR PNG as ASCII", exc_info=True)
            print()
            print("📸 QR (PNG) salvato su disco: aprilo e INQUADRATELO con WhatsApp.")
            print()
    else:
        print()
        print("📸 SCAN THIS QR CODE WITH WHATSAPP:")
        print()
        print_qr_code(qr)
        print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        print("⏹ Operation cancelled by user.")
        sys.exit(0)
