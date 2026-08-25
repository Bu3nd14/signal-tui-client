"""Terminal image-protocol detection (pure, dependency-injectable).

Implements DESIGN_NATIVE_IMAGES.md §5.1: config override → tty check →
tmux/screen guard → true-kitty gate (``TERM=xterm-kitty`` + TGP query) →
catimg fallback, else OFF.

Everything is injectable (``isatty``, ``env``, ``override``, ``which``,
``query_cb``) so the detection can be unit-tested headlessly with no real
terminal I/O.
"""

from __future__ import annotations

import os
import select
import shutil
import sys
import termios
import time
import tty
from collections.abc import Mapping
from contextlib import suppress
from enum import Enum

# TGP (Kitty Graphics Protocol) query/response bytes.
_KITTY_QUERY = b"\x1b_Gi=1,s=1,v=1,a=q,t=d,f=24;AAAA\x1b\\"
_KITTY_OK = b"\x1b_Gi=1;OK\x1b\\"


class ImageSupport(Enum):
    """Available terminal image rendering backends."""

    KITTY = "kitty"
    CATIMG = "catimg"
    OFF = "off"


def query_kitty_ok(stdin_fd: int, stdout_fd: int, timeout: float = 1.0) -> bool:
    """Send a TGP query and return True iff the terminal answers ``OK``.

    Switches stdin to raw mode for the duration of the query (restored in a
    ``finally``), so the terminal's reply is not line-buffered.  Never raises:
    any failure (non-tty, broken pipe, timeout) returns ``False``.

    Timeout: 1.0s by default.  Empirically (2026-08-25, kitty 0.48 via ssh) the
    terminal reply arrives between 0.5s and 1.0s on real connections; the
    previous 0.15s default was far too tight and caused a spurious CATIMG
    fallback even on genuine kitty.
    """
    try:
        old = termios.tcgetattr(stdin_fd)
    except (OSError, ValueError, termios.error):
        # Not a tty (pipe/CI) → never query.
        return False
    try:
        tty.setraw(stdin_fd)
        os.write(stdout_fd, _KITTY_QUERY)
        deadline = time.monotonic() + timeout
        received = b""
        while time.monotonic() < deadline:
            ready, _, _ = select.select([stdin_fd], [], [], 0.02)
            if not ready:
                continue
            try:
                chunk = os.read(stdin_fd, 4096)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            received += chunk
            if _KITTY_OK in received:
                return True
    except (OSError, ValueError, termios.error):
        return False
    finally:
        with suppress(OSError, ValueError, termios.error):
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old)
    return False


def _default_kitty_query() -> bool:
    """Production query: real TGP probe on the process stdin/stdout."""
    try:
        stdin_fd = sys.stdin.fileno()
        stdout_fd = sys.stdout.fileno()
    except (OSError, ValueError, AttributeError):
        return False
    return query_kitty_ok(stdin_fd, stdout_fd, timeout=0.15)


def detect_image_support(
    *,
    isatty: bool,
    env: Mapping[str, str],
    override: str | None = None,
    which=shutil.which,
    query_cb=None,
) -> ImageSupport:
    """Detect the terminal image backend (DESIGN_NATIVE_IMAGES.md §5.1).

    ``override`` is the configured ``IMAGE_PROTOCOL`` value (``auto`` means
    "no override" and falls through to detection).  ``query_cb`` is an optional
    zero-arg callable returning a truthy value when the TGP query succeeds; it
    defaults to the real probe (``query_kitty_ok`` on stdin/stdout).
    """
    # 1. Config override (auto → fall through to detection).
    if override is not None:
        value = override.strip().lower()
        if value == "kitty":
            return ImageSupport.KITTY
        if value == "catimg":
            return ImageSupport.CATIMG
        if value == "off":
            return ImageSupport.OFF

    # 2. Non-tty (pipe/CI/headless) → CATIMG, never query the terminal.
    if not isatty:
        return ImageSupport.CATIMG

    # 3. tmux/screen guard: passthrough is unreliable → CATIMG.
    term = env.get("TERM", "")
    if env.get("TMUX") or term.startswith("screen"):
        return ImageSupport.CATIMG

    # 4. True-kitty gate: TERM=xterm-kitty AND the TGP query answers OK.
    #    The TERM gate excludes iTerm2/Ghostty, whose xterm-256color TERM would
    #    answer OK to the query but never actually render (false positive).
    if term == "xterm-kitty":
        query = query_cb if query_cb is not None else _default_kitty_query
        if query():
            return ImageSupport.KITTY

    # 5. Fallback: catimg if available, else disabled.
    if which("catimg"):
        return ImageSupport.CATIMG
    return ImageSupport.OFF
