"""Attachment/media helpers behind the ``media.*`` operations.

Extension tables, the per-protocol media roots and the cache-busting
attachment version (``mtime_ns-size``) used by ``messages.page``.
"""

from __future__ import annotations

import logging
import mimetypes
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".heic",
    ".heif",
    ".tif",
    ".tiff",
}


_VIDEO_EXTENSIONS = {
    ".mp4",
    ".m4v",
    ".mov",
    ".webm",
    ".mkv",
    ".3gp",
    ".avi",
    ".mpg",
    ".mpeg",
}


def _allowed_media_root(manager: Any, proto: str) -> Path:
    from protocols.db import CACHE_DIR
    from protocols.rpc import SIGNAL_CLI_ATTACHMENTS_DIR

    if proto == "signal":
        return Path(SIGNAL_CLI_ATTACHMENTS_DIR).resolve()
    if proto == "whatsapp":
        instance = manager.get(proto)
        if instance is not None and hasattr(instance, "_ensure_media_dir"):
            return instance._ensure_media_dir().resolve()
        return (CACHE_DIR / "whatsapp-media").resolve()
    if proto == "telegram":
        try:
            from protocols.telegram import _media_dir

            return _media_dir().resolve()
        except ImportError:
            return (Path(tempfile.gettempdir()) / "telegram-media").resolve()
    raise ValueError(f"Unsupported protocol: {proto}")


def _attachment_version(
    manager, proto: str, root: Path, attachment_id: str
) -> str | None:
    """Versione cache-busting ('mtime_ns-size') dell'allegato già su disco.

    Solo fast-path locale (mai lazy download). root è pre-risolta una volta per
    richiesta. Ogni candidate è resolve()+is_relative_to(root)+is_file() prima
    dello stat: nessun leak di mtime/size di file arbitrari (traversal/symlink
    fuori root -> None).
    """
    if not attachment_id:
        return None
    bare = attachment_id.split("?", 1)[0]
    name = Path(bare).name or bare
    if proto == "signal":
        candidates = (root / name, root / bare)
    elif proto == "whatsapp":
        candidates = (root / name,)
    elif proto == "telegram":
        candidates = (Path(bare), root / name)
    else:
        return None
    root_resolved = root.resolve()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except (OSError, ValueError):
            continue
        if not resolved.is_relative_to(root_resolved):
            continue
        try:
            st = resolved.stat()
        except OSError:
            continue
        if resolved.is_file() and st.st_size >= 0:
            return f"{st.st_mtime_ns}-{st.st_size}"
    return None


def _attach_media_versions(manager, proto: str, messages: list[dict[str, Any]]) -> None:
    """Arricchisce attachment['version'] per il cache-busting; best-effort.

    root risolta una sola volta. Qualunque errore (incluso OSError/PermissionError
    dal mkdir di _ensure_media_dir per WhatsApp) degrada silenziosamente a
    'nessuna version': la lista messaggi non deve mai fallire per un cache-buster.
    """
    try:
        root = _allowed_media_root(manager, proto)
    except Exception:  # noqa: BLE001 — funzionalità accessoria, mai 500
        return
    memo: dict[str, str | None] = {}
    for message in messages:
        att = message.get("attachment")
        if not att or not att.get("attachment_id"):
            continue
        att_id = att["attachment_id"]
        if att_id not in memo:
            try:
                memo[att_id] = _attachment_version(manager, proto, root, att_id)
            except Exception:  # noqa: BLE001
                memo[att_id] = None
        version = memo[att_id]
        if version:
            att["version"] = version


_AUDIO_MIME_BY_EXT: dict[str, str] = {
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".oga": "audio/ogg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
}


def _media_content_type(path: Path, *, prefer_audio: bool = False) -> str | None:
    """Return deterministic audio MIME types and guess all other types.

    ``.webm`` è ambiguo (video o audio): ``prefer_audio`` lo serve come
    ``audio/webm`` quando il messaggio lo classifica come audio.
    """
    ext = path.suffix.lower()
    if ext in _AUDIO_MIME_BY_EXT:
        return _AUDIO_MIME_BY_EXT[ext]
    if prefer_audio and ext == ".webm":
        return "audio/webm"
    return mimetypes.guess_type(path.name)[0]


def _attachment_content_type(proto: str, attachment_id: str) -> str | None:
    import protocols.db as backend
    from protocols.db import _DB_LOCK

    with _DB_LOCK:
        try:
            connection = sqlite3.connect(backend.DB_FILE)
            try:
                row = connection.execute(
                    "SELECT content_type, msg_type FROM messages "
                    "WHERE protocol = ? AND attachment_id = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (proto, attachment_id),
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            return None
    if not row:
        return None
    return row[0] or ("image/*" if row[1] == "image" else None)


def _attachment_media_kind(proto: str, attachment_id: str) -> str | None:
    import protocols.db as backend
    from protocols.db import _DB_LOCK

    with _DB_LOCK:
        try:
            connection = sqlite3.connect(backend.DB_FILE)
            try:
                row = connection.execute(
                    "SELECT media_kind FROM messages "
                    "WHERE protocol = ? AND attachment_id = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (proto, attachment_id),
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            return None
    if not row:
        return None
    return row[0]


def _is_thumbnail_candidate(path: Path, proto: str, attachment_id: str) -> bool:
    content_type = (_attachment_content_type(proto, attachment_id) or "").lower()
    suffix = path.suffix.lower()
    is_image = content_type.startswith("image/") or suffix in _IMAGE_EXTENSIONS
    return (
        is_image
        and suffix not in {".gif", ".heic", ".heif"}
        and content_type
        not in {
            "image/gif",
            "image/heic",
            "image/heif",
        }
    )


def _is_video_candidate(path: Path, proto: str, attachment_id: str) -> bool:
    content_type = (_attachment_content_type(proto, attachment_id) or "").lower()
    # Un audio (es. voice webm/m4a) non deve mai passare per la pipeline
    # thumbnail video, anche quando l'estensione `.webm` è nella lista video.
    if content_type.startswith("audio/"):
        return False
    if not content_type and _attachment_media_kind(proto, attachment_id) == "audio":
        return False
    return content_type.startswith("video/") or path.suffix.lower() in _VIDEO_EXTENSIONS


# -- quoted attachment lookup (``media.quoted_attachment``) -------------------

_QUOTE_IMAGE_CLAUSE = (
    "AND (content_type LIKE 'image/%' OR lower(attachment_id) LIKE '%.jpg' "
    "OR lower(attachment_id) LIKE '%.jpeg' OR lower(attachment_id) LIKE '%.png' "
    "OR lower(attachment_id) LIKE '%.gif' OR lower(attachment_id) LIKE '%.webp')"
)


def _quoted_id_by_filename(
    protocol: str, contact_key: str, quote_text: str, content_type: str | None
) -> str | None:
    """Attachment id of the quoted image found by file name (quotes without
    metadata, e.g. a Signal quote of an image: ``IMG_1303.jpg — 🖼️ Immagine``
    with no ``quote_timestamp``/``quote_attachment_id``).  The file name shows
    up in the quoted message's ``attachment_info``."""
    import re

    import protocols.db as backend
    from protocols.db import _DB_LOCK

    if not str(content_type or "").lower().startswith("image/"):
        return None
    match = re.search(
        r"([A-Za-z0-9][\w.\- ]*\.(?:jpe?g|png|gif|webp))\b",
        quote_text,
        re.IGNORECASE,
    )
    if not match:
        return None
    filename = match.group(1).strip()
    try:
        with _DB_LOCK:
            connection = sqlite3.connect(backend.DB_FILE)
            try:
                row = connection.execute(
                    "SELECT attachment_id FROM messages "
                    "WHERE protocol = ? AND contact_number = ? "
                    "AND attachment_id IS NOT NULL "
                    "AND (attachment_info LIKE ? OR attachment_id LIKE ?) "
                    + _QUOTE_IMAGE_CLAUSE
                    + " LIMIT 1",
                    (protocol, contact_key, f"%{filename}%", f"%{filename}%"),
                ).fetchone()
            finally:
                connection.close()
    except (sqlite3.Error, OSError):
        return None
    if row is None or not row[0]:
        return None
    return str(row[0])


def _quoted_id_by_message_id(
    protocol: str, contact_key: str, reply_to_message_id: str
) -> str | None:
    """Quoted image by ``msg_id`` (Telegram/WhatsApp): the backend persists
    ``reply_to_message_id`` but not always the quoted timestamp or attachment,
    and an id match also covers targets that were not cached on arrival."""
    import protocols.db as backend
    from protocols.db import _DB_LOCK

    try:
        with _DB_LOCK:
            connection = sqlite3.connect(backend.DB_FILE)
            try:
                row = connection.execute(
                    "SELECT attachment_id FROM messages "
                    "WHERE protocol = ? AND contact_number = ? "
                    "AND attachment_id IS NOT NULL "
                    "AND (msg_id = ? OR msg_id LIKE '%_' || ?) "
                    + _QUOTE_IMAGE_CLAUSE
                    + " LIMIT 1",
                    (protocol, contact_key, reply_to_message_id, reply_to_message_id),
                ).fetchone()
            finally:
                connection.close()
    except (sqlite3.Error, OSError):
        return None
    if row is None or not row[0]:
        return None
    return str(row[0])


def quoted_attachment_id(
    protocol: str,
    contact_key: str,
    *,
    quote_timestamp: int | None,
    reply_to_message_id: str | None,
    quote_text: str,
    content_type: str | None,
) -> str | None:
    """Attachment id of the image quoted by a reply, in the same chat.

    The exact timestamp match comes first: it avoids false positives when
    nearby messages (+-2s) coexist (a just-sent image and its reply).  The
    +-2s window is used only when no message has the exact timestamp (drift).
    Without a timestamp: by quoted message id, then by file name.
    """
    import protocols.db as backend
    from protocols.db import _DB_LOCK

    ts = quote_timestamp
    if not ts:
        if reply_to_message_id:
            resolved = _quoted_id_by_message_id(
                protocol, contact_key, reply_to_message_id
            )
            if resolved:
                return resolved
        return _quoted_id_by_filename(protocol, contact_key, quote_text, content_type)
    try:
        with _DB_LOCK:
            connection = sqlite3.connect(backend.DB_FILE)
            try:
                row = connection.execute(
                    "SELECT attachment_id FROM messages "
                    "WHERE protocol = ? AND contact_number = ? "
                    "AND timestamp = ? AND attachment_id IS NOT NULL "
                    + _QUOTE_IMAGE_CLAUSE
                    + " LIMIT 1",
                    (protocol, contact_key, ts),
                ).fetchone()
                if row is None:
                    exists = connection.execute(
                        "SELECT 1 FROM messages WHERE protocol = ? AND contact_number = ? "
                        "AND timestamp = ? LIMIT 1",
                        (protocol, contact_key, ts),
                    ).fetchone()
                    if exists:
                        # The quoted message exists but is not an image: no
                        # thumbnail (never pick a nearby message).
                        return None
                    row = connection.execute(
                        "SELECT attachment_id FROM messages "
                        "WHERE protocol = ? AND contact_number = ? "
                        "AND ABS(timestamp - ?) <= 2000 AND attachment_id IS NOT NULL "
                        + _QUOTE_IMAGE_CLAUSE
                        + " ORDER BY ABS(timestamp - ?) LIMIT 1",
                        (protocol, contact_key, ts, ts),
                    ).fetchone()
            finally:
                connection.close()
    except (sqlite3.Error, OSError):
        return None
    if row is None or not row[0]:
        # No timestamp match: image quotes without metadata resolve by name.
        return _quoted_id_by_filename(protocol, contact_key, quote_text, content_type)
    return str(row[0])
