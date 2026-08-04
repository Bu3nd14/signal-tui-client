"""Test per la resa del QR codice (PNG→ASCII) di ``link_whatsapp``.

La WAHA restituisce il QR come immagine PNG; ``link_whatsapp.qr_png_to_ascii``
decodifica il PNG con solo stdlib (``struct``/``zlib``) e lo stampa come QR
scansionabile a terminale, replicando fedelmente l'output di
``qrcode.print_ascii(invert=True)`` (lo stesso che appare nei log docker di WAHA
ed è scansionabile).  Generiamo un PNG QR in modo dipendenza-free e verifichiamo
che il rendering sia geometricamente fedele alla matrice sorgente e senza colori
ANSI (che avevano causato la lettura fallita).
"""

import struct
import zlib

import qrcode

from link_whatsapp import _decode_png_luminance, qr_png_to_ascii


def _make_qr_matrix(text: str):
    """Ritorna la matrice CORE del QR (senza quiet border), così il finder
    top-left è in (0,0) e la geometria è confrontabile con il renderer."""
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=1,
        border=1,
    )
    qr.add_data(text)
    qr.make(fit=True)
    n = qr.modules_count
    return [row[:n] for row in qr.modules[:n]]


def _matrix_to_grayscale_png(mat):
    """Costruisce un PNG 8-bit in scala di grigi da una matrice di booleani.

    Ogni modulo diventa un quadrato 4×4 px (come WAHA), valore 0 (nero) o 255
    (bianco), con un quiet zone di 4 px (un modulo) tutto intorno.  Usa solo
    stdlib (``zlib``+``struct``), quindi non serve Pillow nei test.
    """
    n = len(mat)
    scale = 4
    quiet = 4
    size = n * scale + 2 * quiet
    rows = []
    for r in range(size):
        row = bytearray([255] * size)
        for c in range(size):
            mr = (r - quiet) // scale
            mc = (c - quiet) // scale
            if 0 <= mr < n and 0 <= mc < n and mat[mr][mc]:
                row[c] = 0
        rows.append(bytes([0]) + bytes(row))  # filter type 0 (None)
    raw = b"".join(rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0)  # gray, 8-bit
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def _ascii_to_module_grid(out):
    """Converte l'output di ``qr_png_to_ascii`` (print_ascii invertito) in una
    griglia di booleani (True = modulo scuro).  In modalità invertita i moduli
    chiari sono blocchi pieni (█); i moduli scuri sono ▀ (sopra), ▄ (sotto) o ' '.
    """
    glyph_map = {
        "\u2588": (False, False),   # full block -> top chiaro, bottom chiaro
        "\u2584": (True, False),    # low half   -> top scuro, bottom chiaro
        "\u2580": (False, True),    # up half    -> top chiaro, bottom scuro
        " ": (True, True),          # spazio      -> entrambi scuri
    }
    grid = []
    for line in out.split("\n"):
        top_row, bot_row = [], []
        for ch in line:
            t, b = glyph_map.get(ch, (False, False))
            top_row.append(t)
            bot_row.append(b)
        grid.append(top_row)
        grid.append(bot_row)
    return grid


def test_decode_png_luminance_roundtrip():
    mat = _make_qr_matrix("WAI:prova||abc123||0123456789012")
    png = _matrix_to_grayscale_png(mat)
    w, h, lum = _decode_png_luminance(png)
    assert (w, h) == (len(mat) * 4 + 8, len(mat) * 4 + 8)
    assert lum[0] == 255
    # primo modulo scuro (finder top-left) al centro del quadrato 4x4 in quiet+.
    assert lum[w * (4 + 2) + (4 + 2)] == 0


def test_qr_png_to_ascii_has_no_ansi_and_right_shape():
    """Niente ANSI e dimensioni coerenti con print_ascii (1 col/modulo, 2 rig/mod)."""
    mat = _make_qr_matrix("WAI:secondo||xyz987||999")
    png = _matrix_to_grayscale_png(mat)
    out = qr_png_to_ascii(png)
    n = len(mat)
    lines = out.split("\n")
    assert "\x1b[" not in out              # nessun colore ANSI
    assert len(lines) <= (n // 2) + 3       # ~metà righe modulo + bordi
    assert max(len(l) for l in lines) >= n  # almeno 1 colonna per modulo
    assert max(len(l) for l in lines) <= n + 4


def test_qr_png_to_ascii_preserves_finder_geometry():
    """Il finder top-left (angolo e centro) resta scuro."""
    mat = _make_qr_matrix("WAI:geo||abcdef||0123456789012345")
    png = _matrix_to_grayscale_png(mat)
    out = qr_png_to_ascii(png)
    grid = _ascii_to_module_grid(out)
    # grid = modulo (m, m) -> riga/colonna m+border (m+2). Il finder è in (1,1).
    assert grid[2][2] is True        # angolo del bordo finder (module 0,0) scuro
    assert grid[5][5] is True        # centro finder (module 3,3) scuro


def test_qr_png_to_ascii_fidelity_roundtrip():
    """Riconvertendo l'output si deve riprodurre la matrice sorgente."""
    mat = _make_qr_matrix("WAI:fidelity||abcd1234||0123456789")
    png = _matrix_to_grayscale_png(mat)
    out = qr_png_to_ascii(png)
    grid = _ascii_to_module_grid(out)
    n = len(mat)
    first = next((i for i, r in enumerate(grid) if any(r)), 0)
    cols = [i for i, v in enumerate(grid[first]) if v]
    c0 = min(cols) if cols else 0
    core = [grid[i][c0:c0 + n] for i in range(first, first + n)]
    mismatches = sum(1 for r in range(n) for c in range(n) if core[r][c] != mat[r][c])
    assert mismatches == 0
