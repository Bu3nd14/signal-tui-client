"""Blob resolution: attachments, thumbnails and quote media as local files.

Every function returns a :class:`~server.models_api.BlobOut` (a local path plus
metadata) or raises :class:`~server.api.ApiError`; the adapters deliver the
file their own way.  Attachment paths are always confined to the protocol's
media root (symlinks and ``..`` resolved first): a path outside it is never
handed out.

``get_attachment_chunk`` (partial fetch of a not yet downloaded WhatsApp or
Telegram video) is deliberately *not* an operation: it only feeds the video
thumbnail generation here, so a thumbnail never needs the full download.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Any

from server import media as _media
from server import thumbnails as _thumbnails
from server import video_thumbs as _video_thumbs
from server.api import ApiError
from server.models_api import BlobOut
from server.thumbnails import THUMB_WIDTHS

logger = logging.getLogger(__name__)

__all__ = [
    "THUMB_WIDTHS",
    "attachment_blob",
    "avatar_blob",
    "blob_for",
    "quote_blob",
    "thumbnail_blob",
]


def blob_for(
    path: Path, content_type: str | None, *, immutable: bool = False
) -> BlobOut:
    """Describe the existing local file *path* (``version`` is ``mtime_ns-size``,
    the cache-busting token ``messages.page`` puts on attachments)."""
    try:
        stat = path.stat()
    except OSError:
        return BlobOut(path=str(path), content_type=content_type, immutable=immutable)
    return BlobOut(
        path=str(path),
        content_type=content_type,
        size=stat.st_size,
        version=f"{stat.st_mtime_ns}-{stat.st_size}",
        immutable=immutable,
    )


def _confined_attachment(
    manager: Any, proto: str, attachment_id: str, root: Path
) -> Path | None:
    """The backend's local file for the attachment, only if it is a regular file
    inside *root*; ``None`` otherwise (missing, unresolvable or outside)."""
    path = None
    try:
        resolved = manager.get_attachment_path(proto, attachment_id)
        path = Path(resolved).resolve() if resolved else None
    except Exception:
        logger.warning(
            "media 404 proto=%s attachment_id=%s: resolve failed",
            proto,
            attachment_id,
            exc_info=True,
        )
        return None
    if path is None or not path.is_file() or not path.is_relative_to(root):
        logger.warning(
            "media 404 proto=%s attachment_id=%s resolved=%s",
            proto,
            attachment_id,
            path,
        )
        return None
    return path


def _original_blob(path: Path, proto: str, attachment_id: str) -> BlobOut:
    # ``.webm`` is ambiguous (video or audio): the persisted media_kind decides.
    prefer_audio = (
        path.suffix.lower() == ".webm"
        and _media._attachment_media_kind(proto, attachment_id) == "audio"
    )
    return blob_for(path, _media._media_content_type(path, prefer_audio=prefer_audio))


def attachment_blob(manager: Any, proto: str, attachment_id: str) -> BlobOut:
    """The attachment file (``not_found`` when missing or outside the media root)."""
    root = _media._allowed_media_root(manager, proto)
    path = _confined_attachment(manager, proto, attachment_id, root)
    if path is None:
        raise ApiError("not_found", "Not Found")
    logger.info(
        "media ok proto=%s attachment_id=%s path=%s", proto, attachment_id, path
    )
    return _original_blob(path, proto, attachment_id)


def thumbnail_blob(manager: Any, proto: str, attachment_id: str, width: int) -> BlobOut:
    """A JPEG thumbnail (``immutable``) of an image or video attachment.

    Anything that cannot be thumbnailed (animated/unsupported images, or a
    *width* that is not a thumbnail width) comes back as the original
    attachment blob; a video whose thumbnail cannot be
    produced is ``unprocessable`` ("Video thumbnail unavailable").
    """
    if width not in THUMB_WIDTHS:
        return attachment_blob(manager, proto, attachment_id)
    root = _media._allowed_media_root(manager, proto)
    video_request = _media._is_video_candidate(
        Path(attachment_id), proto, attachment_id
    )
    chunk_available = False
    if video_request and proto in {"whatsapp", "telegram"}:
        # Head/tail chunks of a remote video: no full download for a thumbnail.
        thumb, chunk_available = _video_thumbs._chunk_video_thumbnail(
            manager, proto, attachment_id, width
        )
        if thumb is not None:
            return blob_for(thumb, "image/jpeg", immutable=True)
    path = _confined_attachment(manager, proto, attachment_id, root)
    if path is None:
        if chunk_available:
            raise ApiError("unprocessable", "Video thumbnail unavailable")
        raise ApiError("not_found", "Not Found")
    logger.info(
        "media ok proto=%s attachment_id=%s path=%s w=%s",
        proto,
        attachment_id,
        path,
        width,
    )
    thumb = _thumbnails._thumbnail(path, proto, attachment_id, width)
    if thumb is not None:
        return blob_for(thumb, "image/jpeg", immutable=True)
    if video_request or _media._is_video_candidate(path, proto, attachment_id):
        thumb = _video_thumbs._video_thumbnail(path, proto, attachment_id, width)
        if thumb is None:
            raise ApiError("unprocessable", "Video thumbnail unavailable")
        return blob_for(thumb, "image/jpeg", immutable=True)
    return _original_blob(path, proto, attachment_id)


def quote_blob(proto: str, message_row_id: int, width: int | None) -> BlobOut:
    """The quote thumbnail file saved for message row *message_row_id*
    (confined to ``<CACHE_DIR>/quote-thumbs``); a thumbnail of it when *width*
    is a thumbnail width."""
    import sqlite3

    import protocols.db as backend
    from protocols.db import _DB_LOCK

    with _DB_LOCK:
        try:
            connection = sqlite3.connect(backend.DB_FILE)
            try:
                row = connection.execute(
                    "SELECT quote_attachment_path FROM messages "
                    "WHERE id = ? AND protocol = ?",
                    (message_row_id, proto),
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            logger.exception(
                "Quote media lookup database error: proto=%s row_id=%s",
                proto,
                message_row_id,
            )
            raise ApiError("internal", "Database error") from None

    stored = row[0] if row else None
    root = (Path(backend.CACHE_DIR) / "quote-thumbs").resolve()
    path = Path(stored).resolve() if stored else None
    if path is None or not path.is_file() or path.parent != root:
        logger.warning(
            "quote media 404 proto=%s row_id=%s resolved=%s",
            proto,
            message_row_id,
            path,
        )
        raise ApiError("not_found", "Not Found")

    served_path = path
    immutable = False
    if width in THUMB_WIDTHS:
        thumb = _thumbnails._thumbnail(path, proto, str(message_row_id), width)
        if thumb is not None:
            served_path = thumb
            immutable = True
    return blob_for(
        served_path, mimetypes.guess_type(served_path.name)[0], immutable=immutable
    )


def avatar_blob(proto: str, manager: Any, contact_id: str) -> BlobOut:
    """The contact's profile picture as a file (``not_found``: no picture)."""
    from server.avatar_cache import resolve_avatar_file

    result = resolve_avatar_file(proto, manager, contact_id)
    if result is None:
        raise ApiError("not_found", "No profile picture")
    path, content_type = result
    return blob_for(path, content_type)
