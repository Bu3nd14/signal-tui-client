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

import json
import logging
import os
import queue
import re
import shutil
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from models import (
    PROTOCOL_WHATSAPP,
    ChatContact,
    ChatEvent,
)

from .base import ChatBackend, should_upgrade_outgoing_attachment
from .config import (
    get_address_book_ttl_s,
    get_wa_lid_cache_ttl_days,
    get_whatsapp_api_key,  # noqa: F401  re-export (whatsapp_rest reads it via backends.whatsapp)
    get_whatsapp_media_dir,
    get_whatsapp_session_name,
    get_whatsapp_webhook_url,
    resolve_whatsapp_api_url,
)
from .whatsapp_events import (
    WAHA_ACK_DEVICE,
    WAHA_ACK_READ,
    _event_from_ack,  # noqa: F401  re-export for tests
    _event_from_message,
    _event_from_raw,
    _event_from_reaction,  # noqa: F401  re-export for tests
    _event_from_receipt,  # noqa: F401  re-export for tests
    _event_from_typing,  # noqa: F401  re-export for tests
    _jid_string,
    canonical_msg_id,
)
from .whatsapp_rest import WhatsAppRESTClient

logger = logging.getLogger(__name__)

_FETCHABLE_JID_RE = re.compile(r"^[0-9][0-9-]*@(c\.us|lid|g\.us)$")


def _is_fetchable_jid(jid: str) -> bool:
    return bool(_FETCHABLE_JID_RE.fullmatch(jid))


def _jid_digits(jid: str) -> str:
    """Return only the digits of a JID (the phone for ``@c.us``), or ``""``."""
    return "".join(ch for ch in jid if ch.isdigit())


def _dedup_book_contacts(raw: list[dict]) -> list[dict]:
    """Deduplicate the WAHA address book by phone number (pure, unit-testable).

    Key = digits of the number extracted from ``id`` (``_serialized`` handled
    when ``id`` is a dict), discarding every non-digit and the ``@c.us``/
    ``@s.whatsapp.net`` domain.  Entries with an empty key or ``@broadcast``/
    ``@newsletter``/``@g.us`` are dropped.  Among duplicates of the same number
    the winner is chosen by: (1) a non-empty ``name`` over only-``pushname``,
    (2) the ``@c.us`` domain, (3) first occurrence (stable).
    """
    by_phone: dict[str, dict] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        jid = _jid_string(entry.get("id"))
        if not jid:
            continue
        if "@broadcast" in jid or "@newsletter" in jid or jid.endswith("@g.us"):
            continue
        digits = _jid_digits(jid)
        if not digits:
            continue
        name = entry.get("name") or ""
        pushname = entry.get("pushname") or entry.get("pushName") or None
        current = by_phone.get(digits)
        if current is None:
            by_phone[digits] = {
                "phone": digits,
                "name": name,
                "pushname": pushname,
                "_jid": jid,
            }
            continue
        # (1) name non vuoto batte solo-pushname
        candidate_has_name = bool(name)
        current_has_name = bool(current["name"])
        if candidate_has_name != current_has_name:
            if candidate_has_name:
                by_phone[digits] = {
                    "phone": digits,
                    "name": name,
                    "pushname": pushname,
                    "_jid": jid,
                }
            continue
        # (2) a parità, preferisci il dominio @c.us
        candidate_is_c = jid.endswith("@c.us")
        current_is_c = current["_jid"].endswith("@c.us")
        if candidate_is_c and not current_is_c:
            by_phone[digits] = {
                "phone": digits,
                "name": name,
                "pushname": pushname,
                "_jid": jid,
            }
        # (3) a parità, vince la prima occorrenza (keep current)
    return [
        {"phone": v["phone"], "name": v["name"], "pushname": v["pushname"]}
        for v in by_phone.values()
    ]


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
        self._seen_message_keys: set[tuple[str, ...]] = set()
        self._history_unfetchable: set[str] = set()
        self._media_pending: dict[tuple[str, str], dict] = {}
        self._media_lock = threading.Lock()
        self._media_resolver_thread: threading.Thread | None = None
        self._media_resolver_stop = False

        # ── Address book (rubrica completa) ────────────────────────────
        self._address_book: list[ChatContact] | None = None
        self._address_book_ts: float = 0.0
        #: Persistent cache ``@lid`` → phone number (lazy-loaded from disk).
        self._lid_map: dict[str, dict] | None = None
        self._lid_lock = threading.Lock()
        self._lid_resolver_started = False

        # ── Presence (typing) subscription ─────────────────────────────
        #: Chats already subscribed to ``presence.update`` via WAHA's per-chat
        #: endpoint.  The idempotency guard lives here so the background sweep
        #: and the lazy triggers never double-POST.
        self._presence_subscribed: set[str] = set()
        self._presence_subscribe_started = False

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

        # ── message.ack: translate into a synthetic message event only ────
        # WAHA sends message.ack INSTEAD of a separate message event for
        # outgoing echoes (including messages synced from another linked
        # device).  The ack may have status < 2 (ERROR/PENDING/SERVER), which
        # makes ``_event_from_ack`` return None, so we must build the message
        # event from the ack content *before* the event-is-None early-return.
        #
        # Single-mutation-point rule: we do NOT call ``ingest_message`` here.
        # The consumer (``_handle_message_event``) performs the ingestion and
        # UI mirroring, so the bubble is born WITH the real id and a later
        # receipt can upgrade its status by id.  We only dedup the synthetic
        # event by (contact, id, text) so WAHA retries are not enqueued twice.
        ack_msg_event = None
        evt_name = raw.get("event", "")
        is_ack_event = "ack" in str(evt_name).lower()
        if is_ack_event:
            content = (
                raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
            )
            ack_id = content.get("id") or (content.get("key") or {}).get("id")
            if content.get("fromMe") and ack_id:
                # WAHA may nest remoteJid inside "key" (same envelope as
                # /api/messages).  Without it group outgoing messages fail.
                ack_contact = _jid_string(
                    content.get("to")
                    or content.get("chatId")
                    or content.get("remoteJid")
                    or (content.get("key") or {}).get("remoteJid")
                )
                if ack_contact:
                    # Lazy per-chat presence subscribe: the first outgoing
                    # message event for a contact is a cheap signal that we
                    # want its typing indicator too.
                    self._presence_subscribe_lazy(ack_contact)
                    ack_ts = (
                        int(content.get("timestamp") or 0) * 1000
                    )  # WAHA uses seconds, we use ms
                    ack_text = content.get("body") or content.get("text") or ""
                    ack_has_media = bool(content.get("hasMedia"))
                    ack_media = content.get("media")
                    skip_ack_message = (
                        ack_has_media
                        and not (
                            isinstance(ack_media, dict) and ack_media.get("url")
                        )
                    ) or (not ack_has_media and not str(ack_text).strip())

                    # ── Edit detection (message.ack with a new body) ──────
                    # An outgoing edit arrives as message.ack with the SAME id
                    # and a NEW body.  Detect it against the cache and emit a
                    # message_edit event instead of a synthetic "message" (which
                    # would mount a duplicate bubble).
                    if skip_ack_message:
                        if ack_has_media:
                            self._schedule_media_resolve(ack_contact, str(ack_id))
                        logger.debug(
                            "Skipping empty synthetic WhatsApp ack: id=%s hasMedia=%s",
                            ack_id,
                            ack_has_media,
                        )
                    elif ack_id and self._detect_edit(
                        ack_contact, str(ack_id), ack_text, True, ack_ts
                    ):
                        ack_key = (
                            ack_contact,
                            str(ack_id),
                            " ".join(str(ack_text).split()),
                            "",
                        )
                        if ack_key not in self._seen_message_keys:
                            self._seen_message_keys.add(ack_key)
                            self._enqueue_event(
                                ChatEvent(
                                    type="message_edit",
                                    protocol=PROTOCOL_WHATSAPP,
                                    contact_id=ack_contact,
                                    payload={
                                        "edit_message_id": str(ack_id),
                                        "text": ack_text,
                                        "timestamp": ack_ts,
                                        "edit_timestamp": None,
                                        "is_mine": True,
                                        "sender": "You",
                                        "contact": self._contacts_by_jid.get(
                                            ack_contact
                                        ),
                                        "msg_type": "text",
                                    },
                                )
                            )
                        # niente evento sintetico "message"; gli eventuali
                        # receipt (ack >= 2) prodotti da _event_from_raw
                        # proseguono invariati.
                    else:
                        # ── Extract image/attachment metadata from ack payload ──
                        # WAHA message.ack payloads carry the same hasMedia/media
                        # fields as normal message events.  Without extracting
                        # them the synthetic event would lack msg_type/image and
                        # the TUI would show an empty text bubble instead of [🖼️].
                        ack_msg_type = "text"
                        ack_attachment_id = None
                        ack_attachment_info = None
                        if ack_has_media and isinstance(ack_media, dict):
                            mime = (ack_media.get("mimetype") or "").lower()
                            if mime.startswith("image/"):
                                ack_msg_type = "image"
                            elif any(
                                mime.startswith(p)
                                for p in ("video/", "audio/", "application/")
                            ):
                                ack_msg_type = "attachment"
                            ack_attachment_id = ack_media.get("url")
                            ack_attachment_info = (
                                content.get("caption")
                                or str(
                                    content.get("body") or content.get("text") or ""
                                ).strip()
                                or ack_media.get("caption")
                                or ack_media.get("filename")
                                or mime
                                or "Media"
                            )

                        # Dedup by (contact, id, normalized text) so a retry of the
                        # same ack does not enqueue the synthetic message twice.
                        ack_key = (
                            ack_contact,
                            str(ack_id),
                            " ".join(str(ack_text).split()),
                            str(ack_attachment_id or ""),
                        )
                        if ack_id and ack_key not in self._seen_message_keys:
                            self._seen_message_keys.add(ack_key)
                            if ack_msg_type == "image":
                                ack_text = ""
                            ack_msg_event = ChatEvent(
                                type="message",
                                protocol=PROTOCOL_WHATSAPP,
                                contact_id=ack_contact,
                                payload={
                                    "text": ack_text,
                                    "is_mine": True,
                                    "sender": "You",
                                    "timestamp": ack_ts,
                                    "id": ack_id,
                                    "is_group": ack_contact.endswith("@g.us")
                                    if ack_contact
                                    else False,
                                    "msg_type": ack_msg_type,
                                    "attachment_id": ack_attachment_id,
                                    "attachment_info": ack_attachment_info,
                                },
                            )

        if not is_ack_event:
            content = (
                raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
            )
            media = content.get("media")
            if content.get("hasMedia") and not (
                isinstance(media, dict) and media.get("url")
            ):
                key = content.get("key") or {}
                is_mine = bool(
                    content.get("fromMe")
                    or content.get("isMe")
                    or content.get("outgoing")
                )
                chat = _jid_string(
                    content.get("chatId")
                    or content.get("remoteJid")
                    or key.get("remoteJid")
                    or (content.get("to") if is_mine else content.get("from"))
                    or content.get("from")
                )
                msg_id = content.get("id") or key.get("id")
                if chat and msg_id:
                    self._schedule_media_resolve(chat, str(msg_id))

        events = _event_from_raw(raw, self._contacts_by_jid)
        if not events:
            # Even when the raw event is not recognised as a receipt/typing
            # (e.g. message.ack with status < 2), enqueue the synthetic message
            # event so the TUI mounts and ingests it.
            if ack_msg_event is not None:
                self._enqueue_event(ack_msg_event)
                return True

            return False

        # When a message.ack also produced a receipt event (status >= 2),
        # enqueue the synthetic message event first so the TUI mounts the
        # message BEFORE the receipt tries to update its status.
        if ack_msg_event is not None:
            self._enqueue_event(ack_msg_event)

        # Dedup guard: WAHA can retry the same event.
        # For message events we use the per-message id; for others we use the
        # raw event key to avoid repeats.
        for event in events:
            if event.type == "message":
                # Lazy per-chat presence subscribe on the first message from a
                # contact we haven't subscribed yet (covers new chats).
                self._presence_subscribe_lazy(event.contact_id)
                mid = event.payload.get("id")
                hit = self._detect_edit(
                    event.contact_id,
                    str(mid) if mid else None,
                    event.payload.get("text") or "",
                    bool(event.payload.get("is_mine")),
                    int(event.payload.get("timestamp") or 0),
                )
                if hit is not None:
                    event = ChatEvent(
                        type="message_edit",
                        protocol=PROTOCOL_WHATSAPP,
                        contact_id=event.contact_id,
                        payload={
                            "edit_message_id": str(hit.get("id") or mid),
                            "text": event.payload.get("text") or "",
                            "timestamp": int(hit.get("timestamp") or 0),  # ts ORIGINALE
                            "edit_timestamp": int(event.payload.get("timestamp") or 0)
                            or None,
                            "is_mine": bool(hit.get("is_mine")),
                            "sender": event.payload.get("sender", ""),
                            "contact": self._contacts_by_jid.get(event.contact_id),
                            "msg_type": "text",
                        },
                    )
                key = (
                    event.contact_id,
                    str(mid),
                    " ".join(str(event.payload.get("text") or "").split()),
                    str(event.payload.get("attachment_id") or ""),
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
        # Opportunistico: se già connesso, avvia il resolver @lid in background
        # (idempotente) così la prossima apertura della rubrica beneficia.
        self.start_lid_resolver()
        # Subscribe presence per-chat in background (idempotente): senza questa
        # WAHA non distribuisce ``presence.update`` e il typing non arriva.
        self.start_presence_subscribe()

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
                "presence.update",
                "message.reaction",
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
        self._media_resolver_stop = True
        thread = self._media_resolver_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

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
        self._lid_cache_load()
        # Single call — list_contacts now uses only /chats with 5 s timeout.
        raw_contacts = self._rest.list_contacts() or []
        contacts: list[ChatContact] = []
        for c in raw_contacts:
            jid = c.get("id") or c.get("jid") or c.get("remoteJid")
            if not jid or "@broadcast" in jid:
                continue
            name = c.get("name") or c.get("pushName") or c.get("notifyName") or ""
            last_ts = int(c.get("last_ts") or 0)
            extras: dict[str, object] = {"jid": jid, "last_message_ts": last_ts}
            phone = self._contact_phone(jid)
            if phone:
                extras["phone"] = phone
            contacts.append(
                ChatContact(
                    id=jid,
                    display_name=name or jid,
                    protocol=PROTOCOL_WHATSAPP,
                    extras=extras,
                )
            )
        self.contacts = contacts
        self._contacts_by_jid = {cc.id: cc for cc in contacts}

    def _contact_phone(self, jid: str) -> str:
        """Return the phone for a contact JID, or ``""`` when unknown.

        ``@c.us`` JIDs carry the phone in the local part; ``@lid`` JIDs are
        resolved through the persistent lid→phone cache (possibly empty on first
        boot, until the background resolver fills it).  Everything else has no
        phone number (e.g. ``@g.us``/``@s.whatsapp.net``).
        """
        if jid.endswith("@c.us"):
            return _jid_digits(jid.split("@", 1)[0])
        if jid.endswith("@lid"):
            return str((self._lid_map or {}).get(jid, {}).get("phone") or "")
        return ""

    def _identify_contact(self, jid: str) -> ChatContact | None:
        """Resolve a JID to a known ``ChatContact`` (or a placeholder)."""
        return self._contacts_by_jid.get(jid)

    def register_contact(self, contact: ChatContact) -> None:
        """Registra un contatto (open-or-create) anche nella lookup JID→contact.

        Oltre all'append in ``self.contacts`` (default di ``ChatBackend``),
        aggiorna ``_contacts_by_jid`` così ``_identify_contact`` e il webhook
        riconoscono subito il ghost senza creare placeholder duplicati.
        """
        super().register_contact(contact)
        self._contacts_by_jid[contact.id] = contact

    async def list_contacts(self) -> list[ChatContact]:
        return list(self.contacts)

    # ─── Address book (rubrica completa) ──────────────────────────────

    def list_address_book_sync(self, force: bool = False) -> list[ChatContact]:
        """Rubrica WhatsApp completa = rubrica dedup ∪ chat attive.

        Bloccante (chiamare da worker thread); non solleva mai eccezioni: su
        errore remoto serve la copia cached (stale) o ``[]``.  I ``@lid`` non
        in cache NON vengono risolti qui (zero rete): restano standalone con
        ``lid_unresolved=True`` e li risolve in background ``start_lid_resolver``.
        """
        self.start_lid_resolver()
        now = time.monotonic()
        if (
            not force
            and self._address_book is not None
            and (now - self._address_book_ts) < get_address_book_ttl_s()
        ):
            return list(self._address_book)
        try:
            raw = self._rest.list_all_contacts() if self._rest else None
            book = _dedup_book_contacts(raw or [])
            chats = list(self.contacts)

            by_phone: dict[str, ChatContact] = {}
            for b in book:
                by_phone[b["phone"]] = ChatContact(
                    id=f"{b['phone']}@c.us",
                    display_name=b["name"] or b["pushname"] or f"+{b['phone']}",
                    protocol=PROTOCOL_WHATSAPP,
                    extras={
                        "phone": b["phone"],
                        "jid": f"{b['phone']}@c.us",
                        "address_book": True,
                        "is_chat_active": False,
                        "source": "wa_book",
                    },
                )

            out_extra: list[ChatContact] = []
            for chat in chats:
                cid = chat.id
                if cid.endswith("@g.us"):
                    # Gruppi: SOLO da /chats, mai dalla rubrica.
                    out_extra.append(
                        replace(
                            chat,
                            extras={
                                **chat.extras,
                                "address_book": True,
                                "is_chat_active": True,
                                "source": "wa_chats",
                            },
                        )
                    )
                    continue
                phone = (
                    _jid_digits(cid) if cid.endswith("@c.us") else self._lid_lookup(cid)
                )
                if phone and phone in by_phone:
                    # MERGE: l'id diventa quello della chat attiva (anche @lid)
                    # per mantenere continuity con cache/send path esistenti.
                    merged = by_phone[phone]
                    merged.id = cid
                    merged.extras["is_chat_active"] = True
                    merged.last_message_ts = chat.last_message_ts
                    if cid.endswith("@lid"):
                        merged.extras["lid"] = cid
                    # display_name: vince il nome rubrica; si usa quello della
                    # chat solo se la rubrica non aveva un nome reale.
                    if (
                        merged.display_name == f"+{phone}"
                        and chat.display_name
                        and chat.display_name != cid
                    ):
                        merged.display_name = chat.display_name
                elif phone:
                    # Chat @c.us attiva NON in rubrica.
                    out_extra.append(
                        replace(
                            chat,
                            extras={
                                **chat.extras,
                                "address_book": True,
                                "is_chat_active": True,
                                "source": "wa_chats",
                                "phone": phone,
                            },
                        )
                    )
                else:
                    # @lid non risolto: standalone non raggruppabile.
                    out_extra.append(
                        replace(
                            chat,
                            extras={
                                **chat.extras,
                                "address_book": True,
                                "is_chat_active": True,
                                "lid_unresolved": True,
                                "source": "wa_chats",
                            },
                        )
                    )

            self._address_book = list(by_phone.values()) + out_extra
            self._address_book_ts = now
        except Exception:
            logger.warning("WhatsApp address book build failed", exc_info=True)
            if self._address_book is not None:
                return list(self._address_book)
            return []
        return list(self._address_book)

    # ─── Persistent @lid → phone cache ────────────────────────────────

    def _lid_cache_path(self) -> Path:
        """Return ``CACHE_DIR/wa_lid_map.json`` (respects test override)."""
        from backend import CACHE_DIR

        return Path(CACHE_DIR) / "wa_lid_map.json"

    def _lid_cache_load(self) -> None:
        """Load the lid map from disk (idempotent, never raises)."""
        if self._lid_map is not None:
            return
        with self._lid_lock:
            if self._lid_map is not None:
                return
            self._lid_map = {}
            try:
                path = self._lid_cache_path()
                if not path.exists():
                    return
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("entries"), dict):
                    self._lid_map = {
                        k: v for k, v in data["entries"].items() if isinstance(v, dict)
                    }
            except (OSError, json.JSONDecodeError, ValueError):
                self._lid_map = {}

    def _lid_cache_save(self) -> None:
        """Persist the lid map atomically (tmp + ``os.replace``), never raises."""
        with self._lid_lock:
            entries = self._lid_map if self._lid_map is not None else {}
            payload = {"version": 1, "entries": entries}
            try:
                path = self._lid_cache_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_text(json.dumps(payload), encoding="utf-8")
                os.replace(tmp, path)
            except OSError:
                logger.warning("Failed to persist WhatsApp lid cache", exc_info=True)

    def _lid_lookup(self, jid: str) -> str | None:
        """Return the cached phone for a ``@lid`` JID (memory only, no network)."""
        self._lid_cache_load()
        entry = self._lid_map.get(jid)
        if not isinstance(entry, dict):
            return None
        phone = entry.get("phone")
        if not phone:
            return None
        age = int(time.time()) - int(entry.get("resolved_at") or 0)
        if age > get_wa_lid_cache_ttl_days() * 86400:
            return None
        return str(phone)

    def _lid_cached(self, jid: str) -> bool:
        """True if ``jid`` has a valid cache entry (positive or negative in TTL)."""
        self._lid_cache_load()
        entry = self._lid_map.get(jid)
        if not isinstance(entry, dict):
            return False
        age = int(time.time()) - int(entry.get("resolved_at") or 0)
        if entry.get("phone"):
            return age <= get_wa_lid_cache_ttl_days() * 86400
        return age <= 24 * 3600

    def _lid_resolve_remote(self, jid: str) -> str | None:
        """Resolve a ``@lid`` via REST and update the cache; return the phone."""
        if not self._rest:
            return None
        result = self._rest.resolve_contact(jid)
        if not isinstance(result, dict):
            return None  # transport/HTTP error → no negative cache, retry later
        resolved_jid = _jid_string(result.get("id"))
        phone = None
        if resolved_jid and not resolved_jid.endswith("@lid"):
            phone = _jid_digits(resolved_jid) or None
        name = (
            result.get("name") or result.get("pushname") or result.get("pushName")
        ) or None
        self._lid_cache_load()
        with self._lid_lock:
            self._lid_map[jid] = {
                "phone": phone,
                "name": name,
                "resolved_at": int(time.time()),
            }
        return phone

    def start_lid_resolver(self) -> None:
        """Start the background ``@lid``→number resolver (idempotent)."""
        if self._lid_resolver_started or not self._rest:
            return
        self._lid_resolver_started = True
        threading.Thread(
            target=self._lid_resolver_run,
            name="wa-lid-resolver",
            daemon=True,
        ).start()

    def _lid_resolver_run(self) -> None:
        """Resolve up to 30 uncached ``@lid`` chats, then save and invalidate."""
        try:
            self._lid_cache_load()
            candidates = [
                contact.id
                for contact in list(self.contacts)
                if contact.id
                and contact.id.endswith("@lid")
                and not self._lid_cached(contact.id)
            ]
            if not candidates:
                return
            for jid in candidates[:30]:
                try:
                    self._lid_resolve_remote(jid)
                except Exception:
                    logger.debug("lid resolve failed for %s", jid, exc_info=True)
                time.sleep(0.3)
            self._lid_cache_save()
            self._address_book = None
        except Exception:
            logger.warning("WhatsApp lid resolver run failed", exc_info=True)

    # ─── Presence (typing) subscription ────────────────────────────────
    # WAHA only distributes ``presence.update`` for chats we subscribed to via
    # ``POST /api/{session}/presence/{chatId}/subscribe``.  Subscribing is
    # best-effort and idempotent: a missing API or an error degrades silently
    # to "no typing indicator", never to a crash.
    #
    # IMPORTANTE (21/08/2026): il typing WhatsApp è WON'T FIX sul deploy
    # attuale — WAHA 2026.8.1 engine WEBJS non implementa ``subscribePresence``
    # (ogni subscribe risponde 500).  Lo sweep al connect (N chat × POST
    # fallite + pausa 0.3s) e le lazy subscribe per messaggio/ack sono quindi
    # LAVORO INUTILE che carica WAHA e occupa thread del processo TUI.  La
    # subscription è disattivata per default; riabilitabile con l'env
    # ``WAHA_PRESENCE_SUBSCRIBE=1`` (utile se si passa a NOWEB o WAHA fixa
    # WEBJS).  Il parser ``_event_from_typing`` resta attivo e invariato.
    _PRESENCE_SUBSCRIBE_ENABLED = os.environ.get(
        "WAHA_PRESENCE_SUBSCRIBE", ""
    ).strip().lower() in ("1", "true", "yes", "on")

    def _presence_subscribe_post(self, chat_id: str) -> None:
        """Perform the presence-subscribe POST (best-effort, never raises)."""
        try:
            self._rest.presence_subscribe(chat_id)
        except Exception:
            logger.debug("presence subscribe failed for %s", chat_id, exc_info=True)

    def _presence_subscribe(self, chat_id: str) -> None:
        """Subscribe a chat to presence updates (idempotent, fire-and-forget).

        The idempotency guard on ``self._presence_subscribed`` prevents
        duplicate POSTs.  Used directly by the background sweep (already on a
        worker thread); the lazy call sites use ``_presence_subscribe_lazy``.
        Disabled when presence subscription is off (WON'T FIX su WEBJS).
        """
        if not self._PRESENCE_SUBSCRIBE_ENABLED:
            return
        if not chat_id or not self._rest:
            return
        if chat_id in self._presence_subscribed:
            return
        self._presence_subscribed.add(chat_id)
        self._presence_subscribe_post(chat_id)

    def _presence_subscribe_lazy(self, chat_id: str) -> None:
        """Non-blocking one-time presence subscribe for lazy call sites.

        Marks the chat subscribed immediately (idempotency, no double-POST
        races) and performs the actual POST on a daemon thread so it never
        blocks the UI/webhook/fetch-history caller.  Disabled when presence
        subscription is off (WON'T FIX su WEBJS).
        """
        if not self._PRESENCE_SUBSCRIBE_ENABLED:
            return
        if not chat_id or not self._rest:
            return
        if chat_id in self._presence_subscribed:
            return
        self._presence_subscribed.add(chat_id)
        threading.Thread(
            target=self._presence_subscribe_post,
            args=(chat_id,),
            name="wa-presence-subscribe",
            daemon=True,
        ).start()

    def start_presence_subscribe(self) -> None:
        """Start the background per-chat presence subscription sweep (idempotent)."""
        if not self._PRESENCE_SUBSCRIBE_ENABLED:
            return
        if self._presence_subscribe_started or not self._rest:
            return
        self._presence_subscribe_started = True
        threading.Thread(
            target=self._presence_subscribe_run,
            name="wa-presence-subscribe",
            daemon=True,
        ).start()

    def _presence_subscribe_run(self) -> None:
        """Subscribe presence for the known chats, pausing briefly in between."""
        try:
            for contact in list(self.contacts):
                chat_id = contact.id
                if not chat_id:
                    continue
                self._presence_subscribe(chat_id)
                time.sleep(0.3)
        except Exception:
            logger.warning("WhatsApp presence subscribe sweep failed", exc_info=True)

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
        if (
            not _is_fetchable_jid(contact_id)
            or contact_id in self._history_unfetchable
            or not self._rest
        ):
            return []
        # Lazy per-chat presence subscribe: aprire una chat è il segnale che
        # vogliamo anche il suo indicatore di digitazione.
        self._presence_subscribe_lazy(contact_id)
        raw = self._rest.list_messages(contact_id, limit=limit)
        if (
            raw == []
            and self._rest.last_status == 500
            and "No LID" in (self._rest.last_error or "")
        ):
            self._history_unfetchable.add(contact_id)
        if not isinstance(raw, list):
            return []
        # WAHA ritorna i messaggi dal più recente in giù; li riordiniamo
        # cronologicamente, poi li ingeriamo nel cache.
        msgs = [m for m in raw if isinstance(m, dict)]
        msgs.sort(key=lambda m: int(m.get("timestamp") or 0))
        # Riconciliazione read/delivery dallo storico: per i messaggi MIEI già
        # confermati (ack >= 2 = DEVICE) emettiamo eventi receipt (single
        # mutation point: nessuna scrittura cache/DB qui — sarà process_receipt
        # ad applicarli).
        read_ids: set[str] = set()
        delivered_ids: set[str] = set()
        for m in msgs:
            media = m.get("media")
            if m.get("hasMedia") and not (
                isinstance(media, dict) and media.get("url")
            ):
                msg_id = m.get("id") or (m.get("key") or {}).get("id")
                if msg_id:
                    self._schedule_media_resolve(contact_id, str(msg_id))
            events = _event_from_message(m, self._contacts_by_jid)
            for event in events:
                payload = event.payload

                is_mine = payload.get("is_mine", False)
                # Uno storico WAHA riporta il testo GIÀ editato con id/ts
                # originali: se il messaggio è già in cache con testo diverso,
                # aggiorna la riga esistente invece di ingerire un duplicato.
                if self._detect_edit(
                    contact_id,
                    str(payload.get("id") or ""),
                    payload.get("text", ""),
                    is_mine,
                    int(payload.get("timestamp") or 0),
                ):
                    self.apply_edit(
                        contact_id,
                        str(payload.get("id")),
                        payload.get("text", ""),
                        is_mine=is_mine,
                    )
                    continue
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

                ack = payload.get("ack")
                mid = payload.get("id")
                if is_mine and isinstance(ack, int) and ack >= WAHA_ACK_DEVICE and mid:
                    (read_ids if ack >= WAHA_ACK_READ else delivered_ids).add(str(mid))

        # Enqueue aggregated receipt events AFTER the ingestion, so the
        # consumer mounts the messages before applying their read/delivered
        # status (same ordering contract as the live webhook path).
        for mid in sorted(delivered_ids - read_ids):
            self._enqueue_event(
                ChatEvent(
                    type="receipt",
                    protocol=PROTOCOL_WHATSAPP,
                    contact_id=contact_id,
                    payload={"message_ids": [mid], "is_read": False},
                )
            )
        for mid in sorted(read_ids):
            self._enqueue_event(
                ChatEvent(
                    type="receipt",
                    protocol=PROTOCOL_WHATSAPP,
                    contact_id=contact_id,
                    payload={"message_ids": [mid], "is_read": True},
                )
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
        return len(targets)

    # ─── Messaging ────────────────────────────────────────────────────

    def _resolve_send_chat_id(self, cid: str) -> str:
        if not cid.endswith("@lid"):
            return cid
        phone = self._lid_lookup(cid) or self._lid_resolve_remote(cid)
        if not phone:
            raise RuntimeError(
                f"WhatsApp: chat {cid} non risolvibile a numero — impossibile inviare"
            )
        return f"{phone}@c.us"

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
            reply_to_message_id=reply_to_message_id,
        )

    @staticmethod
    def _extract_message_id(result: dict) -> str | None:
        """Return the Baileys message id from a WAHA ``sendText`` response.

        WAHA versions disagree on both the field name and its shape: some return
        a flat ``id`` string, others nest it under ``key.id`` (same shape as
        ``/api/messages``), and a few older builds use ``messageId``/``msgId``.
        Recent builds return the id as a **dict** (Baileys ``MsgKey``-like)::

            {"id": {"fromMe": True, "remote": "…@lid", "id": "<hex>",
                    "$1": "true_…@lid_<hex>", "_serialized": "true_…@lid_<hex>"}}

        For a dict value we prefer ``_serialized``, then ``$1``, then ``id``:
        ``_serialized``/``$1`` carry the full ``true_…@lid_<hex>`` id used by the
        DB and the edit endpoint, while ``id`` alone is only the hex part.  We
        probe every field so the real id reaches the UI cache regardless of the
        WAHA version.
        """
        if not isinstance(result, dict):
            return None

        def _pick(val: object) -> str | None:
            if isinstance(val, str) and val:
                return val
            if isinstance(val, dict):
                for sub in ("_serialized", "$1", "id"):
                    candidate = val.get(sub)
                    if isinstance(candidate, str) and candidate:
                        return candidate
            return None

        for key in ("id", "messageId", "msgId", "message_id", "msg_id"):
            extracted = _pick(result.get(key))
            if extracted:
                return extracted
        key_obj = result.get("key")
        if isinstance(key_obj, dict):
            extracted = _pick(key_obj.get("id"))
            if extracted:
                return extracted
        return None

    def send_message_sync(
        self,
        contact_id: str,
        text: str,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_message: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> str | None:
        """Send *text* to *contact_id*; returns the Baileys message id.

        Used by the TUI's sync worker threads (same pattern as Signal).  Raises
        ``RuntimeError`` if the API is unreachable or answers with an error, so
        the caller can surface a visible error instead of silently failing.

        Returns the server-assigned message id (string) so the caller can attach
        it to the optimistic entry (mirroring Telegram's ``send_message_sync``).
        When the WAHA response does not carry an id (older/odd builds), returns
        ``None``: the caller then skips the id-upgrade and lets the subsequent
        echo (webhook ack) attach the real id as before.
        """
        if not self._rest:
            raise RuntimeError("WhatsApp API is not configured")
        send_chat_id = self._resolve_send_chat_id(contact_id)
        result = self._rest.send_message(
            send_chat_id,
            text,
            quote_timestamp=quote_timestamp,
            quote_author=quote_author,
            quote_message=quote_message,
            reply_to_message_id=reply_to_message_id,
        )
        if result is None:
            status = self._rest.last_status
            detail = self._rest.last_error or "unreachable"
            raise RuntimeError(
                f"WhatsApp API sendText failed (status={status}): {detail}"
            )
        return self._extract_message_id(result)

    def send_attachment_sync(
        self,
        contact_id: str,
        file_path: Path,
        *,
        caption: str | None = None,
        mime_type: str,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_message: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> str | None:
        if not self._rest:
            raise RuntimeError("WhatsApp API is not configured")
        send_chat_id = self._resolve_send_chat_id(contact_id)
        result = self._rest.send_image(
            send_chat_id,
            file_path,
            caption=caption,
            reply_to_message_id=reply_to_message_id,
            mime_type=mime_type,
        )
        if result is None:
            status = self._rest.last_status
            detail = self._rest.last_error or "unreachable"
            raise RuntimeError(
                f"WhatsApp API sendImage failed (status={status}): {detail}"
            )
        return self._extract_message_id(result)

    def enqueue_sent_message(
        self,
        contact_id: str,
        message_id: str,
        text: str,
        *,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_message: str | None = None,
        reply_to_message_id: str | None = None,
        attachment_path: Path | None = None,
        mime_type: str | None = None,
    ) -> None:
        is_attachment = attachment_path is not None
        msg_type = (
            "image"
            if is_attachment and (mime_type or "").startswith("image/")
            else "attachment"
            if is_attachment
            else "text"
        )
        media_text = "" if msg_type == "image" else text
        attachment_id = None
        if attachment_path is not None:
            suffix = attachment_path.suffix.lower()
            attachment_id = f"sent-{uuid.uuid4().hex}{suffix}"
            shutil.copy2(attachment_path, self._ensure_media_dir() / attachment_id)
        self._enqueue_event(
            ChatEvent(
                type="message",
                protocol=self.protocol,
                contact_id=contact_id,
                payload={
                    "id": message_id,
                    "text": media_text,
                    "is_mine": True,
                    "sender": "You",
                    "timestamp": int(time.time() * 1000),
                    "quote_text": quote_message,
                    "quote_timestamp": quote_timestamp,
                    "quote_author": quote_author,
                    "reply_to_message_id": reply_to_message_id,
                    "msg_type": msg_type,
                    "attachment_info": text or None if is_attachment else None,
                    "attachment_id": attachment_id,
                    "content_type": mime_type,
                },
            )
        )

    def edit_message_sync(
        self, contact_id: str, message_id: str, new_text: str
    ) -> bool:
        if not self._rest:
            return False
        return self._rest.edit_message(contact_id, message_id, new_text) is not None

    def send_reaction_sync(
        self,
        contact_id: str,
        message_id: str,
        emoji: str,
        *,
        target_author: str | None = None,
    ) -> bool:
        if not self._rest:
            return False
        return (
            self._rest._request(
                "PUT",
                "/api/reaction",
                {
                    "messageId": message_id,
                    "reaction": emoji,
                    "session": self.session_name,
                },
            )
            is not None
        )

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
        if not attachment_id:
            return None

        base = self._ensure_media_dir()
        safe_name = Path(attachment_id).name or attachment_id
        candidate = base / safe_name

        # Fast path: already on disk.
        if candidate.is_file():
            return candidate

        # Lazy download from WAHA.
        if not self._rest:
            return None
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

    def _schedule_media_resolve(self, contact_id: str, msg_id: str) -> None:
        """Schedule one unresolved WAHA media message, deduplicated by identity."""
        if not contact_id or not msg_id or self._media_resolver_stop:
            return
        key = (contact_id, msg_id)
        with self._media_lock:
            if key in self._media_pending:
                return
            timestamp = next(
                (
                    int(message.get("timestamp") or 0)
                    for message in self.cache.get(contact_id, [])
                    if str(message.get("id") or "") == msg_id
                ),
                0,
            )
            self._media_pending[key] = {
                "attempts": 0,
                "next": time.time() + 2.0,
                "ts": timestamp or int(time.time() * 1000),
            }
        self._start_media_resolver()

    def _start_media_resolver(self) -> None:
        """Start the WAHA media resolver thread once."""
        with self._media_lock:
            if self._media_resolver_stop:
                return
            thread = self._media_resolver_thread
            if thread is not None and thread.is_alive():
                return
            thread = threading.Thread(
                target=self._media_resolve_loop,
                name="wa-media-resolver",
                daemon=True,
            )
            self._media_resolver_thread = thread
            thread.start()

    def _media_resolve_loop(self) -> None:
        """Resolve pending media downloads without blocking webhook handling."""
        delays = (2.0, 5.0, 15.0)
        while not self._media_resolver_stop:
            try:
                now = time.time()
                with self._media_lock:
                    due = [
                        key
                        for key, pending in self._media_pending.items()
                        if pending["next"] <= now
                    ]

                for key in due:
                    if self._media_resolver_stop:
                        break
                    with self._media_lock:
                        pending = self._media_pending.get(key)
                        if pending is None or pending["next"] > time.time():
                            continue
                        pending["attempts"] += 1
                        attempts = pending["attempts"]
                        fallback_ts = pending["ts"]

                    contact_id, msg_id = key
                    events: list[ChatEvent] = []
                    try:
                        message = (
                            self._rest.get_message_media(contact_id, msg_id)
                            if self._rest
                            else None
                        )
                        if isinstance(message, dict):
                            normalized = dict(message)
                            normalized.setdefault("chatId", contact_id)
                            normalized.setdefault("id", msg_id)
                            normalized.setdefault("timestamp", fallback_ts)
                            events = _event_from_message(
                                normalized, self._contacts_by_jid
                            )
                    except Exception:
                        logger.debug(
                            "WhatsApp media resolve failed: chat=%s id=%s attempt=%s",
                            contact_id,
                            msg_id,
                            attempts,
                            exc_info=True,
                        )

                    if events:
                        for event in events:
                            self._enqueue_event(event)
                        with self._media_lock:
                            self._media_pending.pop(key, None)
                        continue

                    with self._media_lock:
                        pending = self._media_pending.get(key)
                        if pending is None:
                            continue
                        if attempts < 3:
                            pending["next"] = time.time() + delays[attempts - 1]
                        else:
                            self._media_pending.pop(key, None)
                            logger.debug(
                                "WhatsApp media resolve give up: chat=%s id=%s",
                                contact_id,
                                msg_id,
                            )
            except Exception:
                logger.debug("WhatsApp media resolver loop failed", exc_info=True)
            time.sleep(0.5)

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
        attachment_id: str | None = None,
    ) -> dict | None:
        """Return the cached message matching the same identity, or ``None``.

        For **incoming** messages (``is_mine=False``), dedup uses the exact
        id+text match when available.  Since WAHA ids can differ for the same
        message between webhook and REST API, it falls back to text with a
        fuzzy timestamp (±5s).

        For **outgoing** messages (``is_mine=True``) the id is stable and
        used as the primary identity (optimistic-send echo dedup).
        """
        best_idless: dict | None = None
        best_idless_delta: int | None = None
        for msg in self.cache.get(contact_id, []):
            if msg.get("is_mine") != is_mine:
                continue
            # Outgoing: l'id è l'identità primaria e stabile (echo message.ack con
            # body=caption condivide il msg_id del messaggio media reale, ma text
            # diverso).  Il match per id DEVE precedere quello sul testo, altrimenti
            # l'evento ack sintetico viene ingerito come nuovo messaggio di testo.
            cached_attachment_id = msg.get("attachment_id")
            same_attachment = (
                not attachment_id
                or not cached_attachment_id
                or cached_attachment_id == attachment_id
            )
            if (
                is_mine
                and msg_id
                and msg.get("id")
                and msg.get("id") == msg_id
                and same_attachment
            ):
                return msg
            if msg.get("text") != text:
                continue
            if not is_mine:
                if msg_id and msg.get("id") == msg_id and same_attachment:
                    return msg
                if attachment_id and cached_attachment_id != attachment_id:
                    continue
                # Text + fuzzy timestamp — unica identità stabile webhook/REST.
                if abs(msg.get("timestamp", 0) - ts) <= 5000:
                    return msg
            elif msg_id:
                cached_id = msg.get("id")
                if cached_id and cached_id == msg_id:
                    return msg
                # Fallback echo: SOLO per una riga ancora id-less (optimistic
                # non confermato).  Una riga con GIÀ un id diverso è un
                # messaggio DISTINTO: mai fonderla (bug: 2ª/3ª immagine e
                # "OK"+"OK" ravvicinati collassavano).  Tra più candidate
                # id-less vince la più vicina nel tempo (mirror di
                # ``_update_message_id``: ORDER BY ABS(timestamp-?) LIMIT 1).
                if not cached_id and same_attachment:
                    delta = abs(msg.get("timestamp", 0) - ts)
                    if delta <= _ECHO_MATCH_WINDOW_MS and (
                        best_idless_delta is None or delta < best_idless_delta
                    ):
                        best_idless = msg
                        best_idless_delta = delta
            elif abs(msg.get("timestamp", 0) - ts) <= _SEND_DEDUP_WINDOW_MS:
                return msg
        return best_idless

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
            quote_attachment_id=data.get("quote_attachment_id"),
            quote_attachment_path=data.get("quote_attachment_path"),
            quote_content_type=data.get("quote_content_type"),
        )

    def _upgrade_outgoing_attachment(
        self, contact_id: str, message: dict, data: dict, ts: int
    ) -> bool:
        from backend import (
            _update_message_attachment_id,
            _update_message_media_identity,
        )

        current_id = message.get("attachment_id")
        incoming_id = data.get("attachment_id")
        if not incoming_id:
            return False
        # Media race: la riga è stata creata senza attachment (webhook WAHA
        # partito prima del download → media=null, o riga text vuota).  Echo o
        # fetch_history successivi portano il media REALE (URL WAHA o sent-*):
        # aggiorna in place attachment_id + msg_type così la bolla compare
        # senza duplicati (il download del file resta lazy in get_attachment_path).
        if not current_id:
            message["attachment_id"] = incoming_id
            message["msg_type"] = data.get("msg_type", message.get("msg_type", "text"))
            _update_message_media_identity(
                PROTOCOL_WHATSAPP,
                contact_id,
                message.get("id") or data.get("id"),
                int(message.get("timestamp", ts)),
                incoming_id,
                message["msg_type"],
            )
            return True
        media_dir = self._ensure_media_dir()
        current_path = media_dir / Path(current_id).name if current_id else None
        incoming_path = media_dir / Path(incoming_id).name if incoming_id else None
        if (
            not incoming_id
            or not Path(incoming_id).name.startswith("sent-")
            or not should_upgrade_outgoing_attachment(
                is_mine=bool(data.get("is_mine")),
                existing_path=current_path,
                incoming_path=incoming_path,
            )
        ):
            return False
        message["attachment_id"] = incoming_id
        _update_message_attachment_id(
            PROTOCOL_WHATSAPP,
            contact_id,
            message.get("id") or data.get("id"),
            int(message.get("timestamp", ts)),
            incoming_id,
        )
        return True

    def _reuse_failed_db_row(
        self, contact_id: str, data: dict, text: str, msg_id: str, ts: int
    ) -> bool:
        """Reuse the single failed id-less DB row for the echo of a retried send.

        Scenario concreto (bug #44): il retry di un messaggio ``failed`` può
        lasciare in SQLite una riga id-less (``msg_id IS NULL``/``''``) con
        ``status='failed'`` che NON è specchiata in ``self.cache`` (cache
        ri-seminata/sfoltita senza quella entry).  Quando l'echo del retry
        arriva con l'id reale, ``_message_already_cached`` non la trova e —
        senza questo fallback — verrebbe inserita una NUOVA riga, lasciando
        cache e DB divergenti (split-brain).  Qui riusiamo la riga ``failed``
        unica entro la finestra echo, agganciando l'id SOLO a quella riga
        esatta (UPDATE scoped per ``id``) e avanzandone lo status a quello
        dell'echo (default ``sent``: l'echo è la prova che il messaggio è
        partito, quindi non resta ``failed``).

        Guardia di unicità (pattern del fallback id-less del #39): procediamo
        SOLO con esattamente un candidato; con 0 o 2+ candidati non facciamo
        nulla e il chiamante segue il normale percorso di insert.

        Returns ``True`` when a row was reused, ``False`` otherwise.
        """
        from backend import _DB_LOCK, _ECHO_MATCH_WINDOW_MS, DB_FILE, _init_db

        _init_db()
        status = data.get("status", "sent" if data.get("is_mine") else "read")
        with _DB_LOCK:
            conn = sqlite3.connect(DB_FILE)
            try:
                rows = conn.execute(
                    "SELECT id, msg_id, timestamp FROM messages "
                    "WHERE protocol = ? AND contact_number = ? AND is_mine = 1 "
                    "AND text = ? AND (msg_id IS NULL OR msg_id = '') "
                    "AND status = 'failed' AND ABS(timestamp - ?) <= ? "
                    "ORDER BY ABS(timestamp - ?) ASC, rowid ASC",
                    (
                        PROTOCOL_WHATSAPP,
                        contact_id,
                        text,
                        ts,
                        _ECHO_MATCH_WINDOW_MS,
                        ts,
                    ),
                ).fetchall()

                if len(rows) != 1:
                    return False

                row_id, existing_msg_id, existing_ts = rows[0]
                # Aggiorna il timestamp solo se il msg_id era NULL (l'echo
                # sostituisce il ts ottimistico); se era '' (legacy) resta.
                new_ts = ts if existing_msg_id is None else existing_ts
                cursor = conn.execute(
                    "UPDATE messages SET msg_id = ?, timestamp = ?, status = ? "
                    "WHERE id = ?",
                    (msg_id, new_ts, status, row_id),
                )
                conn.commit()
                if cursor.rowcount == 0:
                    return False
            finally:
                conn.close()

        # Specchia la riga riusata in self.cache così cache e DB non divergono
        # mai (stesso id/testo/ts/status del DB).
        self._add_cached_message(
            contact_id,
            {
                "id": msg_id,
                "text": text,
                "is_mine": True,
                "sender": data.get("sender", ""),
                "timestamp": new_ts,
                "quote_text": data.get("quote_text"),
                "msg_type": data.get("msg_type", "text"),
                "attachment_info": data.get("attachment_info"),
                "attachment_id": data.get("attachment_id"),
                "read": True,
                "status": status,
                "quote_timestamp": data.get("quote_timestamp"),
                "quote_author": data.get("quote_author"),
                "reply_to_message_id": data.get("reply_to_message_id"),
            },
        )
        return True

    def ingest_message(
        self, contact_id: str, data: dict, ts: int, persist: bool = True
    ) -> bool | str:
        """Save an incoming/outgoing message to the DB cache and in-memory cache.

        When ``persist=False`` the in-memory cache is still seeded (dedup
        keeps working on the UI thread) but the SQLite write is skipped;
        the caller is responsible for calling ``_persist_message`` later.

        Returns ``True`` if newly added, ``"changed"`` if healed in place,
        and ``False`` if it was an unchanged duplicate.
        """
        from backend import _fill_message_quote_fields, _update_message_id

        if data.get("msg_type") == "image":
            data = {**data, "text": ""}
        text = data["text"]
        is_mine = data["is_mine"]
        msg_id = data.get("id")

        existing = self._message_already_cached(
            contact_id, ts, is_mine, text, msg_id, data.get("attachment_id")
        )
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
            quote_fields = (
                "quote_text",
                "quote_timestamp",
                "quote_author",
                "reply_to_message_id",
            )
            changed = False
            for field in quote_fields:
                value = data.get(field)
                if existing.get(field) in (None, "") and value not in (None, ""):
                    existing[field] = value
                    changed = True
            if changed:
                _fill_message_quote_fields(
                    PROTOCOL_WHATSAPP,
                    contact_id,
                    existing.get("id") or msg_id,
                    int(existing.get("timestamp", ts)),
                    quote_text=data.get("quote_text"),
                    quote_timestamp=data.get("quote_timestamp"),
                    quote_author=data.get("quote_author"),
                    reply_to_message_id=data.get("reply_to_message_id"),
                )
            attachment_changed = self._upgrade_outgoing_attachment(
                contact_id, existing, data, ts
            )
            return "changed" if attachment_changed else False

        # Fallback DB post-send (bug #44): un messaggio failed può avere la sua
        # riga id-less nel DB ma NON in self.cache.  Prima di inserire una nuova
        # riga, riusiamo quella riga (se unica entro la finestra echo) invece di
        # creare un duplicato.  Attivato solo per l'echo outgoing (is_mine=True)
        # con id reale: l'ingest ottimistico non ha ancora id e gli ingressi
        # generici incoming hanno is_mine=False, quindi nessun parametro extra.
        if (
            is_mine
            and msg_id
            and self._reuse_failed_db_row(contact_id, data, text, msg_id, ts)
        ):
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
                "quote_attachment_id": data.get("quote_attachment_id"),
                "quote_attachment_path": data.get("quote_attachment_path"),
                "quote_content_type": data.get("quote_content_type"),
            },
        )
        return True

    def _detect_edit(
        self,
        contact_id: str,
        msg_id: str | None,
        text: str,
        is_mine: bool,
        ts_ms: int,
    ) -> dict | None:
        """Ritorna l'entry cached target di un edit, o None.

        1) Match per id (stabile per outgoing; per incoming stabile tra webhook
           e webhook, che è il caso dell'edit live).
        2) Fallback incoming per ts (±2s, candidato UNICO): gli id incoming
           possono differire tra webhook e REST (/api/messages), quindi un edit
           di un messaggio caricato via fetch_history potrebbe non matchare
           per id.  Il timestamp WhatsApp dell'edit è quello ORIGINALE.
        """
        if not text:
            return None
        entries = self.cache.get(contact_id, [])
        if msg_id:
            for msg in entries:
                if bool(msg.get("is_mine")) != bool(is_mine):
                    continue
                if msg.get("msg_type", "text") != "text":
                    continue
                if str(msg.get("id") or "") == str(msg_id):
                    return msg if msg.get("text", "") != text else None
        if not is_mine and ts_ms:
            candidates = [
                m
                for m in entries
                if not m.get("is_mine")
                and m.get("msg_type", "text") == "text"
                and abs(int(m.get("timestamp") or 0) - ts_ms) <= 2000
                and m.get("text", "") != text
            ]
            if len(candidates) == 1:  # ambiguità → skip (vedi §9)
                return candidates[0]
        return None

    def apply_edit(
        self, contact_id, message_id, new_text, *, is_mine=None, edit_timestamp=None
    ) -> dict | None:
        from backend import _update_message_text

        target = None
        for msg in self.cache.get(contact_id, []):
            if str(msg.get("id") or "") == str(message_id):
                if is_mine is not None and bool(msg.get("is_mine")) != bool(is_mine):
                    continue
                target = msg
                break
        if target is None or target.get("msg_type", "text") != "text":
            return None
        old_text = target.get("text", "")
        if old_text == new_text:
            return None  # echo nostro edit: no-op
        target["text"] = new_text
        target["edited"] = True
        # niente _sort_contact_cache: il timestamp non cambia
        if target.get("id"):
            _update_message_text(
                contact_id,
                new_text,
                protocol=PROTOCOL_WHATSAPP,
                msg_id=str(target["id"]),
            )
        else:
            _update_message_text(
                contact_id,
                new_text,
                protocol=PROTOCOL_WHATSAPP,
                timestamp=int(target.get("timestamp") or 0),
                old_text=old_text,
            )
        return {
            "message_id": str(target.get("id") or message_id),
            "timestamp": int(target.get("timestamp") or 0),
            "old_text": old_text,
            "text": new_text,
            "is_mine": bool(target.get("is_mine")),
        }

    def apply_reaction(self, contact_id: str, payload: dict) -> dict | None:
        """Apply a WAHA reaction delta and aggregate it for a known target."""
        if payload.get("mode") != "delta":
            return None
        target_message_id = payload.get("target_message_id")
        if target_message_id is None or not payload.get("author_key"):
            return None

        from backend import (
            _apply_reaction_delta,
            _load_cache,
            _reactions_for_contact,
            _resolve_reaction_target_row,
        )

        requested_id = str(target_message_id)
        target = next(
            (
                message
                for message in self.cache.get(contact_id, [])
                if str(message.get("id") or "") == requested_id
            ),
            None,
        )
        if target is None:
            requested_canonical = canonical_msg_id(requested_id)
            if requested_canonical:
                target = next(
                    (
                        message
                        for message in self.cache.get(contact_id, [])
                        if canonical_msg_id(str(message.get("id") or ""))
                        == requested_canonical
                    ),
                    None,
                )

        row = _resolve_reaction_target_row(
            PROTOCOL_WHATSAPP,
            contact_id,
            str(target.get("id"))
            if target is not None and target.get("id")
            else requested_id,
            payload.get("target_timestamp"),
        )
        if row is None and target is None:
            requested_canonical = canonical_msg_id(requested_id)
            persisted = _load_cache(protocol=PROTOCOL_WHATSAPP).get(contact_id, [])
            canonical_target = next(
                (
                    message
                    for message in persisted
                    if requested_canonical
                    and canonical_msg_id(str(message.get("id") or ""))
                    == requested_canonical
                ),
                None,
            )
            if canonical_target is not None:
                row = _resolve_reaction_target_row(
                    PROTOCOL_WHATSAPP,
                    contact_id,
                    str(canonical_target.get("id")),
                    payload.get("target_timestamp"),
                )
        if row is not None:
            target = row
        target_resolved = target is not None
        resolved_id = (
            str(target.get("msg_id") or target.get("id") or requested_id)
            if target_resolved
            else requested_id
        )
        target_ts = int(
            (target.get("timestamp") if target_resolved else None)
            or payload.get("target_timestamp")
            or 0
        )
        changed = _apply_reaction_delta(
            PROTOCOL_WHATSAPP,
            contact_id,
            resolved_id,
            target_ts or None,
            str(payload.get("emoji") or ""),
            str(payload["author_key"]),
            str(payload.get("author") or ""),
            bool(payload.get("is_mine")),
            bool(payload.get("is_remove")),
            int(payload.get("timestamp") or 0),
        )
        if not changed or not target_resolved:
            return None

        grouped: dict[str, dict] = {}
        for reaction in _reactions_for_contact(PROTOCOL_WHATSAPP, contact_id):
            if str(reaction.get("target_msg_id") or "") != resolved_id and reaction.get(
                "target_timestamp"
            ) != (target_ts or None):
                continue
            emoji = str(reaction.get("emoji") or "")
            aggregate = grouped.setdefault(
                emoji,
                {
                    "emoji": emoji,
                    "count": 0,
                    "is_mine": False,
                    "authors": set(),
                    "timestamp": int(reaction.get("timestamp") or 0),
                },
            )
            aggregate["count"] += int(reaction.get("count") or 0)
            aggregate["is_mine"] = aggregate["is_mine"] or bool(reaction.get("is_mine"))
            author = str(reaction.get("author") or "").strip()
            if author:
                aggregate["authors"].add(author)
            aggregate["timestamp"] = min(
                aggregate["timestamp"], int(reaction.get("timestamp") or 0)
            )

        reactions = sorted(
            grouped.values(), key=lambda item: (-item["count"], item["timestamp"])
        )
        for reaction in reactions:
            reaction["authors"] = sorted(reaction["authors"])
            reaction.pop("timestamp")
        return {
            "message_id": resolved_id,
            "timestamp": target_ts,
            "reactions": reactions,
        }

    def process_receipt(self, envelope: dict) -> list[dict]:
        """Handle a receipt batch against the in-memory cache.

        Updates ``status`` for sent messages matching the reported ids, with a
        rank guard (pending/failed < sent < delivered < read), persists the
        change (by ``msg_id`` when known, otherwise by timestamp/text for the
        optimistic id-less fallback) so it survives restarts, and returns the
        updated entries (id/timestamp/status/text/is_mine) for the UI to
        mirror and refresh the widgets.

        Matching is done on the **canonical** id (see ``canonical_msg_id``):
        WAHA stores ``true_{jid}_{hex}[_{participant}]`` in the DB/cache while
        the webhook receipt carries only ``{hex}``, so a raw string comparison
        would never match.  When no id matches at all, a conservative fallback
        upgrades the single id-less ``is_mine``/``sent`` optimistic entry of
        the chat (receipt raced ahead of the send id-upgrade).
        """
        ids = envelope.get("message_ids") or []
        if not ids:
            return []
        is_read = bool(envelope.get("is_read"))
        target = "read" if is_read else "delivered"
        rank = {
            "pending": 0,
            "failed": 0,
            "sent": 1,
            "delivered": 2,
            "read": 3,
        }
        target_rank = rank.get(target, 0)
        raw_ids = [str(i) for i in ids]
        canonical_ids = {c for c in (canonical_msg_id(i) for i in raw_ids) if c}
        scoped_contact = envelope.get("contact_id")

        updated: list[dict] = []
        # (msg_id_or_None, timestamp, text, contact_id) for SQLite persistence.
        to_persist: list[tuple[str | None, int, str, str]] = []
        matched_any = False

        contacts_to_scan = [scoped_contact] if scoped_contact else list(self.cache)
        for contact_id in contacts_to_scan:
            for msg in self.cache.get(contact_id, []):
                if not msg.get("is_mine"):
                    continue
                mid = str(msg.get("id", ""))
                if not mid:
                    continue
                if canonical_msg_id(mid) not in canonical_ids:
                    continue
                matched_any = True
                old = msg.get("status", "sent")
                if target_rank > rank.get(old, 0):
                    msg["status"] = target
                    updated.append(
                        {
                            "id": mid,
                            "timestamp": msg.get("timestamp", 0),
                            "status": target,
                            "text": msg.get("text", ""),
                            "is_mine": True,
                        }
                    )
                    to_persist.append(
                        (
                            mid,
                            int(msg.get("timestamp") or 0),
                            str(msg.get("text", "")),
                            contact_id,
                        )
                    )

        # ── Fallback: no id matched at all ──────────────────────────────
        # The receipt can beat the send path's id-upgrade: the optimistic entry
        # still has an empty id.  Only a UNIQUE id-less ``is_mine`` entry in
        # ``sent`` state is eligible (prevents false positives when the chat has
        # several pending bubbles).
        if not matched_any:
            candidates: list[tuple[str, dict]] = []
            for contact_id in contacts_to_scan:
                for msg in self.cache.get(contact_id, []):
                    if (
                        msg.get("is_mine")
                        and not msg.get("id")
                        and msg.get("status", "sent") == "sent"
                    ):
                        candidates.append((contact_id, msg))
            if len(candidates) == 1:
                contact_id, msg = candidates[0]
                msg["status"] = target
                updated.append(
                    {
                        "id": "",
                        "timestamp": msg.get("timestamp", 0),
                        "status": target,
                        "text": msg.get("text", ""),
                        "is_mine": True,
                    }
                )
                to_persist.append(
                    (
                        None,
                        int(msg.get("timestamp") or 0),
                        str(msg.get("text", "")),
                        contact_id,
                    )
                )

        # Diagnostic: nothing matched (neither by canonical id nor by the
        # id-less uniqueness fallback).  Emit enough context to confirm the
        # mismatched shapes on production without leaking payload content.
        # ``matched_any`` guards against noisy warnings on rank-guard no-ops
        # (duplicated/out-of-order receipts, fetch_history reconciliation of
        # messages already read), where the receipt WAS found but no state
        # change was needed.
        if not matched_any and not updated:
            with_id = 0
            empty_id = 0
            for contact_id in contacts_to_scan:
                for msg in self.cache.get(contact_id, []):
                    if not msg.get("is_mine"):
                        continue
                    if msg.get("id"):
                        with_id += 1
                    else:
                        empty_id += 1
            logger.warning(
                "WhatsApp receipt matched no is_mine entry "
                "(contact_id=%r, raw_message_ids=%r, canonical_message_ids=%r, "
                "is_mine_with_id=%d, is_mine_empty_id=%d)",
                scoped_contact,
                raw_ids,
                sorted(canonical_ids),
                with_id,
                empty_id,
            )

        # Persist status changes to SQLite so they survive restarts.
        if to_persist:
            from backend import _update_message_status, _update_message_status_by_id

            for msg_id, ts, text, contact_id in to_persist:
                if msg_id:
                    _update_message_status_by_id(
                        msg_id,
                        target,
                        protocol=PROTOCOL_WHATSAPP,
                        contact_number=contact_id,
                    )
                else:
                    _update_message_status(
                        ts,
                        target,
                        PROTOCOL_WHATSAPP,
                        contact_id,
                        text=text,
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
