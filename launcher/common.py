"""Shared helpers for the launcher package (stdlib only, see ``__init__.py``)."""

from __future__ import annotations

import shutil
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent

_TTY = sys.stdout.isatty()


def _c(code: str) -> str:
    return code if _TTY else ""


C_RED = _c("\033[31m")
C_GREEN = _c("\033[32m")
C_YELLOW = _c("\033[33m")
C_BLUE = _c("\033[34m")
C_BOLD = _c("\033[1m")
C_RESET = _c("\033[0m")


def info(msg: str) -> None:
    print(f"{C_BLUE}{C_BOLD}[INFO]{C_RESET} {msg}")


def ok(msg: str) -> None:
    print(f"{C_GREEN}{C_BOLD}[OK]{C_RESET} {msg}")


def warn(msg: str) -> None:
    print(f"{C_YELLOW}{C_BOLD}[WARN]{C_RESET} {msg}")


def err(msg: str) -> None:
    print(f"{C_RED}{C_BOLD}[ERROR]{C_RESET} {msg}", file=sys.stderr)


def die(msg: str) -> None:
    """Print an error and exit the process with status 1 (mirrors bash ``die()``)."""
    err(msg)
    raise SystemExit(1)


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def port_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    """Best-effort check that something accepts TCP connections on *port*."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((host, port)) == 0
    except OSError:
        return False


def http_get(
    url: str, *, headers: dict[str, str] | None = None, timeout: float = 5
) -> tuple[int, bytes] | None:
    """Return ``(status, body)``, or ``None`` on any transport error."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError, TimeoutError):
        return None


def http_post(
    url: str,
    data: bytes,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 5,
) -> tuple[int, bytes] | None:
    """Return ``(status, body)``, or ``None`` on any transport error."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError, TimeoutError):
        return None
