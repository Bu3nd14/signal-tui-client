"""
Standardized data models for the multi-protocol TUI client.

These dataclasses decouple the Textual UI from any specific chat protocol
(Signal, WhatsApp, ...).  Every backend (``ChatBackend``) converts its
protocol-specific data into these neutral objects, so the UI only ever deals
with ``ChatContact``, ``ChatMessage`` and ``ChatEvent``.

No Textual dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ─── Protocol identifiers ────────────────────────────────────────────────────

PROTOCOL_SIGNAL = "signal"
PROTOCOL_WHATSAPP = "whatsapp"

#: Human-friendly emoji shown next to a contact of a given protocol.
PROTOCOL_EMOJI: dict[str, str] = {
    PROTOCOL_SIGNAL: "📱",
    PROTOCOL_WHATSAPP: "💬",
}


def protocol_emoji(protocol: str) -> str:
    """Return the emoji used to visually tag a contact of *protocol*."""
    return PROTOCOL_EMOJI.get(protocol, "💬")


def contact_cache_key(protocol: str, contact_id: str) -> str:
    """Build a unique cache key for a contact across protocols.

    The same phone number could theoretically exist on both Signal and
    WhatsApp, so the key must be namespaced by protocol.
    """
    return f"{protocol}:{contact_id}"


# ─── Data models ─────────────────────────────────────────────────────────────

@dataclass
class ChatContact:
    """A contact in a chat protocol.

    Attributes
    ----------
    id:
        Protocol-scoped unique identifier (e.g. a phone number for Signal,
        a JID for WhatsApp).
    display_name:
        The name shown in the UI (falls back to ``id`` when unknown).
    protocol:
        One of the ``PROTOCOL_*`` constants.
    extras:
        Protocol-specific metadata (e.g. Signal ACI, WhatsApp profile pic).
    """

    id: str
    display_name: str
    protocol: str
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def cache_key(self) -> str:
        """Unique key for this contact across all protocols."""
        return contact_cache_key(self.protocol, self.id)

    @property
    def last_message_ts(self) -> int:
        """Timestamp (ms) of the last known message, or ``0`` if none.

        Stored in ``extras`` by the UI / backends whenever a new message is
        ingested or when the timestamp is recovered from the SQLite cache or
        the WhatsApp ``/chats`` payload.  Used for "most recent first" sorting.
        """
        return int(self.extras.get("last_message_ts", 0) or 0)

    @last_message_ts.setter
    def last_message_ts(self, value: int) -> None:
        self.extras["last_message_ts"] = int(value or 0)


@dataclass
class ChatMessage:
    """A single message in a chat, normalized across protocols.

    Attributes
    ----------
    id:
        Protocol-scoped unique message identifier (e.g. a timestamp for
        Signal, a message ID for WhatsApp).
    contact_id:
        The ``ChatContact.id`` this message belongs to.
    protocol:
        One of the ``PROTOCOL_*`` constants.
    text:
        The message body (may be empty for pure media messages).
    is_mine:
        Whether this message was sent by the current user.
    sender:
        Display name / id of the sender.
    timestamp:
        Unix timestamp in milliseconds.
    quote_text:
        Text of the message being replied to, if any.
    msg_type:
        ``"text"``, ``"image"``, ``"sticker"``, ``"attachment"``.
    attachment_info:
        Additional attachment details (filename, sticker emoji, ...).
    attachment_id:
        Backend-specific attachment id for resolving the file on disk.
    status:
        Delivery status for sent messages: ``"sent"``, ``"delivered"``,
        ``"read"``.
    """

    id: str
    contact_id: str
    protocol: str
    text: str
    is_mine: bool
    sender: str
    timestamp: int
    quote_text: str | None = None
    msg_type: str = "text"
    attachment_info: str | None = None
    attachment_id: str | None = None
    status: str = "sent"


@dataclass
class ChatEvent:
    """A normalized event emitted by a backend.

    ``type`` is one of:

    - ``"message"``: a new message (payload is a ``ChatMessage`` dict).
    - ``"typing"``: a typing indicator (payload: ``{"action": "STARTED"|"STOPPED"}``).
    - ``"receipt"``: a delivery/read receipt (payload: list of updated messages).
    - ``"contact_update"``: contact metadata changed (payload: ``ChatContact`` dict).
    """

    type: str
    protocol: str
    contact_id: str
    payload: dict[str, Any] = field(default_factory=dict)

