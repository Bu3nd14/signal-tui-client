"""Terminal cell pixel-size detection (kitty graphics, R3).

The kitty graphics protocol addresses the screen in *pixels*, while Textual
regions are in *cells*.  To map one onto the other we need the current cell
size in pixels (``cell_w`` × ``cell_h``), which changes when the user zooms
the font.  Detection order (DESIGN_NATIVE_IMAGES.md §6 / R3):

1. ``ioctl(TIOCGWINSZ)`` → ``ws_xpixel // ws_col`` and ``ws_ypixel // ws_row``;
2. if the terminal reports zero pixels but knows the grid, query the pixel
   size with CSI ``16 t`` (timeout) and divide by the grid;
3. otherwise ``None`` → the renderer is unusable and the app falls back to
   CATIMG.

Everything here is pure and injectable (the ``fd`` is passed in), so it can be
unit-tested headlessly by monkeypatching ``fcntl.ioctl`` / ``os.read``.
"""

from __future__ import annotations

import fcntl
import os
import re
import select
import struct
import termios
import time
import tty
from contextlib import suppress

# struct winsize: ws_row, ws_col, ws_xpixel, ws_ypixel (4 × unsigned short).
_WINSIZE_FMT = "HHHH"

# CSI 16 t reply: ``\x1b[<ypixel>;<xpixel>t`` (note: height;width).
_PIXEL_RE = re.compile(rb"\x1b\[(\d+);(\d+)t")


def ioctl_winsize(fd: int) -> tuple[int, int, int, int] | None:
    """Return ``(rows, cols, xpixel, ypixel)`` via ``TIOCGWINSZ``, else ``None``.

    On a non-tty fd (pipe/CI) the ioctl succeeds but returns all zeros; callers
    treat that as "no grid available".
    """
    try:
        buf = struct.pack(_WINSIZE_FMT, 0, 0, 0, 0)
        result = fcntl.ioctl(fd, termios.TIOCGWINSZ, buf)
        return struct.unpack(_WINSIZE_FMT, result)
    except (OSError, ValueError, struct.error):
        return None


def query_pixel_size(fd: int, timeout: float = 0.15) -> tuple[int, int] | None:
    """Return the window pixel size ``(xpixel, ypixel)`` via CSI ``16 t``.

    Switches the fd to raw mode for the duration of the query (restored in a
    ``finally``), exactly like the TGP probe in ``detect.py``.  Never raises.
    """
    try:
        old = termios.tcgetattr(fd)
    except (OSError, ValueError, termios.error):
        return None
    try:
        tty.setraw(fd)
        os.write(fd, b"\x1b[16t")
        deadline = time.monotonic() + timeout
        received = b""
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.02)
            if not ready:
                continue
            try:
                chunk = os.read(fd, 4096)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            received += chunk
            match = _PIXEL_RE.search(received)
            if match:
                height = int(match.group(1))
                width = int(match.group(2))
                if width > 0 and height > 0:
                    return (width, height)
    except (OSError, ValueError, termios.error):
        return None
    finally:
        with suppress(OSError, ValueError, termios.error):
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return None


def get_cell_size_ioctl(fd: int) -> tuple[int, int] | None:
    """Return ``(cell_w_px, cell_h_px)`` via ``TIOCGWINSZ`` only (no CSI query).

    Returns ``None`` when the terminal reports a zero grid or zero pixel
    dimensions — the caller then keeps the previously-known cell size instead of
    querying the terminal (which would race Textual's key-thread for stdin, P2).
    """
    winsize = ioctl_winsize(fd)
    if winsize is None:
        return None
    rows, cols, xpixel, ypixel = winsize
    if rows <= 0 or cols <= 0 or xpixel <= 0 or ypixel <= 0:
        return None
    return (xpixel // cols, ypixel // rows)


def get_cell_size(fd: int) -> tuple[int, int] | None:
    """Return ``(cell_w_px, cell_h_px)``, or ``None`` if undeterminable.

    ioctl first; if the terminal reports a grid but zero pixels, fall back to
    the CSI ``16 t`` query.  This CSI fallback reads stdin, so it must ONLY run
    before ``app.run()`` (see ``signal_tui``) — never inside the app (P2).
    """
    ioctl_result = get_cell_size_ioctl(fd)
    if ioctl_result is not None:
        return ioctl_result

    winsize = ioctl_winsize(fd)
    if winsize is None:
        return None
    rows, cols, _xpixel, _ypixel = winsize
    if rows <= 0 or cols <= 0:
        return None

    # Zero pixel dimensions but a known grid → ask the terminal for pixels.
    pixels = query_pixel_size(fd)
    if pixels is not None:
        return (pixels[0] // cols, pixels[1] // rows)
    return None
