"""REST API for contacts, persisted messages, media, and message sending."""

import asyncio
import logging
import mimetypes
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from web.bridge import push_event

logger = logging.getLogger(__name__)

_PROTOCOLS = {"signal", "whatsapp", "telegram"}
_MAX_TEXT_LENGTH = 64 * 1024

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


def _unread_counts() -> dict[tuple[str, str], int]:
    import backend
    from backend.db import _DB_LOCK

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


def _messages(protocol: str, contact_id: str) -> list[dict[str, Any]]:
    import backend
    from backend.db import _DB_LOCK

    with _DB_LOCK:
        try:
            connection = sqlite3.connect(backend.DB_FILE)
            connection.row_factory = sqlite3.Row
            try:
                rows = connection.execute(
                    "SELECT id, msg_id, text, is_mine, timestamp, "
                    "attachment_id, attachment_info, content_type, protocol "
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
        if row["attachment_id"]:
            attachment_id = row["attachment_id"]
            attachment_path = attachment_id.split("?", 1)[0]
            attachment = {
                "attachment_id": attachment_id,
                "name": row["attachment_info"] or Path(attachment_path).name,
                "type": _infer_attachment_type(attachment_id, row["content_type"]),
            }
            # WhatsApp stores the WAHA media URL as message text
            # ("Media: http://..."); drop it for WA only, never for
            # legitimate captions of other protocols.
            if row["protocol"] == "whatsapp" and text.startswith("Media: "):
                text = ""
        messages.append(
            {
                "id": row["msg_id"] or str(row["id"]),
                "text": text,
                "direction": "out" if row["is_mine"] else "in",
                "timestamp": row["timestamp"],
                "attachment": attachment,
            }
        )
    return messages


def _allowed_media_root(manager: Any, proto: str) -> Path:
    import backend

    if proto == "signal":
        return Path(backend.SIGNAL_CLI_ATTACHMENTS_DIR).resolve()
    if proto == "whatsapp":
        instance = manager.get(proto)
        if instance is not None and hasattr(instance, "_ensure_media_dir"):
            return instance._ensure_media_dir().resolve()
        return (backend.CACHE_DIR / "whatsapp-media").resolve()
    if proto == "telegram":
        try:
            from backends.telegram import _media_dir

            return _media_dir().resolve()
        except ImportError:
            return (Path(tempfile.gettempdir()) / "telegram-media").resolve()
    raise ValueError(f"Unsupported protocol: {proto}")


def create_api_router() -> Any:
    """Build the FastAPI router without making FastAPI a core dependency."""
    from fastapi import APIRouter, HTTPException, Request
    from fastapi.responses import FileResponse

    router = APIRouter(prefix="/api")

    @router.post("/send")
    async def send(request: Request) -> dict[str, bool]:
        origin = request.headers.get("origin")
        if origin:
            origin_host = urlsplit(origin).netloc.lower()
            request_host = request.headers.get("host", "").lower()
            if not origin_host or origin_host != request_host:
                raise HTTPException(status_code=403, detail="Forbidden")

        try:
            payload = await request.json()
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
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > _MAX_TEXT_LENGTH
        ):
            raise HTTPException(status_code=400, detail="Invalid request")

        quote_timestamp = payload.get("quote_timestamp")
        quote_author = payload.get("quote_author")
        quote_message = payload.get("quote_message")
        reply_to_message_id = payload.get("reply_to_message_id")
        if isinstance(quote_timestamp, bool) or (
            quote_timestamp is not None and not isinstance(quote_timestamp, int)
        ):
            raise HTTPException(status_code=400, detail="Invalid request")
        if any(
            value is not None and not isinstance(value, str)
            for value in (quote_author, quote_message, reply_to_message_id)
        ):
            raise HTTPException(status_code=400, detail="Invalid request")
        is_reply = any(
            value is not None
            for value in (
                quote_timestamp,
                quote_author,
                quote_message,
                reply_to_message_id,
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

        kwargs = {
            "quote_timestamp": quote_timestamp,
            "quote_author": quote_author,
            "quote_message": quote_message,
            "reply_to_message_id": reply_to_message_id,
        }
        try:
            await asyncio.to_thread(
                manager.send_message_sync,
                protocol,
                contact_id,
                text,
                **kwargs,
            )
        except Exception:
            logger.exception(
                "Web send failed: protocol=%s contact=%s", protocol, contact_id
            )
            raise HTTPException(status_code=502, detail="Message send failed") from None

        push_event(
            {
                "type": "message",
                "payload": {"protocol": protocol, "contact_id": contact_id},
            }
        )
        return {"ok": True}

    @router.get("/contacts")
    def contacts(request: Request) -> list[dict[str, Any]]:
        unread = _unread_counts()
        result = []
        for contact in request.app.state.manager.list_contacts():
            extras = dict(contact.extras)
            result.append(
                {
                    "id": str(contact.id),
                    "display_name": str(contact.display_name),
                    "protocol": str(contact.protocol),
                    "extras": extras,
                    "last_message_ts": int(extras.get("last_message_ts", 0) or 0),
                    "unread": unread.get((contact.protocol, contact.id), 0),
                }
            )
        return result

    @router.get("/messages")
    def messages(
        proto: Literal["signal", "whatsapp", "telegram"], contact_id: str
    ) -> list[dict[str, Any]]:
        return _messages(proto, contact_id)

    @router.get("/media/{proto}/{attachment_id:path}")
    def media(
        request: Request,
        proto: Literal["signal", "whatsapp", "telegram"],
        attachment_id: str,
    ) -> Any:
        manager = request.app.state.manager
        root = _allowed_media_root(manager, proto)
        try:
            resolved = manager.get_attachment_path(proto, attachment_id)
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=404) from None
        path = Path(resolved).resolve() if resolved else None
        if path is None or not path.is_file() or not path.is_relative_to(root):
            raise HTTPException(status_code=404)
        return FileResponse(path)

    return router
