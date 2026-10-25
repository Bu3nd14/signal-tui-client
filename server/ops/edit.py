"""Message edit operation."""

from __future__ import annotations

import logging

from models import contact_storage_keys
from server import messages as _domain
from server.api import ApiError, ChatAPI, operation
from server.models_api import MessagesEditRequest, OkOut
from server.ops.common import PROTOCOLS

logger = logging.getLogger(__name__)


@operation("messages.edit", request=MessagesEditRequest, response=OkOut)
def messages_edit(api: ChatAPI, req: MessagesEditRequest) -> OkOut:
    """Edit one of our own sent messages: backend edit, SQLite update, local
    backend cache sync, then a ``message_edit`` web frame.  Blocking."""
    protocol, contact_id = req.protocol, req.contact_id
    message_id, new_text = req.message_id, req.new_text
    if (
        protocol not in PROTOCOLS
        or not contact_id.strip()
        or not message_id.strip()
        or not new_text.strip()
        or len(new_text) > _domain.MAX_TEXT_LENGTH
    ):
        raise ApiError("invalid_request", "Invalid request")

    # Read-union cross-key: la riga può stare sotto la chiave alias (@c.us
    # ghost) mentre la richiesta arriva col canonical (@lid).
    manager = api.manager
    get_backend = getattr(manager, "get", None)
    backend = get_backend(protocol) if callable(get_backend) else None
    find_contact = getattr(backend, "find_contact", None)
    contact = find_contact(contact_id) if callable(find_contact) else None
    edit_keys = tuple(contact_storage_keys(contact)[1:]) if contact is not None else ()
    row = _domain._message_row_for_edit(
        protocol, contact_id, message_id, extra_keys=edit_keys
    )
    if row is None:
        raise ApiError("not_found", "Not Found")
    if _domain._message_edit_id(row) is None:
        raise ApiError("invalid_request", "Message not editable")

    try:
        edited = manager.edit_message_sync(protocol, contact_id, message_id, new_text)
        if not edited:
            raise RuntimeError("edit rejected by server")
        updated_rows = _domain._persist_message_edit(
            protocol, contact_id, message_id, new_text
        )
    except Exception as exc:
        logger.exception(
            "Web edit failed: protocol=%s contact=%s", protocol, contact_id
        )
        # The cause is for local frontends (the web only shows the message).
        raise ApiError(
            "backend_error", "Message edit failed", details={"error": str(exc)}
        ) from None

    if updated_rows == 0:
        logger.debug(
            "Web edit persistence found no row: protocol=%s contact=%s id=%s",
            protocol,
            contact_id,
            message_id,
        )
    backend = manager.get(protocol)
    try:
        backend.apply_edit(contact_id, message_id, new_text, is_mine=True)
    except Exception:
        logger.debug(
            "Web edit backend cache sync failed: protocol=%s contact=%s",
            protocol,
            contact_id,
            exc_info=True,
        )

    api.publish_web_push(
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
    return OkOut(ok=True)
