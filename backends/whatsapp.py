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

    def _request(self, method: str, path: str, payload: dict | None = None,
                 timeout: int = 30) -> dict | None:
        """Execute an HTTP request and return the JSON body.

        Returns ``None`` on any transport/HTTP error (callers treat it as an
        unavailable service, matching Signal's error-tolerant style).
        """
        url = f"{self.base_url}{path}"
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                if not raw:
                    return {}
                return json.loads(raw)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError):
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

    def get_pairing_qr(self) -> str | None:
        """Return the current pairing QR string, or ``None`` if not available.

        WAHA exposes the QR under ``/api/sessions/{name}/qr``.  Fall back to
        creating the session (which may also return a QR on first start).
        """
        qr = self.get_session_qr()
        if qr:
            return qr
        self.create_session()
        return self.get_session_qr()

    def get_session_qr(self) -> str | None:
        """Return the QR string from WAHA, or ``None``."""
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
        """Return the raw contacts list (may contain nested ``data``)."""
        result = self._request("GET", "/api/contacts")
        if not result:
            return []
        if isinstance(result, list):
            return result
        # Defensive handling of { "data": [...] } wrappers.
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


def _event_from_message(raw: dict) -> ChatEvent | None:
    """Normalize a raw incoming message dict into a ``ChatEvent``."""
    chat_jid = raw.get("chatId") or raw.get("from") or raw.get("chat") or raw.get("remoteJid")
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
    chat_jid = raw.get("chatId") or raw.get("from") or raw.get("chat") or raw.get("remoteJid")
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
    chat_jid = raw.get("chatId") or raw.get("from") or raw.get("chat") or raw.get("remoteJid")
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

    if evt in ("message", "messages.upsert", "messages/upsert", "message.new"):
        return _event_from_message(content)
    if evt in ("typing", "presence.update", "presence/update", "presence", "presence.update",
               "presence.update"):
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

    @property
    def needs_pairing(self) -> bool:
        """Whether the backend is not authenticated and needs QR pairing."""
        if not self._rest:
            return False
        status = self._rest.get_session_status() or {}
        s = str(status.get("status") or "").lower()
        return s in ("pending", "connecting", "unauthorized", "not_authenticated", "unpaired")

    async def get_pairing_qr(self) -> str | None:
        """Return the current pairing QR string, or ``None`` if not pairing."""
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
        self._start_ws_consumer()
        self._connected = True

    async def disconnect(self) -> None:
        """Stop the WebSocket consumer and release resources."""
        self.disconnect_sync()

    def disconnect_sync(self) -> None:
        """Synchronous disconnect; stops the WebSocket consumer thread."""
        self._polling_active = False
        self._ws_stop.set()
        if self._ws_thread is not None and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=1.0)
        self._ws_thread = None
        self._connected = False

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
            contacts.append(ChatContact(
                id=jid,
                display_name=name or jid,
                protocol=PROTOCOL_WHATSAPP,
                extras={"jid": jid},
            ))
        self.contacts = contacts
        self._contacts_by_jid = {cc.id: cc for cc in contacts}

    def _identify_contact(self, jid: str) -> ChatContact | None:
        """Resolve a JID to a known ``ChatContact`` (or a placeholder)."""
        return self._contacts_by_jid.get(jid)

    async def list_contacts(self) -> list[ChatContact]:
        return list(self.contacts)


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

        WAHA exposes its event stream at ``/api/server`` (unauthenticated) or
        ``/api/{session}/server``.  Tried in order; the first is used.
        """
        if not self.api_url:
            return None
        for path in (f"/api/{self.session_name}/server", "/api/server"):
            url = f"{self.api_url}{path}"
            if url.startswith("https://"):
                url = "wss://" + url[len("https://"):]
            elif url.startswith("http://"):
                url = "ws://" + url[len("http://"):]
            return url
        return None

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

        while not self._ws_stop.is_set():
            try:
                ws = websocket.create_connection(url, timeout=5)
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
            except Exception:
                # Reconnect with backoff unless we're stopping.
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

