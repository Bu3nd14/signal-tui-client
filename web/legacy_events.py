"""Pure mapping from core bus events to the legacy ``/ws`` JSON frames.

The SPA only treats frames as thin refetch notifications.  This module
reproduces exactly the dicts the TUI used to hand to ``push_event`` so the
wire format of ``/ws`` is unchanged.  ``backend_*`` events and ``GAP`` have no
legacy frame (they were never sent).
"""

from __future__ import annotations

from typing import Any

from models import PROTOCOL_WHATSAPP
from server.bus import Event


def to_legacy(event: Event) -> dict[str, Any] | None:
    """Return the legacy WebSocket frame for *event*, or ``None`` to skip it."""
    kind = event.type
    p = event.payload

    if kind == "web_push":
        frame = p.get("event")
        return frame if isinstance(frame, dict) else None

    if kind == "message":
        if not (p.get("added") or p.get("changed")):
            return None
        message = p.get("message") or {}
        return {
            "type": "message",
            "payload": {
                "id": message.get("id"),
                "protocol": p["protocol"],
                "contact_id": p["contact_id"],
                "timestamp": p.get("timestamp"),
                "is_mine": bool(message.get("is_mine")),
            },
        }

    if kind == "message_edit":
        info = p["info"]
        return {
            "type": "message_edit",
            "payload": {
                "protocol": p["protocol"],
                "contact_id": p["contact_id"],
                "message_id": str(info["message_id"]),
                "timestamp": int(info["timestamp"]),
                "old_text": str(info["old_text"] or ""),
                "text": p["text"],
                "is_mine": bool(info["is_mine"]),
            },
        }

    if kind == "reaction_update":
        info = p["info"]
        return {
            "type": "reaction_update",
            "payload": {
                "protocol": p["protocol"],
                "contact_id": p["contact_id"],
                "message_id": str(info["message_id"]),
                "timestamp": int(info["timestamp"]),
                "reactions": info["reactions"],
            },
        }

    if kind == "receipt":
        return {
            "type": "receipt",
            "payload": {
                "protocol": p["protocol"],
                "contact_id": p["contact_id"],
                "updates": [
                    {
                        "id": str(msg.get("id")) if msg.get("id") else None,
                        "timestamp": int(msg.get("timestamp") or 0),
                        "status": str(msg.get("status") or "sent"),
                        "text": str(msg.get("text") or ""),
                    }
                    for msg in p["updated"]
                ],
            },
        }

    if kind == "typing":
        return {
            "type": "typing",
            "payload": {
                "protocol": p["protocol"],
                "contact_id": p["contact_id"],
                "action": p["action"],
            },
        }

    if kind == "contact_update":
        merged_into = p.get("merged_into")
        if merged_into and p.get("protocol") == PROTOCOL_WHATSAPP:
            return {
                "type": "contact_update",
                "payload": {
                    "protocol": p["protocol"],
                    "contact_id": p["contact_id"],
                    "merged_into": str(merged_into),
                    "display_name": p.get("display_name"),
                    "phone": p.get("phone"),
                },
            }
        return {
            "type": "contact_update",
            "payload": {
                "protocol": p["protocol"],
                "contact_id": p["contact_id"],
                "display_name": p.get("display_name"),
                "phone": p.get("phone"),
            },
        }

    if kind == "lid_warmup_done":
        return {
            "type": "lid_warmup_done",
            "payload": {"protocol": p.get("protocol", PROTOCOL_WHATSAPP)},
        }

    return None
