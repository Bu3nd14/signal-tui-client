"""
Normalization of raw WhatsApp payloads into ``ChatEvent`` objects.

Pure helpers (``_msg_type``, ``_jid_string``, ``_resolve_sender_name``) plus the
``_event_from_*`` functions that map WAHA frames to the protocol-agnostic
``ChatEvent`` consumed by the TUI.
"""

from __future__ import annotations

from models import PROTOCOL_WHATSAPP, ChatEvent


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


def _ack_value(raw: dict) -> int | None:
    """Return the numeric Baileys ack of a message dict, or ``None``.

    WAHA exposes the delivery/read state as an integer ``ack`` (the Baileys
    ``WAMessageAck`` enum: 2=SERVER_ACK, 3=DELIVERY_ACK, 4=READ) and, in some
    builds, a human-readable ``ackName``.  Accept both so the history fetch can
    reconcile read receipts without mutating the DB before enqueueing.
    """
    ack = raw.get("ack")
    if ack is not None:
        try:
            return int(ack)
        except (TypeError, ValueError):
            pass
    name = str(raw.get("ackName") or "").strip().upper()
    mapping = {
        "ERROR": 0,
        "PENDING": 1,
        "SERVER_ACK": 2,
        "DELIVERY_ACK": 3,
        "READ": 4,
    }
    return mapping.get(name)


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
            mime = att.get("mimetype") or ""
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

    quote = raw.get("quote") or raw.get("quotedMessage")
    quote_text = None
    if isinstance(quote, dict):
        quote_text = (
            quote.get("text") or quote.get("body") or quote.get("conversation") or None
        )

    # ── Build events ───────────────────────────────────────────────────
    ack_val = _ack_value(raw)
    if media_items:
        events: list[ChatEvent] = []
        for i, (att_id, att_info, att_type) in enumerate(media_items):
            media_identity = att_id or f"{msg_id}:{i + 1}"
            msg_text = f"Media: {media_identity}"
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
    the WhatsApp delivery pipeline.  The ``status`` field follows the Baileys
    ``WAMessageAck`` enum:

    - 2 = SERVER_ACK  (received by WhatsApp server)   → ignored (not a receipt)
    - 3 = DELIVERY_ACK (delivered to recipient device) → ``is_read=False``
    - 4 = READ         (read by recipient)             → ``is_read=True``

    Only messages sent **by us** (``fromMe: true``) with status ≥ 3 are kept;
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
    status = content.get("status") or content.get("ack") or 0
    try:
        status = int(status)
    except (ValueError, TypeError):
        return None
    if status < 3:
        return None  # PENDING, SERVER_ACK — not a receipt yet
    is_read = status >= 4
    return ChatEvent(
        type="receipt",
        protocol=PROTOCOL_WHATSAPP,
        contact_id=chat_jid,
        payload={"message_ids": [msg_id], "is_read": is_read},
    )


def _event_from_typing(raw: dict) -> ChatEvent | None:
    """Normalize a typing/presence indicator into a ``ChatEvent`` (type 'typing')."""
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
    pr = raw.get("presence") or raw.get("typing") or raw.get("type") or ""
    action = (
        "STARTED" if str(pr).lower() in ("composing", "typing", "true") else "STOPPED"
    )
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
