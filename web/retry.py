"""Retry logic per invio messaggi web UI.

Solo logica pura: classificazione errori e calcolo delay. Nessun I/O.
"""

import errno
import logging
import random
import re
import socket
import subprocess
from typing import Literal

logger = logging.getLogger(__name__)

SEND_RETRY_MAX_ATTEMPTS = 3
SEND_RETRY_BASE_DELAY_S = 1.0
SEND_RETRY_JITTER_MAX_S = 1.0

_SIGNAL_RETRYABLE_PATTERNS = re.compile(
    r"connection refused|name or service not known|temporary failure in name resolution",
    re.IGNORECASE,
)


def classify_send_error(protocol: str, exc: BaseException) -> Literal["retryable", "terminal"]:
    """Classifica un errore di send. Non solleva MAI: degrada a "terminal"."""
    try:
        if protocol == "signal":
            return _classify_signal(exc)
        if protocol == "whatsapp":
            return _classify_whatsapp(exc)
        if protocol == "telegram":
            return _classify_telegram(exc)
        return "terminal"
    except Exception:
        logger.exception("classify_send_error failed, degrading to terminal")
        return "terminal"


def safe_error_text(exc: BaseException, limit: int = 200) -> str:
    """Stringifica un'eccezione senza mai sollevare (fallback al nome del tipo)."""
    try:
        text = str(exc)
    except Exception:  # noqa: BLE001 — fallback robustezza
        text = type(exc).__name__
    return text[:limit]


def _classify_signal(exc: BaseException) -> str:
    if isinstance(exc, (subprocess.TimeoutExpired, subprocess.SubprocessError)):
        return "terminal"
    if isinstance(exc, (FileNotFoundError, PermissionError, IsADirectoryError)):
        return "terminal"
    if isinstance(exc, OSError):
        if exc.errno in {errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH}:
            return "retryable"
        if isinstance(exc, socket.gaierror) and exc.errno in {socket.EAI_AGAIN, socket.EAI_NONAME}:
            return "retryable"
        return "terminal"
    if isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        if "not configured" in msg:
            return "terminal"
        if _SIGNAL_RETRYABLE_PATTERNS.search(msg):
            return "retryable"
        return "terminal"
    return "terminal"


def _classify_whatsapp(exc: BaseException) -> str:
    import urllib.error
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "terminal"
    if isinstance(exc, (FileNotFoundError, PermissionError, IsADirectoryError)):
        return "terminal"
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        if code in {502, 503, 504, 429} or 500 <= code < 600:
            return "retryable"
        return "terminal"
    if isinstance(exc, urllib.error.URLError):
        msg = str(exc).lower()
        if "connection refused" in msg or "name or service not known" in msg:
            return "retryable"
        return "terminal"
    if isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        if "not configured" in msg:
            return "terminal"
        m = re.search(r"status=(\d+)", msg)
        if m:
            code = int(m.group(1))
            if code == 0:
                if "connection refused" in msg or "name or service not known" in msg:
                    return "retryable"
                return "terminal"
            if code in {502, 503, 504, 429} or 500 <= code < 600:
                return "retryable"
            return "terminal"
        return "terminal"
    return "terminal"


def _classify_telegram(exc: BaseException) -> str:
    import concurrent.futures
    if isinstance(exc, concurrent.futures.TimeoutError):
        return "terminal"
    if isinstance(exc, (FileNotFoundError, PermissionError, IsADirectoryError)):
        return "terminal"
    if isinstance(exc, OSError):
        if exc.errno in {errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH}:
            return "retryable"
        if isinstance(exc, socket.gaierror) and exc.errno in {socket.EAI_AGAIN, socket.EAI_NONAME}:
            return "retryable"
        return "terminal"
    if isinstance(exc, RuntimeError):
        return "terminal"
    if isinstance(exc, ValueError):
        return "terminal"
    exc_name = type(exc).__name__
    if "FloodWait" in exc_name:
        seconds = getattr(exc, "seconds", None)
        if seconds is not None and seconds < 5:
            return "retryable"
        return "terminal"
    if any(p in exc_name for p in ("PeerFlood", "UserIsBlocked", "ChatWriteForbidden", "UserIsDeactivated")):
        return "terminal"
    if "ServerError" in exc_name or "RPCError" in exc_name:
        code = getattr(exc, "code", None)
        if code == 500:
            return "retryable"
        return "terminal"
    return "terminal"


def compute_delay(attempt: int, rng: random.Random | None = None) -> float:
    """Delay per il retry. attempt è 1-indexed (1 = primo retry)."""
    if rng is None:
        rng = random.Random()
    base_delay = SEND_RETRY_BASE_DELAY_S * (2 ** (attempt - 1))
    jitter = rng.uniform(0, SEND_RETRY_JITTER_MAX_S)
    return base_delay + jitter
