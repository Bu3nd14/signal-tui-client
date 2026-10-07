"""Disk cache for WhatsApp contact profile pictures.

WAHA returns a short-lived ``profilePictureURL`` (a JPEG that can be
downloaded without an API key).  Cache it on disk with a TTL and a negative
marker so repeated contact-list renders don't hammer WAHA.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from protocols.db import CACHE_DIR

logger = logging.getLogger(__name__)

AVATAR_TTL_SECONDS = 7 * 24 * 3600
NO_PHOTO_TTL_SECONDS = 3600
_DOWNLOAD_TIMEOUT = 15

_AVATAR_LOCKS: dict[Path, threading.Lock] = {}
_AVATAR_LOCKS_GUARD = threading.Lock()


def _avatar_dir() -> Path:
    directory = Path(CACHE_DIR) / "whatsapp-avatars"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _avatar_path(contact_id: str) -> Path:
    digest = hashlib.sha1(contact_id.encode(), usedforsecurity=False).hexdigest()
    return _avatar_dir() / f"{digest}.jpg"


def _no_photo_marker(path: Path) -> Path:
    return path.with_name(path.name + ".nophoto")


def _avatar_lock(path: Path) -> threading.Lock:
    with _AVATAR_LOCKS_GUARD:
        return _AVATAR_LOCKS.setdefault(path, threading.Lock())


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT) as response:
        return response.read()


def _fresh(path: Path, ttl: int, now: float) -> bool:
    try:
        return path.is_file() and (now - path.stat().st_mtime) <= ttl
    except OSError:
        return False


def resolve_whatsapp_avatar(
    rest: Any, contact_id: str, *, now: float | None = None
) -> Path | None:
    """Return a cached avatar path, fetching/downloading it on a cache miss."""
    if rest is None or not contact_id:
        return None
    timestamp = time.time() if now is None else now
    try:
        path = _avatar_path(contact_id)
    except OSError:
        logger.debug("Unable to prepare WhatsApp avatar cache dir", exc_info=True)
        return None
    marker = _no_photo_marker(path)

    if _fresh(marker, NO_PHOTO_TTL_SECONDS, timestamp):
        return None
    if _fresh(path, AVATAR_TTL_SECONDS, timestamp):
        return path

    with _avatar_lock(path):
        if _fresh(marker, NO_PHOTO_TTL_SECONDS, timestamp):
            return None
        if _fresh(path, AVATAR_TTL_SECONDS, timestamp):
            return path
        try:
            url = rest.get_profile_picture_url(contact_id)
        except Exception:
            logger.debug("WhatsApp avatar URL lookup failed", exc_info=True)
            return None
        if url:
            tmp = path.with_name(path.name + ".tmp")
            try:
                data = _download(url)
                tmp.write_bytes(data)
                os.replace(tmp, path)
                marker.unlink(missing_ok=True)
                return path
            except Exception:
                logger.debug("WhatsApp avatar download failed", exc_info=True)
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                return None
        if getattr(rest, "last_status", None) == 200:
            try:
                marker.write_bytes(b"")
            except OSError:
                logger.debug("Unable to write avatar negative marker", exc_info=True)
        return None


def resolve_avatar(
    proto: str, manager: Any, contact_id: str
) -> tuple[bytes, str] | None:
    """Return ``(data, content_type)`` for a contact avatar, or ``None``.

    Dispatches to the protocol backend: WhatsApp reuses the on-disk cache via
    :func:`resolve_whatsapp_avatar`; Signal reads the local signal-cli avatar
    file.  Best-effort: never raises.
    """
    if not contact_id:
        return None
    try:
        if proto == "whatsapp":
            backend = manager.get("whatsapp")
            rest = getattr(backend, "_rest", None)
            path = resolve_whatsapp_avatar(rest, contact_id)
            if path is None:
                return None
            try:
                return path.read_bytes(), "image/jpeg"
            except OSError:
                return None
        if proto == "signal":
            backend = manager.get("signal")
            resolver = getattr(backend, "get_profile_photo_bytes", None)
            if resolver is None:
                return None
            return resolver(contact_id)
        return None
    except Exception:
        logger.debug("Avatar resolution failed for %s", proto, exc_info=True)
        return None
