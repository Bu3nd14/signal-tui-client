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

import struct
import time
import zlib

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


def _decode_png_luminance(png_bytes: bytes) -> tuple[int, int, bytes]:
    """Decode a PNG (WAHA's QR image) into a monochrome luminance grid.

    Pure-Python/stdlib implementation (``struct`` + ``zlib``) so we don't need a
    native imaging library (Pillow/pyzbar are not installed in the project venv).
    Handles the PNG filter types (0-4) and the colour types WAHA emits (grayscale
    / RGB / RGBA / gray+alpha).

    Returns ``(width, height, luminance_bytes)`` where ``luminance[y*w+x]`` is in
    ``0..255`` (lower = darker).
    """
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos = 8
    idat = b""
    meta = None
    while pos < len(png_bytes):
        (ln,) = struct.unpack(">I", png_bytes[pos:pos + 4])
        ctype = png_bytes[pos + 4:pos + 8].decode("ascii")
        cdata = png_bytes[pos + 8:pos + 8 + ln]
        if ctype == "IHDR":
            w, h, bd, ctyp, _ci, _cf, _cint = struct.unpack(">IIBBBBB", cdata[:13])
            meta = (w, h, bd, ctyp)
        elif ctype == "IDAT":
            idat += cdata
        pos += 12 + ln
    if meta is None:
        raise ValueError("PNG senza IHDR")
    w, h, bd, ctyp = meta
    bpp = {0: 1, 2: 3, 4: 2, 6: 4}[ctyp]
    plen = w * bpp
    raw = zlib.decompress(idat)
    out = bytearray(h * plen)
    prev = bytearray(plen)
    for y in range(h):
        f = raw[y * (plen + 1)]
        row = bytearray(raw[y * (plen + 1) + 1:(y + 1) * (plen + 1)])
        for x in range(plen):
            a = row[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if f == 1:
                v = row[x] + a
            elif f == 2:
                v = row[x] + b
            elif f == 3:
                v = row[x] + ((a + b) // 2)
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                v = row[x] + pr
            else:
                v = row[x]
            row[x] = v & 255
        out[y * plen:(y + 1) * plen] = row
        prev = row

    lum = bytearray(w * h)
    for y in range(h):
        for x in range(w):
            o = y * plen
            if ctyp == 6:
                r, g, bb = out[o + x * 4], out[o + x * 4 + 1], out[o + x * 4 + 2]
            elif ctyp == 2:
                r, g, bb = out[o + x * 3], out[o + x * 3 + 1], out[o + x * 3 + 2]
            elif ctyp == 0:
                r = g = bb = out[o + x]
            elif ctyp == 4:
                r = g = bb = out[o + x * 2]
            else:  # palette (3) or gray-16 -- coerce to first channel
                r = g = bb = out[o + x * bpp]
            lum[y * w + x] = int(0.299 * r + 0.587 * g + 0.114 * bb)
    return w, h, lum


def qr_png_to_ascii(png_bytes: bytes, border: int = 2) -> str:
    """Render a WAHA QR PNG (bytes) as a scannable terminal QR.

    Faithfully re-implements ``qrcode.QRCode.print_ascii(invert=True)`` — the
    exact method that produces the scannable QR printed into WAHA's docker logs
    (and used by ``link_account.py`` for Signal).  It maps the decoded QR module
    matrix onto CP437 half-block glyphs, one character per module column and two
    module rows per text line, with a light ``border``-module quiet zone all
    around.  No ANSI colors, so it scans on a normal terminal background.

    Falls back to a fixed downsample only when the module geometry can't be
    detected from the PNG.
    """
    w, h, lum = _decode_png_luminance(png_bytes)
    darkf = lambda x, y: lum[y * w + x] < 128

    # Detect quiet zone (first row containing a dark pixel) and module pitch.
    start = next((y for y in range(h) if any(darkf(x, y) for x in range(w))), 0)
    if start <= 0 or start >= h:
        return _qr_simple(lum, w, h)
    x0 = next((x for x in range(w) if darkf(x, start)), 0)
    run = 0
    while x0 + run < w and darkf(x0 + run, start):
        run += 1
    if run <= 0:
        return _qr_simple(lum, w, h)
    pitch = round(run / 7)  # finder outer ring is 7 modules wide
    if pitch < 1:
        return _qr_simple(lum, w, h)
    modcount = (w - 2 * start) // pitch
    if modcount < 21:  # smallest standard QR is 21x21
        return _qr_simple(lum, w, h)

    def is_dark(r: int, c: int) -> bool:
        # Mirror qrcode's get_module (with invert=True) semantics: out-of-grid /
        # border cells count as "light" here, matching print_ascii's border.
        if min(r, c) < 0 or max(r, c) >= modcount:
            return False
        return darkf(start + c * pitch + pitch // 2, start + r * pitch + pitch // 2)

    # Exactly what print_ascii produces with invert=True: draw the LIGHT modules
    # as solid blocks and the DARK ones as the background, using real Unicode
    # block glyphs (portable across UTF-8 terminals, unlike cp437 chr()).
    # pos = top*1 + bottom*2 for the two vertically stacked module cells:
    #   00 -> full block      11 -> space
    #   10 -> lower half ▄    01 -> upper half ▀
    glyphs = {0: "\u2588", 1: "\u2584", 2: "\u2580", 3: " "}

    lines = []
    # Iterate module rows in pairs (r, r+1), same stepping as print_ascii.
    for r in range(-border, modcount + border, 2):
        line_chars = []
        for c in range(-border, modcount + border):
            top = 1 if is_dark(r, c) else 0
            bottom = 1 if is_dark(r + 1, c) else 0
            line_chars.append(glyphs[top + (bottom << 1)])
        lines.append("".join(line_chars))
    return "\n".join(lines)


def _qr_simple(lum: bytes, w: int, h: int) -> str:
    """Fallback: dump the QR at one cell per 2 pixels (low-res but still visible)."""
    step = max(1, w // 120)
    rows = []
    for y in range(0, h, step):
        line = "".join(
            "██" if lum[y * w + x] < 128 else "  "
            for x in range(0, w, step)
        )
        rows.append(line)
    return "\n".join(rows)



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

    print("⏳ Creating a fresh session / requesting a valid QR...")
    # Sempre un QR nuovo e valido: abbatte la vecchia sessione (un QR scaduto è il
    # motivo più comune del "non puoi collegare nuovi dispositivi") e ne chiede uno
    # appena generato.
    qr = client.get_fresh_pairing_qr(reset=True)
    if not qr:
        if client.last_status == 401:
            print("❌ WhatsApp API ha rifiutato la richiesta (401 Unauthorized).", file=sys.stderr)
            print("   La WAHA richiede una API key via header X-Api-Key.", file=sys.stderr)
            print("   Controlla che il file .env contenga WAHA_API_KEY e che config.json", file=sys.stderr)
            print("   o la variabile WHATSAPP_API_KEY siano allineati.", file=sys.stderr)
        else:
            print("❌ Could not obtain a pairing QR from the API.", file=sys.stderr)
            print("   Es: docker compose up -d  (o ./scripts/start_whatsapp.sh)", file=sys.stderr)
            print("   poi conferma che l'API risponda:", file=sys.stderr)
            print(f"       curl -H 'X-Api-Key: <chiave>' {api_url}/api/version", file=sys.stderr)
        sys.exit(1)

    qr_path = _save_qr_png(qr, session_name)
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
                qr_path = _save_qr_png(new_qr, session_name)
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
    print("❌ Impossibile salvare il QR PNG (nessuna cartella scrivibile).", file=sys.stderr)
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
        except Exception:
            # Fallback: instruct the user to open the saved PNG instead.
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
