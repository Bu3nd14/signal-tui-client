"""
WhatsApp backend — a ``ChatBackend`` implementation for a Baileys-based
WhatsApp HTTP/WebSocket API (e.g. ``whatsapp-http-api``).

The backend is a thin client to a generic external service and never talks to
WhatsApp directly:

- REST endpoints for sessions, contacts, sending, mark-read, media.
- A WebSocket stream for incoming messages, typing indicators and receipts,
  consumed by a dedicated worker thread that fills an event queue.

Incoming events are normalized into ``ChatEvent`` with
``protocol = PROTOCOL_WHATSAPP``, mirroring the Signal backend pattern so the
TUI stays fully protocol-agnostic.
"""

from __future__ import annotations

import json
import logging
import queue
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator

from models import (
    ChatContact,
    ChatEvent,
    PROTOCOL_WHATSAPP,
)

from .base import ChatBackend
from .config import (
    resolve_whatsapp_api_url,
    get_whatsapp_session_name,
    get_whatsapp_media_dir,
    get_whatsapp_api_key,
    get_whatsapp_webhook_url,
)

logger = logging.getLogger(__name__)


# ─── REST client (generic Baileys HTTP API) ───────────────────────────────────

class WhatsAppRESTClient:
    """Minimal synchronous JSON client for the Baileys-based HTTP API.

    Endpoints follow the generic ``whatsapp-http-api`` contract; the base URL
    is provided by ``backends.config.get_whatsapp_api_url``.
    """

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session_name = get_whatsapp_session_name()
        self.api_key = get_whatsapp_api_key()
        # HTTP status of the most recent _request (0 if never attempted).
        self.last_status: int = 0

    def _request(self, method: str, path: str, payload: dict | None = None,
                 timeout: int = 30) -> dict | None:
        """Execute an HTTP request and return the JSON body.

        When a WAHA API key is configured, it is sent as the ``X-Api-Key``
        header (WAHA returns ``401`` for unauthenticated calls).  Returns
        ``None`` on any transport/HTTP error (callers treat it as an
        unavailable service, matching Signal's error-tolerant style).
        """
        url = f"{self.base_url}{path}"
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        req = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                self.last_status = getattr(resp, "status", 200)
                raw = resp.read().decode("utf-8")
                if not raw:
                    return {}
                return json.loads(raw)
        except urllib.error.HTTPError as err:
            self.last_status = err.code
            return None
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            self.last_status = 0
            return None

    def _request_raw(self, method: str, path: str, timeout: int = 30) -> bytes | None:
        """Execute an HTTP request and return the raw (possibly binary) body.

        Used for endpoints WAHA serves as binary (e.g. the pairing QR as a PNG
        image).  Returns ``None`` on transport/HTTP error.
        """
        headers = {"Accept": "*/*"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                self.last_status = getattr(resp, "status", 200)
                return resp.read()
        except urllib.error.HTTPError as err:
            self.last_status = err.code
            return None
        except (urllib.error.URLError, OSError):
            self.last_status = 0
            return None

    # ── Sessions / pairing ────────────────────────────────────────────

    def create_session(self) -> dict | None:
        """Create the session and return the response (may include a QR)."""
        return self._request(
            "POST", "/api/sessions", {"name": self.session_name, "session": self.session_name}
        )

    def get_session_status(self) -> dict | None:
        """Return the current session status dict, or ``None`` on error."""
        return self._request("GET", f"/api/sessions/{self.session_name}")

    def update_session_config(self, config: dict) -> dict | None:
        """Update the config of an existing session via PUT /api/sessions/{name}.

        Used to register the WAHA push webhook(s) on the session itself (the
        WAHA_WEBHOOK_URL env var alone does not make WAHA emit events).  The
        config payload maps to SessionUpdateRequest, e.g.
        {webhooks: [{url: ..., events: [message, message.ack]}]}.  Returns the updated
        session dict, or None on error (callers treat it as best-effort).
        """
        return self._request(
            "PUT", f"/api/sessions/{self.session_name}", config
        )

    def start_session(self) -> dict | None:
        """Create (if needed) and start the session via WAHA ``/api/sessions/start``."""
        return self._request("POST", "/api/sessions/start", {
            "name": self.session_name,
        })

    def reset_session(self, logout: bool = True) -> dict | None:
        """Force a clean pairing state so the next QR is guaranteed fresh and valid.

        A stale/expired QR is the most common reason WhatsApp answers *"can't link
        a new device right now"*.  We tear down the old session (logout/stop it)
        and let ``get_fresh_pairing_qr()`` start a brand-new one.  Uses WAHA's
        ``/api/sessions/logout`` (keeps the session object but invalidates the
        linked device), falling back to ``/api/sessions/stop``.
        """
        if logout:
            result = self._request("POST", "/api/sessions/logout", {"name": self.session_name})
            if result is not None or self.last_status in (200, 201, 204):
                return result
        return self._request("POST", "/api/sessions/stop", {"name": self.session_name})

    def get_fresh_pairing_qr(self, reset: bool = True) -> str | bytes | None:
        """Return a freshly-generated, valid pairing QR for immediate scanning.

        Always tears down any existing session first (so the QR is brand-new and
        not a stale/expired token) then asks WAHA to start a new session and
        returns its current QR (PNG bytes or text).  Returns ``None`` on failure.
        """
        if reset:
            self.reset_session()
        self.start_session()
        return self.get_session_qr()

    def get_pairing_qr(self) -> str | bytes | None:
        """Return the current pairing QR, or ``None`` if not available.

        WAHA exposes the QR as a binary PNG under ``/api/{session}/auth/qr``
        (current versions) or as text under ``/api/sessions/{session}/qr``
        (older/NOWEB versions).  This returns whatever QR WAHA currently has; use
        ``get_fresh_pairing_qr()`` for a guaranteed-fresh one.
        """
        qr = self.get_session_qr()
        if qr:
            return qr
        # A STOPPED session has no QR — ask WAHA to start it first.
        self.start_session()
        return self.get_session_qr()

    def get_session_qr(self) -> str | bytes | None:
        """Return the QR (PNG bytes or text string), or ``None``.

        Tries the current WAHA binary-PNG endpoint first, then falls back to the
        older textual endpoint.
        """
        # Current WAHA: PNG image under /api/{session}/auth/qr.
        png = self._request_raw("GET", f"/api/{self.session_name}/auth/qr")
        if png:
            return png
        # Older WAHA: JSON/text under /api/sessions/{session}/qr.
        result = self._request("GET", f"/api/sessions/{self.session_name}/qr")
        if not result:
            return None
        # WAHA returns the QR under various keys depending on version/format.
        for key in ("qr", "code", "pairingCode", "data"):
            val = result.get(key)
            if isinstance(val, str) and val:
                return val
        data = result.get("data")
        if isinstance(data, dict):
            for key in ("qr", "code", "pairingCode"):
                val = data.get(key)
                if isinstance(val, str) and val:
                    return val
        return None

    # ── Contacts ──────────────────────────────────────────────────────

    def list_contacts(self) -> list[dict]:
        """Return the raw contacts list (may contain nested ``data``).

        Tries the classic ``GET /api/contacts?session=...`` first.  On the
        current WAHA "core" builds that endpoint is broken (it may return 500
        with a ``TypeError`` in ``ContactsController``) — in that case we fall
        back to ``GET /api/{session}/chats``, which returns the session's chats
        (aka the active contacts) with ``id._serialized`` + ``name``.
        """
        result = self._request("GET", f"/api/contacts?session={self.session_name}")
        contacts = self._unwrap_contacts(result)
        if contacts:
            return contacts
        # Fallback: per-session chats endpoint (works on WAHA CORE 2026.x).
        chats = self._request("GET", f"/api/{self.session_name}/chats")
        if not chats or not isinstance(chats, list):
            return []
        out: list[dict] = []
        for chat in chats:
            cid = chat.get("id")
            if isinstance(cid, dict):
                cid = cid.get("_serialized") or cid.get("id")
            name = chat.get("name") or chat.get("pushName") or chat.get("notifyName")
            # Timestamp (seconds) of the last message/activity.  On WAHA
            # ``/api/{session}/chats`` the field is ``timestamp`` (epoch s) and
            # optionally ``lastMessage`` (object with ``t``).  Best-effort, 0
            # if absent so the contact simply sorts as "no messages yet".
            last_ts = 0
            raw_t = chat.get("timestamp") or chat.get("t")
            if isinstance(raw_t, (int, float)):
                last_ts = int(raw_t * 1000) if raw_t < 10**12 else int(raw_t)
            else:
                lm = chat.get("lastMessage") or chat.get("last_message")
                if isinstance(lm, dict):
                    ts = lm.get("t") or lm.get("timestamp")
                    if isinstance(ts, (int, float)):
                        last_ts = int(ts * 1000) if ts < 10**12 else int(ts)
            if cid:
                out.append({
                    "id": cid,
                    "name": name or cid,
                    "isGroup": bool(chat.get("isGroup")),
                    "last_ts": last_ts,
                    "unread": int(chat.get("unreadCount") or 0),
                })
        return out

    @staticmethod
    def _unwrap_contacts(result) -> list[dict]:
        """Normalize a contacts REST response into a flat list of dicts."""
        if not result:
            return []
        if isinstance(result, list):
            return result
        data = result.get("data") or result.get("contacts") or []
        return data if isinstance(data, list) else []


    # ── Messaging ─────────────────────────────────────────────────────

    def send_message(self, to: str, text: str,
                     quote_timestamp: int | None = None,
                     quote_author: str | None = None,
                     quote_message: str | None = None) -> dict | None:
        """Send a message via WAHA ``/api/sendText``.

        Returns the API response or ``None`` on error.
        """
        payload = {
            "session": self.session_name,
            "chatId": to,
            "text": text,
        }
        if quote_message is not None:
            payload["quotedMessage"] = quote_message
        return self._request("POST", "/api/sendText", payload)

    def list_messages(self, chat_id: str, limit: int = 1) -> list[dict]:
        """Fetch recent messages of a chat via ``GET /api/messages``.

        WAHA returns a list of message objects (``body`` holds the text, ``from``
        the JID, ``fromMe`` a bool, ``timestamp`` in seconds).  Returns ``[]`` on
        any error so callers can treat a slow/unreachable API as non-fatal.

        Usato SOLO per il caricamento dello storico di una chat all'apertura
        (``fetch_history``) — NON più per un polling periodico: la ricezione live
        arriva via webhook (Push).  Mantiene comunque un timeout breve (3s) così
        una richiesta lenta non blocca la UI.
        """
        result = self._request(
            "GET",
            f"/api/messages?session={self.session_name}&chatId={chat_id}&limit={int(limit)}",
            timeout=3,
        )
        if not isinstance(result, list):
            return []
        return result

    def mark_read(self, contact_id: str) -> dict | None:
        """Best-effort mark-read (WAHA Core may need a proxy/module).

        Returns ``None`` (treated as non-fatal) if the endpoint is unavailable.
        """
        return self._request("POST", f"/api/chats/{contact_id}/read", {
            "session": self.session_name,
        })

    # ── Attachments ───────────────────────────────────────────────────

    def get_download_url(self, media_id: str) -> str | None:
        """Return a download URL for a media id (if the API exposes one)."""
        result = self._request("GET", f"/api/messages/{media_id}/download")
        if not result:
            return None
        return result.get("url") or (result.get("data") or {}).get("url")



# ─── Incoming event normalization ────────────────────────────────────────────

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


def _event_from_message(raw: dict, contacts_by_jid: dict | None = None) -> ChatEvent | None:
    """Normalize a raw incoming message dict into a ``ChatEvent``."""

    chat_jid = _jid_string(
        raw.get("chatId")
        or raw.get("from")
        or raw.get("remoteJid")
        or (raw.get("chat") if isinstance(raw.get("chat"), dict) else None)
    )
    if not chat_jid:
        return None

    text = raw.get("text") or raw.get("body") or (raw.get("message") or {}).get("conversation") or ""
    ts = raw.get("timestamp")
    ts_ms = 0
    if isinstance(ts, (int, float)):
        ts_ms = int(ts * 1000) if ts < 10**12 else int(ts)
    elif isinstance(ts, str) and ts.isdigit():
        t = int(ts)
        ts_ms = t * 1000 if t < 10**12 else t
    is_mine = bool(raw.get("fromMe") or raw.get("isMe") or raw.get("outgoing"))
    msg_id = raw.get("id") or (raw.get("key") or {}).get("id") or str(ts_ms)
    msg_type = _msg_type(raw)
    caption = raw.get("caption") or ""
    attachments = raw.get("attachments") or []
    attachment_id = None
    attachment_info = None
    if isinstance(attachments, list) and attachments:
        attachment = attachments[0]
        attachment_id = attachment.get("id") or attachment.get("url") or str(ts_ms)
        attachment_info = caption or attachment.get("filename") or attachment.get("mimetype") or "Media"

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
            or (raw.get("key", {}).get("participant")
                if isinstance(raw.get("key"), dict) else None)
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
        sender = raw.get("pushName") or raw.get("senderName") or ("You" if is_mine else chat_jid)

    # Se il sender è un JID (es. "220988985864200@lid"), prova a risolverlo
    # al nome del contatto tramite la rubrica caricata dal backend.
    sender = _resolve_sender_name(sender, contacts_by_jid)

    return ChatEvent(

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
            "quote_text": None,
            "msg_type": msg_type,
            "attachment_info": attachment_info,
            "attachment_id": attachment_id,
            "contact": None,  # resolved by identify_contact() later
        },
    )




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
    is_read = str(receipt_type or raw.get("receiptType") or "").lower() in ("read", "read-receipt")
    return ChatEvent(
        type="receipt",
        protocol=PROTOCOL_WHATSAPP,
        contact_id=chat_jid,
        payload={"message_ids": ids if isinstance(ids, list) else [], "is_read": is_read},
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
    if is_mine:
        chat_jid = _jid_string(
            content.get("to")
            or content.get("chatId")
            or content.get("remoteJid")
        )
    else:
        chat_jid = _jid_string(
            content.get("chatId")
            or content.get("from")
            or content.get("remoteJid")
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
        or (raw.get("chat") if isinstance(raw.get("chat"), dict) else None)
    )
    if not chat_jid:
        return None
    pr = raw.get("presence") or raw.get("typing") or raw.get("type") or ""
    action = "STARTED" if str(pr).lower() in ("composing", "typing", "true") else "STOPPED"
    return ChatEvent(
        type="typing",
        protocol=PROTOCOL_WHATSAPP,
        contact_id=chat_jid,
        payload={"action": action},
    )


def _event_from_raw(raw: dict, contacts_by_jid: dict | None = None) -> ChatEvent | None:
    """Dispatch a raw WebSocket message to the right normalization function.

    WAHA frames look like ``{"event": "...", "session": "...", "payload": {...}}``;
    the ``payload`` (if present) is used for the actual message content.
    """
    evt = (raw.get("event") or raw.get("type") or "").lower()
    payload = raw.get("payload")
    content = payload if isinstance(payload, dict) else raw

    if evt in ("message", "message.any", "message.new", "messages.upsert", "messages/upsert"):
        return _event_from_message(content, contacts_by_jid)
    if evt in ("typing", "presence.update", "presence/update", "presence", "presence.update"):
        return _event_from_typing(content)
    if evt in ("receipt", "receipts.update", "receipt/update", "message.receipt"):
        return _event_from_receipt(content)
    if evt in ("message.ack", "message/ack"):
        return _event_from_ack(content)
    # Some APIs emit the message object directly without an 'event' field.
    if content.get("remoteJid") or content.get("from") or content.get("chatId"):
        if "text" in content or "body" in content or content.get("message") or content.get("attachments"):
            return _event_from_message(content, contacts_by_jid)
    return None




# ─── WhatsAppBackend ─────────────────────────────────────────────────────────

class WhatsAppBackend(ChatBackend):
    """WhatsApp backend adapted to the ``ChatBackend`` interface.

    The backend talks to a generic Baileys HTTP API over REST and receives
    incoming messages as PUSH events (webhooks) instead of polling
    ``GET /api/messages``.  WAHA Core delivers a ``message`` webhook to the
    client's ``/webhook`` HTTP endpoint whenever a new message arrives; the
    endpoint normalizes the payload into a ``ChatEvent`` and pushes it onto a
    queue that the TUI's poll worker drains via ``poll_once()``.  This removes
    the aggressive per-chat GET polling that saturated the server's CPU.

    The backend still mirrors the Signal backend's patterns (``contacts``/
    ``cache`` attributes, ``poll_once``/``receive``, and ``*_sync`` helpers for
    the TUI's sync worker threads) so the UI stays protocol-agnostic.
    """

    protocol = PROTOCOL_WHATSAPP

    def __init__(self, api_url: str | None = None, media_dir: str | None = None,
                 session_name: str | None = None):
        self.api_url = (api_url or resolve_whatsapp_api_url()).rstrip("/")
        self.session_name = session_name or get_whatsapp_session_name()
        self.media_dir = media_dir or get_whatsapp_media_dir() or None
        self._rest = WhatsAppRESTClient(self.api_url) if self.api_url else None

        self.contacts: list[ChatContact] = []
        self._contacts_by_jid: dict[str, ChatContact] = {}
        self.cache: dict[str, list[dict]] = {}
        self._polling_active = False
        self._connected = False
        self._events: queue.Queue[ChatEvent | None] = queue.Queue()
        #: Dedup guard per i webhook: WAHA può ritrasmettere lo stesso evento in
        #: caso di retry, quindi teniamo gli id già visti per non accodare in
        #: doppio un messaggio (il dedup definitivo avviene in ``ingest_message``).
        self._seen_msg_ids: set[str] = set()

    def handle_webhook(self, raw: dict) -> bool:
        """Elabora un payload webhook WAHA (modalità Push/event-driven).

        ``WAHA_WEBHOOK_EVENTS: message`` fa sì che WAHA invii un POST a
        ``/webhook`` con l'envelope ``{"event": "message", "session": "...",
        "payload": {...}}`` ad ogni nuovo messaggio.  L'envelope viene
        normalizzato in un ``ChatEvent`` (riusando ``_event_from_raw``, che
        incapsula ``_event_from_message`` e legge ``from``/``body``/``fromMe``/
        ``timestamp``) e accodato a ``self._events``, consumato poi dalla TUI
        tramite ``poll_once()``.

        Ritorna ``True`` se il pacchetto è stato gestito, ``False`` se non era
        un evento riconosciuto.  L'HTTP handler risponde comunque ``200`` per
        confermare a WAHA la ricezione, come da contratto webhook.
        """
        if not isinstance(raw, dict):
            return False
        event = _event_from_raw(raw, self._contacts_by_jid)
        if event is None:
            return False
        # WAHA sends message.ack INSTEAD of a separate message event for
        # outgoing echoes.  Ingest the payload as a message FIRST so the
        # optimistic-send entry (id=None) gets upgraded to its real id.
        evt_name = raw.get("event", "")
        if "ack" in str(evt_name).lower():
            content = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
            if content.get("fromMe") and content.get("id"):
                ack_contact = _jid_string(
                    content.get("to") or content.get("chatId") or content.get("remoteJid")
                )
                if ack_contact:
                    ack_ts = int(content.get("timestamp") or 0) * 1000  # WAHA uses seconds, we use ms
                    self.ingest_message(ack_contact, {
                        "id": content.get("id"),
                        "text": content.get("body") or content.get("text") or "",
                        "is_mine": True,
                        "sender": "You",
                    }, ack_ts)
        if event.type == "message":
            mid = event.payload.get("id")
            if mid and mid in self._seen_msg_ids:
                return True  # retry già processato: niente doppioni in coda
            if mid:
                self._seen_msg_ids.add(mid)
        self._enqueue_event(event)
        return True

    @property
    def needs_pairing(self) -> bool:
        """Whether the backend is not authenticated and needs QR pairing."""
        if not self._rest:
            return False
        status = self._rest.get_session_status() or {}
        s = str(status.get("status") or "").lower()
        return s in (
            "pending", "connecting", "unauthorized", "not_authenticated",
            "unpaired", "scan_qr", "scan_qr_code",
        )

    async def get_pairing_qr(self) -> str | bytes | None:
        """Return the current pairing QR (text or PNG bytes), or ``None``."""
        if not self._rest:
            return None
        return self._rest.get_pairing_qr()


    # ─── Lifecycle ────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Async connect (used by the manager's ``connect_all``)."""
        self.connect_sync()

    def connect_sync(self) -> None:
        """Establish the connection and load contacts.

        Starts the WebSocket consumer thread (if a ws endpoint is configured)
        and populates the ``contacts`` list from the REST API.  Safe to call
        when the API is unreachable — the backend simply remains idle.

        The in-memory cache is seeded from the SQLite DB (only this protocol's
        messages) so that messages persisted in a previous session are
        immediately available to the UI and the existing ``ingest_message``
        dedup works across sessions — otherwise ``fetch_history`` would
        re-insert the same remote messages on every chat open, duplicating
        them in the DB.
        """
        self.cache = self._load_protocol_cache()
        if not self._rest:
            self._connected = False
            return
        try:
            self._load_contacts()
        except Exception:
            pass
        # La ricezione è basata sui webhook (Push) di WAHA Core, gestiti dall'HTTP
        # handler ``/webhook`` avviato dalla TUI (``ensure_webhook_server``) che
        # chiama ``handle_webhook`` su questo backend.  Qui non si avvia alcun
        # thread di polling: il client NON interroga più ``GET /api/messages``.
        # Fix C: attende che la sessione WAHA sia pronta (status WORKING) prima
        # di marcare il backend come connesso.  Senza questa attesa, i fetch di
        # apertura (/api/messages) eseguiti nei primi ~10-30s interrogano una
        # sessione ancora in connessione e WAHA risponde con una lista VUOTA ->
        # chat viste come vuote / "No message history".  Best-effort: se scade
        # il timeout, procede comunque (non blocca l'avvio all'infinito).
        self._wait_session_ready(timeout=40.0)
        # Registra (o ri-registra) il webhook push per-sessione ora che la
        # sessione e` pronta: il solo WAHA_WEBHOOK_URL (env) non basta a far
        # emettere gli eventi a WAHA, serve la config webhooks sulla sessione.
        # Best-effort: se il server non risponde, parte comunque.
        self._configure_webhook()
        self._connected = True

    def _wait_session_ready(self, timeout: float = 40.0) -> bool:
        """Poll get_session_status finché la sessione WAHA è pronta (WORKING).

        Ritorna True se la sessione è pronta, False se è scaduto il timeout.
        Non solleva eccezioni: se la sessione non risponde (None) ripete.
        """
        if not self._rest:
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                status = self._rest.get_session_status() or {}
                s = str(status.get("status") or "").lower()
                # Pronto: stato WORKING o qualsiasi stato "stabile" non di
                # connessione/pairing (coerente con needs_pairing).
                if s == "working" or s and s not in (
                    "pending", "connecting", "unauthorized",
                    "not_authenticated", "unpaired", "scan_qr", "scan_qr_code",
                    "starting", "loading", "syncing",
                ):
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def _configure_webhook(self) -> None:
        """Registra il webhook push per-sessione su WAHA (best-effort).

        Il solo ``WAHA_WEBHOOK_URL`` (env in docker-compose) non fa emettere
        gli eventi ``message`` a WAHA: serve registrare la config ``webhooks``
        sulla sessione via ``PUT /api/sessions/{name}``.  Questo metodo la
        (ri)applica a ogni avvio cosi' la ricezione in tempo reale non dipende
        da uno stato applicato a mano, e salta il PUT se gia' e' configurato
        (evitando un restart inutile della sessione).  Non solleva mai
        eccezioni: se il server non risponde, il backend resta operativo.
        """
        if not self._rest:
            return
        try:
            webhook = get_whatsapp_webhook_url()
            current = self._rest.get_session_status() or {}
            configured = (current.get("config") or {})
            urls = [
                (w or {}).get("url")
                for w in (configured.get("webhooks") or [])
            ]
            desired_events = ["message", "message.ack"]
            if webhook in urls:
                # URL già registrato — controlla se anche gli eventi sono
                # aggiornati (es. dopo un upgrade che ha aggiunto message.ack).
                for w in (configured.get("webhooks") or []):
                    if (w or {}).get("url") == webhook:
                        current_events = (w or {}).get("events") or []
                        if set(current_events) >= set(desired_events):
                            return  # già aggiornato: niente restart
                        break
            self._rest.update_session_config({
                "config": {
                    "webhooks": [{
                        "url": webhook,
                        "events": desired_events,
                    }]
                }
            })
        except Exception:
            # best-effort: non bloccare mai l'avvio
            pass


    async def disconnect(self) -> None:
        """Stop the WebSocket consumer and release resources."""
        self.disconnect_sync()

    def disconnect_sync(self) -> None:
        """Synchronous disconnect; stops the receiver threads."""
        self._polling_active = False
        self._connected = False

    # ─── Contacts / active chats (one-shot discovery, no polling) ────
    # La ricezione degli eventi è tutta PUSH via webhook (handle_webhook).  I
    # metodi qui sotto servono solo per la scoperta iniziale dei contatti e
    # delle chat non lette all'avvio/resync — un GET /chats occasionale, NON un
    # polling periodico.

    def _discover_active_chats(self) -> list[tuple[str, int, int]]:
        """(Una tantum) Legge le chat dal REST ``/api/{session}/chats``.

        Ritorna una lista ``[(cid, unread, timestamp)]`` per le chat non di
        gruppo, oppure ``[]`` se l'API non risponde.  Invocato all'avvio e da
        ``resync_history`` una singola volta (mai in un loop di polling).
        """
        if not self._rest:
            return []
        raw = self._rest._request("GET", f"/api/{self.session_name}/chats")
        if not isinstance(raw, list):
            return []
        out: list[tuple[str, int, int]] = []
        for c in raw:
            cid = c.get("id")
            if isinstance(cid, dict):
                cid = cid.get("_serialized") or cid.get("id")
            if not cid or c.get("isGroup"):
                continue
            unread = int(c.get("unreadCount") or 0)
            ts = int(c.get("timestamp") or 0)
            out.append((cid, unread, ts))
        return out

    # ─── Contact loading ──────────────────────────────────────────────

    def _load_contacts(self) -> None:
        """Fetch contacts from the API into ``self.contacts``."""
        if not self._rest:
            return
        # Retry best-effort: il GET /chatsWAHA (grande, ~1.3MB) può andare in
        # timeout/fallire nei primi istanti -> restituirebbe 0 contatti pur con
        # session WORKING (sintomo "backend attivo ma zero contatti" osservato
        # nei run 4-6).  Riproviamo un paio di volte prima di rinunciare.
        raw_contacts = self._rest.list_contacts()
        attempt = 0
        while not raw_contacts and attempt < 3:
            attempt += 1
            time.sleep(1.0)
            raw_contacts = self._rest.list_contacts()
        # Se anche dopo il retry non ci sono contatti, lasciamo comunque
        # list(this).contacts vuoto (best-effort, senza crash).
        contacts: list[ChatContact] = []
        for c in raw_contacts:
            jid = c.get("id") or c.get("jid") or c.get("remoteJid")
            if not jid:
                continue
            name = c.get("name") or c.get("pushName") or c.get("notifyName") or ""
            last_ts = int(c.get("last_ts") or 0)
            contacts.append(ChatContact(
                id=jid,
                display_name=name or jid,
                protocol=PROTOCOL_WHATSAPP,
                extras={"jid": jid, "last_message_ts": last_ts},
            ))
        self.contacts = contacts
        self._contacts_by_jid = {cc.id: cc for cc in contacts}

    def _identify_contact(self, jid: str) -> ChatContact | None:
        """Resolve a JID to a known ``ChatContact`` (or a placeholder)."""
        return self._contacts_by_jid.get(jid)

    async def list_contacts(self) -> list[ChatContact]:
        return list(self.contacts)

    def fetch_history(self, contact_id: str, limit: int = 20) -> list[dict]:
        """Scarica lo storico remoto di una chat da WAHA e lo salva nel cache.

        Usato quando l'utente apre un contatto: WAHA CORE espone
        ``GET /api/messages?chatId=...`` (niente WS/stream) e senza questo lo
        storico non verrebbe mai caricato (il cache locale si riempie solo con
        gli arrivi live, limitati).  Normalizza i messaggi con
        ``_event_from_message`` e li ingerisce nel cache locale.

        Ritorna la lista (già ordinata) dei messaggi normalizzati per il
        contatto, oppure ``[]`` se l'API non risponde (fallback non distruttivo).
        """
        if not self._rest:
            return []
        raw = self._rest.list_messages(contact_id, limit=limit)
        if not isinstance(raw, list):
            return []
        # WAHA ritorna i messaggi dal più recente in giù; li riordiniamo
        # cronologicamente, poi li ingeriamo nel cache.
        msgs = [m for m in raw if isinstance(m, dict)]
        msgs.sort(key=lambda m: int(m.get("timestamp") or 0))
        for m in msgs:
            event = _event_from_message(m, self._contacts_by_jid)
            if event is None:
                continue
            payload = event.payload

            is_mine = payload.get("is_mine", False)
            # ingest_message fa dedup interno (stesso (contact, ts, text) non
            # viene ri-salvato) e aggiorna il cache locale + SQLite.  Includiamo
            # sia i ricevuti sia i miei inviati così lo storico è completo.
            self.ingest_message(
                contact_id,
                {
                    "id": payload.get("id"),
                    "text": payload.get("text", ""),
                    "is_mine": is_mine,
                    "sender": payload.get("sender", "You" if is_mine else ""),
                    "quote_text": payload.get("quote_text"),
                    "msg_type": payload.get("msg_type", "text"),
                    "attachment_info": payload.get("attachment_info"),
                    "attachment_id": payload.get("attachment_id"),
                },
                payload.get("timestamp", 0),
            )

        # Ordina la cache della chat per timestamp (idempotente — ingest ha già
        # riordinato a ogni aggiunta, ma riordinare qui è gratuito e garantisce
        # uno stato deterministico per il render).
        self._sort_contact_cache(contact_id)
        return msgs

    def resync_history(self, limit: int = 50) -> int:
        """Re-sync lo storico delle chat rilevanti da WAHA, best-effort.

        Al rilevamento dell'avvio il backend parte con il cache seminato dal DB
        (``_load_protocol_cache``): i messaggi persistiti nelle sessioni passate
        ci sono, ma lo stato può essere incompleto/corrotto (entry senza id,
        gap, invii da un altro client).  Questo metodo scarica di nuovo lo
        storico (via ``fetch_history``) per l'UNIONE di due insiemi:

        - le chat che hanno GIÀ messaggi nel DB locale (chiavi di ``self.cache``)
          — così un DB compromesso dai vecchi bug di dedup viene riparato anche
          se non apri la chat, e i messaggi arrivati da un altro client
          compaiono fin da subito;
        - le chat NON LETTE dichiarate da WAHA (``_discover_active_chats()`` con
          ``unread > 0`` via ``GET /chats``) — copre i nuovi arrivi mai visti
          dalla TUI.

        Le chat già lette e senza storico locale NON vengono toccate (si
        caricano all'apertura, come prima), così l'avvio resta veloce.
        Fallisce in modo non distruttivo: ogni errore per chat viene ignorato,
        e il metodo non solleva eccezioni.

        Ritorna il numero di chat effettivamente interrogate.
        """
        if not self._rest or not self._connected:
            return 0
        targets: set[str] = set(self.cache.keys())
        try:
            # Un solo GET /chats (non un polling periodico): serve a scoprire le
            # chat non lette da ri-sincronizzare all'avvio.  La ricezione live è
            # comunque tutta su webhook (handle_webhook).
            chats = self._discover_active_chats()
            targets.update(
                jid for jid, unread, _ts in chats if unread > 0
            )
        except Exception:
            pass  # se /chats fallisce restiamo sulle sole chat-DB
        for jid in targets:
            try:
                self.fetch_history(jid, limit=limit)
            except Exception:
                pass  # best-effort: mai far fallire l'avvio per una singola chat
        return len(targets)

    # ─── Messaging ────────────────────────────────────────────────────

    async def send_message(
        self,
        contact_id: str,
        text: str,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_message: str | None = None,
    ) -> str:
        """Async send (interface contract); delegates to the sync path."""
        return self.send_message_sync(
            contact_id, text,
            quote_timestamp=quote_timestamp,
            quote_author=quote_author,
            quote_message=quote_message,
        )

    def send_message_sync(
        self,
        contact_id: str,
        text: str,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_message: str | None = None,
    ) -> str:
        """Send *text* to *contact_id*; returns the client timestamp (ms).

        Used by the TUI's sync worker threads (same pattern as Signal).  Raises
        ``RuntimeError`` if the API is unreachable or answers with an error, so
        the caller can surface a visible error instead of silently failing.
        """
        ts = int(time.time() * 1000)
        if not self._rest:
            raise RuntimeError("WhatsApp API is not configured")
        result = self._rest.send_message(
            contact_id, text,
            quote_timestamp=quote_timestamp,
            quote_author=quote_author,
            quote_message=quote_message,
        )
        if result is None:
            raise RuntimeError("WhatsApp API send failed / unreachable")
        return ts

    async def mark_read(self, contact_id: str) -> None:
        """Async mark-read (interface contract)."""
        await self._mark_read_thread(contact_id)

    async def _mark_read_thread(self, contact_id: str) -> None:
        import asyncio
        await asyncio.to_thread(self.mark_read_sync, contact_id)

    def mark_read_sync(self, contact_id: str) -> None:
        """Synchronous mark-read, for use from the TUI's sync callbacks."""
        if self._rest:
            self._rest.mark_read(contact_id)

    # ─── Attachments ──────────────────────────────────────────────────

    def get_attachment_path(self, attachment_id: str) -> Path | None:
        """Map a media id to a local file path.

        The external API stores downloaded media in a local/volume directory
        (``WHATSAPP_MEDIA_DIR``).  If a media id corresponds to a file there
        we return its ``Path``; otherwise ``None``.
        """
        if not attachment_id or not self.media_dir:
            return None
        base = Path(self.media_dir)
        if not base.is_dir():
            return None
        candidate = base / Path(attachment_id).name
        if candidate.is_file():
            return candidate
        return None


    # ─── Incoming event ingestion ─────────────────────────────────────
    # La ricezione è interamente PUSH via webhook di WAHA Core:
    # ``WAHA_WEBHOOK_URL`` + ``WAHA_WEBHOOK_EVENTS: message`` fanno sì che WAHA
    # faccia POST a ``/webhook`` ad ogni nuovo messaggio; ``handle_webhook``
    # normalizza l'envelope e lo accoda.  Nessun thread WS/polling.

    def _enqueue_event(self, event: ChatEvent) -> None:
        """Push an event onto the internal queue (thread-safe)."""
        try:
            self._events.put_nowait(event)
        except queue.Full:
            # Drop oldest to keep the queue bounded.
            try:
                self._events.get_nowait()
            except queue.Empty:
                pass
            self._events.put(event)

    # ─── Event consumption (for the TUI poll worker) ──────────────────

    def poll_once(self) -> list[ChatEvent]:
        """Drain the event queue into a batch of ``ChatEvent`` objects.

        Mirrors ``SignalBackend.poll_once`` so the TUI's plain-thread poll
        worker stays prompt and protocol-agnostic.
        """
        events: list[ChatEvent] = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                break
        return events

    async def receive(self):
        """Async generator (interface contract) yielding buffered events.

        Drains ``poll_once`` and yields each event, pausing briefly so a plain
        consumer never busy-loops.
        """
        import asyncio
        while True:
            for event in self.poll_once():
                yield event
            await asyncio.sleep(0.2)

    # ─── Cache / ingestion helpers ────────────────────────────────────

    def _load_protocol_cache(self) -> dict[str, list[dict]]:
        """Load this protocol's persisted messages from SQLite.

        Mirrors ``SignalBackend._load_protocol_cache`` but filters by the
        WhatsApp protocol so the in-memory cache only contains WhatsApp
        messages (keyed by the raw JID).  Seeding the cache from the DB at
        startup makes the existing ``ingest_message`` dedup work across
        sessions and makes persisted messages immediately available to the UI.
        """
        from backend import _load_cache
        return _load_cache(protocol=PROTOCOL_WHATSAPP)

    def _add_cached_message(self, contact_id: str, msg: dict) -> None:
        """Append a message dict to the in-memory protocol cache (raw id key)."""
        if contact_id not in self.cache:
            self.cache[contact_id] = []
        self.cache[contact_id].append(msg)
        self._sort_contact_cache(contact_id)

    @staticmethod
    def _msg_sort_key(msg: dict) -> tuple:
        """Key canonica per ordinare i messaggi di una chat.

        Ordina per ``timestamp`` (default 0), con tie-break stabile su
        ``id`` (o, in mancanza, sul testo) così due messaggi inviati nello
        stesso secondo conservano un ordine deterministico — la sola
        timestamp (granularità al secondo) non è mai unica tra più messaggi
        WhatsApp ravvicinati.
        """
        ts = int(msg.get("timestamp") or 0)
        identity = msg.get("id") or msg.get("text") or ""
        return (ts, identity)

    def _sort_contact_cache(self, contact_id: str) -> None:
        """Ordina la cache in-memory di una chat per timestamp (stabile).

        STRUTTURALE: la cache di una chat viene popolata da più fonti con
        ordini diversi (``fetch_history``, webhook ``handle_webhook``,
        ``ingest_message``, ``_load_cache`` dal DB) e l'upgrade in-place
        dell'echo può cambiare ``timestamp``/``id`` senza riordinare.  Senza un
        ordinamento deterministico, il render UI che prende gli ``[-N:]`` non
        selezionerebbe davvero gli ultimi messaggi (il sintomo "l'ultimo
        messaggio della chat non è più presente").
        """
        msgs = self.cache.get(contact_id)
        if msgs:
            msgs.sort(key=self._msg_sort_key)



    def _message_already_cached(
        self, contact_id: str, ts: int, is_mine: bool, text: str, msg_id: str | None = None
    ) -> dict | None:
        """Return the cached message matching the same identity, or ``None``.

        Mirrors Signal's dedup: for outgoing messages within a short window the
        echo of an optimistic send is not stored twice; for incoming, the
        timestamp is part of the identity.

        When a stable message ``id`` is available (WhatsApp messages carry one),
        it is used as the PRIMARY identity: two distinct messages that happen to
        share the same text AND the same second (timestamp) must NOT be merged —
        the timestamp alone (second granularity) is not a unique identity and
        would drop the second message from the DB entirely (it never reappeared,
        not even on re-entry).

        Returns the matched cached message dict (not just a bool) so the caller
        can upgrade an optimistic send (``id=None``) with the real WhatsApp id
        when its echo arrives, instead of leaving a duplicate behind.
        """
        for msg in self.cache.get(contact_id, []):
            if not msg.get("is_mine") == is_mine:
                continue
            if msg_id:
                cached_id = msg.get("id")
                if cached_id:
                    # Primary identity: the stable message id.  Same id -> dup.
                    if cached_id == msg_id:
                        return msg
                    # Different id -> definitely a distinct message, never a dup.
                    continue
                # Cached message has NO id (e.g. an optimistic send made by the
                # TUI before the real WhatsApp id was known).  Fall through to
                # the ts/text identity so the echo of that send is still
                # recognized as a duplicate and not shown twice.
            if msg.get("text") != text:
                continue
            if not is_mine:
                if msg.get("timestamp") == ts:
                    return msg
            elif msg_id:
                # Echo (ha un id reale) che corrisponde a un invio ottimistico
                # (entry senza id): il timestamp dell'echo può distare molto dal
                # ts client (WAHA usa il proprio), quindi si abbina per testo.
                # MA solo se l'entry senza id è RECENTE: un'entry legacy (pre-fix,
                # id=None) molto vecchia NON deve "inghiottire" un messaggio mio
                # genuinamente nuovo (es. inviato da un altro client) che ha lo
                # stesso testo.  La finestra copre il normale ritardo dell'echo
                # senza confondere messaggi distinti.
                if abs(msg.get("timestamp", 0) - ts) <= _ECHO_MATCH_WINDOW_MS:
                    return msg
            elif abs(msg.get("timestamp", 0) - ts) <= _SEND_DEDUP_WINDOW_MS:
                return msg
        return None




    def ingest_message(self, contact_id: str, data: dict, ts: int) -> bool:
        """Save an incoming/outgoing message to the DB cache and in-memory cache.

        Returns ``True`` if newly added, ``False`` if it was a duplicate.
        """
        from backend import _add_message_to_cache, _update_message_id
        text = data["text"]
        is_mine = data["is_mine"]
        msg_id = data.get("id")

        existing = self._message_already_cached(contact_id, ts, is_mine, text, msg_id)
        if existing is not None:
            # The echo of an optimistic send (cached with id=None) has arrived
            # with its real WhatsApp id.  Upgrade the cached entry so the
            # id-based dedup works from now on and no duplicate is left behind.
            # Only applies to SENT messages: a received message with id=None is
            # just a legacy DB entry (pre-fix), not an optimistic send awaiting
            # its echo, so it must NOT be re-inserted.
            if is_mine and msg_id and not existing.get("id"):
                existing["id"] = msg_id
                existing["timestamp"] = ts
                # L'upgrade cambia timestamp/id senza riordinare: rimettiamo in
                # ordine la chat così ```[-N:]``` del render resta corretto.
                self._sort_contact_cache(contact_id)
                _update_message_id(
                    contact_id,
                    text,
                    is_mine,
                    ts,
                    msg_id,
                    protocol=PROTOCOL_WHATSAPP,
                )
            return False




        _add_message_to_cache(
            contact_id,
            text,
            is_mine,
            data.get("sender", ""),
            ts,
            quote_text=data.get("quote_text"),
            msg_type=data.get("msg_type", "text"),
            attachment_info=data.get("attachment_info"),
            attachment_id=data.get("attachment_id"),
            protocol=PROTOCOL_WHATSAPP,
            msg_id=msg_id,
        )
        self._add_cached_message(contact_id, {
            "id": msg_id,
            "text": text,
            "is_mine": is_mine,
            "sender": data.get("sender", ""),
            "timestamp": ts,
            "quote_text": data.get("quote_text"),
            "msg_type": data.get("msg_type", "text"),
            "attachment_info": data.get("attachment_info"),
            "attachment_id": data.get("attachment_id"),
            "read": is_mine,
            "status": "sent" if is_mine else "read",
        })
        return True



    def process_receipt(self, envelope: dict) -> list[dict]:
        """Handle a receipt batch against the in-memory cache.

        Updates ``status`` for sent messages matching the reported ids.  Kept
        simple for the generic API contract (Signal's richer receipt handling
        lives in the Signal backend).
        """
        ids = envelope.get("message_ids") or []
        if not ids:
            return []
        is_read = bool(envelope.get("is_read"))
        target = "read" if is_read else "delivered"
        updated: list[dict] = []
        for msgs in self.cache.values():
            for msg in msgs:
                if msg.get("is_mine") and str(msg.get("id", "")) in {str(i) for i in ids}:
                    old = msg.get("status", "sent")
                    rank = {"sent": 0, "delivered": 1, "read": 2}
                    if old != target and rank.get(target, 0) > rank.get(old, 0):
                        msg["status"] = target
                        updated.append(msg)
        # Persist status changes to SQLite so they survive restarts.
        if updated:
            from backend import _update_message_status
            for msg in updated:
                _update_message_status(msg["timestamp"], msg["status"])
        return updated


_SEND_DEDUP_WINDOW_MS = 5000

# Window (ms) entro cui un'entry SENT senza id (invio ottimistico della TUI)
# può essere considerata l'echo di un messaggio con id reale, abbinandola per
# testo.  Copre il normale ritardo dell'echo di WAHA (che usa il proprio
# timestamp server, distante dal ts client) senza però far "inghiottire" a
# un'entry legacy (pre-fix, id=None) molto vecchia un messaggio mio
# genuinamente nuovo (es. inviato da un altro client) con lo stesso testo.
_ECHO_MATCH_WINDOW_MS = 600000  # 10 minuti


