"""
WhatsApp backend — a ``ChatBackend`` implementation for a Baileys-based
WhatsApp HTTP/WebSocket API (e.g. ``whatsapp-http-api``).

The backend is a thin client to a generic external service and never talks to
WhatsApp directly: REST endpoints for sessions/contacts/sending/media, plus a
PUSH webhook stream for incoming messages.  Incoming events are normalized
into ``ChatEvent`` with ``protocol = PROTOCOL_WHATSAPP``, mirroring the Signal
backend pattern so the TUI stays protocol-agnostic.
"""

from __future__ import annotations

import logging
import queue
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from models import (
    PROTOCOL_WHATSAPP,
    ChatContact,
    ChatEvent,
)

from .base import ChatBackend
from .config import (
    get_whatsapp_api_key,  # noqa: F401  re-export (whatsapp_rest reads it via backends.whatsapp)
    get_whatsapp_media_dir,
    get_whatsapp_session_name,
    get_whatsapp_webhook_url,
    resolve_whatsapp_api_url,
)
from .whatsapp_events import (
    _event_from_ack,  # noqa: F401  re-export for tests
    _event_from_message,
    _event_from_raw,
    _event_from_receipt,  # noqa: F401  re-export for tests
    _event_from_typing,  # noqa: F401  re-export for tests
    _jid_string,
)
from .whatsapp_rest import WhatsAppRESTClient

logger = logging.getLogger(__name__)


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

    def __init__(
        self,
        api_url: str | None = None,
        media_dir: str | None = None,
        session_name: str | None = None,
    ):
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
        self._seen_message_keys: set[tuple[str, str, str]] = set()

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

        # ── message.ack: extract & ingest BEFORE event normalisation ──────
        # Fix: WAHA sends message.ack INSTEAD of a separate message event for
        # outgoing echoes (including messages synced from another linked device).
        # The ack may have status < 3 (SERVER_ACK) which causes _event_from_ack
        # to return None, so we must process the ack content *before* the
        # event-is-None early-return.  Additionally, even when the ack yields a
        # receipt event (status >= 3), the TUI's _handle_receipt_event only
        # updates statuses of existing messages — it NEVER mounts new messages.
        # We therefore build a synthetic message ChatEvent when a new outgoing
        # message is ingested and enqueue it ahead of the receipt so the TUI
        # displays the message in real-time.
        added_from_ack = False
        ack_msg_event = None
        evt_name = raw.get("event", "")
        if "ack" in str(evt_name).lower():
            content = (
                raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
            )
            if content.get("fromMe") and content.get("id"):
                # WAHA may nest remoteJid inside "key" (same envelope as
                # /api/messages).  Without it group outgoing messages fail.
                ack_contact = _jid_string(
                    content.get("to")
                    or content.get("chatId")
                    or content.get("remoteJid")
                    or (content.get("key") or {}).get("remoteJid")
                )
                if ack_contact:
                    ack_ts = (
                        int(content.get("timestamp") or 0) * 1000
                    )  # WAHA uses seconds, we use ms
                    ack_text = content.get("body") or content.get("text") or ""

                    # ── Extract image/attachment metadata from ack payload ──
                    # WAHA message.ack payloads carry the same hasMedia/media
                    # fields as normal message events.  Without extracting
                    # them the synthetic event would lack msg_type/image and
                    # the TUI would show an empty text bubble instead of [🖼️].
                    ack_msg_type = "text"
                    ack_attachment_id = None
                    ack_attachment_info = None
                    if content.get("hasMedia"):
                        media = content.get("media")
                        if isinstance(media, dict):
                            mime = (media.get("mimetype") or "").lower()
                            if mime.startswith("image/"):
                                ack_msg_type = "image"
                            elif any(
                                mime.startswith(p)
                                for p in ("video/", "audio/", "application/")
                            ):
                                ack_msg_type = "attachment"
                            ack_attachment_id = media.get("url") or content.get("id")
                            ack_attachment_info = (
                                content.get("caption")
                                or media.get("caption")
                                or media.get("filename")
                                or mime
                                or "Media"
                            )

                    added_from_ack = self.ingest_message(
                        ack_contact,
                        {
                            "id": content.get("id"),
                            "text": ack_text,
                            "is_mine": True,
                            "sender": "You",
                            "msg_type": ack_msg_type,
                            "attachment_id": ack_attachment_id,
                            "attachment_info": ack_attachment_info,
                        },
                        ack_ts,
                    )
                    if added_from_ack:
                        # Synthetic message event: lets _handle_message_event
                        # mirror the message into the UI cache and call
                        # _add_message so it appears in real-time.
                        ack_msg_event = ChatEvent(
                            type="message",
                            protocol=PROTOCOL_WHATSAPP,
                            contact_id=ack_contact,
                            payload={
                                "text": ack_text,
                                "is_mine": True,
                                "sender": "You",
                                "timestamp": ack_ts,
                                "id": content.get("id"),
                                "is_group": ack_contact.endswith("@g.us")
                                if ack_contact
                                else False,
                                "msg_type": ack_msg_type,
                                "attachment_id": ack_attachment_id,
                                "attachment_info": ack_attachment_info,
                            },
                        )

        events = _event_from_raw(raw, self._contacts_by_jid)
        if not events:
            # Even when the raw event is not recognised as a receipt/typing
            # (e.g. message.ack with status < 3), a new outgoing message may
            # have been ingested above.  Enqueue the synthetic message event
            # so the TUI displays it immediately.
            if ack_msg_event is not None:
                self._enqueue_event(ack_msg_event)
                return True

            return False

        # When a message.ack resulted in a new message being ingested AND also
        # produced a receipt event, enqueue the synthetic message event first
        # so the TUI mounts the message BEFORE the receipt tries to update its
        # status.  The receipt is still enqueued afterwards.
        if ack_msg_event is not None:
            self._enqueue_event(ack_msg_event)

        # Dedup guard: WAHA can retry the same event.
        # For message events we use the per-message id; for others we use the
        # raw event key to avoid repeats.
        for event in events:
            if event.type == "message":
                mid = event.payload.get("id")
                key = (
                    event.contact_id,
                    str(mid),
                    " ".join(str(event.payload.get("text") or "").split()),
                )
                if mid and key in self._seen_message_keys:
                    continue  # retry già processato
                if mid:
                    self._seen_message_keys.add(key)
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
            "pending",
            "connecting",
            "unauthorized",
            "not_authenticated",
            "unpaired",
            "scan_qr",
            "scan_qr_code",
        )

    @property
    def is_working(self) -> bool:
        """Whether the WAHA session is confirmed WORKING (genuinely ready)."""
        if not self._rest:
            return False
        status = self._rest.get_session_status() or {}
        s = str(status.get("status") or "").lower()
        return s == "working"

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
        # Wait for session WORKING before loading contacts (otherwise
        # we get empty list while WAHA syncs after a device link).
        self._wait_session_ready(timeout=40.0)
        try:
            self._load_contacts()
        except Exception as _e:
            logger.debug("WhatsApp contact load failed", exc_info=True)
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
                # Dead states: no point waiting, exit immediately
                if s in ("failed", "stopped", "stop", ""):
                    return False
                # Pronto: stato WORKING o qualsiasi stato "stabile" non di
                # connessione/pairing (coerente con needs_pairing).
                if s == "working":
                    return True
                # Still in transient state — keep waiting
                if s not in (
                    "pending",
                    "connecting",
                    "unauthorized",
                    "not_authenticated",
                    "unpaired",
                    "scan_qr",
                    "scan_qr_code",
                    "starting",
                    "loading",
                    "syncing",
                ):
                    return False  # unknown state, don't wait
            except Exception as _e:
                # WAHA unreachable — stop waiting
                logger.debug(
                    "WAHA unreachable while waiting for session", exc_info=True
                )
                return False
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
            configured = current.get("config") or {}
            urls = [(w or {}).get("url") for w in (configured.get("webhooks") or [])]
            desired_events = [
                "message",
                "message.any",
                "message.ack",
                "message.ack.group",
            ]
            if webhook in urls:
                # URL già registrato — controlla se anche gli eventi sono
                # aggiornati (es. dopo un upgrade che ha aggiunto message.ack).
                for w in configured.get("webhooks") or []:
                    if (w or {}).get("url") == webhook:
                        current_events = (w or {}).get("events") or []
                        if set(current_events) >= set(desired_events):
                            return  # già aggiornato: niente restart
                        break
            self._rest.update_session_config(
                {
                    "config": {
                        "webhooks": [
                            {
                                "url": webhook,
                                "events": desired_events,
                            }
                        ]
                    }
                }
            )
        except Exception as _e:
            # best-effort: non bloccare mai l'avvio
            logger.debug("WhatsApp webhook config failed", exc_info=True)

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
        # Single call — list_contacts now uses only /chats with 5 s timeout.
        raw_contacts = self._rest.list_contacts() or []
        contacts: list[ChatContact] = []
        for c in raw_contacts:
            jid = c.get("id") or c.get("jid") or c.get("remoteJid")
            if not jid or "@broadcast" in jid:
                continue
            name = c.get("name") or c.get("pushName") or c.get("notifyName") or ""
            last_ts = int(c.get("last_ts") or 0)
            contacts.append(
                ChatContact(
                    id=jid,
                    display_name=name or jid,
                    protocol=PROTOCOL_WHATSAPP,
                    extras={"jid": jid, "last_message_ts": last_ts},
                )
            )
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
            events = _event_from_message(m, self._contacts_by_jid)
            for event in events:
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
            targets.update(jid for jid, unread, _ts in chats if unread > 0)
        except Exception as _e:  # se /chats fallisce restiamo sulle sole chat-DB
            logger.debug(
                "/chats discovery failed, staying on DB-only chats", exc_info=True
            )

        # Fetch parallelo: WAHA serve le richieste /api/messages concorrenti in
        # parallelo (verificato: 8 chat in ~7.6s totali vs ~34s sommati).  Ogni
        # worker tocca una chat diversa → nessuna contesa sullo stesso dato; le
        # scritture SQLite sono già serializzate da _DB_LOCK in backend/db.py.
        def _fetch_one(jid: str) -> None:
            try:
                self.fetch_history(jid, limit=limit)
            except (
                Exception
            ) as _e:  # best-effort: mai far fallire l'avvio per una singola chat
                logger.debug("History fetch failed for a single chat", exc_info=True)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(_fetch_one, targets))
        # Prune old messages AFTER the resync, so the next startup's cache
        # still includes old messages (for dedup) and the resync can fill
        # any gaps without re-inserting them as new.
        from backend import _prune_cache

        _prune_cache()
        return len(targets)

    # ─── Messaging ────────────────────────────────────────────────────

    async def send_message(
        self,
        contact_id: str,
        text: str,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_message: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> str:
        """Async send (interface contract); delegates to the sync path."""
        return self.send_message_sync(
            contact_id,
            text,
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
        reply_to_message_id: str | None = None,
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
            contact_id,
            text,
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
        from backend import _mark_as_read

        _mark_as_read(contact_id, protocol=PROTOCOL_WHATSAPP)

    # ─── Attachments ──────────────────────────────────────────────────

    def _ensure_media_dir(self) -> Path:
        """Return (and create if needed) the local media cache directory.

        Uses the configured ``WHATSAPP_MEDIA_DIR`` when available; otherwise
        falls back to ``~/.local/share/signal-tui-client/whatsapp-media/``
        so that lazy downloads always have a writeable destination.
        """
        if self.media_dir:
            base = Path(self.media_dir)
        else:
            from backend import CACHE_DIR

            base = CACHE_DIR / "whatsapp-media"
        base.mkdir(parents=True, exist_ok=True)
        return base

    def get_attachment_path(self, attachment_id: str) -> Path | None:
        """Map a media id to a local file path, downloading on-demand.

        1. If the file already exists in the media directory, return its
           ``Path`` immediately (fast path).
        2. Otherwise, attempt a lazy download from the WAHA REST API, save
           the bytes, and return the resulting ``Path``.
        3. If the download or save fails, return ``None`` — the UI shows a
           fallback placeholder and the user can retry by clicking again.

        This mirrors the Signal backend's behaviour where signal-cli
        auto-downloads attachments, but with explicit lazy-fetch for WAHA.
        """
        if not attachment_id or not self._rest:
            return None

        base = self._ensure_media_dir()
        safe_name = Path(attachment_id).name or attachment_id
        candidate = base / safe_name

        # Fast path: already on disk.
        if candidate.is_file():
            return candidate

        # Lazy download from WAHA.
        raw = self._rest.download_media(attachment_id)
        if not raw:
            return None

        try:
            candidate.write_bytes(raw)
        except OSError:
            return None

        return candidate

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
        self,
        contact_id: str,
        ts: int,
        is_mine: bool,
        text: str,
        msg_id: str | None = None,
    ) -> dict | None:
        """Return the cached message matching the same identity, or ``None``.

        For **incoming** messages (``is_mine=False``), dedup uses the exact
        id+text match when available.  Since WAHA ids can differ for the same
        message between webhook and REST API, it falls back to text with a
        fuzzy timestamp (±5s).

        For **outgoing** messages (``is_mine=True``) the id is stable and
        used as the primary identity (optimistic-send echo dedup).
        """
        for msg in self.cache.get(contact_id, []):
            if msg.get("is_mine") != is_mine:
                continue
            if msg.get("text") != text:
                continue
            if not is_mine:
                if msg_id and msg.get("id") == msg_id:
                    return msg
                # Text + fuzzy timestamp — the only stable identity
                # across WAHA's webhook and REST API paths.
                if abs(msg.get("timestamp", 0) - ts) <= 5000:
                    return msg
            elif msg_id:
                # Outgoing: id-based dedup first (echo), then text + window.
                cached_id = msg.get("id")
                if cached_id and cached_id == msg_id:
                    return msg
                if abs(msg.get("timestamp", 0) - ts) <= _ECHO_MATCH_WINDOW_MS:
                    return msg
            elif abs(msg.get("timestamp", 0) - ts) <= _SEND_DEDUP_WINDOW_MS:
                return msg
        return None

    def _persist_message(self, contact_id: str, data: dict, ts: int) -> None:
        """Persist a message to the SQLite cache (WhatsApp protocol)."""
        from backend import _add_message_to_cache

        _add_message_to_cache(
            contact_id,
            data["text"],
            data["is_mine"],
            data.get("sender", ""),
            ts,
            quote_text=data.get("quote_text"),
            msg_type=data.get("msg_type", "text"),
            attachment_info=data.get("attachment_info"),
            attachment_id=data.get("attachment_id"),
            protocol=PROTOCOL_WHATSAPP,
            msg_id=data.get("id"),
            status=data.get("status"),
            quote_timestamp=data.get("quote_timestamp"),
            quote_author=data.get("quote_author"),
            reply_to_message_id=data.get("reply_to_message_id"),
        )

    def ingest_message(
        self, contact_id: str, data: dict, ts: int, persist: bool = True
    ) -> bool:
        """Save an incoming/outgoing message to the DB cache and in-memory cache.

        When ``persist=False`` the in-memory cache is still seeded (dedup
        keeps working on the UI thread) but the SQLite write is skipped;
        the caller is responsible for calling ``_persist_message`` later.

        Returns ``True`` if newly added, ``False`` if it was a duplicate.
        """
        from backend import _update_message_id

        text = data["text"]
        is_mine = data["is_mine"]
        msg_id = data.get("id")

        existing = self._message_already_cached(contact_id, ts, is_mine, text, msg_id)
        if existing is not None:
            # Upgrade an existing entry that lacks an id (legacy / optimistic send).
            # This applies to both outgoing (optimistic send echo) and incoming
            # (legacy DB entries from before the msg_id column existed) so the
            # id-based dedup works on the next fetch and the message is never
            # re-inserted with read=False.
            if msg_id and not existing.get("id"):
                existing["id"] = msg_id
                if is_mine:
                    existing["timestamp"] = ts
                # L'upgrade cambia timestamp/id senza riordinare: rimettiamo in
                # ordine la chat così ```[-N:]``` del render resta corretto.
                self._sort_contact_cache(contact_id)
                _update_message_id(
                    contact_id,
                    text,
                    is_mine,
                    ts if is_mine else existing["timestamp"],
                    msg_id,
                    protocol=PROTOCOL_WHATSAPP,
                )
            return False

        if persist:
            self._persist_message(contact_id, data, ts)
        self._add_cached_message(
            contact_id,
            {
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
                "status": data.get("status", "sent" if is_mine else "read"),
                "quote_timestamp": data.get("quote_timestamp"),
                "quote_author": data.get("quote_author"),
                "reply_to_message_id": data.get("reply_to_message_id"),
            },
        )
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
        to_persist: list[tuple[str, dict]] = []
        for contact_id, msgs in self.cache.items():
            for msg in msgs:
                if msg.get("is_mine") and str(msg.get("id", "")) in {
                    str(i) for i in ids
                }:
                    old = msg.get("status", "sent")
                    rank = {
                        "pending": 0,
                        "failed": 0,
                        "sent": 1,
                        "delivered": 2,
                        "read": 3,
                    }
                    if old != target and rank.get(target, 0) > rank.get(old, 0):
                        msg["status"] = target
                        updated.append(msg)
                        to_persist.append((contact_id, msg))
        # Persist status changes to SQLite so they survive restarts.
        if to_persist:
            from backend import _update_message_status

            for contact_id, msg in to_persist:
                _update_message_status(
                    msg["timestamp"],
                    msg["status"],
                    protocol=PROTOCOL_WHATSAPP,
                    contact_number=contact_id,
                )
        return updated


_SEND_DEDUP_WINDOW_MS = 5000

# Window (ms) entro cui un'entry SENT senza id (invio ottimistico della TUI)
# può essere considerata l'echo di un messaggio con id reale, abbinandola per
# testo.  Copre il normale ritardo dell'echo di WAHA (che usa il proprio
# timestamp server, distante dal ts client) senza però far "inghiottire" a
# un'entry legacy (pre-fix, id=None) molto vecchia un messaggio mio
# genuinamente nuovo (es. inviato da un altro client) con lo stesso testo.
_ECHO_MATCH_WINDOW_MS = 600000  # 10 minuti
