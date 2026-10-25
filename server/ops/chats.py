"""Per-chat state operations (read marker, archive)."""

from __future__ import annotations

import logging

from server.api import ApiError, ChatAPI, operation
from server.models_api import (
    ChatsArchiveOut,
    ChatsArchiveRequest,
    ChatsMarkReadRequest,
    StatusOkOut,
)
from server.ops.common import PROTOCOLS

logger = logging.getLogger(__name__)


@operation("chats.mark_read", request=ChatsMarkReadRequest, response=StatusOkOut)
async def chats_mark_read(api: ChatAPI, req: ChatsMarkReadRequest) -> StatusOkOut:
    """Mark a chat read: backend mark-read plus ``read=1`` in SQLite.

    Backend failures are logged and swallowed: the caller always gets ``ok``.
    """
    if req.protocol not in PROTOCOLS or not req.contact_id:
        raise ApiError("invalid_request", "Invalid request")
    try:
        # Come il TUI: mark-read del backend (remoto, es. WAHA) + persistenza
        # read=1 in SQLite (da cui la web UI calcola i badge non letti).
        await api.manager.mark_read(req.protocol, req.contact_id)
    except Exception:
        logger.warning(
            "web mark-read failed proto=%s contact_id=%s",
            req.protocol,
            req.contact_id,
            exc_info=True,
        )
    return StatusOkOut(status="ok")


@operation("chats.archive", request=ChatsArchiveRequest, response=ChatsArchiveOut)
def chats_archive(api: ChatAPI, req: ChatsArchiveRequest) -> ChatsArchiveOut:
    """Archive/unarchive a WhatsApp chat.  Blocking (remote call)."""
    if req.protocol != "whatsapp" or not req.contact_id:
        raise ApiError("invalid_request", "Invalid request")
    backend = api.manager.get(req.protocol)
    if backend is None or not hasattr(backend, "set_chat_archived_sync"):
        raise ApiError("backend_down", "Backend unavailable")
    if not backend.set_chat_archived_sync(req.contact_id, req.archived):
        raise ApiError("backend_error", "Archive failed")
    return ChatsArchiveOut(archived=req.archived)
