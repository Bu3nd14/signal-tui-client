"""
Standardized data models for the multi-protocol TUI client.

These dataclasses decouple the Textual UI from any specific chat protocol
(Signal, WhatsApp, ...).  Every backend (``ChatBackend``) converts its
protocol-specific data into these neutral objects, so the UI only ever deals
with ``ChatContact``, ``ChatMessage`` and ``ChatEvent``.

No Textual dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ─── Protocol identifiers ────────────────────────────────────────────────────

PROTOCOL_SIGNAL = "signal"
PROTOCOL_WHATSAPP = "whatsapp"
PROTOCOL_TELEGRAM = "telegram"

#: Human-friendly emoji shown next to a contact of a given protocol.
PROTOCOL_EMOJI: dict[str, str] = {
    PROTOCOL_SIGNAL: "📱",
    PROTOCOL_WHATSAPP: "💬",
    PROTOCOL_TELEGRAM: "📨",
}


def protocol_emoji(protocol: str) -> str:
    """Return the emoji used to visually tag a contact of *protocol*."""
    return PROTOCOL_EMOJI.get(protocol, "💬")


#: Human-friendly protocol names shown on grouped member rows.
PROTOCOL_NAMES: dict[str, str] = {
    PROTOCOL_SIGNAL: "Signal",
    PROTOCOL_WHATSAPP: "WhatsApp",
    PROTOCOL_TELEGRAM: "Telegram",
}


def protocol_name(protocol: str) -> str:
    """Return the human-friendly name for *protocol* (fallback: the raw string)."""
    return PROTOCOL_NAMES.get(protocol, protocol)


def contact_cache_key(protocol: str, contact_id: str) -> str:
    """Build a unique cache key for a contact across protocols.

    The same phone number could theoretically exist on both Signal and
    WhatsApp, so the key must be namespaced by protocol.
    """
    return f"{protocol}:{contact_id}"


# ─── Media kinds ──────────────────────────────────────────────────────────────

MEDIA_KIND_VALUES = frozenset(
    {"image", "gif", "video", "voice", "audio", "document", "sticker"}
)


def media_kind_from_mime(
    mime: str | None, *, is_gif: bool = False, is_voice: bool = False
) -> str | None:
    """Map a mime type and protocol hints to a normalized media kind."""
    normalized = (mime or "").lower().split(";", 1)[0].strip()
    if not normalized:
        return None
    if normalized == "image/gif" or is_gif:
        return "gif"
    if normalized.startswith("image/"):
        return "image"
    if normalized.startswith("video/"):
        return "video"
    if normalized.startswith("audio/"):
        return "voice" if is_voice else "audio"
    return "document"


def msg_type_for_media_kind(kind: str) -> str:
    """Return the backwards-compatible message type for a media kind."""
    if kind in ("image", "gif"):
        return "image"
    if kind == "sticker":
        return "sticker"
    return "attachment"


# ─── Media quote placeholders ────────────────────────────────────────────────

#: Canonical typed placeholders for a quoted media message (bug #37).  When a
#: message quoting a media carries no real caption, the backends synthesize a
#: ``quote_text`` from this mapping so the quote bubble is still rendered.
#: These are display-only values: they must never travel on the wire as a
#: Signal ``quoteMessage`` (see ``is_media_quote_placeholder``).
MEDIA_QUOTE_PLACEHOLDERS: dict[str, str] = {
    "image": "🖼️ Immagine",
    "sticker": "🎨 Sticker",
    "attachment": "📎 File",
    "audio": "🎵 Audio",
    "video": "🎬 Video",
}

_IMAGE_BASE64_PREFIXES = ("/9j/", "iVBORw0KGgo", "R0lGOD", "UklGR")


def embedded_media_quote_placeholder(value: object) -> str | None:
    """Return a safe placeholder when *value* contains inline media data."""
    if not isinstance(value, str):
        return None
    compact = "".join(value.split())
    lower = compact.lower()
    if lower.startswith("data:") and ";base64," in lower[:128]:
        mime = lower[5:].split(";", 1)[0]
        if mime.startswith("image/"):
            return MEDIA_QUOTE_PLACEHOLDERS["image"]
        if mime.startswith("video/"):
            return MEDIA_QUOTE_PLACEHOLDERS["video"]
        if mime.startswith("audio/"):
            return MEDIA_QUOTE_PLACEHOLDERS["audio"]
        return MEDIA_QUOTE_PLACEHOLDERS["attachment"]
    if compact.startswith(_IMAGE_BASE64_PREFIXES):
        return MEDIA_QUOTE_PLACEHOLDERS["image"]
    if len(compact) >= 128 and re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", compact):
        return MEDIA_QUOTE_PLACEHOLDERS["attachment"]
    return None


def media_quote_placeholder(msg_type: str, detail: str | None = None) -> str:
    """Return a human-readable label for a quoted media message.

    ``detail`` is the real user caption/filename when available; it takes
    priority over the typed placeholder derived from ``msg_type``.  Unknown
    message types degrade to the generic "📎 File" placeholder.
    """
    if detail:
        return detail
    return MEDIA_QUOTE_PLACEHOLDERS.get(
        msg_type, MEDIA_QUOTE_PLACEHOLDERS["attachment"]
    )


def is_media_quote_placeholder(text: str | None) -> bool:
    """Return True if *text* is exactly one of the canonical media placeholders.

    The predicate recognises only the 5 canonical strings (never the composed
    ``"filename — placeholder"`` form, which exists only on the display path).
    Used by the retry path to reconstruct the wire-faithful ``quote_wire_body``
    when a media reply is retried after reload.
    """
    return text is not None and text in MEDIA_QUOTE_PLACEHOLDERS.values()


def is_media_quote_placeholder_composite(text: str | None) -> bool:
    """Return True if *text* is a media placeholder or its composed form.

    The composed form ``"filename — placeholder"`` is display-only (built by
    ``_signal_quote_text``); the exact ``is_media_quote_placeholder`` remains the
    retry/wire predicate.  Used by ``QuoteWidget`` to hide the textual
    placeholder when a native thumbnail replaces it (a real caption stays
    visible).
    """
    if not text:
        return False
    if text in MEDIA_QUOTE_PLACEHOLDERS.values():
        return True
    return any(text.endswith(f" — {p}") for p in MEDIA_QUOTE_PLACEHOLDERS.values())


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
        Protocol-specific metadata (e.g. Signal ACI, WhatsApp profile pic,
        normalized ``phone`` number, address-book provenance markers).
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
    def phone(self) -> str:
        """Normalized phone number (E.164 without ``+``), or ``""`` if unknown.

        Stored in ``extras`` by the address-book backends; used as the
        cross-backend grouping key and for search.
        """
        return str(self.extras.get("phone", "") or "")

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
        Delivery status for sent messages: ``"pending"``, ``"failed"``,
        ``"sent"``, ``"delivered"``, ``"read"``.
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
    content_type: str | None = None
    status: str = "sent"
    reply_to_message_id: str | None = None
    # Quoted-media thumbnail metadata (DESIGN_QUOTE_THUMBNAIL, additive).
    # ``quote_attachment_id``/``quote_content_type`` are backend-produced and
    # persisted; ``quote_attachment_path`` is UI-derived (resolved lazily via
    # ``get_attachment_path``) and is NOT persisted.
    quote_attachment_id: str | None = None
    quote_attachment_path: str | None = None
    quote_content_type: str | None = None
    media_kind: str | None = None


@dataclass
class ChatEvent:
    """A normalized event emitted by a backend.

    ``type`` is one of:

    - ``"message"``: a new message (payload is a ``ChatMessage`` dict).
    - ``"message_edit"``: un messaggio esistente è stato modificato.
      payload: ``{"edit_message_id": str, "text": str, "timestamp": int (ts ORIGINALE),
      "edit_timestamp": int|None, "is_mine": bool, "sender": str,
      "contact": ChatContact|None, "msg_type": "text"}``.
    - ``"reaction_update"``: aggiornamento reazioni a un messaggio.
      payload: ``{"target_message_id": str|None, "target_timestamp": int|None,
      "mode": "delta"|"snapshot", "emoji": str, "is_remove": bool,
      "author": str, "author_key": str, "is_mine": bool,
      "snapshot": [{"emoji": str, "count": int, "is_mine": bool,
      "authors": list[str]}]|None, "timestamp": int,
      "contact": ChatContact|None}``.
    - ``"typing"``: a typing indicator (payload: ``{"action": "STARTED"|"STOPPED"}``).
    - ``"receipt"``: a delivery/read receipt (payload: list of updated messages).
    - ``"contact_update"``: contact metadata changed (payload: ``ChatContact`` dict).
    """

    type: str
    protocol: str
    contact_id: str
    payload: dict[str, Any] = field(default_factory=dict)
