"""Map thumbnails of shared WhatsApp locations.

WhatsApp ships a small JPEG map preview with every shared position
(``jpegThumbnail``, or the ``body`` itself on WAHA WEBJS).  It is saved on
disk keyed by the coordinates, so the web UI can show it as the card of the
Google Maps link generated for the message, without new DB columns and
without contacting Google.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_THUMB_BYTES = 256 * 1024
_MAPS_URL_RE = re.compile(
    r"^https://maps\.google\.com/\?q=(-?\d{1,3}\.\d{6}),(-?\d{1,3}\.\d{6})$"
)


def maps_url(lat: float, lng: float) -> str:
    """Return the Google Maps link used for a shared location."""
    return f"https://maps.google.com/?q={lat:.6f},{lng:.6f}"


def parse_maps_url(url: str) -> tuple[float, float] | None:
    """Return ``(lat, lng)`` for a link built by :func:`maps_url`, else ``None``."""
    match = _MAPS_URL_RE.match(url or "")
    if not match:
        return None
    lat, lng = float(match.group(1)), float(match.group(2))
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return lat, lng


def _thumbs_dir() -> Path:
    from protocols import db  # late lookup: tests patch ``db.CACHE_DIR``

    return Path(db.CACHE_DIR) / "location-thumbs"


def thumbnail_path(lat: float, lng: float) -> Path:
    return _thumbs_dir() / f"{lat:.6f}_{lng:.6f}.jpg"


def decode_thumbnail(value: object) -> bytes | None:
    """Decode a WAHA thumbnail (base64 string or Buffer JSON) to JPEG bytes."""
    data: bytes | None = None
    if isinstance(value, str) and value.strip():
        compact = "".join(value.split())
        if compact.lower().startswith("data:"):
            compact = compact.split(",", 1)[-1]
        try:
            data = base64.b64decode(compact + "=" * (-len(compact) % 4))
        except (binascii.Error, ValueError):
            return None
    elif isinstance(value, dict) and isinstance(value.get("data"), list):
        try:
            data = bytes(value["data"])
        except (TypeError, ValueError):
            return None
    if not data or len(data) > _MAX_THUMB_BYTES:
        return None
    return data if data.startswith(b"\xff\xd8\xff") else None


def save_thumbnail(lat: float, lng: float, data: bytes) -> None:
    """Persist *data* as the map thumbnail for ``(lat, lng)`` (first one wins)."""
    path = thumbnail_path(lat, lng)
    if path.is_file():
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except OSError:
        logger.debug("location thumbnail save failed for %s", path, exc_info=True)


def load_thumbnail(lat: float, lng: float) -> bytes | None:
    try:
        return thumbnail_path(lat, lng).read_bytes()
    except OSError:
        return None
