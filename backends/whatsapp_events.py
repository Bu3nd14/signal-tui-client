"""
Normalization of raw WhatsApp payloads into ``ChatEvent`` objects.

Pure helpers (``_msg_type``, ``_jid_string``, ``_resolve_sender_name``) plus the
``_event_from_*`` functions that map WAHA frames to the protocol-agnostic
``ChatEvent`` consumed by the TUI.
"""

from __future__ import annotations

import logging
import mimetypes
import re

from models import (
    PROTOCOL_WHATSAPP,
    ChatEvent,
    embedded_media_quote_placeholder,
    media_quote_placeholder,
)

logger = logging.getLogger(__name__)

# Only [0-9A-Fa-f] (Baileys message ids are uppercase hex); used to tell the
# canonical hex segment apart from the ``@``-bearing JID segments.
_HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")

# ─── Official WAHA message ack enum ─────────────────────────────────────────
# ``message.ack`` exposes the delivery/read state both as an integer ``ack``
# (the authoritative field) and as a human-readable ``ackName``.  WAHA uses
# the enum below (docu ``how-to/events#message.ack``); it differs from the
# Baileys ``WAMessageAck`` values that some older builds of the underlying
# engine used to mirror.
WAHA_ACK_ERROR = -1
WAHA_ACK_PENDING = 0
WAHA_ACK_SERVER = 1
WAHA_ACK_DEVICE = 2
WAHA_ACK_READ = 3
WAHA_ACK_PLAYED = 4


def _msg_type(raw: dict) -> str:
    """Map a WhatsApp message dict to a neutral ``msg_type``."""
    mtype = raw.get("type") or raw.get("messageType") or "text"
    lower = str(mtype).lower()
    if lower in ("image", "photo"):
        return "image"
    if lower in ("sticker",):
        return "sticker"
    if lower in ("video", "audio", "document", "file"):
        return "attachment"
    return "text"


# ─── Quoted media detection ──────────────────────────────────────────────────
# WAHA exposes a quote in multiple shapes, mirroring the media shapes handled
# for the message itself in ``_event_from_message``: nested ``*Message`` keys
# (directly or under ``message``), a flat ``type`` field, or ``mimetype``.

_WA_QUOTE_MEDIA_TYPES = (
    ("imageMessage", "image"),
    ("videoMessage", "video"),
    ("audioMessage", "audio"),
    ("documentMessage", "attachment"),
    ("stickerMessage", "sticker"),
)

_WA_IMAGE_BASE64_PREFIXES = ("/9j/", "iVBORw0KGgo", "R0lGOD", "UklGR")


def _looks_like_embedded_media(value: object) -> bool:
    """Return whether *value* is an inline data URL or media-looking base64."""
    return embedded_media_quote_placeholder(value) is not None


def _wa_quote_media_type(quote: dict) -> str | None:
    """Return the neutral ``msg_type`` of a quoted WhatsApp media, or ``None``.

    Detection order matches the media extraction in ``_event_from_message``:
    nested ``*Message`` keys first (on the quote itself or under ``message``),
    then the flat ``type`` field, then ``mimetype``.
    """
    for container in (quote, quote.get("message")):
        if not isinstance(container, dict):
            continue
        for media_key, msg_type in _WA_QUOTE_MEDIA_TYPES:
            if container.get(media_key) is not None:
                return msg_type
    flat = str(quote.get("type") or "").lower()
    if flat in ("image", "photo"):
        return "image"
    if flat == "sticker":
        return "sticker"
    if flat == "video":
        return "video"
    if flat in ("audio", "voice", "ptt"):
        return "audio"
    if flat in ("document", "file"):
        return "attachment"
    mime = str(quote.get("mimetype") or "").lower()
    media = quote.get("media")
    if not mime and isinstance(media, dict):
        mime = str(media.get("mimetype") or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime:
        return "attachment"
    for field in ("body", "text", "conversation", "caption"):
        value = quote.get(field)
        if isinstance(value, str) and (
            value.strip().startswith(_WA_IMAGE_BASE64_PREFIXES)
            or value.strip().lower().startswith("data:image/")
        ):
            return "image"
    if quote.get("hasMedia"):
        return "attachment"
    return None


def _wa_quote_media_detail(quote: dict) -> str | None:
    """Return WAHA's human-readable media metadata, never its inline data.

    WAHA overloads ``replyTo.body`` with a base64 thumbnail for some media.
    Explicit filename/caption/description metadata from the quote, its
    ``media`` object, or a nested ``*Message`` object takes priority.  A short
    non-media ``body`` remains a valid WAHA caption/description fallback.
    """
    containers: list[dict] = [quote]
    for container in (quote.get("media"), quote.get("message")):
        if isinstance(container, dict):
            containers.append(container)
    for container in tuple(containers):
        for media_key, _msg_type in _WA_QUOTE_MEDIA_TYPES:
            media = container.get(media_key)
            if isinstance(media, dict):
                containers.append(media)

    for key in ("filename", "fileName", "caption", "description"):
        for container in containers:
            value = container.get(key)
            if (
                isinstance(value, str)
                and value.strip()
                and not _looks_like_embedded_media(value)
            ):
                return value.strip()
    for container in containers[1:]:
        value = container.get("name")
        if (
            isinstance(value, str)
            and value.strip()
            and not _looks_like_embedded_media(value)
        ):
            return value.strip()
    for key in ("body", "text", "conversation"):
        value = quote.get(key)
        if (
            isinstance(value, str)
            and value.strip()
            and not _looks_like_embedded_media(value)
        ):
            return value.strip()
    return None


def _wa_quote_text(quote) -> str | None:
    """Resolve the ``quote_text`` for a WhatsApp quoted message.

    Media quotes use human-readable metadata or a typed placeholder; WAHA's
    overloaded ``body`` is accepted only when it is not inline media.  Text
    replies keep using ``text``/``body``/``conversation``/``caption``.
    Returns ``None`` for a non-dict/unknown quote (no bubble, as before).
    """
    if not isinstance(quote, dict):
        return None
    msg_type = _wa_quote_media_type(quote)
    if msg_type is not None:
        return media_quote_placeholder(msg_type, _wa_quote_media_detail(quote))
    text = (
        quote.get("text")
        or quote.get("body")
        or quote.get("conversation")
        or quote.get("caption")
    )
    if text and not _looks_like_embedded_media(text):
        return str(text).strip() or None
    return None


def _jid_string(value) -> str | None:
    """Normalize a JID into a plain string regardless of shape.

    WAHA may represent a chat/jid either as a plain string ("3112@c.us")
    or as an object like ``{"id": {"_serialized": "3112@c.us", "_id": ...}}``
    (or ``{"_serialized": "..."}``).  Return the string JID in both cases,
    ``None`` if it cannot be resolved.
    """
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        # Forma più comune: {"id": {"_serialized": ...}}
        inner = value.get("id") or value.get("_serialized")
        if isinstance(inner, dict):
            return inner.get("_serialized") or inner.get("_id") or None
        if isinstance(inner, str):
            return inner or None
        # Forma: {"_serialized": "..."}
        serialized = value.get("_serialized")
        if isinstance(serialized, str) and serialized:
            return serialized
    return None


def canonical_msg_id(raw_id: str | None) -> str:
    """Return a comparable canonical message id for receipt matching.

    WAHA 2026.8.1 (WEBJS) disagrees on the id shape depending on where it is
    read from, so the *same* message can appear as:

    - ``true_{jid@lid}_{hex}``                      (DM, ``sendText`` ``_serialized``)
    - ``true_{jid@g.us}_{hex}_{participant@lid}``   (group, ``sendText`` ``_serialized``)
    - ``{hex}``                                     (``message.ack`` webhook ``id``)

    ``process_receipt`` compares the DB/serialized id against the ack id, so
    both sides must be reduced to a single stable token.  This function
    extracts that token:

    - Baileys ``true_…``/``false_…`` serialized ids → the ``{hex}`` segment
      (the only ``@``-free, hex-alphabetic segment); the participant suffix is
      dropped and the JID prefix is ignored.
    - A plain hex id (no ``@``, no ``_``, hex chars only) → the id uppercased.
    - Any other flat/opaque id → returned unchanged (so synthetic/test ids and
      unknown formats degrade gracefully instead of matching by accident).

    Returns ``""`` for empty/``None`` input.
    """
    if raw_id is None:
        return ""
    raw = str(raw_id).strip()
    if not raw:
        return ""

    if raw.startswith(("true_", "false_")):
        body = raw.split("_", 1)[1]
        hex_segments = [
            seg
            for seg in body.split("_")
            if seg and "@" not in seg and _HEX_RE.fullmatch(seg)
        ]
        # Exactly one hex segment → unambiguous; return it (normalized case).
        if len(hex_segments) == 1:
            return hex_segments[0].upper()
        # Ambiguous/unknown serialized shape → keep the raw id so the caller
        # logs the mismatch instead of guessing.
        return raw

    if _HEX_RE.fullmatch(raw):
        return raw.upper()
    return raw


def _ack_value(raw: dict) -> int | None:
    """Return the numeric WAHA ack of a message dict, or ``None``.

    WAHA exposes the delivery/read state as an integer ``ack`` (the official
    enum: ``-1=ERROR, 0=PENDING, 1=SERVER, 2=DEVICE, 3=READ, 4=PLAYED``) and,
    in some builds, a human-readable ``ackName``.  Accept both so the history
    fetch can reconcile read receipts without mutating the DB before
    enqueueing.  The integer field is authoritative; ``ackName`` is only the
    readable fallback.
    """
    ack = raw.get("ack")
    if ack is not None:
        try:
            return int(ack)
        except (TypeError, ValueError):
            pass
    name = str(raw.get("ackName") or "").strip().upper()
    mapping = {
        "ERROR": WAHA_ACK_ERROR,
        "PENDING": WAHA_ACK_PENDING,
        "SERVER": WAHA_ACK_SERVER,
        "DEVICE": WAHA_ACK_DEVICE,
        "READ": WAHA_ACK_READ,
        "PLAYED": WAHA_ACK_PLAYED,
    }
    value = mapping.get(name)
    if value is None:
        # Minimal field instrumentation: helps confirm the enum emitted by the
        # WAHA build in use without leaking any payload content.
        logger.debug("Unknown WAHA ackName: %r", raw.get("ackName"))
    return value


def _resolve_sender_name(sender: str, contacts_by_jid: dict | None) -> str:
    """Resolve a JID sender to a contact display name when possible.

    If ``sender`` looks like a JID (contains ``@``), try to find a matching
    contact in ``contacts_by_jid``.  First try an exact JID match, then a
    match by phone number (the part before ``@``) to handle cases where the
    message JID uses a different domain (e.g. ``@lid`` vs ``@c.us``).
    """
    if not contacts_by_jid or "@" not in sender:
        return sender
    # Exact JID match.
    contact = contacts_by_jid.get(sender)
    if contact is not None:
        return contact.display_name
    # Match by phone number (part before '@').
    number = sender.split("@", 1)[0]
    if number:
        for cid, c in contacts_by_jid.items():
            if cid.split("@", 1)[0] == number:
                return c.display_name
    return sender


def _event_from_message(
    raw: dict, contacts_by_jid: dict | None = None
) -> list[ChatEvent]:
    """Normalize a raw incoming message dict into zero or more ``ChatEvent`` objects.

    When *raw* carries an ``attachments`` array with N elements, N events are
    returned (one per attachment).  The message body is attached to the first
    event only.  All other paths (nested media, hasMedia, pure text) produce a
    single-element list (or an empty list when the payload is invalid).
    """

    data = raw.get("_data") or {}
    nested_message = data.get("message") if isinstance(data, dict) else None
    reaction_message = (
        nested_message.get("reactionMessage")
        if isinstance(nested_message, dict)
        else None
    )
    if (
        str(raw.get("type") or "").lower() == "reaction"
        or reaction_message is not None
        or (
            raw.get("reaction") is not None and not (raw.get("body") or raw.get("text"))
        )
    ):
        return []

    is_mine = bool(raw.get("fromMe") or raw.get("isMe") or raw.get("outgoing"))

    # Fix: for outgoing messages (fromMe=True) the ``from`` field holds the
    # USER'S own JID, NOT the chat partner / group.  We must use ``remoteJid``
    # (or ``to``, or ``chatId``) instead, mirroring _event_from_ack's logic.
    # Otherwise the ChatEvent is attributed to the wrong contact_id, the
    # TUI's cache_key comparison fails, and the message is never displayed
    # in real-time (only appears after re-entering the chat via fetch_history).
    # WAHA may nest remoteJid inside ``key`` (e.g. /api/messages envelope).
    # ``from`` is kept as ABSOLUTE last resort for outgoing messages (when the
    # REST response lacks remoteJid entirely, e.g. fetch_history context).
    _nested_remote = (raw.get("key") or {}).get("remoteJid")
    if is_mine:
        chat_jid = _jid_string(
            raw.get("chatId")
            or raw.get("remoteJid")
            or _nested_remote
            or raw.get("to")
            or raw.get("from")  # last resort: user's own JID (fallback)
            or (raw.get("chat") if isinstance(raw.get("chat"), dict) else None)
        )
    else:
        chat_jid = _jid_string(
            raw.get("chatId")
            or raw.get("from")
            or raw.get("remoteJid")
            or _nested_remote
            or (raw.get("chat") if isinstance(raw.get("chat"), dict) else None)
        )
    if not chat_jid:
        return None

    # Gli status (storie) arrivano con JID "status@broadcast": non sono
    # messaggi di chat, li ignoriamo del tutto (niente ingestione in cache/DB).
    if "@broadcast" in chat_jid:
        return []

    text = (
        raw.get("text")
        or raw.get("body")
        or (raw.get("message") or {}).get("conversation")
        or ""
    )
    ts = raw.get("timestamp")
    ts_ms = 0
    if isinstance(ts, (int, float)):
        ts_ms = int(ts * 1000) if ts < 10**12 else int(ts)
    elif isinstance(ts, str) and ts.isdigit():
        t = int(ts)
        ts_ms = t * 1000 if t < 10**12 else t
    msg_id = raw.get("id") or (raw.get("key") or {}).get("id") or str(ts_ms)
    msg_type = _msg_type(raw)
    caption = (
        raw.get("caption")
        or str(raw.get("body") or raw.get("text") or "").strip()
        or ""
    )
    if _looks_like_embedded_media(caption):
        caption = ""

    # ── Attachment extraction ──────────────────────────────────────────
    # WAHA can deliver media in three shapes:
    # 1. Flat:  top-level "attachments" array  (legacy / v1 API) — may carry multiple
    # 2. Nested: raw["message"]["imageMessage"] / ["videoMessage"] / …  (WAHA Core)
    # 3. Flat hasMedia / media fields  (current WAHA Core)
    media_items: list[tuple[str | None, str | None, str]] = []
    # (attachment_id, attachment_info, msg_type_override)

    # Try top-level attachments first (legacy format — supports multiples).
    attachments = raw.get("attachments") or []
    if isinstance(attachments, list) and attachments:
        for att in attachments:
            att_id = att.get("id") or att.get("url")
            mime = (
                att.get("mimetype")
                or mimetypes.guess_type(
                    str(att.get("filename") or att.get("url") or "")
                )[0]
                or ""
            )
            if mime.startswith("image/"):
                att_type = "image"
            elif mime.startswith(("video/", "audio/", "application/")):
                att_type = "attachment"
            else:
                att_type = msg_type if msg_type != "text" else "attachment"
            att_info = (
                caption or att.get("caption") or att.get("filename") or mime or "Media"
            )
            media_items.append((att_id, att_info, att_type))

    # If still no media, look inside the nested "message" object (WAHA Core).
    if not media_items:
        nested = raw.get("message")
        if isinstance(nested, dict):
            media_keys = [
                ("imageMessage", "image"),
                ("videoMessage", "attachment"),
                ("audioMessage", "attachment"),
                ("documentMessage", "attachment"),
                ("stickerMessage", "sticker"),
            ]
            for media_key, fallback_type in media_keys:
                media = nested.get(media_key)
                if isinstance(media, dict):
                    att_type = fallback_type if msg_type == "text" else msg_type
                    att_id = media.get("id") or media.get("url")
                    att_info = (
                        caption
                        or media.get("caption")
                        or media.get("filename")
                        or media.get("mimetype")
                        or (
                            f"{media_key} ({msg_id[:16]}...)"
                            if len(str(msg_id)) > 16
                            else f"{media_key}"
                        )
                    )
                    media_items.append((att_id, att_info, att_type))
                    break

    # If still no media, look at WAHA's flat hasMedia / media fields (current WAHA Core).
    if not media_items and raw.get("hasMedia"):
        media = raw.get("media")
        if isinstance(media, dict):
            mime = (media.get("mimetype") or "").lower()
            if mime.startswith("image/"):
                att_type = "image"
            elif mime.startswith(("video/", "audio/", "application/")):
                att_type = "attachment"
            elif raw.get("stickerMessage") is not None:
                att_type = "sticker"
            else:
                att_type = "attachment"
            att_id = media.get("id") or media.get("url")
            att_info = (
                caption
                or media.get("caption")
                or media.get("filename")
                or mime
                or "Media"
            )
            media_items.append((att_id, att_info, att_type))

    # Rileva se la chat è un gruppo WhatsApp (JID termina con @g.us).
    is_group = chat_jid.endswith("@g.us")

    # Determina il mittente.  Per i messaggi di gruppo, il mittente effettivo
    # è spesso in un campo separato (participant/sender/author) che contiene il
    # JID del mittente (es. "3912345678@c.us"), mentre pushName/senderName
    # possono contenere il nome visualizzato.  Per i messaggi diretti, il
    # mittente è la chat stessa.
    sender = ""
    if is_group:
        # JID del mittente effettivo nel gruppo.
        sender_jid = _jid_string(
            raw.get("participant")
            or raw.get("sender")
            or raw.get("author")
            or (
                raw.get("key", {}).get("participant")
                if isinstance(raw.get("key"), dict)
                else None
            )
        )

        # Nome visualizzato se disponibile, altrimenti il JID del mittente.
        sender = (
            raw.get("pushName")
            or raw.get("senderName")
            or raw.get("notifyName")
            or sender_jid
            or ("You" if is_mine else chat_jid)
        )
    else:
        sender = (
            raw.get("pushName")
            or raw.get("senderName")
            or ("You" if is_mine else chat_jid)
        )

    # Se il sender è un JID (es. "220988985864200@lid"), prova a risolverlo
    # al nome del contatto tramite la rubrica caricata dal backend.
    sender = _resolve_sender_name(sender, contacts_by_jid)

    quote = raw.get("replyTo") or raw.get("quote") or raw.get("quotedMessage")
    quote_text = _wa_quote_text(quote)
    quote_timestamp = None
    quote_author = None
    reply_to_message_id = None
    if isinstance(quote, dict):
        quote_ts = quote.get("timestamp")
        if isinstance(quote_ts, (int, float)):
            quote_timestamp = (
                int(quote_ts * 1000) if quote_ts < 10**12 else int(quote_ts)
            )
        elif isinstance(quote_ts, str) and quote_ts.isdigit():
            value = int(quote_ts)
            quote_timestamp = value * 1000 if value < 10**12 else value
        quote_author = _jid_string(
            quote.get("participant") or quote.get("author") or quote.get("from")
        )
        if quote_author is not None:
            quote_author = quote_author.strip() or None
        quoted_id = quote.get("id") or quote.get("messageId")
        if quoted_id is not None:
            reply_to_message_id = str(quoted_id)

    # ── Build events ───────────────────────────────────────────────────
    ack_val = _ack_value(raw)
    if media_items:
        events: list[ChatEvent] = []
        for i, (att_id, att_info, att_type) in enumerate(media_items):
            media_identity = att_id or f"{msg_id}:{i + 1}"
            msg_text = "" if att_type == "image" else f"Media: {media_identity}"
            events.append(
                ChatEvent(
                    type="message",
                    protocol=PROTOCOL_WHATSAPP,
                    contact_id=chat_jid,
                    payload={
                        "id": msg_id,
                        "text": msg_text,
                        "is_mine": is_mine,
                        "sender": sender,
                        "is_group": is_group,
                        "timestamp": ts_ms,
                        "quote_text": quote_text,
                        "quote_timestamp": quote_timestamp,
                        "quote_author": quote_author,
                        "reply_to_message_id": reply_to_message_id,
                        "msg_type": att_type,
                        "attachment_info": att_info,
                        "attachment_id": att_id,
                        "ack": ack_val,
                        "contact": None,
                    },
                )
            )
        return events

    # No media: pure text message (or sticker from msg_type).
    return [
        ChatEvent(
            type="message",
            protocol=PROTOCOL_WHATSAPP,
            contact_id=chat_jid,
            payload={
                "id": msg_id,
                "text": text,
                "is_mine": is_mine,
                "sender": sender,
                "is_group": is_group,
                "timestamp": ts_ms,
                "quote_text": quote_text,
                "quote_timestamp": quote_timestamp,
                "quote_author": quote_author,
                "reply_to_message_id": reply_to_message_id,
                "msg_type": msg_type,
                "attachment_info": None,
                "attachment_id": None,
                "ack": ack_val,
                "contact": None,
            },
        )
    ]


def _event_from_receipt(raw: dict) -> ChatEvent | None:
    """Normalize a delivery/read receipt into a ``ChatEvent`` (type 'receipt')."""
    chat_jid = _jid_string(
        raw.get("chatId")
        or raw.get("from")
        or raw.get("remoteJid")
        or (raw.get("chat") if isinstance(raw.get("chat"), dict) else None)
    )
    if not chat_jid:
        return None
    receipt = raw.get("receipt") or raw.get("receipt_message") or raw
    ids = receipt.get("messageIds") if isinstance(receipt, dict) else None
    ids = ids or (receipt.get("ids") if isinstance(receipt, dict) else None) or []
    receipt_type = receipt.get("type") if isinstance(receipt, dict) else None
    is_read = str(receipt_type or raw.get("receiptType") or "").lower() in (
        "read",
        "read-receipt",
    )
    return ChatEvent(
        type="receipt",
        protocol=PROTOCOL_WHATSAPP,
        contact_id=chat_jid,
        payload={
            "message_ids": ids if isinstance(ids, list) else [],
            "is_read": is_read,
        },
    )


def _event_from_ack(raw: dict) -> ChatEvent | None:
    """Normalize a ``message.ack`` (delivery/read receipt) from WAHA into a
    ``ChatEvent`` (type 'receipt').

    WAHA emits ``message.ack`` for outgoing messages as they progress through
    the WhatsApp delivery pipeline.  The ``status`` field follows the official
    WAHA enum (``ack``/``ackName``):

    - -1 = ERROR    → ignored
    -  0 = PENDING  → ignored
    -  1 = SERVER   (accepted by server)          → ignored (= already "sent")
    -  2 = DEVICE   (delivered to recipient device) → ``is_read=False``
    -  3 = READ     (read by recipient)             → ``is_read=True``
    -  4 = PLAYED   (played, voice messages only)   → ``is_read=True``

    Only messages sent **by us** (``fromMe: true``) with status ≥ 2 are kept;
    everything else is dropped silently.
    """
    # The payload may be nested under ``raw.payload`` (WAHA Core webhook
    # envelope) or be the raw dict itself (direct normalisation).
    content = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
    is_mine = content.get("fromMe", False)
    # When the message is ours, the contact is the *recipient* (``to``),
    # not the sender (``from``).  The WAHA ``message.ack`` payload carries
    # ``from`` = our own JID and ``to`` = the chat partner's JID.
    # WAHA may nest remoteJid inside "key" for group messages.
    _ack_key = content.get("key") or {}
    if is_mine:
        chat_jid = _jid_string(
            content.get("to")
            or content.get("chatId")
            or content.get("remoteJid")
            or _ack_key.get("remoteJid")
        )
    else:
        chat_jid = _jid_string(
            content.get("chatId") or content.get("from") or content.get("remoteJid")
        )
    msg_id = content.get("id") or content.get("msgId") or content.get("messageId")
    if not chat_jid or not msg_id:
        return None
    if not content.get("fromMe", False):
        return None
    # Canonicalize up front so the receipt payload already carries the token
    # ``process_receipt`` compares against the cache (missing participant /
    # different serialized prefix can no longer break the match).
    msg_id = canonical_msg_id(msg_id)
    status = content.get("status") or content.get("ack") or 0
    try:
        status = int(status)
    except (ValueError, TypeError):
        return None
    if status < WAHA_ACK_DEVICE:
        return None  # ERROR, PENDING, SERVER — not a receipt yet
    is_read = status >= WAHA_ACK_READ
    return ChatEvent(
        type="receipt",
        protocol=PROTOCOL_WHATSAPP,
        contact_id=chat_jid,
        payload={"message_ids": [msg_id], "is_read": is_read},
    )


def _event_from_reaction(
    raw: dict, contacts_by_jid: dict | None = None
) -> ChatEvent | None:
    """Normalize a WAHA ``message.reaction`` into a reaction delta event."""
    content = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
    is_mine = bool(content.get("fromMe"))
    if is_mine:
        chat_jid = _jid_string(
            content.get("to") or content.get("chatId") or content.get("remoteJid")
        )
    else:
        chat_jid = _jid_string(
            content.get("from")
            or content.get("chatId")
            or content.get("remoteJid")
            or content.get("chat")
        )
    if not chat_jid:
        return None

    reaction = content.get("reaction")
    if not isinstance(reaction, dict):
        return None
    target_message_id = reaction.get("messageId")
    if target_message_id is None or str(target_message_id) == "":
        return None

    participant = _jid_string(content.get("participant"))
    if is_mine:
        # Una reaction propria (anche quella inviata via WAHA API) arriva con
        # ``fromMe=True`` ma ``participant`` può comunque contenere il nostro
        # LID: per coerenza con gli altri protocolli l'autore è sempre "me".
        author_key = "me"
        author = "You"
    else:
        author_key = participant or _jid_string(content.get("from"))
        author = _resolve_sender_name(author_key, contacts_by_jid)
    if not author_key:
        return None
    emoji = str(reaction.get("text") or "")

    ts = content.get("timestamp")
    ts_ms = 0
    if isinstance(ts, (int, float)):
        ts_ms = int(ts * 1000) if ts < 10**12 else int(ts)
    elif isinstance(ts, str) and ts.isdigit():
        value = int(ts)
        ts_ms = value * 1000 if value < 10**12 else value

    return ChatEvent(
        type="reaction_update",
        protocol=PROTOCOL_WHATSAPP,
        contact_id=chat_jid,
        payload={
            "target_message_id": str(target_message_id),
            "target_timestamp": None,
            "mode": "delta",
            "emoji": emoji,
            "is_remove": emoji == "",
            "author": author,
            "author_key": author_key,
            "is_mine": is_mine,
            "timestamp": ts_ms,
            "contact": None,
        },
    )


def _event_from_typing(raw: dict) -> ChatEvent | None:
    """Normalize a typing/presence indicator into a ``ChatEvent`` (type 'typing').

    Official WAHA shape (``presence.update``)::

        {"id": "39123@c.us", "presences": [
            {"participant": "39123@c.us", "lastKnownPresence": "typing", ...}
        ]}

    where ``payload.id`` is the chat JID (direct ``@c.us``/``@lid`` or group
    ``@g.us``) and ``lastKnownPresence`` ∈ ``online | offline | typing |
    recording | paused``.  Some builds use ``composing`` instead of ``typing``.

    Legacy fallback: the scalar ``presence`` / ``typing`` / ``type`` field is
    treated as a single state (compatibility with older engines/builds).

    Mapping (per-chat indicator, a single affordance in the UI):

    - ``typing`` / ``composing`` / ``recording`` / ``true`` (legacy) → ``STARTED``
    - ``paused``                                            → ``STOPPED``
    - ``online`` / ``offline`` / ``unavailable`` / unknown / absent → ``None``

    The ``online``/``offline`` filter is mandatory: presence pings are frequent
    and unrelated to typing; without it every ping would light the 💭 mumbling
    state.  With multiple ``presences`` (group) any composing-like state wins
    over ``paused``, which wins over everything else.
    """
    chat_jid = _jid_string(
        raw.get("chatId")
        or raw.get("from")
        or raw.get("remoteJid")
        or raw.get("id")
        or raw.get("participant")
        or (raw.get("chat") if isinstance(raw.get("chat"), dict) else None)
    )
    if not chat_jid:
        return None

    states: list[str] = []
    presences = raw.get("presences")
    if isinstance(presences, list):
        for presence in presences:
            if not isinstance(presence, dict):
                continue
            last_known = presence.get("lastKnownPresence")
            if last_known is not None:
                states.append(str(last_known).lower())
    if not states:
        # Legacy scalar fallback (presence / typing / type).
        scalar = raw.get("presence") or raw.get("typing") or raw.get("type")
        if scalar is not None and str(scalar).strip() != "":
            states.append(str(scalar).lower())

    action: str | None = None
    for state in states:
        if state in ("composing", "typing", "recording", "true"):
            action = "STARTED"
            break
    if action is None:
        for state in states:
            if state == "paused":
                action = "STOPPED"
                break
    if action is None:
        return None  # online/offline/unavailable/unknown → filtered out

    return ChatEvent(
        type="typing",
        protocol=PROTOCOL_WHATSAPP,
        contact_id=chat_jid,
        payload={"action": action},
    )


def _event_from_raw(raw: dict, contacts_by_jid: dict | None = None) -> list[ChatEvent]:
    """Dispatch a raw WebSocket message to the right normalization function.

    WAHA frames look like ``{"event": "...", "session": "...", "payload": {...}}``;
    the ``payload`` (if present) is used for the actual message content.

    Returns a **list** of ``ChatEvent`` objects (typically one, but may be
    multiple when the message carries several attachments).
    """
    evt = (raw.get("event") or raw.get("type") or "").lower()
    payload = raw.get("payload")
    content = payload if isinstance(payload, dict) else raw

    if evt in (
        "message",
        "message.any",
        "message.new",
        "messages.upsert",
        "messages/upsert",
    ):
        return _event_from_message(content, contacts_by_jid)
    if evt == "message.reaction":
        event = _event_from_reaction(content, contacts_by_jid)
        return [event] if event is not None else []
    if evt in (
        "typing",
        "presence.update",
        "presence/update",
        "presence",
        "presence.update",
    ):
        event = _event_from_typing(content)
        return [event] if event is not None else []
    if evt in ("receipt", "receipts.update", "receipt/update", "message.receipt"):
        event = _event_from_receipt(content)
        return [event] if event is not None else []
    if evt in ("message.ack", "message/ack", "message.ack.group"):
        event = _event_from_ack(content)
        return [event] if event is not None else []
    # Some APIs emit the message object directly without an 'event' field.
    if (content.get("remoteJid") or content.get("from") or content.get("chatId")) and (
        "text" in content
        or "body" in content
        or content.get("message")
        or content.get("attachments")
        or content.get("hasMedia")
        or content.get("media")
    ):
        return _event_from_message(content, contacts_by_jid)
    return []
