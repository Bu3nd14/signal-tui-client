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

import qrcode

from backends.config import (
    resolve_whatsapp_api_url,
    get_whatsapp_session_name,
)
from backends.whatsapp import WhatsAppRESTClient


def print_qr_code(link: str) -> None:
    """Generate and print the QR code in the terminal as ASCII."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=2,
        border=2,
    )
    qr.add_data(link)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


def main() -> None:
    # Use the configured URL if set, otherwise the local WAHA default (port 3005).
    api_url = resolve_whatsapp_api_url()
    if not api_url:
        print("❌ WhatsApp API URL not configured.", file=sys.stderr)
        print("   Set WHATSAPP_API_URL or add 'whatsapp_api_url' to config.json.", file=sys.stderr)
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

    print("⏳ Creating session / requesting QR...")
    qr = client.get_pairing_qr()
    if not qr:
        print("❌ Could not obtain a pairing QR from the API.", file=sys.stderr)
        print("   Es: docker compose up -d  (o ./scripts/start_whatsapp.sh)", file=sys.stderr)
        print("   poi conferma che l'API risponda:", file=sys.stderr)
        print(f"       curl {api_url}/api/server", file=sys.stderr)
        sys.exit(1)
        print("   Make sure the WhatsApp HTTP API is running and reachable.", file=sys.stderr)
        sys.exit(1)

    print()
    print("📸 SCAN THIS QR CODE WITH WHATSAPP:")
    print()
    print_qr_code(qr)
    print()
    print("⏳ Waiting for scan from phone...")
    print("   (press Ctrl+C to cancel)")
    print("=" * 60)

    # Poll the session status until connected (or timeout).
    deadline = time.time() + 180
    while time.time() < deadline:
        status = client.get_session_status() or {}
        s = str(status.get("status") or "").lower()
        if s in ("connected", "authenticated", "ready", "open"):
            print()
            print("✅ WhatsApp session linked and connected!")
            # The session is now saved on the API side; nothing else to write
            # locally (the backend reads session + media config from env/config).
            sys.exit(0)
        time.sleep(2)

    print()
    print("❌ Timed out waiting for the phone to scan the QR code.", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        print("⏹ Operation cancelled by user.")
        sys.exit(0)
