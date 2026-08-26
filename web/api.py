"""Read-only REST API for contacts, persisted messages, and media."""

import sqlite3
from pathlib import Path, PurePath
from typing import Any, Literal


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
                    "attachment_id, attachment_info, content_type "
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
        if row["attachment_id"]:
            attachment = {
                "attachment_id": row["attachment_id"],
                "name": row["attachment_info"],
                "type": row["content_type"],
            }
        messages.append(
            {
                "id": row["msg_id"] or str(row["id"]),
                "text": row["text"] or "",
                "direction": "out" if row["is_mine"] else "in",
                "timestamp": row["timestamp"],
                "attachment": attachment,
            }
        )
    return messages


def _attachment_root(manager: Any, protocol: str) -> Path:
    import backend

    if protocol == "signal":
        return Path(backend.SIGNAL_CLI_ATTACHMENTS_DIR).resolve()
    if protocol == "whatsapp":
        backend_instance = manager.get(protocol)
        configured = getattr(backend_instance, "media_dir", None)
        return Path(configured or backend.CACHE_DIR / "whatsapp-media").resolve()
    return Path.cwd().resolve()


def _validate_attachment_id(attachment_id: str, attachment_dir: Path) -> None:
    from fastapi import HTTPException

    candidate = Path(attachment_id)
    if (
        not attachment_id
        or attachment_id.startswith("tgref:")
        or candidate.is_absolute()
        or ".." in PurePath(attachment_id).parts
        or not (attachment_dir / candidate).resolve().is_relative_to(attachment_dir)
    ):
        raise HTTPException(status_code=400, detail="Invalid attachment id")


def create_api_router() -> Any:
    """Build the FastAPI router without making FastAPI a core dependency."""
    from fastapi import APIRouter, HTTPException, Request
    from fastapi.responses import FileResponse

    router = APIRouter(prefix="/api")

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
        attachment_root = _attachment_root(request.app.state.manager, proto)
        _validate_attachment_id(attachment_id, attachment_root)
        resolved = request.app.state.manager.get_attachment_path(proto, attachment_id)
        if resolved is None:
            raise HTTPException(status_code=404, detail="Media not found")
        path = Path(resolved).resolve()
        if not path.is_relative_to(attachment_root) or not path.is_file():
            raise HTTPException(status_code=404, detail="Media not found")
        return FileResponse(path)

    return router
