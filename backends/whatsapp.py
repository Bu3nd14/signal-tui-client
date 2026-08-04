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
import queue
import threading
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
)


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
        """
        result = self._request(
            "GET",
            f"/api/messages?session={self.session_name}&chatId={chat_id}&limit={int(limit)}",
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


def _event_from_message(raw: dict) -> ChatEvent | None:
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

    return ChatEvent(
        type="message",
        protocol=PROTOCOL_WHATSAPP,
        contact_id=chat_jid,
        payload={
            "id": msg_id,
            "text": text,
            "is_mine": is_mine,
            "sender": raw.get("pushName") or raw.get("senderName") or ("You" if is_mine else chat_jid),
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


def _event_from_raw(raw: dict) -> ChatEvent | None:
    """Dispatch a raw WebSocket message to the right normalization function.

    WAHA frames look like ``{"event": "...", "session": "...", "payload": {...}}``;
    the ``payload`` (if present) is used for the actual message content.
    """
    evt = (raw.get("event") or raw.get("type") or "").lower()
    payload = raw.get("payload")
    content = payload if isinstance(payload, dict) else raw

    if evt in ("message", "message.any", "message.new", "messages.upsert", "messages/upsert"):
        return _event_from_message(content)
    if evt in ("typing", "presence.update", "presence/update", "presence", "presence.update"):
        return _event_from_typing(content)
    if evt in ("receipt", "receipts.update", "receipt/update", "message.receipt"):
        return _event_from_receipt(content)
    # Some APIs emit the message object directly without an 'event' field.
    if content.get("remoteJid") or content.get("from") or content.get("chatId"):
        if "text" in content or "body" in content or content.get("message") or content.get("attachments"):
            return _event_from_message(content)
    return None



# ─── WhatsAppBackend ─────────────────────────────────────────────────────────

class WhatsAppBackend(ChatBackend):
    """WhatsApp backend adapted to the ``ChatBackend`` interface.

    The backend talks to a generic Baileys HTTP API over REST and consumes
    incoming events from a WebSocket stream in a dedicated worker thread.  It
    mirrors the Signal backend's patterns (``contacts``/``cache`` attributes,
    ``poll_once``/``receive``, and ``*_sync`` helpers for the TUI's sync worker
    threads) so the UI stays protocol-agnostic.
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
        self._ws_thread: threading.Thread | None = None
        self._ws_stop = threading.Event()
        # Ricezione via polling su GET /api/messages (la build WAHA CORE/WEBJS
        # non espone lo stream WS ``/api/{session}/server`` -> 404).  Un thread
        # dedicato interroga periodicamente le chat attive ed accoda gli eventi.
        self._seen_msg_ids: set[str] = set()
        self._bootstrapped = False
        self._poll_thread: threading.Thread | None = None
        self._poll_stop = threading.Event()
        #: Intervallo (s) tra un giro di polling "veloce" (chat calde, ~1s).
        self._POLL_INTERVAL = 1.0
        #: Numero massimo di chat attive interrogate ad ogni giro veloce.
        self._POLL_TOP = 6
        #: Intervallo (s) tra gli aggiornamenti "lenti" della mappa chat attive
        #: (GET /chats, ~1.3MB).  Raro per non saturare banda/CPU della UI.
        self._CHATS_REFRESH_INTERVAL = 15.0
        #: Timestamp (epoch) dell'ultimo refresh della mappa chat attive.
        self._chats_last_refresh = 0.0
        #: Chat attive note: {chat_id: (unread, timestamp)}.
        self._active_chats: dict[str, tuple[int, int]] = {}

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
        """
        if not self._rest:
            self._connected = False
            return
        try:
            self._load_contacts()
        except Exception:
            pass
        self._start_receiver()
        self._connected = True

    async def disconnect(self) -> None:
        """Stop the WebSocket consumer and release resources."""
        self.disconnect_sync()

    def disconnect_sync(self) -> None:
        """Synchronous disconnect; stops the receiver threads."""
        self._polling_active = False
        self._ws_stop.set()
        self._poll_stop.set()
        if self._ws_thread is not None and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=1.0)
        self._ws_thread = None
        if self._poll_thread is not None and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=1.0)
        self._poll_thread = None
        self._connected = False

    # ─── Event receiver (polling) ─────────────────────────────────────
    # La build WAHA CORE/WEBJS non espone lo stream WS ``/api/{session}/``
    # server (404) e non consente di registrare webhook a runtime su una
    # sessione esistente.  La ricezione è quindi basata su polling di
    # ``GET /api/messages`` sulle chat attive — robusto e senza modifiche
    # al container.

    def _start_receiver(self) -> None:
        """Start the polling thread (and attempt the WS as a graceful extra)."""
        self._poll_stop.clear()
        self._start_ws_consumer()  # tentativo: su WAHA CORE fallisce (404)
        if self._poll_thread is not None and self._poll_thread.is_alive():
            return
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="whatsapp-poll"
        )
        self._poll_thread.start()

    def _poll_loop(self) -> None:
        """Two-speed polling: a slow /chats refresh (discover active chats)
        interleaved with a fast ~1s poll of only the hot chats' latest message."""
        while not self._poll_stop.is_set():
            try:
                # Aggiornamento lento e raro della mappa "chat attive"
                # (GET /chats ~1.3MB): scopre quali chat sono calde/ nuove.
                n = self._refresh_active_chats()
                # Giro veloce (~1s) sulle chat calde: list_messages limit=1.
                if n:
                    self._fetch_fast_recent()
            except Exception:
                pass
            if self._poll_stop.wait(timeout=self._POLL_INTERVAL):
                break

    def _refresh_active_chats(self) -> bool:
        """(Raro) Aggiorna ``self._active_chats`` con un solo GET /chats.

        Ritorna ``True`` se la mappa è stata aggiornata in questo giro, ``False``
        se non è ancora il momento (usato dal giro veloce per sapere se conviene
        ri-ordinare i candidati).  Non usa ``list_contacts`` (che farebbe il
        doppio /api/contacts->500 + /chats): va dritto a ``/chats``.
        """
        if not self._rest:
            return False
        now = time.time()
        if now - self._chats_last_refresh < self._CHATS_REFRESH_INTERVAL:
            return False
        raw = self._rest._request("GET", f"/api/{self.session_name}/chats")
        if not isinstance(raw, list):
            return False
        m = {}
        for c in raw:
            cid = c.get("id")
            if isinstance(cid, dict):
                cid = cid.get("_serialized") or cid.get("id")
            if not cid or c.get("isGroup"):
                continue
            unread = int(c.get("unreadCount") or 0)
            ts = int(c.get("timestamp") or 0)
            m[cid] = (unread, ts)
        self._active_chats = m
        self._chats_last_refresh = now
        return True

    def _active_chat_ids(self) -> list[str]:
        """Ordina le chat note per priorità (non lette prima, poi attività)."""
        now = int(time.time())
        scored = []
        for cid, (unread, ts) in self._active_chats.items():
            recency = max(now - ts, 0) if ts else 2**31
            scored.append((unread, -recency if ts else 0, cid))
        scored.sort(key=lambda x: (x[0] == 0, x[0], x[1]))
        return [cid for _u, _r, cid in scored]

    def _fetch_fast_recent(self) -> None:
        """Giro veloce: per le ``_POLL_TOP`` chat più calde interroga l'ultimo
        messaggio (limit=1) e accoda i nuovi eventi (dedup per id)."""
        if not self._rest or not self._active_chats:
            return
        cutoff_s = int(time.time()) - 30 * 24 * 3600  # 30 giorni (ms più sotto)
        limit = 1 if not self._bootstrapped else 1
        for cid in self._active_chat_ids()[: self._POLL_TOP]:
            try:
                msgs = self._rest.list_messages(cid, limit=limit)
            except Exception:
                continue
            for m in msgs or []:
                if not isinstance(m, dict):
                    continue
                mid = m.get("id") or ""
                if mid and mid in self._seen_msg_ids:
                    continue
                event = _event_from_message(m)
                if event is None:
                    continue
                is_mine = event.payload.get("is_mine", False)
                ts = event.payload.get("timestamp") or 0
                if ts and ts < cutoff_s * 1000:
                    continue  # ignora cronologia troppo vecchia
                # Per i messaggi miei: accodali SOLO se non già noti.  Se il
                # messaggio (timestamp+testo) è già in cache è un echo di un
                # invio fatto dalla TUI -> skip (no doppioni).  Se non è in
                # cache è un invio fatto da un ALTRO client (WhatsApp Web/
                # telefono) che la TUI non ha ancora visto -> andiamo a mostrarlo.
                if is_mine:
                    if self._message_already_cached(cid, ts, True, event.payload.get("text", "")):
                        continue
                # Attribuisci l'evento alla chat interrogata (jid della chat,
                # es. ``@lid``) invece del ``from`` (che può essere ``@c.us``):
                # così `_identify_contact` ritrova il contatto nella lista.
                event.contact_id = cid
                if mid:
                    self._seen_msg_ids.add(mid)
                self._enqueue_event(event)
        self._bootstrapped = True

    # ─── Contact loading ──────────────────────────────────────────────

    def _load_contacts(self) -> None:
        """Fetch contacts from the API into ``self.contacts``."""
        if not self._rest:
            return
        raw_contacts = self._rest.list_contacts()
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
            event = _event_from_message(m)
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
        return msgs

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


    # ─── Incoming event stream (WebSocket) ────────────────────────────

    def _ws_url(self) -> str | None:
        """Build the WebSocket URL for WAHA, or ``None``.

        WAHA exposes its event stream at the session-scoped ``/api/{session}/``
        server endpoint.  Authentication is done via the ``X-Api-Key`` header
        when a key is configured (see ``_ws_loop``), so a key-less socket is
        rejected by an authenticated WAHA.
        """
        if not self.api_url:
            return None
        scheme = "wss" if self.api_url.startswith("https://") else "ws"
        netloc = self.api_url.split("://", 1)[-1]
        return f"{scheme}://{netloc}/api/{self.session_name}/server"

    def _start_ws_consumer(self) -> None:
        """Start the dedicated thread that consumes the WebSocket stream."""
        if self._ws_thread is not None and self._ws_thread.is_alive():
            return
        self._ws_stop.clear()
        self._ws_thread = threading.Thread(
            target=self._ws_loop, daemon=True, name="whatsapp-ws"
        )
        self._ws_thread.start()

    def _ws_loop(self) -> None:
        """Consume the WebSocket, pushing normalized ``ChatEvent`` onto a queue.

        The thread is daemon and stops promptly when ``_ws_stop`` is set or the
        socket closes.  Events are normalized into ``ChatEvent``.
        """
        try:
            import websocket  # websocket-client
        except ImportError:
            return

        url = self._ws_url()
        if not url:
            return

        # Autenticazione: WAHA con API key richiede X-Api-Key anche sul WS;
        # senza, un stream autenticato viene rifiutato e i messaggi in ingresso
        # non arriverebbero mai (mentre l'invio REST funzionerebbe).
        headers: dict[str, str] | None = None
        if self._rest is not None and self._rest.api_key:
            headers = {"X-Api-Key": self._rest.api_key}

        # Su WAHA CORE/WEBJS lo stream /api/{session}/server non esiste (404);
        # dopo pochi tentativi falliti rinunciamo al WS (il polling è il
        # ricevitore effettivo) per non spammare log all'infinito.
        consecutive_failures = 0
        MAX_WS_RETRIES = 3

        while not self._ws_stop.is_set():
            try:
                ws = websocket.create_connection(url, timeout=5, header=headers)
                consecutive_failures = 0  # connessione ok: azzera il contatore
                while not self._ws_stop.is_set():
                    opcode, raw = ws.recv_data(control_frame=True)
                    if raw is None:
                        break
                    if opcode == websocket.ABNF.OPCODE_TEXT:
                        try:
                            payload = json.loads(raw.decode("utf-8", errors="replace"))
                        except json.JSONDecodeError:
                            continue
                        # A WS frame may be a single object or an array.
                        items = payload if isinstance(payload, list) else [payload]
                        for item in items:
                            if not isinstance(item, dict):
                                continue
                            event = _event_from_raw(item)
                            if event is not None:
                                self._enqueue_event(event)
                try:
                    ws.close()
                except Exception:
                    pass
            except Exception as exc:
                consecutive_failures += 1
                if consecutive_failures >= MAX_WS_RETRIES:
                    break  # WS non disponibile: il polling gestisce la ricezione
                import sys as _sys
                try:
                    print(f"[whatsapp-ws] WS error (retry in 2s): {exc}", file=_sys.stderr)
                except Exception:
                    pass
                try:
                    with open("/tmp/signal-ws-error.log", "a") as f:
                        f.write(f"{time.time()}: {exc}\n")
                except Exception:
                    pass
                if self._ws_stop.wait(timeout=2.0):
                    break
                continue

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

    def _add_cached_message(self, contact_id: str, msg: dict) -> None:
        """Append a message dict to the in-memory protocol cache (raw id key)."""
        if contact_id not in self.cache:
            self.cache[contact_id] = []
        self.cache[contact_id].append(msg)


    def _message_already_cached(self, contact_id: str, ts: int, is_mine: bool, text: str) -> bool:
        """Return True if a message with the same identity is already cached.

        Mirrors Signal's dedup: for outgoing messages within a short window the
        echo of an optimistic send is not stored twice; for incoming, the
        timestamp is part of the identity.
        """
        for msg in self.cache.get(contact_id, []):
            if not msg.get("is_mine") == is_mine:
                continue
            if msg.get("text") != text:
                continue
            if not is_mine:
                if msg.get("timestamp") == ts:
                    return True
            elif abs(msg.get("timestamp", 0) - ts) <= _SEND_DEDUP_WINDOW_MS:
                return True
        return False

    def ingest_message(self, contact_id: str, data: dict, ts: int) -> bool:
        """Save an incoming/outgoing message to the DB cache and in-memory cache.

        Returns ``True`` if newly added, ``False`` if it was a duplicate.
        """
        from backend import _add_message_to_cache
        text = data["text"]
        is_mine = data["is_mine"]

        if self._message_already_cached(contact_id, ts, is_mine, text):
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
        )
        self._add_cached_message(contact_id, {
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
        return updated


_SEND_DEDUP_WINDOW_MS = 5000

