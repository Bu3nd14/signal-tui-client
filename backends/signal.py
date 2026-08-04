"""
Signal backend — a ``ChatBackend`` implementation for signal-cli.

This wraps the existing signal-cli handling in ``backend.py`` (JSON-RPC over
HTTP daemon, subprocess fallback, SQLite cache) and exposes it through the
neutral ``ChatBackend`` interface.  Envelope parsing that used to live in the
TUI is gathered here so the UI only deals with normalized ``ChatContact`` /
``ChatEvent`` objects.
"""

from __future__ import annotations

import asyncio
import subprocess
import time

from models import (
    ChatContact,
    ChatEvent,
    PROTOCOL_SIGNAL,
)

from .base import ChatBackend

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


class SignalBackend(ChatBackend):
    """signal-cli backend adapted to the ``ChatBackend`` interface."""

    protocol = PROTOCOL_SIGNAL

    def __init__(self, user_number: str = USER_NUMBER):
        self.user_number = user_number
        self._rpc = SignalRPCClient()
        self._use_daemon = False
        self.daemon_proc: subprocess.Popen | None = None
        self._polling_active = False

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
        _prune_cache()
        self.cache = self._load_protocol_cache()

        if _is_daemon_running():
            self._use_daemon = True
            self._load_contacts_rpc()
            return

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

    async def disconnect(self) -> None:
        """Stop polling.  The daemon itself is left running by design."""
        self._polling_active = False

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
        legacy = []
        for line in output.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split()
            number, name, aci = "", "", ""
            for i, p in enumerate(parts):
                if p.startswith("Number:"):
                    number = p.split(":", 1)[1].strip()
                elif p.startswith("Name:"):
                    name = p.split(":", 1)[1].strip()
                elif p.startswith("ACI:"):
                    aci_val = p.split(":", 1)[1].strip()
                    if aci_val and aci_val != "-":
                        aci = aci_val
            if number:
                legacy.append(Contact(number=number, name=name, aci=aci))
        return [self._to_chat_contact(c) for c in legacy]

    def _parse_and_update_contacts(self, contacts_data: list[dict]) -> None:
        contacts = []
        for c in contacts_data:
            number = c.get("number", "")
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
        """Identify which contact an envelope belongs to."""
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

        source = envelope.get("source", "")
        source_number = envelope.get("sourceNumber", "")
        source_uuid = envelope.get("sourceUuid", "")
        for contact in self.contacts:
            if source == contact.id or source_number == contact.id:
                return contact
            aci = contact.extras.get("aci")
            if source_uuid and aci and source_uuid == aci:
                return contact

        if sent:
            dest = sent.get("destination", "")
            for contact in self.contacts:
                if dest == contact.id:
                    return contact

        return None

    def _extract_message_data(self, envelope: dict) -> dict | None:
        """Extract normalized message data from a Signal envelope."""
        source_name = envelope.get("sourceName", "")
        source_number = envelope.get("sourceNumber", "") or envelope.get("source", "")

        def _classify_attachments(attachments: list) -> tuple[str, str, str | None]:
            if not attachments:
                return ("text", None, None)
            for att in attachments:
                content_type = att.get("contentType", "") or ""
                fname = att.get("filename", "") or ""
                caption = att.get("caption", "") or ""
                att_id = att.get("id") or att.get("attachmentId") or None
                if content_type.startswith("image/"):
                    info = caption or (f"Image: {fname}" if fname else "🖼️ Image")
                    return ("image", info, att_id)
                if content_type.startswith("video/"):
                    info = caption or (f"Video: {fname}" if fname else "🎬 Video")
                    return ("attachment", info, att_id)
                if content_type.startswith("audio/"):
                    info = caption or (f"Audio: {fname}" if fname else "🎵 Audio")
                    return ("attachment", info, att_id)
                info = caption or fname or content_type or "📎 File"
                return ("attachment", info, att_id)
            return ("attachment", "📎 File", None)

        def _extract_sticker(sticker: dict | None) -> tuple[str, str] | None:
            if not sticker:
                return None
            pack_id = sticker.get("packId", "")
            sticker_id = sticker.get("stickerId", "")
            if pack_id:
                return ("sticker", f"Sticker #{sticker_id} (pack:{pack_id[:8]}…)")
            return ("sticker", f"Sticker #{sticker_id}")

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
                return {
                    "sender": sender, "text": text, "is_mine": False,
                    "quote_text": quote_text, "msg_type": msg_type,
                    "attachment_info": att_info,
                }

            attachments = data_msg.get("attachments", [])
            msg_type, att_info, att_id = _classify_attachments(attachments)
            if not text and attachments:
                text = att_info or "Media"
            return {
                "sender": sender, "text": text, "is_mine": False,
                "quote_text": quote_text, "msg_type": msg_type,
                "attachment_info": att_info, "attachment_id": att_id,
            }

        sync = envelope.get("syncMessage", {})
        sent = sync.get("sentMessage", {})
        if sent:
            text = sent.get("message", "") or ""
            quote = sent.get("quote", {})
            quote_text = quote.get("text", "") if quote else None

            sticker_data = _extract_sticker(sent.get("sticker"))
            if sticker_data:
                msg_type, att_info = sticker_data
                if not text:
                    text = att_info or "🎨 Sticker"
                return {
                    "sender": "You", "text": text, "is_mine": True,
                    "quote_text": quote_text, "msg_type": msg_type,
                    "attachment_info": att_info,
                }

            attachments = sent.get("attachments", [])
            msg_type, att_info, att_id = _classify_attachments(attachments)
            if not text and attachments:
                text = att_info or "Media"
            return {
                "sender": "You", "text": text, "is_mine": True,
                "quote_text": quote_text, "msg_type": msg_type,
                "attachment_info": att_info, "attachment_id": att_id,
            }

        return None

    def _get_message_timestamp(self, envelope: dict) -> int:
        ts = envelope.get("timestamp", 0)
        if not ts:
            data = envelope.get("dataMessage", {})
            ts = data.get("timestamp", 0)
        if not ts:
            sync = envelope.get("syncMessage", {})
            ts = (sync.get("sentMessage", {}) or {}).get("timestamp", 0)
        return ts

    def envelope_to_event(self, envelope: dict) -> ChatEvent | None:
        """Classify a Signal envelope into a normalized ``ChatEvent``.

        Returns ``None`` for envelopes that carry no user-visible data.
        """
        # Typing indicator
        typing = _process_typing(envelope)
        if typing is not None:
            source, action = typing
            return ChatEvent(
                type="typing", protocol=self.protocol,
                contact_id=source, payload={"action": action},
            )

        # Receipt message
        if "receiptMessage" in envelope:
            receipt = envelope.get("receiptMessage", {})
            source = envelope.get("sourceNumber", "") or envelope.get("source", "")
            return ChatEvent(
                type="receipt", protocol=self.protocol,
                contact_id=source,
                payload={"receipt": receipt},
            )

        # Real message
        contact = self._identify_contact_for_envelope(envelope)
        if contact is None:
            return None
        data = self._extract_message_data(envelope)
        if data is None:
            return None
        ts = self._get_message_timestamp(envelope)
        return ChatEvent(
            type="message", protocol=self.protocol,
            contact_id=contact.id,
            payload={**data, "timestamp": ts, "contact": contact},
        )

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

        For incoming messages the timestamp is part of the identity, so a
        genuine re-delivery (same text at the same instant) is caught without
        merging distinct received messages that happen to share text.
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

    # ─── Receive loop ─────────────────────────────────────────────────

    async def receive(self):
        """Poll signal-cli and yield normalized ``ChatEvent`` objects."""
        self._polling_active = True
        while self._polling_active:
            try:
                if self._use_daemon and self._rpc:
                    messages = self._rpc.receive()
                    for msg in messages:
                        envelope = msg.get("envelope", {})
                        event = self.envelope_to_event(envelope)
                        if event is not None:
                            yield event
            except Exception:
                # Polling errors are non-fatal; logged by the caller.
                pass
            await asyncio.sleep(1)

    def poll_once(self) -> list[ChatEvent]:
        """Perform one polling round and return the normalized events.

        A single blocking call to ``receive()`` that converts any envelopes
        into ``ChatEvent`` objects.  It never sleeps and never loops internally,
        so the caller (a worker thread) controls the cadence and can stop
        promptly on shutdown.  Returns an empty list when there is nothing new.
        """
        events: list[ChatEvent] = []
        try:
            if self._use_daemon and self._rpc:
                messages = self._rpc.receive()
                for msg in messages:
                    envelope = msg.get("envelope", {})
                    event = self.envelope_to_event(envelope)
                    if event is not None:
                        events.append(event)
        except Exception:
            # Polling errors are non-fatal; logged by the caller.
            pass
        return events

    def receive_events_sync(self):
        """Blocking-poll signal-cli and yield normalized ``ChatEvent`` objects.

        NOTE: this generator only yields when messages arrive; when idle it
        busy-polls and never relinquishes control, which defeats prompt
        shutdown.  Prefer ``poll_once()`` driven by a worker loop that sleeps.
        """
        self._polling_active = True
        while self._polling_active:
            for event in self.poll_once():
                yield event

