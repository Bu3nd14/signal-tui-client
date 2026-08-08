"""
QR code rendering utilities shared across the project.

Used by:
- ``link_account.py`` / ``link_whatsapp.py`` (standalone CLI scripts)
- ``device_link_screen.py`` (TUI linking flow)
"""
from __future__ import annotations

import struct
import zlib

import qrcode


def print_qr_code(link: str) -> None:
    """Generate and print the QR code in the terminal as ASCII (stdout)."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=2,
        border=2,
    )
    qr.add_data(link)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


def qr_to_ascii(link: str) -> str:
    """Generate a compact QR code ASCII string from *link*.

    Uses half-block glyphs (▀▄█) so every terminal line packs 2 module rows,
    halving the vertical height while keeping modules approximately square
    on typical 2:1 terminals.  Invert=True: light→block, dark→space.

    Mirrors the rendering logic of ``qr_png_to_ascii`` so Signal and
    WhatsApp QRs have consistent proportions.
    """
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=1,
        border=2,
    )
    qr.add_data(link)
    qr.make(fit=True)

    modcount = qr.modules_count
    border = 2
    # Each char encodes 2 module rows stacked vertically (half-block).
    #   00 light/light → █ (full block)
    #   10 dark/light  → ▄ (lower half)
    #   01 light/dark  → ▀ (upper half)
    #   11 dark/dark   →   (space)
    glyphs = {0: "█", 1: "▄", 2: "▀", 3: " "}

    def is_dark(r: int, c: int) -> bool:
        if 0 <= r < modcount and 0 <= c < modcount:
            return qr.modules[r][c]
        return False  # quiet zone → light

    lines: list[str] = []
    for r in range(-border, modcount + border, 2):
        row: list[str] = []
        for c in range(-border, modcount + border):
            top = 1 if is_dark(r, c) else 0
            bottom = 1 if is_dark(r + 1, c) else 0
            row.append(glyphs[top + (bottom << 1)])
        lines.append("".join(row))
    return "\n".join(lines)


def _decode_png_luminance(png_bytes: bytes) -> tuple[int, int, bytes]:
    """Decode a PNG into a monochrome luminance grid.

    Pure-Python/stdlib implementation (``struct`` + ``zlib``) so we don't need a
    native imaging library.
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
    """Render a WAHA QR PNG (bytes) as a scannable terminal QR string.

    Faithfully re-implements ``qrcode.QRCode.print_ascii(invert=True)`` — the
    exact method that produces the scannable QR.
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
        if min(r, c) < 0 or max(r, c) >= modcount:
            return False
        return darkf(start + c * pitch + pitch // 2, start + r * pitch + pitch // 2)

    glyphs = {0: "\u2588", 1: "\u2584", 2: "\u2580", 3: " "}

    lines = []
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
