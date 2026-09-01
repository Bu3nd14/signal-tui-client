"""REST API for contacts, persisted messages, media, and message sending."""

import asyncio
import hashlib
import logging
import mimetypes
import os
import re
import sqlite3
import tempfile
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote as url_quote
from urllib.parse import urlsplit

from models import is_media_quote_placeholder_composite
from web.bridge import push_event

logger = logging.getLogger(__name__)

_PROTOCOLS = {"signal", "whatsapp", "telegram"}
_MAX_TEXT_LENGTH = 64 * 1024
_THUMB_WIDTHS = {96, 240, 480}
_THUMB_CACHE_LIMIT = 500 * 1024 * 1024
_THUMB_LOCKS: dict[Path, threading.Lock] = {}
_THUMB_LOCKS_GUARD = threading.Lock()

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


def _effective_host(request: Any) -> str:
    """Return the browser-facing host, trusting proxies only for HTTPS origins."""
    forwarded = request.headers.get("x-forwarded-host")
    origin = request.headers.get("origin", "")
    forwarded_proto = request.headers.get("x-forwarded-proto")
    if (
        forwarded
        and urlsplit(origin).scheme.lower() == "https"
        and (
            not forwarded_proto
            or forwarded_proto.split(",", 1)[0].strip().lower() == "https"
        )
    ):
        return forwarded.split(",", 1)[0].strip().lower()
    return (request.headers.get("host") or "").lower()


@lru_cache(maxsize=1)
def _emoji_categories() -> list[dict[str, Any]]:
    import emoji

    from emoji_data import PREDEFINED_CATEGORIES

    aliases = {
        char: data.get("en", "").strip(":")
        for char, data in emoji.EMOJI_DATA.items()
        if data.get("en")
    }
    return [
        {
            "category": name,
            "icon": icon,
            "emojis": list(chars),
            "aliases": {char: aliases.get(char, "") for char in chars},
        }
        for name, icon, chars in PREDEFINED_CATEGORIES
    ]


def _infer_attachment_type(attachment_id: str, content_type: str | None) -> str | None:
    if content_type and content_type.strip():
        return content_type
    path = attachment_id.split("?", 1)[0]
    suffix = Path(path).suffix.lower()
    if suffix not in _IMAGE_EXTENSIONS:
        return None
    guessed_type, _ = mimetypes.guess_type(path)
    if guessed_type and guessed_type.startswith("image/"):
        return guessed_type
    return f"image/{'jpeg' if suffix in {'.jpg', '.jpeg'} else suffix[1:]}"


def _is_whatsapp_synthetic_text(text: str | None) -> bool:
    stripped = (text or "").strip()
    if not stripped.lower().startswith("media:"):
        return False

    media_identity = stripped[len("media:") :].strip()
    if not media_identity:
        return False
    return bool(
        re.fullmatch(r"https?://\S+", media_identity, re.IGNORECASE)
        or re.fullmatch(
            r"(?:false|true)_[^\s@]*@[^\s@]+", media_identity, re.IGNORECASE
        )
        or re.fullmatch(r"\d+:\d+", media_identity)
        or re.fullmatch(r"\S+", media_identity)
    )


def _unread_counts() -> dict[tuple[str, str], int]:
    import protocols.db as backend
    from protocols.db import _DB_LOCK

    with _DB_LOCK:
        try:
            connection = sqlite3.connect(backend.DB_FILE)
            try:
                rows = connection.execute(
                    "SELECT protocol, contact_number, COUNT(*) FROM messages "
                    "WHERE is_mine = 0 AND read = 0 "
                    "GROUP BY protocol, contact_number"
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.Error:
            return {}
    return {(row[0], row[1]): row[2] for row in rows}


def _contact_payload(
    contact: Any, unread: dict[tuple[str, str], int]
) -> dict[str, Any]:
    """Serializza un contatto nel formato usato dalla web UI."""
    extras = dict(contact.extras)
    return {
        "id": str(contact.id),
        "display_name": str(contact.display_name),
        "protocol": str(contact.protocol),
        "extras": extras,
        "last_message_ts": int(extras.get("last_message_ts", 0) or 0),
        "unread": unread.get((contact.protocol, contact.id), 0),
    }


def _message_edit_id(row: sqlite3.Row | dict[str, Any]) -> str | None:
    if (
        not row["is_mine"]
        or row["msg_type"] != "text"
        or row["status"] in {"pending", "failed"}
    ):
        return None
    if row["protocol"] == "signal":
        return str(row["msg_id"] or row["timestamp"])
    return str(row["msg_id"]) if row["msg_id"] else None


def _message_row_for_edit(
    protocol: str, contact_id: str, message_id: str
) -> dict[str, Any] | None:
    import protocols.db as backend
    from protocols.db import _DB_LOCK

    with _DB_LOCK:
        try:
            connection = sqlite3.connect(backend.DB_FILE)
            connection.row_factory = sqlite3.Row
            try:
                row = connection.execute(
                    "SELECT id, msg_id, text, is_mine, timestamp, protocol, msg_type, "
                    "status FROM messages WHERE protocol = ? AND contact_number = ? "
                    "AND (msg_id = ? OR (? = 'signal' AND msg_id IS NULL "
                    "AND timestamp = CAST(? AS INTEGER))) "
                    "ORDER BY CASE WHEN msg_id = ? THEN 0 ELSE 1 END LIMIT 1",
                    (
                        protocol,
                        contact_id,
                        message_id,
                        protocol,
                        message_id,
                        message_id,
                    ),
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            from fastapi import HTTPException

            logger.exception(
                "Web edit lookup database error: protocol=%s contact=%s",
                protocol,
                contact_id,
            )
            raise HTTPException(status_code=500, detail="Database error") from None
    return dict(row) if row else None


def _message_row_for_reaction(
    protocol: str, contact_id: str, message_id: str
) -> dict[str, Any] | None:
    import protocols.db as backend
    from protocols.db import _DB_LOCK

    with _DB_LOCK:
        try:
            connection = sqlite3.connect(backend.DB_FILE)
            connection.row_factory = sqlite3.Row
            try:
                row = connection.execute(
                    "SELECT id, msg_id, is_mine, timestamp, protocol FROM messages "
                    "WHERE protocol = ? AND contact_number = ? AND "
                    "(msg_id = ? "
                    "OR (? = 'signal' AND timestamp = CAST(? AS INTEGER)) "
                    "OR (? = 'signal' AND id = CAST(? AS INTEGER))) "
                    "ORDER BY CASE WHEN msg_id = ? THEN 0 ELSE 1 END LIMIT 1",
                    (
                        protocol,
                        contact_id,
                        message_id,
                        protocol,
                        message_id,
                        protocol,
                        message_id,
                        message_id,
                    ),
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            from fastapi import HTTPException

            logger.exception(
                "Web reaction lookup database error: protocol=%s contact=%s",
                protocol,
                contact_id,
            )
            raise HTTPException(status_code=500, detail="Database error") from None
    return dict(row) if row else None


def _persist_message_edit(
    protocol: str, contact_id: str, message_id: str, new_text: str
) -> int:
    import protocols.db as backend
    from protocols.db import _DB_LOCK

    with _DB_LOCK:
        connection = sqlite3.connect(backend.DB_FILE)
        try:
            cursor = connection.execute(
                "UPDATE messages SET text = ?, edited = 1 "
                "WHERE protocol = ? AND contact_number = ? "
                "AND (msg_id = ? OR (? = 'signal' AND msg_id IS NULL "
                "AND timestamp = CAST(? AS INTEGER)))",
                (
                    new_text,
                    protocol,
                    contact_id,
                    message_id,
                    protocol,
                    message_id,
                ),
            )
            connection.commit()
            return cursor.rowcount
        finally:
            connection.close()


def _aggregate_reactions(
    message_rows: list[sqlite3.Row], reaction_rows: list[dict[str, Any]]
) -> dict[int, list[dict[str, Any]]]:
    """Aggregate persisted reactions for each matching message row."""
    result: dict[int, list[dict[str, Any]]] = {}
    for message_row in message_rows:
        message_timestamp = int(message_row["timestamp"])
        message_ids = {str(message_timestamp)}
        if message_row["msg_id"] is not None:
            message_ids.add(str(message_row["msg_id"]))

        grouped: dict[str, dict[str, Any]] = {}
        for reaction_row in reaction_rows:
            target_msg_id = reaction_row.get("target_msg_id")
            matches_id = target_msg_id is not None and str(target_msg_id) in message_ids
            matches_timestamp = (
                reaction_row.get("target_timestamp") == message_timestamp
            )
            if not matches_id and not matches_timestamp:
                continue

            emoji = str(reaction_row["emoji"])
            reaction_timestamp = int(reaction_row["timestamp"])
            aggregate = grouped.setdefault(
                emoji,
                {
                    "emoji": emoji,
                    "count": 0,
                    "is_mine": False,
                    "authors": set(),
                    "first_timestamp": reaction_timestamp,
                },
            )
            aggregate["count"] += int(reaction_row.get("count") or 0)
            aggregate["is_mine"] = aggregate["is_mine"] or bool(
                reaction_row.get("is_mine")
            )
            author = str(reaction_row.get("author") or "").strip()
            if author:
                aggregate["authors"].add(author)
            aggregate["first_timestamp"] = min(
                aggregate["first_timestamp"], reaction_timestamp
            )

        if grouped:
            ordered = sorted(
                grouped.values(),
                key=lambda item: (-item["count"], item["first_timestamp"]),
            )
            result[int(message_row["id"])] = [
                {
                    "emoji": item["emoji"],
                    "count": item["count"],
                    "is_mine": item["is_mine"],
                    "authors": sorted(item["authors"]),
                }
                for item in ordered
            ]
    return result


def _messages(protocol: str, contact_id: str) -> list[dict[str, Any]]:
    import protocols.db as backend
    from protocols.db import _DB_LOCK, _reactions_for_contact

    with _DB_LOCK:
        try:
            connection = sqlite3.connect(backend.DB_FILE)
            connection.row_factory = sqlite3.Row
            try:
                rows = connection.execute(
                    "SELECT id, msg_id, text, is_mine, timestamp, contact_number, "
                    "sender, attachment_id, attachment_info, content_type, media_kind, "
                    "protocol, msg_type, "
                    "quote_text, quote_timestamp, quote_author, quote_attachment_id, "
                    "quote_content_type, quote_attachment_path, status, edited, read, "
                    "reply_to_message_id "
                    "FROM messages WHERE protocol = ? AND contact_number = ? "
                    "ORDER BY timestamp, id",
                    (protocol, contact_id),
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.Error:
            return []

    messages = []
    for row in rows:
        attachment = None
        text = row["text"] or ""
        suppress_attachment_info_caption = False
        if row["attachment_id"]:
            attachment_id = row["attachment_id"]
            attachment_path = attachment_id.split("?", 1)[0]
            attachment = {
                "attachment_id": attachment_id,
                "name": row["attachment_info"] or Path(attachment_path).name,
                "type": (
                    row["content_type"]
                    or ("image/*" if row["msg_type"] == "image" else None)
                    or _infer_attachment_type(attachment_id, None)
                ),
                "media_kind": row["media_kind"],
            }
            logger.debug(
                "web messages proto=%s id=%s attachment_id=%s content_type=%s type=%s",
                row["protocol"],
                row["msg_id"] or str(row["id"]),
                attachment_id,
                row["content_type"],
                attachment["type"],
            )
            if (
                row["msg_type"] == "image"
                or (attachment["type"] or "").lower().startswith("image/")
            ) and text:
                # Caption: mantieni solo se reale; azzera placeholder media,
                # nome file e URL media WA ("Media: http://...").
                from models import is_media_quote_placeholder

                stripped = text.strip()
                info = str(row["attachment_info"] or "").strip()
                prefixed_info = bool(
                    info
                    and any(
                        stripped.casefold() == f"{label}: {info}".casefold()
                        for label in (
                            "immagine",
                            "image",
                            "media",
                            "video",
                            "audio",
                            "file",
                        )
                    )
                )
                if (
                    stripped == info
                    or is_media_quote_placeholder(text)
                    or prefixed_info
                    or (protocol == "whatsapp" and _is_whatsapp_synthetic_text(text))
                ):
                    text = ""
            elif text and (
                str(row["media_kind"] or "").strip().lower()
                in {"video", "audio", "document", "sticker", "voice"}
                or str(row["msg_type"] or "").strip().lower()
                in {"attachment", "video", "audio", "document", "sticker", "voice"}
            ):
                from models import is_media_quote_placeholder

                stripped = text.strip()
                info = str(row["attachment_info"] or "").strip()
                prefixed_info = bool(info and stripped.startswith(f"{info}:"))
                if (
                    is_media_quote_placeholder(text)
                    or prefixed_info
                    or (protocol == "whatsapp" and _is_whatsapp_synthetic_text(text))
                ):
                    text = ""
                    suppress_attachment_info_caption = prefixed_info
            elif protocol == "whatsapp" and text:
                from models import is_media_quote_placeholder

                if is_media_quote_placeholder(text) or _is_whatsapp_synthetic_text(
                    text
                ):
                    text = ""
            if (
                not text
                and row["attachment_info"]
                and not suppress_attachment_info_caption
            ):
                # Caption in attachment_info (es. invii WhatsApp che salvano la
                # caption qui, col text = URL media): usala se non è un nome file.
                info = str(row["attachment_info"] or "").strip()
                file_name = Path(str(row["attachment_id"] or "")).name
                from models import is_media_quote_placeholder

                if (
                    info
                    and info != file_name
                    # Un mimetype puro (es. "image/jpeg", "video/mp4") non è mai
                    # una caption: arriva da attachment_info usato come mimetype
                    # (reperto live WAHA su messaggi immagine inviati).
                    and not re.fullmatch(
                        r"[a-z0-9.+-]+/[a-z0-9.+-]+(?:\s*;.*)?", info.lower()
                    )
                    and not info.lower().endswith(
                        (
                            ".png",
                            ".jpg",
                            ".jpeg",
                            ".gif",
                            ".webp",
                            ".heic",
                            ".mp4",
                            ".mp3",
                            ".oga",
                            ".ogg",
                            ".opus",
                            ".aac",
                            ".m4a",
                            ".wav",
                            ".pdf",
                            ".doc",
                            ".docx",
                        )
                    )
                    and not is_media_quote_placeholder(info)
                    and not (
                        protocol == "whatsapp" and _is_whatsapp_synthetic_text(info)
                    )
                    and info.lower()
                    not in {
                        "photo",
                        "image",
                        "immagine",
                        "video",
                        "audio",
                        "document",
                        "documento",
                        "file",
                        "allegato",
                        "sticker",
                    }
                    and info.strip().lower()
                    not in {
                        "🖼️ image",
                        "🖼️ immagine",
                        "🖼️ photo",
                        "🎬 video",
                        "🎵 audio",
                        "🎨 sticker",
                        "📎 file",
                    }
                ):
                    text = info
        direction = "out" if row["is_mine"] else "in"
        messages.append(
            {
                "id": row["msg_id"] or str(row["id"]),
                "text": text,
                "direction": direction,
                "sender": row["sender"] or "",
                "timestamp": row["timestamp"],
                "attachment": attachment,
                "quote_text": row["quote_text"],
                "quote_timestamp": row["quote_timestamp"],
                "quote_author": row["quote_author"],
                "quote_attachment_id": row["quote_attachment_id"],
                "quote_content_type": row["quote_content_type"],
                "quote_thumb_url": _quote_thumb_url(row),
                "quote_media_placeholder": _quote_media_placeholder(row["quote_text"]),
                "status": (row["status"] or "sent") if direction == "out" else None,
                "read": bool(row["read"]),
                "edited": bool(row["edited"]),
                "edit_id": _message_edit_id(row),
            }
        )

    reactions_by_message = _aggregate_reactions(
        rows, _reactions_for_contact(protocol, contact_id)
    )
    for row, message in zip(rows, messages):
        reactions = reactions_by_message.get(int(row["id"]))
        if reactions:
            message["reactions"] = reactions
    return messages


def _telegram_reaction_snapshot(
    contact_id: str, message_id: str, reaction_emoji: str
) -> list[dict[str, Any]]:
    message = next(
        (
            item
            for item in _messages("telegram", contact_id)
            if str(item["id"]) == message_id
        ),
        {},
    )
    snapshot: list[dict[str, Any]] = []
    for reaction in message.get("reactions", []):
        count = int(reaction.get("count") or 0)
        authors = [
            author
            for author in reaction.get("authors", [])
            if author not in {"You", "Tu"}
        ]
        if reaction.get("is_mine"):
            count -= 1
        if count > 0:
            snapshot.append(
                {
                    "emoji": str(reaction.get("emoji") or ""),
                    "count": count,
                    "is_mine": False,
                    "authors": authors,
                }
            )

    selected = next(
        (item for item in snapshot if item["emoji"] == reaction_emoji), None
    )
    if selected is None:
        selected = {
            "emoji": reaction_emoji,
            "count": 0,
            "is_mine": False,
            "authors": [],
        }
        snapshot.append(selected)
    selected["count"] += 1
    selected["is_mine"] = True
    selected["authors"].append("You")
    return snapshot


def _quote_thumb_url(row: sqlite3.Row | dict[str, Any]) -> str | None:
    """URL miniatura della quote, calcolato dal server (cascata).

    1. ``quote_attachment_path`` sotto ``CACHE_DIR/quote-thumbs`` (thumb
       estratta) → ``/api/quote-media`` (endpoint dedicato anti-traversal);
    2. ``quote_attachment_path`` esistente e immagine/video (anche sotto la media
       root del protocollo, es. allegato originale Signal/Telegram/WhatsApp)
       → ``/api/media`` con il path;
    3. ``quote_attachment_id`` + content type media (o estensione nota per
       quote legacy senza content_type) → ``/api/media`` con l'id;
    altrimenti ``None`` (nessuna miniatura: placeholder testuale).
    """
    proto = str(row["protocol"])
    from protocols.db import CACHE_DIR

    thumbs_root = (Path(CACHE_DIR) / "quote-thumbs").resolve()
    image_extensions = (".jpg", ".jpeg", ".png", ".gif", ".webp")

    def _media_url(value: str) -> str:
        encoded = "/".join(url_quote(segment, safe="") for segment in value.split("/"))
        return f"/api/media/{proto}/{encoded}?w=96"

    path = row["quote_attachment_path"]
    if path:
        resolved = Path(path).resolve()
        if resolved.parent == thumbs_root:
            return f"/api/quote-media/{proto}/{row['id']}?w=96"
        content_type = (row["quote_content_type"] or "").lower()
        is_image = content_type.startswith("image/") or str(path).lower().endswith(
            image_extensions
        )
        is_video = content_type.startswith("video/") or resolved.suffix.lower() in (
            _VIDEO_EXTENSIONS
        )
        if (is_image or is_video) and resolved.is_file():
            return _media_url(str(path))
    quote_attachment_id = row["quote_attachment_id"]
    quote_content_type = (row["quote_content_type"] or "").lower()
    is_image = quote_content_type.startswith("image/") or (
        not quote_content_type
        and str(quote_attachment_id).lower().endswith(image_extensions)
    )
    is_video = quote_content_type.startswith("video/") or (
        not quote_content_type
        and Path(str(quote_attachment_id)).suffix.lower() in _VIDEO_EXTENSIONS
    )
    if quote_attachment_id and (is_image or is_video):
        return _media_url(str(quote_attachment_id))
    # 4. Fallback (reply inviate senza metadati quote persistiti): risolvi
    # il media quotato dalla stessa chat tramite quote_timestamp e usa il
    # suo attachment_id (il backend non persiste i metadati all'invio).
    quote_text = str(row["quote_text"] or "").lower()
    has_video_name = any(
        re.search(rf"{re.escape(extension)}\b", quote_text)
        for extension in _VIDEO_EXTENSIONS
    )
    has_image_name = bool(re.search(r"\.(?:jpe?g|png|gif|webp)\b", quote_text))
    media_kind = (
        "video"
        if quote_content_type.startswith("video/")
        or has_video_name
        or "🎬" in quote_text
        else "image"
        if quote_content_type.startswith("image/")
        or has_image_name
        or "🖼️" in quote_text
        else None
    )
    quoted = _quoted_media_attachment_id(
        proto, str(row["contact_number"]), row["quote_timestamp"], media_kind
    )
    if not quoted and media_kind in {"image", "video"}:
        # 5. Fallback per nome file: la quote testuale cita il file (es.
        # "IMG_1303.jpg — 🖼️ Immagine"); lo cerchiamo tra gli allegati della
        # chat quando il timestamp quotato non è stato persistito dal backend.
        quoted = _quoted_media_by_filename(
            proto,
            str(row["contact_number"]),
            row["quote_text"],
            row["timestamp"],
            media_kind,
        )
    if not quoted:
        # 6. Fallback per id messaggio quotato (Telegram/WhatsApp persistono
        # reply_to_message_id ma non sempre il timestamp quotato né l'allegato).
        quoted = _quoted_media_by_message_id(
            proto,
            str(row["contact_number"]),
            row["reply_to_message_id"],
            media_kind,
        )
    if quoted:
        return _media_url(quoted)
    return None


def _quoted_media_clause(kind: Literal["image", "video"] | None) -> str:
    image = (
        "content_type LIKE 'image/%' OR lower(attachment_id) LIKE '%.jpg' "
        "OR lower(attachment_id) LIKE '%.jpeg' OR lower(attachment_id) LIKE '%.png' "
        "OR lower(attachment_id) LIKE '%.gif' OR lower(attachment_id) LIKE '%.webp'"
    )
    video = "content_type LIKE 'video/%' OR " + " OR ".join(
        f"lower(attachment_id) LIKE '%{extension}'"
        for extension in sorted(_VIDEO_EXTENSIONS)
    )
    if kind == "image":
        return f"AND ({image})"
    if kind == "video":
        return f"AND ({video})"
    return f"AND ({image} OR {video})"


def _quoted_media_attachment_id(
    proto: str,
    contact_number: str,
    quote_ts: Any,
    kind: Literal["image", "video"] | None = None,
) -> str | None:
    """Attachment id del media quotato (stessa chat).

    Match PRIMA esatto sul timestamp: evita falsi positivi quando messaggi
    vicini (±2s) convivono (es. un'immagine appena inviata e la reply).
    La finestra ±2s viene usata solo se il timestamp esatto non esiste affatto
    (drift del protocollo).
    """
    if not quote_ts:
        return None
    import protocols.db as backend
    from protocols.db import _DB_LOCK

    media_clause = _quoted_media_clause(kind)
    try:
        with _DB_LOCK:
            connection = sqlite3.connect(backend.DB_FILE)
            try:
                row = connection.execute(
                    "SELECT attachment_id FROM messages "
                    "WHERE protocol = ? AND contact_number = ? "
                    "AND timestamp = ? AND attachment_id IS NOT NULL "
                    + media_clause
                    + " LIMIT 1",
                    (proto, contact_number, quote_ts),
                ).fetchone()
                if row is None:
                    exists = connection.execute(
                        "SELECT 1 FROM messages WHERE protocol = ? AND contact_number = ? "
                        "AND timestamp = ? LIMIT 1",
                        (proto, contact_number, quote_ts),
                    ).fetchone()
                    if exists:
                        # Il messaggio quotato esiste ma non è un media compatibile:
                        # niente miniatura (mai prendere messaggi vicini).
                        return None
                    row = connection.execute(
                        "SELECT attachment_id FROM messages "
                        "WHERE protocol = ? AND contact_number = ? "
                        "AND ABS(timestamp - ?) <= 2000 AND attachment_id IS NOT NULL "
                        + media_clause
                        + " ORDER BY ABS(timestamp - ?) LIMIT 1",
                        (proto, contact_number, quote_ts, quote_ts),
                    ).fetchone()
            finally:
                connection.close()
    except sqlite3.Error:
        return None
    if row is None or not row[0]:
        return None
    return str(row[0])


def _quoted_media_by_filename(
    proto: str,
    contact_number: str,
    quote_text: Any,
    quoter_ts: Any,
    kind: Literal["image", "video"] | None = None,
) -> str | None:
    """Attachment id del media quotato, risolto per nome file nella quote.

    Usato quando il backend non ha persistito ``quote_timestamp`` né metadati
    dell'allegato quotato (es. quote Signal a un'immagine): la ``quote_text``
    contiene il nome del file (``IMG_1303.jpg — 🖼️ Immagine``) che troviamo
    tra gli allegati della stessa chat, scegliendo quello più vicino temporalmente
    (solo prima del messaggio quotante, per non citare messaggi futuri).
    """
    if not quote_text:
        return None
    image_pattern = r"jpe?g|png|gif|webp"
    video_pattern = "|".join(
        extension.removeprefix(".") for extension in sorted(_VIDEO_EXTENSIONS)
    )
    extension_pattern = (
        image_pattern
        if kind == "image"
        else video_pattern
        if kind == "video"
        else f"{image_pattern}|{video_pattern}"
    )
    match = re.search(
        rf"([A-Za-z0-9][\w.\- ]*\.(?:{extension_pattern}))\b",
        str(quote_text),
        re.IGNORECASE,
    )
    if not match:
        return None
    filename = match.group(1).strip()
    if not filename or not quoter_ts:
        return None
    import protocols.db as backend
    from protocols.db import _DB_LOCK

    media_clause = _quoted_media_clause(kind)
    try:
        with _DB_LOCK:
            connection = sqlite3.connect(backend.DB_FILE)
            try:
                rows = connection.execute(
                    "SELECT attachment_id, timestamp FROM messages "
                    "WHERE protocol = ? AND contact_number = ? "
                    "AND attachment_id IS NOT NULL AND timestamp < ? "
                    "AND (attachment_info LIKE ? OR attachment_id LIKE ?) "
                    + media_clause
                    + " "
                    "ORDER BY timestamp DESC LIMIT 5",
                    (
                        proto,
                        contact_number,
                        int(quoter_ts),
                        f"%{filename}%",
                        f"%{filename}%",
                    ),
                ).fetchall()
            finally:
                connection.close()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    # Più match: preferisci il più vicino al messaggio quotante.
    best = min(
        rows,
        key=lambda r: abs(int(r[1]) - int(quoter_ts)),
    )
    return str(best[0])


def _quoted_media_by_message_id(
    proto: str,
    contact_number: str,
    reply_to_message_id: Any,
    kind: Literal["image", "video"] | None = None,
) -> str | None:
    """Attachment id del media quotato per ``msg_id``.

    Telegram/WhatsApp persistono ``reply_to_message_id`` (= id del messaggio
    quotato, colonna ``msg_id``) ma non sempre il timestamp quotato: il match
    per id è preciso e copre anche i target non in cache all'arrivo.
    """
    if not reply_to_message_id:
        return None
    import protocols.db as backend
    from protocols.db import _DB_LOCK

    media_clause = _quoted_media_clause(kind)
    try:
        with _DB_LOCK:
            connection = sqlite3.connect(backend.DB_FILE)
            try:
                row = connection.execute(
                    "SELECT attachment_id FROM messages "
                    "WHERE protocol = ? AND contact_number = ? "
                    "AND attachment_id IS NOT NULL "
                    "AND (msg_id = ? OR msg_id LIKE '%_' || ?) "
                    + media_clause
                    + " LIMIT 1",
                    (
                        proto,
                        contact_number,
                        str(reply_to_message_id),
                        str(reply_to_message_id),
                    ),
                ).fetchone()
            finally:
                connection.close()
    except sqlite3.Error:
        return None
    if row is None or not row[0]:
        return None
    return str(row[0])


def _quote_media_placeholder(text: Any) -> bool:
    """True se ``text`` è un placeholder media tipizzato (o forma composta).

    Riusa il predicato della TUI così la web UI nasconde il placeholder quando
    la miniatura lo sostituisce (una caption reale resta visibile).
    """
    return is_media_quote_placeholder_composite(str(text or ""))


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


def _web_thumb_dir(proto: str) -> Path:
    cache_home = Path(
        os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")
    ).expanduser()
    return cache_home / "signal-tui-client" / "web-thumbs" / proto


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
    return content_type.startswith("video/") or path.suffix.lower() in _VIDEO_EXTENSIONS


def _thumb_lock(path: Path) -> threading.Lock:
    with _THUMB_LOCKS_GUARD:
        return _THUMB_LOCKS.setdefault(path, threading.Lock())


def _prune_thumb_cache(cache_root: Path) -> None:
    try:
        files = [path for path in cache_root.rglob("*.jpg") if path.is_file()]
        entries = sorted(
            ((path.stat().st_mtime_ns, path.stat().st_size, path) for path in files),
            reverse=True,
        )
        total = sum(size for _, size, _ in entries)
        for _, size, path in reversed(entries):
            if total <= _THUMB_CACHE_LIMIT:
                break
            path.unlink(missing_ok=True)
            total -= size
    except OSError:
        logger.debug("Unable to prune web thumbnail cache", exc_info=True)


def _thumbnail(path: Path, proto: str, attachment_id: str, width: int) -> Path | None:
    if not _is_thumbnail_candidate(path, proto, attachment_id):
        return None

    from PIL import Image

    thumb_dir = _web_thumb_dir(proto)
    digest = hashlib.sha1(
        f"{path}|{path.stat().st_mtime_ns}|{width}".encode(), usedforsecurity=False
    ).hexdigest()
    thumb = thumb_dir / f"{digest}.jpg"
    with _thumb_lock(thumb):
        if thumb.exists():
            try:
                thumb.touch()
            except OSError:
                pass
            return thumb
        tmp = thumb.with_suffix(".tmp")
        try:
            thumb_dir.mkdir(parents=True, exist_ok=True)
            with Image.open(path) as image:
                if image.format == "GIF" or (
                    image.format == "WEBP" and getattr(image, "is_animated", False)
                ):
                    return None
                image.draft("RGB", (width, width))
                with image.convert("RGB") as converted:
                    converted.thumbnail((width, width), Image.BILINEAR)
                    converted.save(tmp, "JPEG", quality=78, optimize=True)
            tmp.replace(thumb)
            _prune_thumb_cache(thumb_dir.parent)
            return thumb
        except (OSError, ValueError):
            tmp.unlink(missing_ok=True)
            logger.debug("Unable to generate web thumbnail for %s", path, exc_info=True)
            return None


def _quoted_sender(proto: str, contact_id: str, quote_ts: Any) -> str | None:
    """Sender del messaggio quotato nella stessa chat (match esatto su timestamp)."""
    if not quote_ts:
        return None
    import protocols.db as backend
    from protocols.db import _DB_LOCK

    try:
        with _DB_LOCK:
            connection = sqlite3.connect(backend.DB_FILE)
            try:
                row = connection.execute(
                    "SELECT sender FROM messages "
                    "WHERE protocol = ? AND contact_number = ? AND timestamp = ? "
                    "LIMIT 1",
                    (proto, contact_id, quote_ts),
                ).fetchone()
            finally:
                connection.close()
    except sqlite3.Error:
        return None
    if row is None or not row[0]:
        return None
    return str(row[0])


def _group_sender_resolver(manager: Any, proto: str):
    """Risolvi il sender (JID/numero) al nome della rubrica, come la TUI.

    Mappa una volta la rubrica aggregata (id/phone → display_name), poi per
    ogni sender: nome diretto, @lid→phone (WA), @c.us→numero, numero nudo.
    """
    phone_to_name: dict[str, str] = {}
    id_to_name: dict[str, str] = {}
    try:
        book = manager.list_address_book_sync(protocols={proto}, force=False)
    except Exception:  # noqa: BLE001 — rubrica best-effort
        book = []
    for contact in book:
        name = str(contact.display_name or "")
        if not name:
            continue
        cid = str(contact.id or "")
        phone = str(contact.phone or "")
        if cid:
            id_to_name[cid] = name
        if phone:
            phone_to_name[phone] = name

    def resolve(sender: str) -> str:
        if not sender:
            return sender
        if sender in id_to_name:
            return id_to_name[sender]
        is_jid = sender.endswith(("@lid", "@c.us"))
        local = sender.split("@", 1)[0]
        if sender.endswith("@lid"):
            try:
                phone = str(manager.get(proto)._jid_to_phone(sender) or "")
            except Exception:  # noqa: BLE001
                phone = ""
            if phone in phone_to_name:
                return phone_to_name[phone]
            if phone:
                return phone
        if sender.endswith("@c.us") and local in phone_to_name:
            return phone_to_name[local]
        if sender in phone_to_name:
            return phone_to_name[sender]
        if is_jid:
            # Fallback leggibile: numero senza @lid/@c.us (mai il JID grezzo).
            return local
        return sender

    return resolve


def create_api_router() -> Any:
    """Build the FastAPI router without making FastAPI a core dependency."""
    from fastapi import APIRouter, HTTPException, Request
    from fastapi.responses import FileResponse, JSONResponse

    router = APIRouter(prefix="/api")

    @router.get("/emoji")
    def emojis() -> JSONResponse:
        return JSONResponse(
            content=_emoji_categories(),
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @router.get("/reactions")
    async def reactions(request: Request, protocol: str) -> dict[str, list[str]]:
        if protocol != "telegram":
            return {"emojis": []}
        try:
            backend = request.app.state.manager.get(protocol)
            if backend is None or not hasattr(backend, "get_available_reactions"):
                return {"emojis": []}
            emojis = await asyncio.to_thread(backend.get_available_reactions)
        except Exception:
            logger.debug("Telegram available reactions request failed", exc_info=True)
            return {"emojis": []}
        return {"emojis": emojis if isinstance(emojis, list) else []}

    @router.post("/send")
    async def send(request: Request) -> dict[str, bool]:
        origin = request.headers.get("origin")
        if origin:
            origin_host = urlsplit(origin).netloc.lower()
            request_host = _effective_host(request)
            if not origin_host or origin_host != request_host:
                raise HTTPException(status_code=403, detail="Forbidden")

        is_multipart = (
            request.headers.get("content-type", "")
            .lower()
            .startswith("multipart/form-data")
        )
        upload_file = None
        try:
            if is_multipart:
                from web.uploads import _MAX_BYTES_BY_KIND

                content_length = request.headers.get("content-length")
                if (
                    content_length
                    and int(content_length)
                    > max(_MAX_BYTES_BY_KIND.values()) + 1024 * 1024
                ):
                    raise HTTPException(status_code=413, detail="Upload too large")
                form = await request.form()
                payload = dict(form)
                upload_file = form.get("file")
                if upload_file is None or not hasattr(upload_file, "read"):
                    raise HTTPException(status_code=400, detail="Invalid request")
                payload.pop("file", None)
            else:
                payload = await request.json()
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="Invalid request") from None
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid request")

        protocol = payload.get("protocol")
        contact_id = payload.get("contact_id")
        text = payload.get("text")
        if protocol not in _PROTOCOLS:
            raise HTTPException(status_code=400, detail="Invalid request")
        if not isinstance(contact_id, str) or not contact_id.strip():
            raise HTTPException(status_code=400, detail="Invalid request")
        if not isinstance(text, str) or len(text) > _MAX_TEXT_LENGTH:
            raise HTTPException(status_code=400, detail="Invalid request")
        if not text.strip() and upload_file is None:
            raise HTTPException(status_code=400, detail="Invalid request")

        quote_timestamp = payload.get("quote_timestamp")
        quote_author = payload.get("quote_author")
        quote_message = payload.get("quote_message")
        reply_to_message_id = payload.get("reply_to_message_id")
        quote_content_type = (
            payload.get("quote_content_type") if protocol == "signal" else None
        )
        quote_attachment_id = (
            payload.get("quote_attachment_id") if protocol == "signal" else None
        )
        if is_multipart:
            quote_author = quote_author or None
            reply_to_message_id = reply_to_message_id or None
            quote_content_type = quote_content_type or None
            quote_attachment_id = quote_attachment_id or None
            if quote_timestamp == "":
                quote_timestamp = None
            elif quote_timestamp is not None:
                try:
                    quote_timestamp = int(quote_timestamp)
                except (TypeError, ValueError):
                    raise HTTPException(
                        status_code=400, detail="Invalid request"
                    ) from None
        if isinstance(quote_author, str):
            quote_author = quote_author.strip() or None
        if isinstance(quote_content_type, str):
            quote_content_type = quote_content_type.strip() or None
        if isinstance(quote_attachment_id, str):
            quote_attachment_id = quote_attachment_id.strip() or None
        if isinstance(quote_message, str):
            stripped_quote_message = quote_message.strip()
            quote_message = (
                stripped_quote_message
                if stripped_quote_message or quote_content_type
                else None
            )
        if isinstance(reply_to_message_id, str):
            reply_to_message_id = reply_to_message_id.strip() or None
        if isinstance(quote_timestamp, bool) or (
            quote_timestamp is not None and not isinstance(quote_timestamp, int)
        ):
            raise HTTPException(status_code=400, detail="Invalid request")
        if any(
            value is not None and not isinstance(value, str)
            for value in (
                quote_author,
                quote_message,
                reply_to_message_id,
                quote_content_type,
                quote_attachment_id,
            )
        ):
            raise HTTPException(status_code=400, detail="Invalid request")
        is_reply = any(
            value is not None
            for value in (
                quote_timestamp,
                quote_author,
                quote_message,
                reply_to_message_id,
                quote_content_type,
                quote_attachment_id,
            )
        )
        if protocol == "telegram" and is_reply:
            try:
                if int(reply_to_message_id) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="Invalid request") from None
        if protocol == "whatsapp" and is_reply and not reply_to_message_id:
            raise HTTPException(status_code=400, detail="Invalid request")

        manager = request.app.state.manager
        backend = manager.get(protocol)
        if backend is None:
            raise HTTPException(status_code=404, detail="Not Found")
        known_contact = any(
            str(contact.id) == contact_id and str(contact.protocol) == protocol
            for contact in manager.list_contacts()
        )
        if not known_contact:
            raise HTTPException(status_code=404, detail="Not Found")

        quote_attachments = None
        if protocol == "signal" and quote_attachment_id is not None:
            contact_attachment_ids = {
                message["attachment"]["attachment_id"]
                for message in _messages(protocol, contact_id)
                if message["attachment"] is not None
            }
            if quote_attachment_id not in contact_attachment_ids:
                raise HTTPException(status_code=400, detail="Invalid request")
        if protocol == "signal" and quote_content_type is not None:
            if quote_attachment_id is None:
                raise HTTPException(status_code=400, detail="Invalid request")
            resolved = manager.get_attachment_path(protocol, quote_attachment_id)
            path = Path(resolved) if resolved else None
            quote_attachments = (
                [f"{quote_content_type}:{path.name}:{path}"]
                if path
                else [quote_content_type]
            )

        kwargs = {
            "quote_timestamp": quote_timestamp,
            "quote_author": quote_author,
            "quote_message": quote_message,
        }
        if protocol == "signal" and quote_attachments is not None:
            kwargs["quote_attachments"] = quote_attachments
        if protocol in {"whatsapp", "telegram"} and reply_to_message_id is not None:
            kwargs["reply_to_message_id"] = reply_to_message_id
        upload = None
        try:
            if upload_file is not None:
                from web.uploads import UploadValidationError, store_upload

                try:
                    upload = await store_upload(upload_file)
                except UploadValidationError as exc:
                    detail = (
                        "Upload too large"
                        if exc.status_code == 413
                        else "Unsupported media type"
                    )
                    raise HTTPException(
                        status_code=exc.status_code, detail=detail
                    ) from None
                await asyncio.to_thread(
                    manager.send_attachment_sync,
                    protocol,
                    contact_id,
                    upload.path,
                    caption=text or None,
                    mime_type=upload.mime_type,
                    media_kind=upload.media_kind,
                    filename=upload.filename,
                    **kwargs,
                )
            else:
                await asyncio.to_thread(
                    manager.send_message_sync,
                    protocol,
                    contact_id,
                    text,
                    **kwargs,
                )
        except HTTPException:
            raise
        except NotImplementedError:
            if upload_file is not None:
                raise HTTPException(
                    status_code=501, detail="Attachment send not supported"
                ) from None
            logger.exception(
                "Web send failed: protocol=%s contact=%s", protocol, contact_id
            )
            raise HTTPException(status_code=502, detail="Message send failed") from None
        except Exception:
            logger.exception(
                "Web send failed: protocol=%s contact=%s", protocol, contact_id
            )
            raise HTTPException(status_code=502, detail="Message send failed") from None
        finally:
            if upload is not None:
                upload.cleanup()

        push_event(
            {
                "type": "message",
                "payload": {"protocol": protocol, "contact_id": contact_id},
            }
        )
        return {"ok": True}

    @router.get("/contacts")
    def contacts(request: Request, q: str | None = None) -> list[dict[str, Any]]:
        unread = _unread_counts()
        manager = request.app.state.manager
        contacts = manager.list_contacts()
        query = (q or "").strip()
        if query:
            # Riusa la ricerca del picker TUI (substring case-insensitive su
            # nome/id/telefono) sulle chat attive (risposta rapida).
            from contact_picker import (
                search_contacts,  # lazy: evitare import TUI all'avvio
            )

            contacts = search_contacts(contacts, query)
        return [_contact_payload(contact, unread) for contact in contacts]

    @router.get("/contacts/book")
    def contacts_book(request: Request, q: str) -> list[dict[str, Any]]:
        unread = _unread_counts()
        manager = request.app.state.manager
        query = (q or "").strip()
        if not query:
            return []
        # Rubrica completa aggregata (come il picker TUI, in background):
        # lenta, il client la chiama dopo aver già mostrato i risultati delle chat.
        from contact_picker import search_contacts  # lazy: evitare import TUI all'avvio

        contacts = search_contacts(manager.list_address_book_sync(force=False), query)
        return [_contact_payload(contact, unread) for contact in contacts]

    @router.post("/log")
    def client_log(payload: dict[str, Any]) -> dict[str, str]:
        message = str(payload.get("message") or "")[:500]
        logger.info("[web-client] %s", message)
        return {"status": "ok"}

    @router.get("/messages")
    async def messages(
        request: Request,
        proto: Literal["signal", "whatsapp", "telegram"],
        contact_id: str,
    ) -> list[dict[str, Any]]:
        if proto == "telegram":
            try:
                backend = request.app.state.manager.get(proto)
                refreshed = await asyncio.to_thread(
                    backend.fetch_history, contact_id, 20
                )
                logger.debug(
                    "telegram reactions refresh: %s -> %d",
                    contact_id,
                    len(refreshed),
                )
            except Exception:
                logger.debug(
                    "telegram reactions refresh failed: %s",
                    contact_id,
                    exc_info=True,
                )
        result = _messages(proto, contact_id)
        is_group = contact_id.endswith("@g.us")
        if not is_group:
            manager = request.app.state.manager
            try:
                for contact in manager.list_contacts():
                    if str(contact.id) == contact_id and contact.protocol == proto:
                        is_group = bool(contact.extras.get("is_group"))
                        break
            except (AttributeError, TypeError):
                is_group = False
        if is_group:
            manager = request.app.state.manager
            resolver = _group_sender_resolver(manager, proto)
            for message in result:
                message["is_group"] = True
                message["sender"] = resolver(message.get("sender") or "")
                qa = message.get("quote_author") or ""
                resolved_qa = resolver(qa)
                if qa and resolved_qa.endswith(("@lid", "@c.us")):
                    # L'autore della quote non risolto: il messaggio quotato
                    # nella stessa chat ha il sender (spesso già un nome,
                    # es. pushname) — lo usiamo come autore.
                    quoted_sender = _quoted_sender(
                        proto, contact_id, message.get("quote_timestamp")
                    )
                    if quoted_sender:
                        resolved_qa = resolver(quoted_sender)
                message["quote_author"] = resolved_qa
        else:
            # Chat 1:1: l'autore della quote è sempre il contatto (o noi) —
            # ridondante, e il valore grezzo potrebbe essere un @lid.
            for message in result:
                message["quote_author"] = ""
        return result

    @router.post("/messages/read")
    async def messages_read(
        request: Request, payload: dict[str, Any]
    ) -> dict[str, str]:
        proto = str(payload.get("protocol") or "").strip()
        contact_id = str(payload.get("contact_id") or "").strip()
        if proto not in ("signal", "whatsapp", "telegram") or not contact_id:
            raise HTTPException(status_code=400, detail="Invalid request")
        manager = request.app.state.manager
        try:
            # Come il TUI: mark-read del backend (remoto, es. WAHA) + persistenza
            # read=1 in SQLite (da cui la web UI calcola i badge non letti).
            await manager.mark_read(proto, contact_id)
        except Exception:
            logger.warning(
                "web mark-read failed proto=%s contact_id=%s",
                proto,
                contact_id,
                exc_info=True,
            )
        return {"status": "ok"}

    @router.post("/messages/edit")
    async def messages_edit(
        request: Request, payload: dict[str, Any]
    ) -> dict[str, bool]:
        protocol = payload.get("protocol")
        contact_id = payload.get("contact_id")
        message_id = payload.get("message_id")
        new_text = payload.get("new_text")
        if (
            not isinstance(protocol, str)
            or protocol not in _PROTOCOLS
            or not isinstance(contact_id, str)
            or not contact_id.strip()
            or not isinstance(message_id, str)
            or not message_id.strip()
            or not isinstance(new_text, str)
            or not new_text.strip()
            or len(new_text) > _MAX_TEXT_LENGTH
        ):
            raise HTTPException(status_code=400, detail="Invalid request")

        row = _message_row_for_edit(protocol, contact_id, message_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Not Found")
        if _message_edit_id(row) is None:
            raise HTTPException(status_code=400, detail="Message not editable")

        manager = request.app.state.manager
        try:
            edited = await asyncio.to_thread(
                manager.edit_message_sync,
                protocol,
                contact_id,
                message_id,
                new_text,
            )
            if not edited:
                raise RuntimeError("Backend rejected message edit")
            updated_rows = await asyncio.to_thread(
                _persist_message_edit,
                protocol,
                contact_id,
                message_id,
                new_text,
            )
        except Exception:
            logger.exception(
                "Web edit failed: protocol=%s contact=%s", protocol, contact_id
            )
            raise HTTPException(status_code=502, detail="Message edit failed") from None

        if updated_rows == 0:
            logger.debug(
                "Web edit persistence found no row: protocol=%s contact=%s id=%s",
                protocol,
                contact_id,
                message_id,
            )
        backend = manager.get(protocol)
        try:
            await asyncio.to_thread(
                backend.apply_edit,
                contact_id,
                message_id,
                new_text,
                is_mine=True,
            )
        except Exception:
            logger.debug(
                "Web edit backend cache sync failed: protocol=%s contact=%s",
                protocol,
                contact_id,
                exc_info=True,
            )

        push_event(
            {
                "type": "message_edit",
                "payload": {
                    "protocol": protocol,
                    "contact_id": contact_id,
                    "message_id": str(message_id),
                    "timestamp": int(row["timestamp"]),
                    "old_text": row["text"] or "",
                    "text": new_text,
                    "is_mine": True,
                },
            }
        )
        return {"ok": True}

    @router.post("/messages/reaction")
    async def messages_reaction(
        request: Request, payload: dict[str, Any]
    ) -> dict[str, Any]:
        protocol = payload.get("protocol")
        contact_id = payload.get("contact_id")
        message_id = payload.get("message_id")
        reaction_emoji = payload.get("emoji")
        if (
            not isinstance(protocol, str)
            or protocol not in _PROTOCOLS
            or not isinstance(contact_id, str)
            or not contact_id.strip()
            or not isinstance(message_id, str)
            or not message_id.strip()
            or not isinstance(reaction_emoji, str)
            or not reaction_emoji
            or len(reaction_emoji) > 8
        ):
            raise HTTPException(status_code=400, detail="Invalid request")
        import emoji

        if not emoji.is_emoji(reaction_emoji):
            raise HTTPException(status_code=400, detail="Invalid request")

        contact_id = contact_id.strip()
        message_id = message_id.strip()
        manager = request.app.state.manager
        backend = manager.get(protocol)
        if backend is None:
            raise HTTPException(status_code=404, detail="Backend not found")
        row = _message_row_for_reaction(protocol, contact_id, message_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Message not found")

        target_timestamp = int(row["timestamp"])
        target_author = None
        backend_message_id = message_id
        if protocol == "signal":
            backend_message_id = str(target_timestamp)
            target_author = (
                str(getattr(backend, "user_number", ""))
                if row["is_mine"]
                else contact_id
            )
        try:
            sent = await asyncio.to_thread(
                backend.send_reaction_sync,
                contact_id,
                backend_message_id,
                reaction_emoji,
                target_author=target_author,
            )
        except Exception:  # noqa: BLE001
            sent = False
        if not sent:
            raise HTTPException(status_code=502, detail="Reaction send failed")

        event_timestamp = int(time.time() * 1000)
        reaction_payload: dict[str, Any] = {
            "mode": "snapshot" if protocol == "telegram" else "delta",
            "target_message_id": str(row["msg_id"] or backend_message_id),
            "target_timestamp": target_timestamp,
            "emoji": reaction_emoji,
            "is_mine": True,
            "author_key": "me",
            "author": "You",
            "is_remove": False,
            "timestamp": event_timestamp,
        }
        if protocol == "telegram":
            reaction_payload["snapshot"] = _telegram_reaction_snapshot(
                contact_id, message_id, reaction_emoji
            )
        aggregate = await asyncio.to_thread(
            backend.apply_reaction, contact_id, reaction_payload
        )
        reactions = aggregate.get("reactions", []) if aggregate else []
        resolved_message_id = (
            str(aggregate["message_id"])
            if aggregate and aggregate.get("message_id") is not None
            else str(row["msg_id"] or backend_message_id)
        )
        resolved_timestamp = (
            int(aggregate["timestamp"])
            if aggregate and aggregate.get("timestamp") is not None
            else target_timestamp
        )
        push_event(
            {
                "type": "reaction_update",
                "payload": {
                    "protocol": protocol,
                    "contact_id": contact_id,
                    "message_id": resolved_message_id,
                    "timestamp": resolved_timestamp,
                    "reactions": reactions,
                },
            }
        )
        return {"ok": True, "reactions": reactions}

    @router.get("/media/{proto}/{attachment_id:path}")
    def media(
        request: Request,
        proto: Literal["signal", "whatsapp", "telegram"],
        attachment_id: str,
        w: int | None = None,
    ) -> Any:
        manager = request.app.state.manager
        root = _allowed_media_root(manager, proto)
        path = None
        video_request = w in _THUMB_WIDTHS and _is_video_candidate(
            Path(attachment_id), proto, attachment_id
        )
        chunk_available = False
        if video_request and proto in {"whatsapp", "telegram"}:
            from web.video_thumbs import _chunk_video_thumbnail

            thumb, chunk_available = _chunk_video_thumbnail(
                manager, proto, attachment_id, w
            )
            if thumb is not None:
                return FileResponse(
                    thumb,
                    media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=31536000, immutable"},
                )
        try:
            resolved = manager.get_attachment_path(proto, attachment_id)
        except Exception:  # noqa: BLE001
            logger.warning(
                "web media 404 proto=%s attachment_id=%s resolved=%s",
                proto,
                attachment_id,
                path,
            )
            if chunk_available:
                raise HTTPException(
                    status_code=422, detail="Video thumbnail unavailable"
                ) from None
            raise HTTPException(status_code=404) from None
        path = Path(resolved).resolve() if resolved else None
        if path is None or not path.is_file() or not path.is_relative_to(root):
            logger.warning(
                "web media 404 proto=%s attachment_id=%s resolved=%s",
                proto,
                attachment_id,
                path,
            )
            if chunk_available:
                raise HTTPException(
                    status_code=422, detail="Video thumbnail unavailable"
                )
            raise HTTPException(status_code=404)
        logger.info(
            "web media ok proto=%s attachment_id=%s path=%s w=%s",
            proto,
            attachment_id,
            path,
            w,
        )
        if w in _THUMB_WIDTHS:
            thumb = _thumbnail(path, proto, attachment_id, w)
            if thumb is not None:
                return FileResponse(
                    thumb,
                    media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=31536000, immutable"},
                )
            if video_request or _is_video_candidate(path, proto, attachment_id):
                from web.video_thumbs import _video_thumbnail

                thumb = _video_thumbnail(path, proto, attachment_id, w)
                if thumb is None:
                    raise HTTPException(
                        status_code=422, detail="Video thumbnail unavailable"
                    )
                return FileResponse(
                    thumb,
                    media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=31536000, immutable"},
                )
        return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})

    @router.get("/quote-media/{proto}/{message_row_id}")
    def quote_media(
        proto: Literal["signal", "whatsapp", "telegram"],
        message_row_id: int,
        w: int | None = None,
    ) -> Any:
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
                    "Web quote media lookup database error: proto=%s row_id=%s",
                    proto,
                    message_row_id,
                )
                raise HTTPException(status_code=500, detail="Database error") from None

        stored = row[0] if row else None
        root = (Path(backend.CACHE_DIR) / "quote-thumbs").resolve()
        path = Path(stored).resolve() if stored else None
        if path is None or not path.is_file() or path.parent != root:
            logger.warning(
                "web quote media 404 proto=%s row_id=%s resolved=%s",
                proto,
                message_row_id,
                path,
            )
            raise HTTPException(status_code=404)

        served_path = path
        cache_control = "private, max-age=86400"
        if w in _THUMB_WIDTHS:
            thumb = _thumbnail(path, proto, str(message_row_id), w)
            if thumb is not None:
                served_path = thumb
                cache_control = "private, max-age=31536000, immutable"
        media_type, _ = mimetypes.guess_type(served_path.name)
        return FileResponse(
            served_path,
            media_type=media_type,
            headers={"Cache-Control": cache_control},
        )

    return router
