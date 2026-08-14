"""
Telegram backend — a ``ChatBackend`` implementation using Telethon (MTProto).

The backend runs a dedicated asyncio event loop in a daemon thread (same
pattern as the Signal SSE listener).  Incoming messages from Telethon event
handlers are normalised into ``ChatEvent`` objects and placed on a
``queue.Queue``, consumed by the TUI poll worker via ``poll_once()``.

QR-code pairing is supported through ``get_pairing_qr()`` (returns a
``tg://login?token=...`` URL that the ``DeviceLinkPickerScreen`` renders
as a QR code).
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from pathlib import Path
from typing import Any

from models import (
    ChatContact,
    ChatEvent,
    PROTOCOL_TELEGRAM,
)

from .base import ChatBackend
from .config import (
    get_telegram_api_id,
    get_telegram_api_hash,
    get_telegram_session_path,
)

logger = logging.getLogger(__name__)

# Log Telethon messages to a file to avoid Textual stderr suppression.
_log_fh = logging.FileHandler("/tmp/telegram.log", mode="w")
_log_fh.setLevel(logging.DEBUG)
_log_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_log_fh)
logger.setLevel(logging.DEBUG)

# Max cached messages per contact.
_MAX_CACHE_PER_CONTACT = 50

# Dedup window (ms) for incoming messages from the same (contact, text).
_INCOMING_DEDUP_WINDOW_MS = 2000


# ─── TelegramBackend ──────────────────────────────────────────────────────────

class TelegramBackend(ChatBackend):
    """Telegram backend using Telethon with a dedicated asyncio event loop."""

    protocol = PROTOCOL_TELEGRAM

    def __init__(self) -> None:
        self._api_id = get_telegram_api_id()
        self._api_hash = get_telegram_api_hash()
        self._session_path = str(get_telegram_session_path())

        self._client: Any = None  # TelegramClient, set in _connect_sync
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._running = False
        self._connected = False

        # Normalised contact list
        self.contacts: list[ChatContact] = []
        self._contacts_by_id: dict[int, ChatContact] = {}

        # Protocol-aware message cache (contact_id → list[dict])
        self.cache: dict[str, list[dict]] = {}

        # Event queue — Telethon handlers push, poll_once() drains
        self._events: queue.Queue[ChatEvent] = queue.Queue()

        # Seen message ids for dedup
        self._seen_msg_ids: set[str] = set()

        # 2FA state for QR login
        self._needs_2fa: bool = False

    # ─── Attachments ──────────────────────────────────────────────────────

    def get_attachment_path(self, attachment_id: str) -> Path | None:
        """Resolve an attachment id to a local file path."""
        p = Path(attachment_id)
        return p if p.is_file() else None

    # ─── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Not used — connection is sync (dedicated event loop)."""
        pass

    async def disconnect(self) -> None:
        """Stop the Telethon event loop and disconnect."""
        self.disconnect_sync()

    def disconnect_sync(self) -> None:
        """Stop the event loop thread and disconnect the client."""
        self._running = False
        self._connected = False
        if self._loop is not None and self._client is not None:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._client.disconnect(), self._loop
                ).result(timeout=5)
            except Exception:
                pass
        if self._loop_thread is not None and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=5)
        self._loop = None
        self._loop_thread = None
        self._client = None

    # ─── Connection (called from worker thread by the TUI) ─────────────────

    def _connect_sync(self) -> None:
        """Start the Telethon client and event loop (blocking, called in worker)."""
        from telethon import TelegramClient, events

        # Tear down any previous client/loop BEFORE creating a new one.  A
        # reconnect (Ctrl+L) used to leave the old Telethon client running on
        # the same session file: two concurrent clients corrupt the update
        # state (pts/qts) and break live message delivery.
        self.disconnect_sync()

        self.cache = self._load_protocol_cache()

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        self._client = TelegramClient(
            self._session_path, self._api_id, self._api_hash, loop=self._loop,
        )

        try:
            self._loop.run_until_complete(self._client.connect())
        except Exception as exc:
            logger.exception("Telegram connect failed: %s", exc)
            self._connected = False
            return

        authorised = self._loop.run_until_complete(
            self._client.is_user_authorized()
        )

        if not authorised:
            logger.info("Telegram: not authorised, waiting for QR pairing")
            self._connected = False
            return

        self._connected = True
        try:
            self._loop.run_until_complete(self._load_contacts())
        except Exception as exc:
            logger.exception("Telegram _load_contacts failed: %s", exc)
            self.contacts = []
            self._contacts_by_id = {}
        logger.info("Telegram: loaded %d contacts", len(self.contacts))

        # Register Telethon event handlers
        @self._client.on(events.NewMessage)
        async def _on_new_message(event: Any) -> None:
            await self._handle_new_message(event)

        @self._client.on(events.Raw)
        async def _on_raw(update: Any) -> None:
            from telethon.tl.types import UpdateReadHistoryOutbox
            if isinstance(update, UpdateReadHistoryOutbox):
                await self._handle_read_receipt(update)
            else:
                logger.info("Telegram raw: %s", type(update).__name__)

        self._running = True
        self._loop_thread = threading.Thread(
            target=self._run_event_loop,
            name="telegram-loop",
            daemon=True,
        )
        self._loop_thread.start()
        logger.info("Telegram: connected, %d contacts", len(self.contacts))

    def _run_event_loop(self) -> None:
        """Run the asyncio event loop forever (called in daemon thread)."""
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        except Exception:
            logger.exception("Telegram event loop crashed")
        finally:
            logger.info("Telegram event loop stopped")

    # ─── Contacts ──────────────────────────────────────────────────────────

    @staticmethod
    def _entity_to_contact(entity: Any) -> ChatContact:
        """Convert a Telethon User/Chat/Channel entity into a ``ChatContact``.

        Supports both real Telethon types and mock objects from tests.
        """
        from telethon.tl.types import User as TelethonUser
        from telethon.tl.types import Chat as TelethonChat
        from telethon.tl.types import Channel as TelethonChannel

        eid: int = entity.id
        name: str = ""
        extras: dict[str, Any] = {}

        if isinstance(entity, TelethonUser):
            name = entity.first_name or ""
            if entity.last_name:
                name += " " + entity.last_name
            if not name.strip():
                name = entity.username or str(eid)
            extras["username"] = entity.username or ""
            extras["phone"] = entity.phone or ""
            extras["is_group"] = False
        elif isinstance(entity, TelethonChat):
            name = getattr(entity, "title", "") or str(eid)
            extras["is_group"] = True
        elif isinstance(entity, TelethonChannel):
            name = getattr(entity, "title", "") or str(eid)
            extras["is_channel"] = True
        else:
            # Mock objects from tests — duck-type check
            if hasattr(entity, "first_name"):
                name = getattr(entity, "first_name", "") or ""
                if hasattr(entity, "last_name") and entity.last_name:
                    name += " " + entity.last_name
                if not name.strip():
                    name = getattr(entity, "username", "") or str(eid)
                extras["username"] = getattr(entity, "username", "") or ""
                extras["phone"] = getattr(entity, "phone", "") or ""
                extras["is_group"] = False
            elif hasattr(entity, "title"):
                name = entity.title or str(eid)
                extras["is_group"] = True
                if hasattr(entity, "id") and str(entity.id).startswith("-100"):
                    extras["is_group"] = False
                    extras["is_channel"] = True
            else:
                name = str(eid)

        return ChatContact(
            id=str(eid),
            display_name=name.strip() or str(eid),
            protocol=PROTOCOL_TELEGRAM,
            extras=extras,
        )

    async def _load_contacts(self) -> None:
        """Fetch dialogs from Telethon and build ``ChatContact`` list."""
        contacts: list[ChatContact] = []
        by_id: dict[int, ChatContact] = {}

        try:
            dialogs = await self._client.get_dialogs(limit=200)
        except Exception as exc:
            logger.exception("Telegram get_dialogs failed: %s", exc)
            self.contacts = []
            self._contacts_by_id = {}
            return

        for dialog in dialogs:
            cc = self._entity_to_contact(dialog.entity)
            if dialog.message and dialog.message.date:
                cc.last_message_ts = int(dialog.message.date.timestamp() * 1000)
            contacts.append(cc)
            by_id[dialog.entity.id] = cc

        self.contacts = contacts
        self._contacts_by_id = by_id

    def fetch_recent_history(self, limit: int = 20) -> int:
        """Fetch recent messages for all known contacts from Telethon.

        Processes them through ``ingest_message`` to populate cache + SQLite.
        Returns the number of messages fetched.
        """
        if self._client is None or self._loop is None or not self._connected:
            return 0

        async def _fetch():
            total = 0
            for contact in self.contacts:
                try:
                    eid = int(contact.id)
                except (ValueError, TypeError):
                    continue
                try:
                    entity = await self._client.get_input_entity(eid)
                    messages = await self._client.get_messages(entity, limit=limit)
                    for msg in messages:
                        if msg is None or not msg.date:
                            continue
                        evt = self._message_to_chat_event(msg)
                        if evt is None:
                            continue
                        ts = evt.payload.get("timestamp", 0)
                        self.ingest_message(contact.id, evt.payload, ts)
                        total += 1
                except Exception:
                    logger.exception("Telegram fetch_history failed for %s", contact.id)
            return total

        try:
            future = asyncio.run_coroutine_threadsafe(_fetch(), self._loop)
            return future.result(timeout=120)
        except Exception:
            logger.exception("Telegram fetch_recent_history failed")
            return 0

    def _identify_contact(self, contact_id: str) -> ChatContact | None:
        """Resolve a Telegram user id to a known ``ChatContact``."""
        try:
            eid = int(contact_id)
        except (ValueError, TypeError):
            return None
        return self._contacts_by_id.get(eid)

    async def list_contacts(self) -> list[ChatContact]:
        return list(self.contacts)

    # ─── Messaging ─────────────────────────────────────────────────────────

    async def send_message(
        self,
        contact_id: str,
        text: str,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_message: str | None = None,
    ) -> str:
        """Send a text message via Telethon (called from TUI thread)."""
        if self._loop is None or self._client is None:
            raise RuntimeError("Telegram backend not connected")

        async def _send() -> str:
            try:
                eid = int(contact_id)
            except (ValueError, TypeError):
                raise ValueError(f"Invalid Telegram contact id: {contact_id}")
            entity = await self._client.get_input_entity(eid)
            msg = await self._client.send_message(entity, text)
            return str(msg.id)

        future = asyncio.run_coroutine_threadsafe(_send(), self._loop)
        return future.result(timeout=30)

    async def mark_read(self, contact_id: str) -> None:
        """Mark messages as read (Telethon handles this automatically)."""
        pass

    def send_message_sync(
        self,
        contact_id: str,
        text: str,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_message: str | None = None,
    ) -> str:
        """Synchronous send, for use from the TUI's sync callbacks."""
        if self._loop is None or self._client is None:
            raise RuntimeError("Telegram backend not connected")
        async def _send():
            try:
                eid = int(contact_id)
            except (ValueError, TypeError):
                raise ValueError(f"Invalid Telegram contact id: {contact_id}")
            entity = await self._client.get_input_entity(eid)
            msg = await self._client.send_message(entity, text)
            return str(msg.id)
        future = asyncio.run_coroutine_threadsafe(_send(), self._loop)
        return future.result(timeout=30)

    def mark_read_sync(self, contact_id: str) -> None:
        """Synchronous mark-read — persists read status to SQLite."""
        try:
            from backend import _mark_as_read
            _mark_as_read(contact_id, protocol=PROTOCOL_TELEGRAM)
        except Exception:
            pass

    # ─── Event reception ───────────────────────────────────────────────────

    async def receive(self):
        """Yield ChatEvent objects (contract — unused, poll_once is used)."""
        if False:
            yield

    async def _handle_new_message(self, event: Any) -> None:
        """Telethon event handler: normalise and enqueue a new message."""
        msg = event.message
        logger.info("Telegram: NewMessage event, msg_id=%s chat_id=%s", 
                     getattr(msg, 'id', '?'), getattr(msg, 'chat_id', '?'))
        if msg is None:
            return

        # Download media attachments (photos, documents) for inline viewing
        attachment_id: str | None = None
        if msg.photo or msg.document:
            try:
                import tempfile, os
                media_dir = os.path.join(tempfile.gettempdir(), "telegram-media")
                os.makedirs(media_dir, exist_ok=True)
                path = await msg.download_media(file=media_dir)
                if path:
                    attachment_id = str(path)
                    logger.info("Telegram: downloaded media to %s", path)
            except Exception:
                logger.exception("Telegram: failed to download media")

        evt = self._message_to_chat_event(msg, attachment_id=attachment_id)
        if evt is not None:
            self._events.put(evt)
            logger.info("Telegram: enqueued event for chat %s", evt.contact_id)

    def _message_to_chat_event(self, msg: Any, attachment_id: str | None = None) -> ChatEvent | None:
        """Convert a Telethon ``Message`` (or mock) into a ``ChatEvent``."""
        try:
            chat_id = str(msg.chat_id) if msg.chat_id else None
        except Exception:
            chat_id = None
        if chat_id is None:
            return None

        text = msg.text or ""
        is_mine = bool(getattr(msg, "out", False))

        # Sender display name
        sender = ""
        if msg.sender:
            sender = getattr(msg.sender, "first_name", "") or ""
            if getattr(msg.sender, "last_name", ""):
                sender += " " + msg.sender.last_name
            if not sender.strip():
                sender = str(getattr(msg.sender, "id", ""))

        ts = 0
        if msg.date:
            ts = int(msg.date.timestamp() * 1000)

        msg_type = "text"
        att_id = attachment_id  # use downloaded path if available
        attachment_info: str | None = None

        if msg.photo:
            msg_type = "image"
            attachment_info = "🖼️ Photo"
        elif msg.document:
            msg_type = "attachment"
            if not att_id:
                att_id = str(msg.id) if msg.id else None
            attachment_info = "📎 Document"
            for attr in getattr(msg.document, "attributes", []):
                name = getattr(attr, "file_name", None)
                if name:
                    attachment_info = f"📎 {name}"
                    break
        elif msg.sticker:
            msg_type = "sticker"
            attachment_info = "🎨 Sticker"
        elif msg.video:
            msg_type = "attachment"
            attachment_info = "🎬 Video"
        elif msg.voice:
            msg_type = "attachment"
            attachment_info = "🎤 Voice"
        elif msg.audio:
            msg_type = "attachment"
            attachment_info = "🎵 Audio"

        # Quote / reply
        quote_text: str | None = None
        if msg.reply_to and getattr(msg.reply_to, "reply_to_msg_id", None):
            cached = self.cache.get(chat_id, [])
            for m in cached:
                if str(m.get("id")) == str(msg.reply_to.reply_to_msg_id):
                    quote_text = m.get("text", "")
                    break

        payload: dict[str, Any] = {
            "id": str(msg.id),
            "text": text,
            "is_mine": is_mine,
            "sender": sender,
            "timestamp": ts,
            "quote_text": quote_text,
            "msg_type": msg_type,
            "attachment_info": attachment_info,
            "attachment_id": att_id,
            "status": "sent",
            "protocol": PROTOCOL_TELEGRAM,
            "contact": self._identify_contact(chat_id),
        }

        return ChatEvent(
            type="message",
            protocol=PROTOCOL_TELEGRAM,
            contact_id=chat_id,
            payload=payload,
        )

    async def _handle_read_receipt(self, update: Any) -> None:
        """Handle ``UpdateReadHistoryOutbox`` — marks sent messages as read."""
        from telethon.tl.types import PeerUser, PeerChat, PeerChannel

        # Determine the peer's ID
        peer = update.peer
        if isinstance(peer, PeerUser):
            contact_id = str(peer.user_id)
        elif isinstance(peer, PeerChat):
            contact_id = str(peer.chat_id)
        elif isinstance(peer, PeerChannel):
            contact_id = str(peer.channel_id)
        else:
            return

        max_id = update.max_id
        logger.info("Telegram read receipt: contact=%s max_id=%s", contact_id, max_id)

        # Update in-memory cache status
        updated: list[dict] = []
        for msg in self.cache.get(contact_id, []):
            mid = msg.get("id")
            if mid and int(mid) <= max_id and msg.get("is_mine") and msg.get("status") != "read":
                msg["status"] = "read"
                updated.append(msg)

        if updated:
            # Persist to SQLite
            try:
                from backend import _update_message_status
                for msg in updated:
                    _update_message_status(
                        msg["timestamp"], "read",
                        protocol=PROTOCOL_TELEGRAM, contact_number=contact_id,
                    )
            except Exception:
                logger.exception("Telegram: _update_message_status failed")

            # Enqueue receipt event for the TUI (matches generic pattern)
            message_ids = [msg.get("id") for msg in updated if msg.get("id")]
            self._events.put(ChatEvent(
                type="receipt",
                protocol=PROTOCOL_TELEGRAM,
                contact_id=contact_id,
                payload={"message_ids": message_ids, "is_read": True},
            ))

    # ─── poll_once (queue drain) ───────────────────────────────────────────

    def process_receipt(self, envelope: dict) -> list[dict]:
        """Handle a receipt batch against the in-memory cache.

        Updates ``status`` for sent messages matching the reported ids.
        Follows the same pattern as WhatsApp's ``process_receipt``.
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
                if msg.get("is_mine") and str(msg.get("id", "")) in {str(i) for i in ids}:
                    old = msg.get("status", "sent")
                    rank = {"sent": 0, "delivered": 1, "read": 2}
                    if old != target and rank.get(target, 0) > rank.get(old, 0):
                        msg["status"] = target
                        updated.append(msg)
                        to_persist.append((contact_id, msg))
        if to_persist:
            from backend import _update_message_status
            for contact_id, msg in to_persist:
                _update_message_status(
                    msg["timestamp"], msg["status"],
                    protocol=PROTOCOL_TELEGRAM, contact_number=contact_id,
                )
        return updated

    # ─── poll_once (queue drain) ───────────────────────────────────────────

    def poll_once(self) -> list[ChatEvent]:
        """Drain all pending events from the queue without blocking."""
        events: list[ChatEvent] = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                break
        return events

    # ─── Cache ─────────────────────────────────────────────────────────────

    def _load_protocol_cache(self) -> dict[str, list[dict]]:
        """Load Telegram message cache from the shared SQLite database."""
        try:
            from backend import _load_cache as _load_sqlite_cache
            return _load_sqlite_cache(protocol=PROTOCOL_TELEGRAM)
        except Exception:
            logger.exception("Telegram: failed to load protocol cache")
            return {}

    def ingest_message(
        self, contact_id: str, data: dict, ts: int
    ) -> bool:
        """Add a message to the in-memory cache AND SQLite with dedup.

        Returns True if the message was newly added, False if duplicate.
        """
        from backend import _add_message_to_cache, _update_message_id

        mid = data.get("id")
        if mid and mid in self._seen_msg_ids:
            return False
        if mid:
            self._seen_msg_ids.add(mid)

        # Dedup by (contact, text, ts) within a small window.
        # If the existing entry has no id and the new one does, upgrade it.
        # The optimistic timestamp is KEPT (not replaced with the echo's): the
        # UI cache, the backend cache and SQLite must keep the SAME timestamp,
        # otherwise the next backend-cache reload (e.g. Ctrl+L reconnect) seeds
        # the old client ts and `_on_backend_ready` re-adds the sent message
        # (exact-ts identity mismatch) → doubled "sent" messages in UI.
        text = data.get("text", "")
        for m in self.cache.get(contact_id, []):
            if (
                m.get("text") == text
                and abs(int(m.get("timestamp", 0)) - ts) <= _INCOMING_DEDUP_WINDOW_MS
            ):
                if mid and not m.get("id"):
                    m["id"] = mid
                    try:
                        _update_message_id(
                            contact_id,
                            text,
                            bool(m.get("is_mine", False)),
                            m.get("timestamp"),
                            mid,
                            protocol=PROTOCOL_TELEGRAM,
                        )
                    except Exception:
                        logger.exception("Telegram: _update_message_id failed")
                return False

        # Persist to SQLite (same pattern as Signal/WhatsApp backends)
        try:
            _add_message_to_cache(
                contact_id,
                text,
                data.get("is_mine", False),
                data.get("sender", ""),
                ts,
                quote_text=data.get("quote_text"),
                msg_type=data.get("msg_type", "text"),
                attachment_info=data.get("attachment_info"),
                attachment_id=data.get("attachment_id"),
                protocol=PROTOCOL_TELEGRAM,
                msg_id=mid,
            )
        except Exception:
            logger.exception("Telegram: _add_message_to_cache failed")

        if contact_id not in self.cache:
            self.cache[contact_id] = []

        self.cache[contact_id].append({
            "id": mid,
            "text": text,
            "is_mine": data.get("is_mine", False),
            "sender": data.get("sender", ""),
            "timestamp": ts,
            "quote_text": data.get("quote_text"),
            "msg_type": data.get("msg_type", "text"),
            "attachment_info": data.get("attachment_info"),
            "attachment_id": data.get("attachment_id"),
            "read": data.get("is_mine", False),  # incoming = unread
            "status": "sent" if data.get("is_mine") else "read",
        })

        # Keep cache bounded
        if len(self.cache[contact_id]) > _MAX_CACHE_PER_CONTACT:
            self.cache[contact_id] = self.cache[contact_id][-_MAX_CACHE_PER_CONTACT:]

        return True

    # ─── Pairing ───────────────────────────────────────────────────────────

    @property
    def needs_pairing(self) -> bool:
        """True if the backend needs QR pairing (not authorised).

        Performs a quick check only — no network I/O.  The actual auth
        verification is done by ``_connect_sync``.
        """
        if self._api_id == 0 or not self._api_hash:
            return False
        if self._connected:
            return False
        # No session file → definitely needs pairing
        if not Path(self._session_path).exists():
            return True
        # Session file exists → let _connect_sync verify it
        return False

    def get_pairing_qr(self) -> str | None:
        """Start QR login, return the ``tg://login?token=...`` URL.

        A background thread runs the Telethon event loop and waits for
        the QR scan to complete.  On success, ``_connected`` is set to
        ``True`` and the TUI can detect it via ``_check_telegram_done``.

        If 2FA is required, the error is logged and the user must disable
        2FA temporarily or use the ``link_telegram.py`` CLI script.
        """
        if self._api_id == 0 or not self._api_hash:
            return None

        from telethon import TelegramClient
        from telethon.errors import SessionPasswordNeededError

        # Clean up any previous QR login attempt
        if self._loop_thread is not None and self._loop_thread.is_alive():
            # Previous attempt still waiting — let it finish naturally
            self._loop_thread.join(timeout=2)
        self._connected = False
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
            self._loop.close()
            self._loop = None

        loop = asyncio.new_event_loop()
        self._loop = loop

        client = TelegramClient(
            self._session_path, self._api_id, self._api_hash, loop=loop,
        )
        self._client = client

        try:
            loop.run_until_complete(client.connect())
        except Exception as exc:
            logger.exception("Telegram QR connect failed: %s", exc)
            loop.close()
            self._loop = None
            self._client = None
            return f"ERROR: {exc}"

        if loop.run_until_complete(client.is_user_authorized()):
            loop.run_until_complete(client.disconnect())
            loop.close()
            self._loop = None
            self._client = None
            self._connected = True
            return "INFO: Already logged in"

        try:
            qr_login = loop.run_until_complete(client.qr_login())
        except Exception as exc:
            logger.exception("Telegram QR login start failed: %s", exc)
            loop.run_until_complete(client.disconnect())
            loop.close()
            self._loop = None
            self._client = None
            return f"ERROR: {exc}"

        qr_url = qr_login.url if hasattr(qr_login, "url") else ""
        if not qr_url:
            qr_url = f"tg://login?token={qr_login.token}"

        # Start a background thread that waits for the QR scan
        def _wait_thread() -> None:
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(qr_login.wait(timeout=120))
                logger.info("Telegram QR login: scan completed successfully")
                self._connected = True
            except SessionPasswordNeededError:
                logger.info("Telegram QR login: 2FA required, waiting for password")
                self._needs_2fa = True
                # Keep client + loop alive for complete_2fa()
                return
            except TimeoutError:
                logger.info("Telegram QR login: timeout (120s), will refresh")
            except Exception as exc:
                logger.exception("Telegram QR login wait failed: %s", exc)
            finally:
                # Only cleanup if not waiting for 2FA
                if not self._needs_2fa:
                    try:
                        loop.run_until_complete(client.disconnect())
                    except Exception:
                        pass
                    loop.close()

        self._loop_thread = threading.Thread(
            target=_wait_thread, name="telegram-qr-wait", daemon=True,
        )
        self._loop_thread.start()

        return qr_url

    def complete_2fa(self, password: str) -> bool:
        """Complete 2FA after QR scan using the given password.

        Returns True on success, False on failure.
        """
        if not self._needs_2fa or self._client is None or self._loop is None:
            return False

        async def _sign_in():
            return await self._client.sign_in(password=password)

        try:
            self._loop.run_until_complete(_sign_in())
            logger.info("Telegram 2FA: sign_in succeeded")
            self._connected = True
            self._needs_2fa = False
            # Cleanup
            try:
                self._loop.run_until_complete(self._client.disconnect())
            except Exception:
                pass
            self._loop.close()
            self._loop = None
            self._loop_thread = None
            return True
        except Exception as exc:
            logger.exception("Telegram 2FA sign_in failed: %s", exc)
            self._needs_2fa = False
            return False
