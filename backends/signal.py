"""
Signal backend — a ``ChatBackend`` implementation for signal-cli.

This wraps the existing signal-cli handling in ``backend.py`` (JSON-RPC over
HTTP daemon, subprocess fallback, SQLite cache) and exposes it through the
neutral ``ChatBackend`` interface.  Envelope parsing that used to live in the
TUI is gathered here so the UI only deals with normalized ``ChatContact`` /
``ChatEvent`` objects.
"""

from __future__ import annotations

import logging
import asyncio
import queue
import re
import subprocess
import threading
import time

from models import (
    ChatContact,
    ChatEvent,
    PROTOCOL_SIGNAL,
)

from .base import ChatBackend

logger = logging.getLogger(__name__)
_fh = logging.FileHandler("/tmp/signal-sse.log", mode="w")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_fh)
logger.setLevel(logging.DEBUG)

from backend import (
    Contact,
    SignalRPCClient,
    _add_message_to_cache,
    _load_cache,
    _prune_cache,
    _mark_as_read,
    _update_message_status,
    _process_receipt,
    _process_typing,
    _is_daemon_running,
    _run_subprocess,
    _send_subprocess,
    get_attachment_path,
    SIGNAL_CLI_PATH,
    USER_NUMBER,
    DAEMON_HTTP_PORT,
)


# Window (ms) within which an outgoing message echo is considered the same
# logical message as the optimistic send, for de-duplication purposes.
_SEND_DEDUP_WINDOW_MS = 5000

# Window (ms) within which an incoming message with the same (text, contact)
# is considered a duplicate even if the timestamp differs slightly.
# Prevents double-counting when signal-cli re-delivers the same envelope
# (e.g. sync from another device) with a slightly different timestamp.
_INCOMING_DEDUP_WINDOW_MS = 2000

_RE_CONTACT_LINE = re.compile(
    r"Number:(?P<number>\S+)\s+"
    r"Name:(?P<name>.+?)"
    r"(?:\s+ACI:(?P<aci>\S+))?"
    r"(?:\s+Profile name:.*)?"
    r"$"
)


class SignalBackend(ChatBackend):
    """signal-cli backend adapted to the ``ChatBackend`` interface."""

    protocol = PROTOCOL_SIGNAL

    def __init__(self, user_number: str = USER_NUMBER):
        self.user_number = user_number
        self._rpc = SignalRPCClient()
        self._use_daemon = False
        self.daemon_proc: subprocess.Popen | None = None
        self._polling_active = False

        # SSE real-time delivery
        self._event_queue: queue.Queue[ChatEvent] = queue.Queue()
        self._sse_thread: threading.Thread | None = None

        # Normalized contact list
        self.contacts: list[ChatContact] = []
        self._contacts_by_key: dict[str, ChatContact] = {}

        # Protocol-aware message cache: key = contact_cache_key(protocol, id)
        self.cache: dict[str, list[dict]] = {}

    # ─── Lifecycle ────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Prune old cache, load history, start daemon and load contacts."""
        await asyncio.to_thread(self._connect_sync)

    def _connect_sync(self) -> None:
        self.cache = self._load_protocol_cache()

        if _is_daemon_running():
            self._use_daemon = True
            self._load_contacts_rpc()
        else:
            self.daemon_proc = subprocess.Popen(
                [
                    str(SIGNAL_CLI_PATH),
                    "-u", self.user_number,
                    "daemon",
                    "--http", f"127.0.0.1:{DAEMON_HTTP_PORT}",
                    "--receive-mode", "on-connection",
                    "--no-receive-stdout",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            # Start SSE listener immediately — the daemon's HTTP server
            # comes up before it's fully initialized.  The SSE listener
            # has built-in retry logic: it reconnects every 5s on failure.
            # This way we connect as soon as the HTTP server is ready,
            # capturing pending messages that --receive-mode on-start
            # downloads in the first few seconds of daemon startup.
            self._start_sse_listener()

            for _ in range(15):
                try:
                    test = self._rpc._call("listContacts")
                    if "result" in test:
                        self._use_daemon = True
                        break
                except Exception:
                    pass
                time.sleep(1)
            else:
                # Daemon not available in time → use subprocess fallback.
                self._use_daemon = False
                self._load_contacts_subprocess()

            if self._use_daemon:
                self._load_contacts_rpc()

        # Start real-time SSE listener if daemon is available.
        # For fresh starts it was already started above (as soon as
        # the daemon responded).  For already-running daemons, start
        # it here.  _start_sse_listener is idempotent.
        if self._use_daemon:
            self._start_sse_listener()
            # Request the Signal server to re-send any pending messages.
            # With SSE already connected, they will arrive via the normal
            # pipeline.  Best-effort, never blocks startup.
            try:
                result = self._rpc._call("sendSyncRequest")
                logger.info("SYNC-REQUEST: result=%s", 
                           "ok" if isinstance(result, dict) and "result" in result else str(result)[:100])
            except Exception as e:
                logger.info("SYNC-REQUEST: exception=%s", e)

    async def disconnect(self) -> None:
        """Stop the SSE listener and polling.  The daemon itself is left running by design."""
        self._polling_active = False
        # Signal the SSE thread to stop and wait for it
        sse_thread = self._sse_thread
        self._sse_thread = None
        if sse_thread is not None and sse_thread.is_alive():
            # Wake up the blocking urlopen by closing is not possible,
            # but the thread checks _polling_active; the timeout (30 s)
            # ensures it wakes up and exits within that window.
            sse_thread.join(timeout=5)

    # ─── Contact loading ──────────────────────────────────────────────

    @staticmethod
    def _to_chat_contact(c: Contact) -> ChatContact:
        """Convert a legacy ``Contact`` into a neutral ``ChatContact``."""
        return ChatContact(
            id=c.number,
            display_name=c.display_name,
            protocol=PROTOCOL_SIGNAL,
            extras={"aci": c.aci, "number": c.number},
        )

    def _load_contacts_rpc(self) -> None:
        contacts_data = self._rpc.list_contacts()
        if isinstance(contacts_data, list) and len(contacts_data) > 0:
            self._parse_and_update_contacts(contacts_data)
        else:
            self._load_contacts_subprocess()

    def _load_contacts_subprocess(self) -> None:
        try:
            output = _run_subprocess(["listContacts"])
            contacts = self._parse_contacts_from_output(output)
            self._set_contacts(contacts)
        except Exception:
            # Swallow — the UI reports errors, not the backend.
            pass

    def _parse_contacts_from_output(self, output: str) -> list[ChatContact]:
        """Parse the output of ``signal-cli listContacts`` (subprocess fallback).

        Uses a regex instead of ``line.split()`` to correctly handle names
        that contain spaces (e.g. ``Mario Rossi``).
        """
        legacy = []
        for line in output.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            m = _RE_CONTACT_LINE.match(line)
            if m:
                number = m.group("number")
                name = m.group("name").strip()
                aci = m.group("aci") or ""
                legacy.append(Contact(number=number, name=name, aci=aci))
        return [self._to_chat_contact(c) for c in legacy]

    def _parse_and_update_contacts(self, contacts_data: list[dict]) -> None:
        contacts = []
        for c in contacts_data:
            number = c.get("number") or c.get("uuid", "") or ""
            name = (
                c.get("name")
                or c.get("givenName")
                or (c.get("profile") or {}).get("givenName")
                or number
            )
            aci = c.get("uuid", "") or c.get("aci", "")
            contacts.append(Contact(number=number, name=name, aci=aci))
        self._set_contacts([self._to_chat_contact(c) for c in contacts])

    def _set_contacts(self, contacts: list[ChatContact]) -> None:
        # Filtra contatti di sistema (es. "status@broadcast" per le Signal
        # Stories) che non sono utenti reali.
        contacts = [c for c in contacts if c.id and "@broadcast" not in c.id]
        # Recupera per ogni contatto il timestamp dell'ultimo messaggio dalla
        # cache SQLite locale (costo ~0, offline): così l'ordinamento "ultimi
        # messaggi in alto" funziona già all'avvio senza fetch di rete.
        for c in contacts:
            msgs = self.cache.get(c.id) or []
            ts = 0
            for m in msgs:
                mts = m.get("timestamp") or 0
                if mts > ts:
                    ts = mts
            c.last_message_ts = ts
        self.contacts = contacts
        self._contacts_by_key = {c.cache_key: c for c in contacts}

    async def list_contacts(self) -> list[ChatContact]:
        return list(self.contacts)
    # ─── Cache ────────────────────────────────────────────────────────
    # NOTE: ``self.cache`` is keyed by the *raw* contact id (e.g. the phone
    # number) so it is compatible with ``backend._process_receipt`` and
    # ``backend._process_typing`` which look up by the raw id.  The UI keeps
    # its own copy keyed by ``contact_cache_key`` (protocol-aware).

    def _load_protocol_cache(self) -> dict[str, list[dict]]:
        """Load cache keyed by raw contact id (phone number)."""
        return _load_cache()

    def _add_cached_message(self, contact_id: str, msg: dict) -> None:
        if contact_id not in self.cache:
            self.cache[contact_id] = []
        self.cache[contact_id].append(msg)

    # ─── Sending / reading ────────────────────────────────────────────

    async def send_message(
        self,
        contact_id: str,
        text: str,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_message: str | None = None,
    ) -> str:
        """Send *text* to *contact_id*; returns the client timestamp (ms)."""
        return await asyncio.to_thread(
            self._send_message_sync,
            contact_id, text, quote_timestamp, quote_author, quote_message,
        )

    def _send_message_sync(
        self,
        contact_id: str,
        text: str,
        quote_timestamp: int | None,
        quote_author: str | None,
        quote_message: str | None,
    ) -> str:
        ts = int(time.time() * 1000)
        if self._use_daemon and self._rpc:
            result = self._rpc.send_message(
                text,
                contact_id,
                timestamp=ts,
                quote_timestamp=quote_timestamp,
                quote_author=quote_author,
                quote_message=quote_message,
            )
            if "error" in result:
                raise RuntimeError(result["error"])
        else:
            _send_subprocess(
                text,
                contact_id,
                quote_timestamp=quote_timestamp,
                quote_author=quote_author,
                quote_message=quote_message,
            )
        return ts

    def send_message_sync(
        self,
        contact_id: str,
        text: str,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_message: str | None = None,
    ) -> str:
        """Synchronous send, for use from the TUI's sync worker threads.

        Wraps ``_send_message_sync`` and returns the client timestamp (ms).
        """
        return self._send_message_sync(
            contact_id,
            text,
            quote_timestamp=quote_timestamp,
            quote_author=quote_author,
            quote_message=quote_message,
        )

    async def mark_read(self, contact_id: str) -> None:
        await asyncio.to_thread(_mark_as_read, contact_id)

    def mark_read_sync(self, contact_id: str) -> None:
        """Synchronous mark-read, for use from the TUI's sync callbacks."""
        _mark_as_read(contact_id)

    def get_attachment_path(self, attachment_id: str):
        return get_attachment_path(attachment_id)

    # ─── Envelope parsing → normalized events ─────────────────────────

    def _identify_contact_for_envelope(self, envelope: dict) -> ChatContact | None:
        """Identify which contact an envelope belongs to.

        For outgoing (syncMessage.sentMessage) envelopes the target is the
        *destination*, not the source.  If no contact matches the destination
        fields we return ``None`` rather than falling through to the source
        search — a sent envelope's ``source`` is the local user, not a real
        contact.
        """
        sync = envelope.get("syncMessage", {})
        sent = sync.get("sentMessage", {})
        if sent:
            dest = sent.get("destination", "")
            dest_number = sent.get("destinationNumber", "")
            dest_uuid = sent.get("destinationUuid", "")
            for contact in self.contacts:
                if dest == contact.id or dest_number == contact.id:
                    return contact
                aci = contact.extras.get("aci")
                if dest_uuid and aci and dest_uuid == aci:
                    return contact
            return None

        source = envelope.get("source", "")
        source_number = envelope.get("sourceNumber", "")
        source_uuid = envelope.get("sourceUuid", "")
        for contact in self.contacts:
            if source == contact.id or source_number == contact.id:
                return contact
            aci = contact.extras.get("aci")
            if source_uuid and aci and source_uuid == aci:
                return contact

        return None

    def _extract_message_data(self, envelope: dict) -> list[dict]:
        """Extract normalized message data from a Signal envelope.

        Returns a **list** of message dicts.  When *envelope* carries N
        attachments, N dicts are returned (one per attachment).  The
        message body is attached to the first dict only to avoid
        duplication.  A pure text message produces a single-element list.
        """
        source_name = envelope.get("sourceName", "")
        source_number = envelope.get("sourceNumber", "") or envelope.get("source", "")

        def _classify_attachments(attachments: list) -> list[tuple[str, str, str | None]]:
            """Classify every attachment in *attachments*, returning one
            ``(msg_type, info, att_id)`` tuple for each element."""
            result: list[tuple[str, str, str | None]] = []
            for att in attachments:
                content_type = att.get("contentType", "") or ""
                fname = att.get("filename", "") or ""
                caption = att.get("caption", "") or ""
                att_id = att.get("id") or att.get("attachmentId") or None
                if content_type.startswith("image/"):
                    info = caption or (f"Image: {fname}" if fname else "🖼️ Image")
                    result.append(("image", info, att_id))
                elif content_type.startswith("video/"):
                    info = caption or (f"Video: {fname}" if fname else "🎬 Video")
                    result.append(("attachment", info, att_id))
                elif content_type.startswith("audio/"):
                    info = caption or (f"Audio: {fname}" if fname else "🎵 Audio")
                    result.append(("attachment", info, att_id))
                else:
                    info = caption or fname or content_type or "📎 File"
                    result.append(("attachment", info, att_id))
            return result

        def _extract_sticker(sticker: dict | None) -> tuple[str, str] | None:
            if not sticker:
                return None
            pack_id = sticker.get("packId", "")
            sticker_id = sticker.get("stickerId", "")
            if pack_id:
                return ("sticker", f"Sticker #{sticker_id} (pack:{pack_id[:8]}…)")
            return ("sticker", f"Sticker #{sticker_id}")

        def _build_msg_dicts(sender: str, text: str, is_mine: bool,
                             quote_text: str | None, attachments: list) -> list[dict]:
            """Build one dict per classified attachment, or a single text dict."""
            classified = _classify_attachments(attachments)
            if classified:
                msgs: list[dict] = []
                for i, (msg_type, att_info, att_id) in enumerate(classified):
                    if i == 0 and text:
                        msg_text = text
                    else:
                        label = att_info or "Media"
                        # Suffisso con attachment_id per garantire unicita'
                        # del testo: senza, attachment multipli dello stesso
                        # tipo condividerebbero lo stesso text e il dedup
                        # di ingest_message li tratterebbe come duplicati.
                        if att_id:
                            fname = str(att_id)
                            msg_text = f"{label}: {fname}"
                        else:
                            msg_text = label
                    msgs.append({
                        "sender": sender, "text": msg_text, "is_mine": is_mine,
                        "quote_text": quote_text, "msg_type": msg_type,
                        "attachment_info": att_info, "attachment_id": att_id,
                    })
                return msgs
            # No attachments: pure text message.
            return [{
                "sender": sender, "text": text, "is_mine": is_mine,
                "quote_text": quote_text, "msg_type": "text",
                "attachment_info": None, "attachment_id": None,
            }]

        data_msg = envelope.get("dataMessage", {})
        if data_msg:
            text = data_msg.get("message", "") or ""
            sender = source_name or source_number
            quote = data_msg.get("quote", {})
            quote_text = quote.get("text", "") if quote else None

            sticker_data = _extract_sticker(data_msg.get("sticker"))
            if sticker_data:
                msg_type, att_info = sticker_data
                if not text:
                    text = att_info or "🎨 Sticker"
                return [{
                    "sender": sender, "text": text, "is_mine": False,
                    "quote_text": quote_text, "msg_type": msg_type,
                    "attachment_info": att_info,
                }]

            return _build_msg_dicts(
                sender, text, is_mine=False,
                quote_text=quote_text,
                attachments=data_msg.get("attachments", []),
            )

        sync = envelope.get("syncMessage", {})
        sent = sync.get("sentMessage", {})
        if sent:
            text = sent.get("message", "") or ""
            sender = "You"
            quote = sent.get("quote", {})
            quote_text = quote.get("text", "") if quote else None

            sticker_data = _extract_sticker(sent.get("sticker"))
            if sticker_data:
                msg_type, att_info = sticker_data
                if not text:
                    text = att_info or "🎨 Sticker"
                return [{
                    "sender": sender, "text": text, "is_mine": True,
                    "quote_text": quote_text, "msg_type": msg_type,
                    "attachment_info": att_info,
                }]

            return _build_msg_dicts(
                sender, text, is_mine=True,
                quote_text=quote_text,
                attachments=sent.get("attachments", []),
            )

        return []

    def _get_message_timestamp(self, envelope: dict) -> int:
        ts = envelope.get("timestamp", 0)
        if not ts:
            data = envelope.get("dataMessage", {})
            ts = data.get("timestamp", 0)
        if not ts:
            sync = envelope.get("syncMessage", {})
            ts = (sync.get("sentMessage", {}) or {}).get("timestamp", 0)
        return ts

    def envelope_to_event(self, envelope: dict) -> list[ChatEvent]:
        """Classify a Signal envelope into zero or more ``ChatEvent`` objects.

        Returns an empty list for envelopes that carry no user-visible data
        (e.g. unknown contact, empty message).  An envelope with N
        attachments produces N events (one per attachment).
        """
        # Typing indicator
        typing = _process_typing(envelope)
        if typing is not None:
            source, action = typing
            return [ChatEvent(
                type="typing", protocol=self.protocol,
                contact_id=source, payload={"action": action},
            )]

        # Receipt message
        if "receiptMessage" in envelope:
            receipt = envelope.get("receiptMessage", {})
            source = envelope.get("sourceNumber", "") or envelope.get("source", "")
            return [ChatEvent(
                type="receipt", protocol=self.protocol,
                contact_id=source,
                payload={"receipt": receipt},
            )]

        # Real message
        contact = self._identify_contact_for_envelope(envelope)
        if contact is None:
            return []
        data_list = self._extract_message_data(envelope)
        if not data_list:
            return []
        ts = self._get_message_timestamp(envelope)
        events: list[ChatEvent] = []
        for data in data_list:
            events.append(ChatEvent(
                type="message", protocol=self.protocol,
                contact_id=contact.id,
                payload={**data, "timestamp": ts, "contact": contact},
            ))
        return events

    # ─── Incoming message ingestion ───────────────────────────────────

    def _message_already_cached(
        self, contact_id: str, ts: int, is_mine: bool, text: str
    ) -> bool:
        """Return True if a message with the same identity is already cached.

        For outgoing messages (``is_mine=True``) the optimistic save on send
        and the later sync sent-envelope can carry different timestamps but are
        the same logical message.  To avoid merging genuinely distinct messages
        with identical text, an outgoing echo is only deduplicated if it falls
        within a short window of the existing outgoing message.

        For incoming messages a window is also used (instead of exact timestamp
        match) so that signal-cli re-deliveries (e.g. sync from another device)
        with a slightly different timestamp are still recognised as duplicates.
        """
        for msg in self.cache.get(contact_id, []):
            if not msg.get("is_mine") == is_mine:
                continue
            if msg.get("text") != text:
                continue
            if not is_mine:
                if abs(msg.get("timestamp", 0) - ts) <= _INCOMING_DEDUP_WINDOW_MS:
                    return True
            elif abs(msg.get("timestamp", 0) - ts) <= _SEND_DEDUP_WINDOW_MS:
                return True
        return False

    def ingest_message(self, contact_id: str, data: dict, ts: int) -> bool:
        """Save an incoming/outgoing message to cache and DB.

        Idempotent per message identity: if the same message was already
        ingested (e.g. optimistically on send and later as a sync sent-envelope),
        it is *not* added a second time — preventing duplicates on reload.

        Returns ``True`` if the message was newly added, ``False`` if it was a
        duplicate (already present).
        """
        text = data["text"]
        is_mine = data["is_mine"]

        if self._message_already_cached(contact_id, ts, is_mine, text):
            return False

        _add_message_to_cache(
            contact_id,
            text,
            is_mine,
            data["sender"],
            ts,
            quote_text=data["quote_text"],
            msg_type=data["msg_type"],
            attachment_info=data["attachment_info"],
            attachment_id=data.get("attachment_id"),
        )
        self._add_cached_message(contact_id, {
            "text": text,
            "is_mine": is_mine,
            "sender": data["sender"],
            "timestamp": ts,
            "quote_text": data["quote_text"],
            "msg_type": data["msg_type"],
            "attachment_info": data["attachment_info"],
            "attachment_id": data.get("attachment_id"),
            "read": is_mine,
            "status": "sent" if is_mine else "read",
        })
        return True

    def process_receipt(self, envelope: dict) -> list[dict]:
        """Process a receipt envelope against the in-memory cache.

        Returns the list of updated message dicts (for the UI) and persists
        the status changes to the SQLite DB.
        """
        updated = _process_receipt(envelope, self.cache)
        for msg in updated:
            _update_message_status(msg["timestamp"], msg["status"])
        return updated

    # ─── Receive loop (SSE real-time) ───────────────────────────────────

    def _start_sse_listener(self) -> None:
        """Start the SSE listener in a dedicated daemon thread."""
        if self._sse_thread is not None and self._sse_thread.is_alive():
            return
        self._polling_active = True
        self._sse_thread = threading.Thread(
            target=self._sse_listener, name="signal-sse", daemon=True,
        )
        self._sse_thread.start()

    def restart_sse(self) -> None:
        """Restart the SSE listener (called after device linking)."""
        self._polling_active = False
        t = self._sse_thread
        self._sse_thread = None
        if t is not None and t.is_alive():
            t.join(timeout=5)
        self._start_sse_listener()

    def _sse_listener(self) -> None:
        """Dedicated thread: connect to signal-cli SSE endpoint, push events
        into ``_event_queue``.  Reconnects automatically on connection loss.

        The ``urlopen`` call uses a 30-second socket timeout; if signal-cli
        stops sending keep-alive comments (every 15 s), the socket will time
        out and the generator returns, triggering a reconnect after a brief
        pause.
        """
        while self._polling_active and self._sse_thread is not None:
            try:
                for envelope in self._rpc.listen_events(self.user_number):
                    if not self._polling_active:
                        return
                    events = self.envelope_to_event(
                        envelope.get("envelope", {})
                    )
                    for event in events:
                        if event is not None:
                            self._event_queue.put(event)
                    if events:
                        logger.info("SSE: received %d events", len(events))
                if envelope:
                    logger.info("SSE: envelope received")
            except Exception as e:
                logger.info("SSE: connection lost, retrying... (%s)", e)
            # Brief pause before reconnect — keep it short (1s)
            # so we don't miss pending messages from a fresh daemon
            # startup with --receive-mode on-start.
            for _ in range(10):
                if not self._polling_active:
                    return
                time.sleep(0.1)

    async def receive(self):
        """Yield normalized ``ChatEvent`` objects from the SSE queue.

        Implements the ``ChatBackend`` interface contract.  Events are
        drained from the internal ``_event_queue``, which is populated in
        real time by the SSE listener thread.
        """
        self._polling_active = True
        while self._polling_active:
            try:
                yield self._event_queue.get(timeout=0.5)
            except queue.Empty:
                await asyncio.sleep(0)
            except Exception:
                pass

    def poll_once(self) -> list[ChatEvent]:
        """Drain all pending events from the SSE queue without blocking.
        
        Called by the poll worker thread.

        Called by the ``_poll_worker`` thread in ``signal_tui.py``.
        Replaces the old HTTP-polling approach with a non-blocking queue
        drain.
        """
        events: list[ChatEvent] = []
        while True:
            try:
                events.append(self._event_queue.get_nowait())
            except queue.Empty:
                break
        return events

