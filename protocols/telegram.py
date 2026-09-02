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
import os
import queue
import shutil
import sqlite3
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from models import (
    PROTOCOL_TELEGRAM,
    ChatContact,
    ChatEvent,
    is_caption_like,
    media_kind_from_mime,
    media_quote_placeholder,
    msg_type_for_media_kind,
)

from .base import ChatBackend, should_upgrade_outgoing_attachment
from .config import (
    get_address_book_ttl_s,
    get_telegram_api_hash,
    get_telegram_api_id,
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

_AVAILABLE_REACTIONS_TTL_S = 600

# Prefix for lazy-download media references (``tgref:<chat_id>:<msg_id>``).
_TGREF_PREFIX = "tgref:"


def _tg_media_kind(msg: Any) -> tuple[str | None, str | None, str | None]:
    """Return ``(media_kind, filename, mime)`` for a Telethon message."""
    if getattr(msg, "photo", None):
        return "image", None, "image/jpeg"
    document = getattr(msg, "document", None)
    if document is None:
        return None, None, None

    mime = (getattr(document, "mime_type", "") or "").lower()
    filename = None
    sticker = animated = video = voice = audio = False
    for attr in getattr(document, "attributes", None) or []:
        name = type(attr).__name__
        if name == "DocumentAttributeFilename":
            filename = getattr(attr, "file_name", None) or filename
        elif name == "DocumentAttributeSticker":
            sticker = True
        elif name == "DocumentAttributeAnimated":
            animated = True
        elif name == "DocumentAttributeVideo":
            video = True
        elif name == "DocumentAttributeVoice":
            voice = True
        elif name == "DocumentAttributeAudio":
            if getattr(attr, "voice", False):
                voice = True
            else:
                audio = True

    if sticker:
        return "sticker", filename, mime
    if animated:
        return "gif", filename, mime
    if voice:
        return "voice", filename, mime
    if video:
        return "video", filename, mime
    if audio or mime.startswith("audio/"):
        return "audio", filename, mime
    return media_kind_from_mime(mime) or "document", filename, mime


def _tg_label_for(kind: str, filename: str | None) -> str:
    """Return Telegram's canonical attachment label for a media kind."""
    if filename:
        return f"📎 {filename}"
    return {
        "image": "Photo",
        "gif": "📎 Document",
        "video": "🎬 Video",
        "voice": "🎤 Voice",
        "audio": "🎵 Audio",
        "document": "📎 Document",
        "sticker": "🎨 Sticker",
    }[kind]


#: Maps Telegram's fine-grained attachment labels (set by
#: ``_message_to_chat_event``) to the canonical placeholder types used by
#: ``media_quote_placeholder``.  Telegram's coarse ``msg_type`` is ``"image"``
#: for photos and ``"attachment"`` for video/audio/voice/document, so the
#: label is what distinguishes "🎬 Video" / "🎵 Audio" from a generic "📎 File".
_TG_QUOTE_INFO_TYPE = {
    "Photo": "image",
    "🎨 Sticker": "sticker",
    "🎬 Video": "video",
    "🎤 Voice": "audio",
    "🎵 Audio": "audio",
    "📎 Document": "attachment",
}


def _tg_quote_text_from_cached(target: dict) -> str | None:
    """Compose the ``quote_text`` for a cached Telegram reply target.

    Text targets keep their text.  Media targets use their caption (``text``)
    when present, otherwise a typed placeholder derived from ``msg_type`` and
    ``attachment_info``.  Returns ``None`` for a text-less target of unknown
    type (no bubble mounted).
    """
    msg_type = target.get("msg_type", "text")
    if msg_type == "text":
        return target.get("text") or None
    caption = (target.get("text") or "").strip()
    if caption:
        return caption
    info = (target.get("attachment_info") or "").strip()
    fine_type = _TG_QUOTE_INFO_TYPE.get(info)
    if fine_type is not None:
        return media_quote_placeholder(fine_type)
    if info:
        # A filename/label outside the typed map (e.g. "📎 report.pdf") enriches
        # the placeholder, mirroring Signal's "filename — placeholder" form.
        return f"{info} — {media_quote_placeholder(msg_type)}"
    return media_quote_placeholder(msg_type)


def _media_dir() -> Path:
    """Return the Telegram media cache directory (persistent, app-local).

    Storicamente i media Telegram vivevano in ``/tmp/telegram-media``
    (volatili: sparivano a ogni riavvio del sistema). Dal 28/08/2026 la
    directory è persistente sotto ``CACHE_DIR``; ``_migrate_legacy_media_dir``
    sposta i file già scaricati e aggiorna i riferimenti nel DB.
    """
    from protocols.db import CACHE_DIR

    return Path(CACHE_DIR) / "telegram-media"


def _migrate_legacy_media_dir() -> None:
    """One-shot best-effort: media Telegram da /tmp → CACHE_DIR persistente.

    Sposta i file da ``<tmp>/telegram-media`` (se esiste) nel nuovo percorso
    e riscrive i riferimenti nel DB (``attachment_id``, ``quote_attachment_id``,
    ``quote_attachment_path``) che puntano al vecchio prefisso. Non solleva
    mai: in caso di errore logga in debug e prosegue (i file restano dove sono).
    """
    from protocols.db import _DB_LOCK, CACHE_DIR, DB_FILE

    new_dir = Path(CACHE_DIR) / "telegram-media"
    old_dir = Path(tempfile.gettempdir()) / "telegram-media"

    moved = False
    if old_dir.is_dir():
        try:
            new_dir.mkdir(parents=True, exist_ok=True)
            for item in old_dir.iterdir():
                try:
                    shutil.move(str(item), str(new_dir / item.name))
                except OSError:
                    continue
            try:
                old_dir.rmdir()  # rimuove la dir se ormai vuota
            except OSError:
                pass
            moved = True
        except OSError:
            logger.debug("Telegram media migration failed", exc_info=True)
    if not moved:
        return

    old_prefixes = {str(old_dir)}
    generic = "/tmp/telegram-media"
    if generic != str(old_dir):
        old_prefixes.add(generic)
    new_prefix = str(new_dir)
    columns = ("attachment_id", "quote_attachment_id", "quote_attachment_path")
    try:
        with _DB_LOCK:
            connection = sqlite3.connect(DB_FILE)
            try:
                for column in columns:
                    for old in old_prefixes:
                        connection.execute(
                            "UPDATE messages SET "
                            + column
                            + " = replace("
                            + column
                            + ", ?, ?) WHERE protocol = 'telegram' AND "
                            + column
                            + " LIKE ?",
                            (old, new_prefix, old + "%"),
                        )
                connection.commit()
            finally:
                connection.close()
    except sqlite3.Error:
        logger.debug("Telegram media DB migration failed", exc_info=True)


# ─── TelegramBackend ──────────────────────────────────────────────────────────


class TelegramBackend(ChatBackend):
    """Telegram backend using Telethon with a dedicated asyncio event loop."""

    protocol = PROTOCOL_TELEGRAM
    attachment_send_timeout = 120

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

        # Address book (rubrica completa) — cache + TTL
        self._address_book: list[ChatContact] | None = None
        self._address_book_ts: float = 0.0

        self._available_reactions: list[str] | None = None
        self._available_reactions_ts: float = 0.0

        # Protocol-aware message cache (contact_id → list[dict])
        self.cache: dict[str, list[dict]] = {}

        # Event queue — Telethon handlers push, poll_once() drains
        self._events: queue.Queue[ChatEvent] = queue.Queue()

        # Seen message ids for dedup
        self._seen_msg_ids: set[str] = set()

        # 2FA state for QR login
        self._needs_2fa: bool = False

    # ─── Attachments ──────────────────────────────────────────────────────

    @staticmethod
    def _media_ref(chat_id: str, msg_id: int) -> str | None:
        """Build a lazy-download reference for a Telegram media message."""
        if msg_id is None:
            return None
        return f"{_TGREF_PREFIX}{chat_id}:{msg_id}"

    def get_attachment_path(self, attachment_id: str) -> Path | None:
        """Resolve an attachment id to a local file path.

        1. Empty id → ``None``.
        2. Existing local file → its ``Path`` (live download, legacy rows).
        3. Legacy path from another machine → basename in ``_media_dir()``.
        4. ``tgref:<chat_id>:<msg_id>`` → lazy download via Telethon.
        5. Anything else → ``None``.
        """
        if not attachment_id:
            return None

        p = Path(attachment_id)
        if p.is_file():
            return p

        if not attachment_id.startswith(_TGREF_PREFIX):
            safe_name = p.name
            if (
                safe_name not in {"", ".", ".."}
                and "/" not in safe_name
                and ".." not in safe_name
                and ".." not in p.parts
            ):
                candidate = _media_dir() / safe_name
                if candidate.is_file():
                    return candidate
            return None

        rest = attachment_id[len(_TGREF_PREFIX) :]
        try:
            chat_id_s, msg_id_s = rest.rsplit(":", 1)
            chat_id = int(chat_id_s)
            msg_id = int(msg_id_s)
        except (ValueError, TypeError):
            return None

        mirrored = next(
            (
                candidate
                for candidate in _media_dir().glob(f"{chat_id}-{msg_id}-sent*")
                if candidate.is_file()
            ),
            None,
        )
        if mirrored is not None:
            return mirrored

        return self._download_media_by_ref(chat_id, msg_id)

    def _download_media_by_ref(self, chat_id: int, msg_id: int) -> Path | None:
        """Lazily download a media message referenced by ``tgref:``.

        Runs on the Telethon event loop (blocking, max 30s), mirroring the
        pattern already used by ``fetch_recent_history`` and
        ``send_message_sync``.  Returns the local file path or ``None``.
        """
        if self._client is None or self._loop is None:
            return None
        if not self._connected or not self._loop.is_running():
            return None

        async def _download() -> Path | None:
            entity = await self._client.get_input_entity(chat_id)
            msg = await self._client.get_messages(entity, ids=msg_id)
            if msg is None or not (msg.photo or msg.document):
                return None

            msg_file = getattr(msg, "file", None)
            original = getattr(msg_file, "name", None) or ""
            if original:
                name = Path(original).name
            else:
                ext = getattr(msg_file, "ext", None) or ".jpg"
                name = f"photo{ext}"

            target = _media_dir() / f"{chat_id}-{msg_id}-{name}"
            if target.is_file():
                return target

            await msg.download_media(file=str(target))
            return target

        try:
            future = asyncio.run_coroutine_threadsafe(_download(), self._loop)
            return future.result(timeout=30)
        except Exception:
            logger.exception("Telegram: lazy media download failed")
            return None

    def get_attachment_chunk(
        self,
        attachment_id: str,
        start: int | None,
        length: int,
    ) -> bytes | None:
        """Download a bounded document range through Telethon."""
        if not attachment_id or length <= 0:
            return None
        local = Path(attachment_id)
        if local.is_file():
            try:
                with local.open("rb") as source:
                    if start is None:
                        source.seek(max(0, local.stat().st_size - length))
                    else:
                        source.seek(max(0, start))
                    return source.read(length)
            except OSError:
                return None
        if not attachment_id.startswith(_TGREF_PREFIX):
            return None
        try:
            chat_id_s, msg_id_s = attachment_id[len(_TGREF_PREFIX) :].rsplit(":", 1)
            chat_id = int(chat_id_s)
            msg_id = int(msg_id_s)
        except (TypeError, ValueError):
            return None
        mirrored = next(
            (
                candidate
                for candidate in _media_dir().glob(f"{chat_id}-{msg_id}-sent*")
                if candidate.is_file()
            ),
            None,
        )
        if mirrored is not None:
            try:
                with mirrored.open("rb") as source:
                    if start is None:
                        source.seek(max(0, mirrored.stat().st_size - length))
                    else:
                        source.seek(max(0, start))
                    return source.read(length)
            except OSError:
                return None
        if (
            self._client is None
            or self._loop is None
            or not self._connected
            or not self._loop.is_running()
        ):
            return None

        async def _download_chunk() -> bytes | None:
            entity = await self._client.get_input_entity(chat_id)
            msg = await self._client.get_messages(entity, ids=msg_id)
            document = getattr(msg, "document", None) if msg is not None else None
            size = int(getattr(document, "size", 0) or 0)
            if document is None or size <= 0:
                return None
            offset = max(0, size - length) if start is None else max(0, start)
            amount = min(length, max(0, size - offset))
            if amount <= 0:
                return b""
            chunks = bytearray()
            iterator = self._client.iter_download(
                document,
                offset=offset,
                limit=1,
                chunk_size=amount,
                request_size=max(
                    4096,
                    min(512 * 1024, ((amount + 4095) // 4096) * 4096),
                ),
                file_size=size,
            )
            async for chunk in iterator:
                chunks.extend(chunk)
            return bytes(chunks[:amount])

        try:
            future = asyncio.run_coroutine_threadsafe(_download_chunk(), self._loop)
            return future.result(timeout=30)
        except Exception:
            logger.debug("Telegram: partial media download failed", exc_info=True)
            return None

    # ─── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Not used — connection is sync (dedicated event loop)."""

    async def disconnect(self) -> None:
        """Stop the Telethon event loop and disconnect."""
        await asyncio.to_thread(self.disconnect_sync)

    def disconnect_sync(self) -> None:
        """Stop the event loop thread and disconnect the client."""

        async def _disconnect_on_loop(client):
            await client.disconnect()

        self._running = False
        self._connected = False
        loop, client = self._loop, self._client
        if loop is not None and client is not None:
            try:
                if loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        _disconnect_on_loop(client), loop
                    ).result(timeout=5)
                    loop.call_soon_threadsafe(loop.stop)
                elif not loop.is_closed():
                    client.disconnect()
            except Exception as _e:
                logger.debug("Telegram client disconnect failed", exc_info=True)
        if self._loop_thread is not None and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=5)
        self._loop = self._loop_thread = self._client = None

    # ─── Connection (called from worker thread by the TUI) ─────────────────

    def _connect_sync(self) -> None:
        """Start the Telethon client and event loop (blocking, called in worker)."""
        from telethon import TelegramClient, events

        # Tear down any previous client/loop BEFORE creating a new one.  A
        # reconnect (Ctrl+L) used to leave the old Telethon client running on
        # the same session file: two concurrent clients corrupt the update
        # state (pts/qts) and break live message delivery.
        self.disconnect_sync()

        _migrate_legacy_media_dir()

        self.cache = self._load_protocol_cache()

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        self._client = TelegramClient(
            self._session_path,
            self._api_id,
            self._api_hash,
            loop=self._loop,
        )

        # Register before connecting so updates delivered while Telethon catches
        # up the session state are not lost during contact loading.
        @self._client.on(events.NewMessage)
        async def _on_new_message(event: Any) -> None:
            await self._handle_new_message(event)

        @self._client.on(events.MessageEdited)
        async def _on_message_edited(event: Any) -> None:
            await self._handle_message_edited(event)

        @self._client.on(events.Raw)
        async def _on_raw(update: Any) -> None:
            from telethon.tl.types import (
                UpdateChannelUserTyping,
                UpdateChatUserTyping,
                UpdateMessageReactions,
                UpdateReadHistoryOutbox,
                UpdateUserTyping,
            )

            if isinstance(update, UpdateReadHistoryOutbox):
                await self._handle_read_receipt(update)
            elif isinstance(update, UpdateMessageReactions):
                reactions = getattr(update, "reactions", None)
                logger.debug(
                    "Telegram reaction update: peer=%s msg_id=%s results=%d",
                    type(getattr(update, "peer", None)).__name__,
                    getattr(update, "msg_id", None),
                    len(getattr(reactions, "results", None) or []),
                )
                await self._handle_reactions_update(update)
            elif isinstance(
                update,
                (UpdateUserTyping, UpdateChatUserTyping, UpdateChannelUserTyping),
            ):
                await self._handle_typing_update(update)
            else:
                logger.debug("Telegram raw: %s", type(update).__name__)

        try:
            self._loop.run_until_complete(self._client.connect())
        except Exception:
            logger.exception("Telegram connect failed")
            self._connected = False
            return

        authorised = self._loop.run_until_complete(self._client.is_user_authorized())

        if not authorised:
            logger.info("Telegram: not authorised, waiting for QR pairing")
            self._connected = False
            return

        try:
            self._loop.run_until_complete(self._configure_reaction_notify())
        except Exception:
            logger.debug(
                "Telegram reactions notify configuration failed", exc_info=True
            )

        self._connected = True
        try:
            self._loop.run_until_complete(self._load_contacts())
        except Exception:
            logger.exception("Telegram _load_contacts failed")
            self.contacts = []
            self._contacts_by_id = {}
        logger.info("Telegram: loaded %d contacts", len(self.contacts))

        self._running = True
        self._loop_thread = threading.Thread(
            target=self._run_event_loop,
            name="telegram-loop",
            daemon=True,
        )
        self._loop_thread.start()
        logger.info("Telegram: connected, %d contacts", len(self.contacts))

    async def _configure_reaction_notify(self) -> None:
        from telethon.tl.functions.account import (
            GetReactionsNotifySettingsRequest,
            SetReactionsNotifySettingsRequest,
        )
        from telethon.tl.types import (
            ReactionNotificationsFromAll,
            ReactionsNotifySettings,
        )

        settings = await self._client(GetReactionsNotifySettingsRequest())
        current = settings.messages_notify_from
        logger.info("Telegram reactions notify settings: %s", current)

        enabled = os.environ.get("TELEGRAM_REACTIONS_NOTIFY", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if not enabled:
            logger.info("Telegram reactions notify: disabled by environment")
            return
        if isinstance(current, ReactionNotificationsFromAll):
            logger.info("Telegram reactions notify: already enabled for all")
            return

        updated = ReactionsNotifySettings(
            sound=settings.sound,
            show_previews=settings.show_previews,
            messages_notify_from=ReactionNotificationsFromAll(),
            stories_notify_from=settings.stories_notify_from,
            poll_votes_notify_from=settings.poll_votes_notify_from,
        )
        await self._client(SetReactionsNotifySettingsRequest(settings=updated))
        logger.info("Telegram reactions notify: enabled for all")

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
        from telethon.tl.types import Channel as TelethonChannel
        from telethon.tl.types import Chat as TelethonChat
        from telethon.tl.types import User as TelethonUser

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
        except Exception:
            logger.exception("Telegram get_dialogs failed")
            self.contacts = []
            self._contacts_by_id = {}
            return

        for dialog in dialogs:
            cc = self._entity_to_contact(dialog.entity)
            if dialog.message and dialog.message.date:
                cc.last_message_ts = int(dialog.message.date.timestamp() * 1000)
            read_max_id = getattr(dialog, "read_outbox_max_id", None)
            if read_max_id:
                cc.extras["read_outbox_max_id"] = int(read_max_id)
            contacts.append(cc)
            by_id[dialog.entity.id] = cc

        self.contacts = contacts
        self._contacts_by_id = by_id
        self._reconcile_read_state()

    def _reconcile_read_state(self) -> None:
        """Mark outgoing messages as read based on server ``read_outbox_max_id``.

        Called after contacts/dialogs are loaded so that receipts received while
        the TUI was closed are not lost.  Scoped per contact; never downgrades.
        """
        from protocols.db import _update_message_status_by_id

        rank = {
            "pending": 0,
            "failed": 0,
            "sent": 1,
            "delivered": 2,
            "read": 3,
        }
        for contact in self.contacts:
            max_id = contact.extras.get("read_outbox_max_id")
            if not max_id:
                continue
            try:
                max_id_int = int(max_id)
            except (ValueError, TypeError):
                continue

            updated_ids: list[str] = []
            for msg in self.cache.get(contact.id, []):
                if not msg.get("is_mine"):
                    continue
                mid = msg.get("id")
                if mid is None:
                    continue
                try:
                    mid_int = int(mid)
                except (ValueError, TypeError):
                    continue
                if mid_int <= max_id_int:
                    old = msg.get("status", "sent")
                    if rank.get("read", 0) > rank.get(old, 0):
                        msg["status"] = "read"
                        updated_ids.append(str(mid))
                        try:
                            _update_message_status_by_id(
                                str(mid),
                                "read",
                                protocol=PROTOCOL_TELEGRAM,
                                contact_number=contact.id,
                            )
                        except Exception:
                            logger.exception(
                                "Telegram: _update_message_status_by_id failed"
                            )

            if updated_ids:
                self._events.put(
                    ChatEvent(
                        type="receipt",
                        protocol=PROTOCOL_TELEGRAM,
                        contact_id=contact.id,
                        payload={"message_ids": updated_ids, "is_read": True},
                    )
                )

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

    def fetch_history(self, contact_id: str, limit: int = 20) -> list[dict]:
        """Fetch and ingest recent messages for one Telegram chat."""
        if self._client is None or self._loop is None or not self._connected:
            return []
        try:
            entity_id = int(contact_id)
        except (ValueError, TypeError):
            return []

        async def _fetch() -> list[dict]:
            entity = await self._client.get_input_entity(entity_id)
            messages = await self._client.get_messages(entity, limit=limit)
            fetched = []
            for msg in messages:
                if msg is None or not msg.date:
                    continue
                event = self._message_to_chat_event(msg)
                if event is None:
                    continue
                self.ingest_message(
                    contact_id,
                    event.payload,
                    event.payload.get("timestamp", 0),
                )
                fetched.append(event.payload)
            return fetched

        try:
            future = asyncio.run_coroutine_threadsafe(_fetch(), self._loop)
            return future.result(timeout=120)
        except Exception:
            logger.exception("Telegram fetch_history failed for %s", contact_id)
            return []

    def _identify_contact(self, contact_id: str) -> ChatContact | None:
        """Resolve a Telegram user id to a known ``ChatContact``."""
        try:
            eid = int(contact_id)
        except (ValueError, TypeError):
            return None
        return self._contacts_by_id.get(eid)

    def register_contact(self, contact: ChatContact) -> None:
        """Registra un contatto (open-or-create) anche nella lookup id→contact.

        Oltre all'append in ``self.contacts`` (default di ``ChatBackend``),
        estende ``_contacts_by_id`` così ``_identify_contact`` e il fallback di
        invio (``_resolve_input_entity``) riconoscono il ghost.  Un id non
        intero viene ignorato (guard ``ValueError``/``TypeError``).
        """
        super().register_contact(contact)
        try:
            self._contacts_by_id[int(contact.id)] = contact
        except (ValueError, TypeError):
            pass

    async def list_contacts(self) -> list[ChatContact]:
        return list(self.contacts)

    # ─── Address book (rubrica completa) ──────────────────────────────────

    @staticmethod
    def _user_to_address_book_contact(user: Any) -> ChatContact:
        """Convert a Telethon ``User`` (or mock) into a rubrica ``ChatContact``.

        Extends ``_entity_to_contact`` with the address-book schema: normalized
        ``phone`` digits (may be ``""`` for contacts without a number, e.g.
        "Mamma Vod"), ``access_hash`` (required for ``InputPeerUser`` sends) and
        the ``address_book``/``source`` markers.
        """
        eid: int = getattr(user, "id", 0)
        first = getattr(user, "first_name", "") or ""
        last = getattr(user, "last_name", "") or ""
        username = getattr(user, "username", "") or ""
        raw_phone = getattr(user, "phone", "") or ""
        phone = "".join(ch for ch in raw_phone if ch.isdigit())
        name = (
            f"{first} {last}".strip()
            or username
            or (f"+{phone}" if phone else "")
            or str(eid)
        )
        return ChatContact(
            id=str(eid),
            display_name=name.strip() or str(eid),
            protocol=PROTOCOL_TELEGRAM,
            extras={
                "phone": phone,
                "username": username,
                "access_hash": str(getattr(user, "access_hash", "") or ""),
                "is_group": False,
                "address_book": True,
                "source": "tg_book",
            },
        )

    async def _fetch_address_book(self) -> list[ChatContact]:
        """Fetch the full Telegram contacts book via ``GetContactsRequest``.

        Runs on the dedicated Telethon event loop (scheduled from
        ``list_address_book_sync`` via ``run_coroutine_threadsafe``).  Skips
        bots and deleted accounts ("Deleted Account").
        """
        from telethon.tl.functions.contacts import GetContactsRequest

        result = await self._client(GetContactsRequest(hash=0))
        users = getattr(result, "users", []) or []
        contacts: list[ChatContact] = []
        for user in users:
            if getattr(user, "bot", False):
                continue
            first = getattr(user, "first_name", "") or ""
            last = getattr(user, "last_name", "") or ""
            name = f"{first} {last}".strip()
            if getattr(user, "deleted", False) or "deleted account" in name.lower():
                continue
            contacts.append(self._user_to_address_book_contact(user))
        return contacts

    def list_address_book_sync(self, force: bool = False) -> list[ChatContact]:
        """Telegram rubrica = contacts book ∪ dialogs (TTL-cached, never raises).

        Bloccante (chiamare da worker thread).  Esegue ``_fetch_address_book``
        sul loop Telethon dedicato via ``run_coroutine_threadsafe``; su errore
        remoto / non connesso serve la copia cached (stale) o ``[]``.
        """
        now = time.monotonic()
        if (
            not force
            and self._address_book is not None
            and (now - self._address_book_ts) < get_address_book_ttl_s()
        ):
            return list(self._address_book)

        if self._loop is None or self._client is None or not self._connected:
            return list(self._address_book) if self._address_book is not None else []

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._fetch_address_book(), self._loop
            )
            book = future.result(timeout=20)
        except Exception:
            logger.warning("Telegram address book build failed", exc_info=True)
            return list(self._address_book) if self._address_book is not None else []

        # Merge with dialogs (self.contacts, loaded at connect).  Book users
        # without a dialog stay as-is; groups/channels/non-book users with a
        # dialog are carried over from self.contacts only.
        dialogs_by_id: dict[int, ChatContact] = {}
        for c in self.contacts:
            try:
                dialogs_by_id[int(c.id)] = c
            except (ValueError, TypeError):
                continue

        entries: list[ChatContact] = []
        for cc in book:
            eid = int(cc.id)
            dialog = dialogs_by_id.pop(eid, None)
            if dialog is not None:
                cc.last_message_ts = dialog.last_message_ts
                cc.extras["is_chat_active"] = True
                read_max = dialog.extras.get("read_outbox_max_id")
                if read_max:
                    cc.extras["read_outbox_max_id"] = read_max
            entries.append(cc)

        for dialog in dialogs_by_id.values():
            entries.append(
                replace(
                    dialog,
                    extras={
                        **dialog.extras,
                        "address_book": True,
                        "is_chat_active": True,
                        "source": "tg_dialogs",
                    },
                )
            )

        self._address_book = entries
        self._address_book_ts = now

        # Extend the lookup index with book users (used by _identify_contact
        # and the send fallback) without touching self.contacts (main list).
        for cc in book:
            try:
                self._contacts_by_id.setdefault(int(cc.id), cc)
            except (ValueError, TypeError):
                continue

        return list(self._address_book)

    async def _resolve_input_entity(self, eid: int):
        """Resolve a Telegram user id to an entity usable by ``send_message``.

        1. ``get_input_entity(eid)`` — fast path (entity already in session).
        2. Fallback: ``InputPeerUser(eid, access_hash)`` from the address book.
        3. No ``access_hash`` available → ``RuntimeError`` with a clear message.
        """
        try:
            return await self._client.get_input_entity(eid)
        except Exception:
            logger.debug(
                "Telegram get_input_entity failed, falling back to address book",
                exc_info=True,
            )
            contact = self._contacts_by_id.get(eid)
            raw_hash = contact.extras.get("access_hash") if contact else None
            try:
                access_hash = int(raw_hash) if raw_hash else None
            except (ValueError, TypeError):
                access_hash = None
            if access_hash is None:
                raise RuntimeError(
                    f"Telegram: access_hash mancante per {eid}"
                ) from None
            from telethon.tl.types import InputPeerUser

            return InputPeerUser(eid, access_hash)

    # ─── Messaging ─────────────────────────────────────────────────────────

    async def send_message(
        self,
        contact_id: str,
        text: str,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_message: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> str:
        """Send a text message via Telethon (called from TUI thread)."""
        if self._loop is None or self._client is None:
            raise RuntimeError("Telegram backend not connected")

        async def _send() -> str:
            try:
                eid = int(contact_id)
            except (ValueError, TypeError):
                raise ValueError(f"Invalid Telegram contact id: {contact_id}")
            reply_to = self._validated_reply_to_message_id(reply_to_message_id)
            entity = await self._resolve_input_entity(eid)
            msg = await self._client.send_message(entity, text, reply_to=reply_to)
            return str(msg.id)

        future = asyncio.run_coroutine_threadsafe(_send(), self._loop)
        return future.result(timeout=30)

    async def mark_read(self, contact_id: str) -> None:
        """Mark messages as read.

        Telethon gestisce il read lato remoto; qui persistiamo anche
        ``read=1`` in SQLite (da cui web UI e TUI calcolano i badge non
        letti) via ``mark_read_sync``.
        """
        await asyncio.to_thread(self.mark_read_sync, contact_id)

    def send_message_sync(
        self,
        contact_id: str,
        text: str,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_message: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> str:
        """Synchronous send, for use from the TUI's sync callbacks."""
        if self._loop is None or self._client is None:
            raise RuntimeError("Telegram backend not connected")

        async def _send():
            try:
                eid = int(contact_id)
            except (ValueError, TypeError):
                raise ValueError(f"Invalid Telegram contact id: {contact_id}")
            reply_to = self._validated_reply_to_message_id(reply_to_message_id)
            entity = await self._resolve_input_entity(eid)
            msg = await self._client.send_message(entity, text, reply_to=reply_to)
            return str(msg.id)

        future = asyncio.run_coroutine_threadsafe(_send(), self._loop)
        return future.result(timeout=30)

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
        media_kind: str | None = None,
        filename: str | None = None,
    ) -> str:
        if self._loop is None or self._client is None:
            raise RuntimeError("Telegram backend not connected")

        async def _send() -> str:
            try:
                eid = int(contact_id)
            except (ValueError, TypeError):
                raise ValueError(f"Invalid Telegram contact id: {contact_id}")
            reply_to = self._validated_reply_to_message_id(reply_to_message_id)
            entity = await self._resolve_input_entity(eid)
            normalized_mime = mime_type.lower().split(";", 1)[0].strip()
            kind = media_kind or media_kind_from_mime(normalized_mime) or "document"
            kwargs = {
                "caption": caption or None,
                "reply_to": reply_to,
                "force_document": kind == "document",
                "voice_note": kind == "voice" and normalized_mime == "audio/ogg",
            }
            upload = str(file_path)
            if filename is not None:
                upload = await self._client.upload_file(upload, file_name=filename)
            msg = await self._client.send_file(entity, upload, **kwargs)
            return str(msg.id)

        future = asyncio.run_coroutine_threadsafe(_send(), self._loop)
        return future.result(timeout=self.attachment_send_timeout)

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
        media_kind: str | None = None,
        filename: str | None = None,
    ) -> None:
        is_attachment = attachment_path is not None
        media_kind = (
            media_kind or media_kind_from_mime(mime_type) or "document"
            if is_attachment
            else None
        )
        msg_type = msg_type_for_media_kind(media_kind) if media_kind else "text"
        media_text = "" if msg_type == "image" else text
        attachment_id = None
        if attachment_path is not None:
            media_dir = _media_dir()
            media_dir.mkdir(parents=True, exist_ok=True)
            suffix = attachment_path.suffix.lower()
            persisted = media_dir / f"{int(contact_id)}-{int(message_id)}-sent{suffix}"
            shutil.copy2(attachment_path, persisted)
            attachment_id = str(persisted)
        self._events.put(
            ChatEvent(
                type="message",
                protocol=self.protocol,
                contact_id=contact_id,
                payload={
                    "id": str(message_id),
                    "text": media_text,
                    "is_mine": True,
                    "sender": "You",
                    "timestamp": int(time.time() * 1000),
                    "quote_text": quote_message,
                    "quote_timestamp": quote_timestamp,
                    "quote_author": quote_author,
                    "reply_to_message_id": reply_to_message_id,
                    "msg_type": msg_type,
                    "attachment_info": (
                        (text or filename or None)
                        if msg_type == "image"
                        else (filename or text or None)
                    )
                    if is_attachment
                    else None,
                    "attachment_id": attachment_id,
                    "content_type": mime_type,
                    "media_kind": media_kind,
                },
            )
        )

    def edit_message_sync(
        self, contact_id: str, message_id: str, new_text: str
    ) -> bool:
        """Edit via Telethon; gira sul loop dedicato (pattern di send_message_sync)."""
        if self._loop is None or self._client is None:
            raise RuntimeError("Telegram backend not connected")
        try:
            eid = int(contact_id)
            mid = int(message_id)
        except (TypeError, ValueError):
            raise ValueError("Invalid Telegram contact/message id") from None
        if mid <= 0:
            raise ValueError("Invalid Telegram message id")

        async def _edit() -> bool:
            entity = await self._resolve_input_entity(eid)
            await self._client.edit_message(entity, mid, new_text)
            return True

        future = asyncio.run_coroutine_threadsafe(_edit(), self._loop)
        return future.result(timeout=30)

    def send_reaction_sync(
        self,
        contact_id: str,
        message_id: str,
        emoji: str,
        *,
        target_author: str | None = None,
    ) -> bool:
        if self._loop is None or self._client is None:
            return False
        try:
            entity_id = int(contact_id)
            target_id = int(message_id)
        except (TypeError, ValueError):
            return False
        if target_id <= 0:
            return False

        async def _send_reaction() -> bool:
            from telethon.tl.functions.messages import SendReactionRequest
            from telethon.tl.types import ReactionEmoji

            entity = await self._resolve_input_entity(entity_id)
            await self._client(
                SendReactionRequest(
                    peer=entity,
                    msg_id=target_id,
                    reaction=[ReactionEmoji(emoticon=emoji)],
                )
            )
            return True

        try:
            future = asyncio.run_coroutine_threadsafe(_send_reaction(), self._loop)
            return future.result(timeout=30)
        except Exception:
            logger.debug("Telegram reaction send failed", exc_info=True)
            return False

    def get_available_reactions(self) -> list[str]:
        now = time.monotonic()
        if (
            self._available_reactions is not None
            and now - self._available_reactions_ts < _AVAILABLE_REACTIONS_TTL_S
        ):
            return list(self._available_reactions)
        if self._loop is None or self._client is None:
            return []

        async def _fetch() -> list[str]:
            from telethon.tl.functions.messages import GetAvailableReactionsRequest
            from telethon.tl.types import ReactionEmoji

            result = await self._client(GetAvailableReactionsRequest(hash=0))
            emojis: list[str] = []
            for available in getattr(result, "reactions", []) or []:
                reaction = getattr(available, "reaction", None)
                if isinstance(reaction, ReactionEmoji):
                    emoticon = reaction.emoticon
                elif isinstance(reaction, str):
                    emoticon = reaction
                else:
                    continue
                if emoticon:
                    emojis.append(emoticon)
            return emojis

        try:
            future = asyncio.run_coroutine_threadsafe(_fetch(), self._loop)
            emojis = future.result(timeout=20)
        except Exception:
            logger.debug("Telegram available reactions fetch failed", exc_info=True)
            return []
        self._available_reactions = list(emojis)
        self._available_reactions_ts = now
        return list(emojis)

    @staticmethod
    def _validated_reply_to_message_id(reply_to_message_id: str | None) -> int | None:
        """Return a valid Telegram reply id, rejecting invalid reply targets."""
        if reply_to_message_id is None:
            return None
        try:
            reply_to = int(reply_to_message_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid Telegram reply message id") from exc
        if isinstance(reply_to_message_id, bool) or reply_to <= 0:
            raise ValueError("Invalid Telegram reply message id")
        return reply_to

    def mark_read_sync(self, contact_id: str) -> None:
        """Synchronous mark-read — persists read status to SQLite."""
        try:
            from protocols.db import _mark_as_read

            _mark_as_read(contact_id, protocol=PROTOCOL_TELEGRAM)
        except Exception as _e:
            logger.debug("Telegram mark-read persistence failed", exc_info=True)

    # ─── Event reception ───────────────────────────────────────────────────

    async def receive(self):
        """Yield ChatEvent objects (contract — unused, poll_once is used)."""
        if False:
            yield

    async def _handle_new_message(self, event: Any) -> None:
        """Telethon event handler: normalise and enqueue a new message."""
        msg = event.message
        logger.info(
            "Telegram: NewMessage event, msg_id=%s chat_id=%s",
            getattr(msg, "id", "?"),
            getattr(msg, "chat_id", "?"),
        )
        if msg is None:
            return

        # Download media attachments (photos, documents) for inline viewing
        attachment_id: str | None = None
        if msg.photo or msg.document:
            try:
                media_dir = _media_dir()
                media_dir.mkdir(parents=True, exist_ok=True)
                path = await msg.download_media(file=str(media_dir))
                if path:
                    attachment_id = str(path)
                    logger.info("Telegram: downloaded media to %s", path)
            except Exception:
                logger.exception("Telegram: failed to download media")

        evt = self._message_to_chat_event(msg, attachment_id=attachment_id)
        if evt is not None:
            self._events.put(evt)
            logger.info("Telegram: enqueued event for chat %s", evt.contact_id)

    async def _handle_message_edited(self, event: Any) -> None:
        """Telethon event handler: normalise and enqueue an edited message."""
        msg = event.message
        if msg is None or msg.chat_id is None:
            return
        # Solo testo: caption/media edit fuori scope.
        if (
            msg.photo
            or msg.document
            or msg.sticker
            or msg.video
            or msg.voice
            or msg.audio
        ):
            return
        new_text = msg.text or ""
        if not new_text.strip():
            return  # edit di sola formattazione/altro
        chat_id = str(msg.chat_id)
        ts = int(msg.date.timestamp() * 1000) if msg.date else 0
        edit_ts = (
            int(msg.edit_date.timestamp() * 1000)
            if getattr(msg, "edit_date", None)
            else None
        )
        self._events.put(
            ChatEvent(
                type="message_edit",
                protocol=PROTOCOL_TELEGRAM,
                contact_id=chat_id,
                payload={
                    "edit_message_id": str(msg.id),
                    "text": new_text,
                    "timestamp": ts,  # msg.date = ts ORIGINALE
                    "edit_timestamp": edit_ts,
                    "is_mine": bool(getattr(msg, "out", False)),
                    "sender": "",
                    "contact": self._identify_contact(chat_id),
                    "msg_type": "text",
                },
            )
        )

    def _message_to_chat_event(
        self, msg: Any, attachment_id: str | None = None
    ) -> ChatEvent | None:
        """Convert a Telethon ``Message`` (or mock) into a ``ChatEvent``."""
        try:
            chat_id = str(msg.chat_id) if msg.chat_id else None
        except Exception as _e:
            logger.debug("Failed to read message chat_id", exc_info=True)
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
        kind, filename, mime = _tg_media_kind(msg)
        content_type: str | None = None
        if kind:
            msg_type = msg_type_for_media_kind(kind)
            attachment_info = text or _tg_label_for(kind, filename)
            if kind in ("image", "sticker"):
                text = ""
            content_type = mime or None

        # Lazy-download fallback: photos and documents without a downloaded
        # path (history fetch, failed live download) get a ``tgref:`` reference.
        if not att_id and (msg.photo or msg.document) and msg.id:
            att_id = self._media_ref(chat_id, msg.id)

        # Quote / reply
        quote_text: str | None = None
        quote_attachment_id: str | None = None
        quote_content_type: str | None = None
        reply_to_message_id: str | None = None
        if msg.reply_to and getattr(msg.reply_to, "reply_to_msg_id", None):
            reply_to_message_id = str(msg.reply_to.reply_to_msg_id)
            cached = self.cache.get(chat_id, [])
            for m in cached:
                if str(m.get("id")) == str(msg.reply_to.reply_to_msg_id):
                    quote_text = _tg_quote_text_from_cached(m)
                    # Best-effort quoted-attachment metadata: the cached target
                    # may carry a resolvable attachment id (tgref).  The path is
                    # NOT resolved here (lazy download is get_attachment_path's).
                    quote_attachment_id = m.get("attachment_id")
                    quote_content_type = m.get("content_type")
                    break

        if (
            text == ""
            and not any(
                (msg.photo, msg.document, msg.sticker, msg.video, msg.voice, msg.audio)
            )
            and not msg.reply_to
        ):
            return None

        payload: dict[str, Any] = {
            "id": str(msg.id),
            "text": text,
            "is_mine": is_mine,
            "sender": sender,
            "timestamp": ts,
            "quote_text": quote_text,
            "quote_attachment_id": quote_attachment_id,
            "quote_content_type": quote_content_type,
            "reply_to_message_id": reply_to_message_id,
            "msg_type": msg_type,
            "attachment_info": attachment_info,
            "attachment_id": att_id,
            "media_kind": kind,
            "content_type": content_type,
            "protocol": PROTOCOL_TELEGRAM,
            "contact": self._identify_contact(chat_id),
        }

        event = ChatEvent(
            type="message",
            protocol=PROTOCOL_TELEGRAM,
            contact_id=chat_id,
            payload=payload,
        )

        reaction_event = self._reactions_event_from_msg(msg, chat_id)
        if reaction_event is not None:
            self._events.put(reaction_event)

        return event

    async def _handle_read_receipt(self, update: Any) -> None:
        """Translate ``UpdateReadHistoryOutbox`` into a generic receipt event.

        Does not mutate caches or SQLite directly: that is the responsibility
        of ``process_receipt`` on the UI thread.

        Protocol limitation (by design): MTProto cloud chats expose no delivery
        confirmation, only the read receipt handled here.  No synthetic
        ``delivered`` event is emitted for Telegram — outgoing messages go
        straight from ``sent`` to ``read`` when this update arrives.
        """
        from telethon.tl.types import PeerChannel, PeerChat, PeerUser

        peer = update.peer
        if isinstance(peer, PeerUser):
            contact_id = str(peer.user_id)
        elif isinstance(peer, PeerChat):
            # Match the convention used by Message.chat_id for legacy groups.
            contact_id = str(-peer.chat_id)
        elif isinstance(peer, PeerChannel):
            # Match the convention used by Message.chat_id for channels.
            contact_id = str(-1000000000000 - peer.channel_id)
        else:
            return

        max_id = update.max_id
        logger.info("Telegram read receipt: contact=%s max_id=%s", contact_id, max_id)

        # Resolve which messages belong to this contact and are <= max_id.
        message_ids: list[str] = []
        for msg in self.cache.get(contact_id, []):
            mid = msg.get("id")
            if mid and int(mid) <= max_id and msg.get("is_mine"):
                message_ids.append(str(mid))

        if message_ids:
            self._events.put(
                ChatEvent(
                    type="receipt",
                    protocol=PROTOCOL_TELEGRAM,
                    contact_id=contact_id,
                    payload={"message_ids": message_ids, "is_read": True},
                )
            )

    async def _handle_reactions_update(self, update: Any) -> None:
        """Translate a Telegram reaction snapshot into a generic chat event."""
        from telethon.tl.types import PeerChannel, PeerChat, PeerUser

        peer = update.peer
        if isinstance(peer, PeerUser):
            contact_id = str(peer.user_id)
        elif isinstance(peer, PeerChat):
            contact_id = str(-peer.chat_id)
        elif isinstance(peer, PeerChannel):
            contact_id = str(-1000000000000 - peer.channel_id)
        else:
            return

        reactions = getattr(update, "reactions", None)
        if reactions is None:
            logger.debug(
                "Telegram reaction update without snapshot: peer=%s msg_id=%s",
                type(peer).__name__,
                getattr(update, "msg_id", None),
            )
            return
        snapshot = self._build_reactions_snapshot(reactions)
        timestamp = int(time.time() * 1000)
        self._events.put(
            ChatEvent(
                type="reaction_update",
                protocol=PROTOCOL_TELEGRAM,
                contact_id=contact_id,
                payload={
                    "mode": "snapshot",
                    "snapshot": snapshot,
                    "target_message_id": str(update.msg_id),
                    "target_timestamp": None,
                    "timestamp": timestamp,
                    "contact": self._identify_contact(contact_id),
                },
            )
        )

    def _reactions_event_from_msg(self, msg: Any, chat_id: str) -> ChatEvent | None:
        """Build a reaction snapshot event from a Telethon message."""
        reactions = getattr(msg, "reactions", None)
        results = getattr(reactions, "results", None) or []
        if not results:
            return None

        logger.debug(
            "Telegram reaction from msg: msg_id=%s results=%d",
            getattr(msg, "id", None),
            len(results),
        )
        return ChatEvent(
            type="reaction_update",
            protocol=PROTOCOL_TELEGRAM,
            contact_id=chat_id,
            payload={
                "mode": "snapshot",
                "snapshot": self._build_reactions_snapshot(reactions),
                "target_message_id": str(msg.id),
                "target_timestamp": None,
                "timestamp": int(time.time() * 1000),
                "contact": self._identify_contact(chat_id),
            },
        )

    def _build_reactions_snapshot(self, reactions: Any) -> list[dict[str, Any]]:
        """Convert Telethon reaction counts into a renderable snapshot."""
        from telethon.tl.types import (
            PeerChannel,
            PeerChat,
            PeerUser,
            ReactionCustomEmoji,
            ReactionEmoji,
        )

        all_recent_reactions = reactions.recent_reactions or []
        recent_reactions = (
            reactions.recent_reactions if reactions.can_see_list else []
        ) or []
        snapshot: list[dict[str, Any]] = []

        for reaction_count in reactions.results or []:
            reaction = reaction_count.reaction
            if isinstance(reaction, ReactionCustomEmoji):
                logger.debug(
                    "Telegram: skipping custom emoji reaction document_id=%s",
                    reaction.document_id,
                )
                continue
            if not isinstance(reaction, ReactionEmoji):
                continue

            emoticon = reaction.emoticon
            matching_recent_reactions = [
                peer_reaction
                for peer_reaction in all_recent_reactions
                if isinstance(peer_reaction.reaction, ReactionEmoji)
                and peer_reaction.reaction.emoticon == emoticon
            ]
            authors: list[str] = []
            for peer_reaction in recent_reactions:
                recent = peer_reaction.reaction
                if not isinstance(recent, ReactionEmoji):
                    continue
                if recent.emoticon != emoticon:
                    continue

                author_peer = peer_reaction.peer_id
                if isinstance(author_peer, PeerUser):
                    peer_id = author_peer.user_id
                    author_contact_id = str(peer_id)
                elif isinstance(author_peer, PeerChat):
                    peer_id = author_peer.chat_id
                    author_contact_id = str(-peer_id)
                elif isinstance(author_peer, PeerChannel):
                    peer_id = author_peer.channel_id
                    author_contact_id = str(-1000000000000 - peer_id)
                else:
                    continue

                contact = self._contacts_by_id.get(peer_id) or self._identify_contact(
                    author_contact_id
                )
                author = contact.display_name if contact is not None else str(peer_id)
                if author and author not in authors:
                    authors.append(author)

            snapshot.append(
                {
                    "emoji": emoticon,
                    "count": reaction_count.count,
                    "is_mine": reaction_count.chosen_order is not None
                    or any(
                        getattr(peer_reaction, "my", False)
                        for peer_reaction in matching_recent_reactions
                    ),
                    "authors": authors,
                }
            )
        return snapshot

    async def _handle_typing_update(self, update: Any) -> None:
        """Translate MTProto typing updates into a generic ``ChatEvent``.

        Pure translator: does not mutate caches or SQLite — it only enqueues a
        ``ChatEvent(type="typing", ...)`` for the UI, same shape as
        ``_handle_read_receipt``.  Telegram re-sends the chat-action every ~5 s
        while the contact is composing, so no backend-side dedup is applied:
        the UI refreshes the ``✍️`` keep-alive on each STARTED and coalesces the
        noise (see ``tui/events.py::_handle_typing_event``).
        """
        from telethon.tl.types import (
            SendMessageCancelAction,
            UpdateChannelUserTyping,
            UpdateChatUserTyping,
            UpdateUserTyping,
        )

        if isinstance(update, UpdateUserTyping):
            contact_id = str(update.user_id)
            action = update.action
        elif isinstance(update, UpdateChatUserTyping):
            # Legacy groups: the indicator is per-chat (not per-actor), so the
            # chat id is used, matching the Message.chat_id convention.
            contact_id = str(-update.chat_id)
            action = update.action
        elif isinstance(update, UpdateChannelUserTyping):
            # Channels/supergroups: match the Message.chat_id convention.
            contact_id = str(-1000000000000 - update.channel_id)
            action = update.action
        else:
            return

        # Any "SendMessage*Action" other than cancel signals active composing.
        typing_action = (
            "STOPPED" if isinstance(action, SendMessageCancelAction) else "STARTED"
        )

        self._events.put(
            ChatEvent(
                type="typing",
                protocol=PROTOCOL_TELEGRAM,
                contact_id=contact_id,
                payload={"action": typing_action},
            )
        )

    # ─── poll_once (queue drain) ───────────────────────────────────────────

    def process_receipt(self, envelope: dict) -> list[dict]:
        """Handle a receipt batch: the only mutator for Telegram receipts.

        Matches messages by their stable ``msg_id`` within the chat specified
        by ``envelope["contact_id"]`` (if provided), advances the status with
        a rank guard (pending/failed < sent < delivered < read), persists the
        change via ``_update_message_status_by_id``, and returns the updated
        entries so the UI can refresh its own cache and widgets.

        Protocol limitation (by design): MTProto cloud chats have no delivery
        confirmation, so no Telegram producer emits ``is_read=False`` (target
        ``"delivered"``).  The branch is kept for protocol-agnostic completeness
        and would light up automatically if such a producer were ever added.
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
        id_set = {str(i) for i in ids}
        scoped_contact = envelope.get("contact_id")

        updated: list[dict] = []
        contacts_to_scan = [scoped_contact] if scoped_contact else self.cache.keys()
        for contact_id in contacts_to_scan:
            if contact_id not in self.cache:
                continue
            for msg in self.cache.get(contact_id, []):
                if not msg.get("is_mine"):
                    continue
                mid = str(msg.get("id", ""))
                if mid not in id_set:
                    continue
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
                    try:
                        from protocols.db import _update_message_status_by_id

                        _update_message_status_by_id(
                            mid,
                            target,
                            protocol=PROTOCOL_TELEGRAM,
                            contact_number=contact_id,
                        )
                    except Exception:
                        logger.exception(
                            "Telegram: _update_message_status_by_id failed"
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
            from protocols.db import _load_cache as _load_sqlite_cache

            cache = _load_sqlite_cache(protocol=PROTOCOL_TELEGRAM)
            self._seen_msg_ids = {
                str(m.get("id")) for msgs in cache.values() for m in msgs if m.get("id")
            }
            return cache
        except Exception:
            logger.exception("Telegram: failed to load protocol cache")
            self._seen_msg_ids = set()
            return {}

    def _persist_message(self, contact_id: str, data: dict, ts: int) -> None:
        """Persist a message to the SQLite cache (Telegram protocol)."""
        from protocols.db import _add_message_to_cache

        _add_message_to_cache(
            contact_id,
            data.get("text", ""),
            data.get("is_mine", False),
            data.get("sender", ""),
            ts,
            quote_text=data.get("quote_text"),
            msg_type=data.get("msg_type", "text"),
            attachment_info=data.get("attachment_info"),
            attachment_id=data.get("attachment_id"),
            content_type=data.get("content_type"),
            media_kind=data.get("media_kind"),
            protocol=PROTOCOL_TELEGRAM,
            msg_id=data.get("id"),
            status=data.get("status"),
            quote_timestamp=data.get("quote_timestamp"),
            quote_author=data.get("quote_author"),
            reply_to_message_id=data.get("reply_to_message_id"),
            quote_attachment_id=data.get("quote_attachment_id"),
            quote_attachment_path=data.get("quote_attachment_path"),
            quote_content_type=data.get("quote_content_type"),
        )

    def ingest_message(
        self, contact_id: str, data: dict, ts: int, persist: bool = True
    ) -> bool | Literal["changed"]:
        """Add a message to the in-memory cache AND SQLite with dedup.

        When ``persist=False`` the in-memory cache is still seeded (dedup
        keeps working on the UI thread) but the SQLite write is skipped;
        the caller is responsible for calling ``_persist_message`` later.

        Returns True when added, ``"changed"`` for an attachment upgrade,
        and False for an unchanged duplicate.
        """
        from protocols.db import (
            _update_message_attachment_id,
            _update_message_attachment_info,
            _update_message_id,
        )

        if data.get("msg_type") == "image":
            data = {**data, "text": ""}
        mid = data.get("id")
        text = data.get("text", "")

        # (1) Cross-session dedup by stable msg_id.  Never touch status on hit.
        if mid:
            if mid in self._seen_msg_ids:
                entry = next(
                    (
                        m
                        for m in self.cache.get(contact_id, [])
                        if str(m.get("id") or "") == str(mid)
                    ),
                    None,
                )
                if (
                    entry is not None
                    and entry.get("msg_type", "text") == "text"
                    and entry.get("text", "") != text
                    and text
                ):
                    self.apply_edit(contact_id, str(mid), text)
                incoming_attachment = data.get("attachment_id")
                if (
                    entry is not None
                    and data.get("is_mine")
                    and entry.get("is_mine")
                    and str(entry.get("attachment_id") or "").startswith(_TGREF_PREFIX)
                    and incoming_attachment
                    and should_upgrade_outgoing_attachment(
                        is_mine=True,
                        existing_path=entry.get("attachment_id"),
                        incoming_path=incoming_attachment,
                    )
                ):
                    logger.info(
                        "telegram ingest: echo is_mine id=%s att_incoming=%s att_existing=%s upgrade=%s",
                        mid,
                        incoming_attachment,
                        entry.get("attachment_id"),
                        True,
                    )
                    entry["attachment_id"] = incoming_attachment
                    _update_message_attachment_id(
                        PROTOCOL_TELEGRAM,
                        contact_id,
                        str(mid),
                        int(entry.get("timestamp", ts)),
                        incoming_attachment,
                    )
                    attachment_changed = True
                else:
                    attachment_changed = False
                incoming_info = data.get("attachment_info")
                caption_changed = bool(
                    entry is not None
                    and data.get("msg_type") == "image"
                    and is_caption_like(incoming_info)
                    and not is_caption_like(entry.get("attachment_info"))
                )
                if caption_changed:
                    entry["attachment_info"] = incoming_info
                    _update_message_attachment_info(
                        PROTOCOL_TELEGRAM,
                        contact_id,
                        str(mid),
                        int(entry.get("timestamp", ts)),
                        incoming_info,
                    )
                if attachment_changed or caption_changed:
                    return "changed"
                if (
                    entry is not None
                    and data.get("is_mine")
                    and entry.get("is_mine")
                    and incoming_attachment
                    and (
                        str(incoming_attachment).startswith(_TGREF_PREFIX)
                        or str(entry.get("attachment_id") or "").startswith(
                            _TGREF_PREFIX
                        )
                    )
                ):
                    logger.info(
                        "telegram ingest: echo is_mine id=%s att_incoming=%s att_existing=%s upgrade=%s",
                        mid,
                        incoming_attachment,
                        entry.get("attachment_id"),
                        False,
                    )
                return False
            for m in self.cache.get(contact_id, []):
                if (
                    not m.get("id")
                    and m.get("text") == text
                    and abs(int(m.get("timestamp", 0)) - ts)
                    <= _INCOMING_DEDUP_WINDOW_MS
                ):
                    # Echo of an optimistic message: attach the real id, keep
                    # the original optimistic timestamp and status intact.
                    m["id"] = mid
                    self._seen_msg_ids.add(mid)
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
                    incoming_info = data.get("attachment_info")
                    if (
                        data.get("msg_type") == "image"
                        and is_caption_like(incoming_info)
                        and not is_caption_like(m.get("attachment_info"))
                    ):
                        m["attachment_info"] = incoming_info
                        _update_message_attachment_info(
                            PROTOCOL_TELEGRAM,
                            contact_id,
                            str(mid),
                            int(m.get("timestamp", ts)),
                            incoming_info,
                        )
                        return "changed"
                    return False
            self._seen_msg_ids.add(mid)
        else:
            # (2) Fallback dedup for id-less optimistic rows only.
            for m in self.cache.get(contact_id, []):
                if (
                    m.get("text") == text
                    and abs(int(m.get("timestamp", 0)) - ts)
                    <= _INCOMING_DEDUP_WINDOW_MS
                ):
                    return False

        # Persist to SQLite (same pattern as Signal/WhatsApp backends)
        if persist:
            try:
                self._persist_message(contact_id, data, ts)
            except Exception:
                logger.exception("Telegram: _add_message_to_cache failed")

        if contact_id not in self.cache:
            self.cache[contact_id] = []

        self.cache[contact_id].append(
            {
                "id": mid,
                "text": text,
                "is_mine": data.get("is_mine", False),
                "sender": data.get("sender", ""),
                "timestamp": ts,
                "quote_text": data.get("quote_text"),
                "msg_type": data.get("msg_type", "text"),
                "attachment_info": data.get("attachment_info"),
                "attachment_id": data.get("attachment_id"),
                "content_type": data.get("content_type"),
                "read": data.get("is_mine", False),  # incoming = unread
                "status": data.get("status", "sent" if data.get("is_mine") else "read"),
                "quote_timestamp": data.get("quote_timestamp"),
                "quote_author": data.get("quote_author"),
                "reply_to_message_id": data.get("reply_to_message_id"),
                "quote_attachment_id": data.get("quote_attachment_id"),
                "quote_attachment_path": data.get("quote_attachment_path"),
                "quote_content_type": data.get("quote_content_type"),
            }
        )

        # Keep cache bounded
        if len(self.cache[contact_id]) > _MAX_CACHE_PER_CONTACT:
            self.cache[contact_id] = self.cache[contact_id][-_MAX_CACHE_PER_CONTACT:]

        return True

    def apply_edit(
        self,
        contact_id: str,
        message_id: str,
        new_text: str,
        *,
        is_mine: bool | None = None,
        edit_timestamp: int | None = None,
        mark_edited: bool = True,
    ) -> dict | None:
        from protocols.db import _update_message_text

        for msg in self.cache.get(contact_id, []):
            if str(msg.get("id") or "") != str(message_id):
                continue
            if is_mine is not None and bool(msg.get("is_mine")) != bool(is_mine):
                continue
            if msg.get("msg_type", "text") != "text":
                return None
            old_text = msg.get("text", "")
            if old_text == new_text:
                return None  # echo del nostro edit: no-op
            msg["text"] = new_text
            msg["edited"] = mark_edited
            _update_message_text(
                contact_id,
                new_text,
                protocol=PROTOCOL_TELEGRAM,
                msg_id=str(message_id),
                mark_edited=mark_edited,
            )
            return {
                "message_id": str(message_id),
                "timestamp": int(msg.get("timestamp") or 0),
                "old_text": old_text,
                "text": new_text,
                "is_mine": bool(msg.get("is_mine")),
            }
        return None

    def apply_reaction(self, contact_id: str, payload: dict) -> dict | None:
        """Persist a complete Telegram reaction snapshot."""
        if payload.get("mode") != "snapshot":
            return None

        from protocols.db import (
            _reactions_for_contact,
            _replace_reactions_snapshot,
            _resolve_reaction_target_row,
        )

        target_message_id = str(payload.get("target_message_id") or "")
        if not target_message_id:
            return None

        target_ts: int | None = None
        target_resolved = False
        for msg in self.cache.get(contact_id, []):
            if str(msg.get("id")) == target_message_id:
                target_ts = int(msg.get("timestamp") or 0)
                target_resolved = True
                break
        if target_ts is None:
            target = _resolve_reaction_target_row(
                PROTOCOL_TELEGRAM,
                contact_id,
                target_message_id,
                None,
            )
            if target is not None:
                target_ts = int(target.get("timestamp") or 0)
                target_resolved = True
            else:
                target_ts = int(payload.get("target_timestamp") or 0) or None

        entries = [
            {
                "emoji": item["emoji"],
                "count": int(item["count"]),
                "is_mine": bool(item["is_mine"]),
                "author": ", ".join(item.get("authors") or []),
                "author_key": f"__agg__:{item['emoji']}",
            }
            for item in payload.get("snapshot") or []
        ]
        changed = _replace_reactions_snapshot(
            protocol=PROTOCOL_TELEGRAM,
            contact=contact_id,
            target_msg_id=target_message_id,
            target_ts=target_ts,
            entries=entries,
            ts=int(payload.get("timestamp") or time.time() * 1000),
        )
        if not changed or not target_resolved:
            return None

        aggregate: dict[str, dict[str, Any]] = {}
        for row in _reactions_for_contact(PROTOCOL_TELEGRAM, contact_id):
            if str(row.get("target_msg_id") or "") != target_message_id:
                continue
            emoji = row["emoji"]
            item = aggregate.setdefault(
                emoji,
                {
                    "emoji": emoji,
                    "count": 0,
                    "is_mine": False,
                    "authors": [],
                    "timestamp": int(row.get("timestamp") or 0),
                },
            )
            item["count"] += int(row.get("count") or 0)
            item["is_mine"] = item["is_mine"] or bool(row.get("is_mine"))
            item["timestamp"] = min(item["timestamp"], int(row.get("timestamp") or 0))
            author = row.get("author") or ""
            if author and author not in item["authors"]:
                item["authors"].append(author)

        ordered = sorted(
            aggregate.values(), key=lambda item: (-item["count"], item["timestamp"])
        )
        reactions = [
            {
                "emoji": item["emoji"],
                "count": item["count"],
                "is_mine": item["is_mine"],
                "authors": item["authors"],
            }
            for item in ordered
        ]
        return {
            "message_id": target_message_id,
            "timestamp": target_ts,
            "reactions": reactions,
        }

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
        # No session file → definitely needs pairing; otherwise let
        # _connect_sync verify the existing session.
        return not Path(self._session_path).exists()

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
            except Exception as _e:
                logger.debug("Failed to stop previous event loop", exc_info=True)
            self._loop.close()
            self._loop = None

        loop = asyncio.new_event_loop()
        self._loop = loop

        client = TelegramClient(
            self._session_path,
            self._api_id,
            self._api_hash,
            loop=loop,
        )
        self._client = client

        try:
            loop.run_until_complete(client.connect())
        except Exception as exc:
            logger.exception("Telegram QR connect failed")
            loop.close()
            self._loop = None
            self._client = None
            return f"ERROR: {exc}"

        if loop.run_until_complete(client.is_user_authorized()):
            client.disconnect()
            loop.close()
            self._loop = None
            self._client = None
            self._connected = True
            return "INFO: Already logged in"

        try:
            qr_login = loop.run_until_complete(client.qr_login())
        except Exception as exc:
            logger.exception("Telegram QR login start failed")
            client.disconnect()
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
            except Exception:
                logger.exception("Telegram QR login wait failed")
            finally:
                # Only cleanup if not waiting for 2FA
                if not self._needs_2fa:
                    try:
                        client.disconnect()
                    except Exception as _e:
                        logger.debug(
                            "Telegram QR cleanup disconnect failed", exc_info=True
                        )
                    loop.close()

        self._loop_thread = threading.Thread(
            target=_wait_thread,
            name="telegram-qr-wait",
            daemon=True,
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
                self._client.disconnect()
            except Exception as _e:
                logger.debug("Telegram 2FA cleanup disconnect failed", exc_info=True)
            self._loop.close()
            self._loop = None
            self._loop_thread = None
            return True
        except Exception:
            logger.exception("Telegram 2FA sign_in failed")
            self._needs_2fa = False
            return False
