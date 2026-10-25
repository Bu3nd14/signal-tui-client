"""Reaction operations (set our reaction on a message)."""

from __future__ import annotations

import time
from typing import Any

from server import messages as _domain
from server.api import ApiError, ChatAPI, operation
from server.models_api import ReactionsSetOut, ReactionsSetRequest
from server.ops.common import PROTOCOLS


@operation("reactions.set", request=ReactionsSetRequest, response=ReactionsSetOut)
def reactions_set(api: ChatAPI, req: ReactionsSetRequest) -> ReactionsSetOut:
    """React to a message: backend send, local aggregate, ``reaction_update``
    web frame.  Blocking (remote send, SQLite)."""
    protocol, contact_id = req.protocol, req.contact_id
    message_id, reaction_emoji = req.message_id, req.emoji
    if (
        protocol not in PROTOCOLS
        or not contact_id.strip()
        or not message_id.strip()
        or not reaction_emoji
        or len(reaction_emoji) > 8
    ):
        raise ApiError("invalid_request", "Invalid request")
    import emoji

    if not emoji.is_emoji(reaction_emoji):
        raise ApiError("invalid_request", "Invalid request")

    contact_id = contact_id.strip()
    message_id = message_id.strip()
    backend = api.manager.get(protocol)
    if backend is None:
        raise ApiError("not_found", "Backend not found")
    row = _domain._message_row_for_reaction(protocol, contact_id, message_id)
    if row is None:
        raise ApiError("not_found", "Message not found")

    target_timestamp = int(row["timestamp"])
    target_author = None
    backend_message_id = message_id
    if protocol == "signal":
        backend_message_id = str(target_timestamp)
        target_author = (
            str(getattr(backend, "user_number", "")) if row["is_mine"] else contact_id
        )
    try:
        sent = backend.send_reaction_sync(
            contact_id,
            backend_message_id,
            reaction_emoji,
            target_author=target_author,
        )
    except Exception:  # noqa: BLE001
        sent = False
    if not sent:
        raise ApiError("backend_error", "Reaction send failed")

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
        reaction_payload["snapshot"] = _domain._telegram_reaction_snapshot(
            contact_id, message_id, reaction_emoji
        )
    aggregate = backend.apply_reaction(contact_id, reaction_payload)
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
    api.publish_web_push(
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
    return ReactionsSetOut(ok=True, reactions=reactions)
