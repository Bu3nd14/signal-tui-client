"""
Regression tests for the Telegram backend (backends/telegram.py).

Tests the normalization layer (Telethon objects → ChatContact/ChatEvent),
the event queue bridge, cache/dedup logic, and the thread boundary contract.

Telethon objects are mocked — no real MTProto connection is required.
"""

from __future__ import annotations

import queue
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backends.telegram import TelegramBackend
from models import (
    PROTOCOL_TELEGRAM,
    ChatEvent,
    contact_cache_key,
)

# ─── Telethon mock objects ────────────────────────────────────────────────


@dataclass
class MockTelethonUser:
    """Mimics telethon.tl.types.User."""

    id: int
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    phone: str = ""


@dataclass
class MockTelethonChat:
    """Mimics telethon.tl.types.Chat (group)."""

    id: int
    title: str = ""


@dataclass
class MockTelethonChannel:
    """Mimics telethon.tl.types.Channel."""

    id: int
    title: str = ""


class MockTelethonDate:
    """Mimics datetime.datetime for timestamp conversion."""

    def __init__(self, ts_ms: int):
        self._ts = ts_ms / 1000.0

    def timestamp(self) -> float:
        return self._ts


class MockTelethonMessage:
    """Mimics telethon.tl.types.Message."""

    def __init__(
        self,
        *,
        id: int,
        chat_id: int,
        text: str = "",
        out: bool = False,
        sender=None,
        date: MockTelethonDate | None = None,
        photo=None,
        sticker=None,
        document=None,
        video=None,
        audio=None,
        voice=None,
        file=None,
        reply_to=None,
    ):
        self.id = id
        self.chat_id = chat_id
        self.text = text
        self.out = out
        self.sender = sender
        self.date = date or MockTelethonDate(0)
        self.photo = photo
        self.sticker = sticker
        self.document = document
        self.video = video
        self.audio = audio
        self.voice = voice
        self.file = file
        self.reply_to = reply_to


class MockTelethonDocument:
    """Mimics telethon.tl.types.Document."""

    def __init__(self, *, attributes=None, mime_type: str = ""):
        self.attributes = attributes or []
        self.mime_type = mime_type


class DocumentAttributeSticker:
    pass


class DocumentAttributeFilename:
    def __init__(self, file_name: str):
        self.file_name = file_name


class DocumentAttributeVideo:
    pass


class DocumentAttributeVoice:
    pass


class DocumentAttributeAudio:
    pass


class MockReplyTo:
    """Mimics telethon.tl.types.MessageReplyHeader."""

    def __init__(self, reply_to_msg_id: int | None = None):
        self.reply_to_msg_id = reply_to_msg_id


# ─── Test helpers ─────────────────────────────────────────────────────────


def _make_backend() -> TelegramBackend:
    """Create a TelegramBackend with Telethon client mocked."""
    backend = TelegramBackend.__new__(TelegramBackend)
    backend._client = None
    backend._events = queue.Queue()
    backend._loop = None
    backend._loop_thread = None
    backend._running = False
    backend._connected = False
    backend.contacts = []
    backend.cache = {}
    backend._contacts_by_id = {}
    backend._seen_msg_ids = set()
    return backend


# ─── Contact mapping ──────────────────────────────────────────────────────


class TestContactMapping:
    """📨 Mappatura Telethon User/Chat/Channel → ChatContact."""

    def test_contact_from_user(self):
        """User Telethon → ChatContact con tutti i campi."""
        backend = _make_backend()
        user = MockTelethonUser(
            id=123456789,
            first_name="Mario",
            last_name="Rossi",
            username="mario_rossi",
            phone="+391234567890",
        )
        contact = backend._entity_to_contact(user)
        assert contact.id == "123456789"
        assert contact.display_name == "Mario Rossi"
        assert contact.protocol == PROTOCOL_TELEGRAM
        assert contact.extras["username"] == "mario_rossi"
        assert contact.extras["phone"] == "+391234567890"
        assert contact.extras["is_group"] is False

    def test_contact_from_user_no_last_name(self):
        backend = _make_backend()
        user = MockTelethonUser(id=42, first_name="Alice")
        assert backend._entity_to_contact(user).display_name == "Alice"

    def test_contact_from_user_username_fallback(self):
        backend = _make_backend()
        user = MockTelethonUser(id=42, username="ghost_user")
        assert backend._entity_to_contact(user).display_name == "ghost_user"

    def test_contact_from_user_id_fallback(self):
        backend = _make_backend()
        user = MockTelethonUser(id=987654321)
        assert backend._entity_to_contact(user).display_name == "987654321"

    def test_contact_from_chat(self):
        backend = _make_backend()
        chat = MockTelethonChat(id=-456789, title="Famiglia")
        contact = backend._entity_to_contact(chat)
        assert contact.id == "-456789"
        assert contact.display_name == "Famiglia"
        assert contact.extras["is_group"] is True

    def test_contact_from_channel(self):
        backend = _make_backend()
        channel = MockTelethonChannel(id=-1001234567890, title="Notizie")
        contact = backend._entity_to_contact(channel)
        assert contact.id == "-1001234567890"
        assert contact.display_name == "Notizie"
        assert contact.extras["is_channel"] is True

    def test_contact_cache_key_includes_protocol(self):
        backend = _make_backend()
        user = MockTelethonUser(id=1, first_name="Test")
        contact = backend._entity_to_contact(user)
        assert contact.cache_key == contact_cache_key(PROTOCOL_TELEGRAM, "1")


# ─── Message mapping ──────────────────────────────────────────────────────


class TestMessageMapping:
    """📩 Mappatura Telethon Message → ChatEvent."""

    def test_message_to_chat_event_basic(self):
        backend = _make_backend()
        sender = MockTelethonUser(id=111, first_name="Giovanni")
        msg = MockTelethonMessage(
            id=42,
            chat_id=111,
            text="Ciao!",
            out=False,
            sender=sender,
            date=MockTelethonDate(1700000000000),
        )
        evt = backend._message_to_chat_event(msg)
        assert evt is not None
        assert evt.type == "message"
        assert evt.protocol == PROTOCOL_TELEGRAM
        assert evt.contact_id == "111"
        assert evt.payload["id"] == "42"
        assert evt.payload["text"] == "Ciao!"
        assert evt.payload["is_mine"] is False
        assert evt.payload["sender"] == "Giovanni"
        assert evt.payload["timestamp"] == 1700000000000
        assert evt.payload["msg_type"] == "text"

    def test_message_outgoing_is_mine(self):
        backend = _make_backend()
        msg = MockTelethonMessage(
            id=1,
            chat_id=222,
            text="Ok",
            out=True,
            sender=MockTelethonUser(id=999, first_name="You"),
            date=MockTelethonDate(1700000000000),
        )
        evt = backend._message_to_chat_event(msg)
        assert evt.payload["is_mine"] is True
        assert "status" not in evt.payload

    def test_message_photo_type(self):
        backend = _make_backend()
        msg = MockTelethonMessage(
            id=3,
            chat_id=111,
            photo=True,
            out=False,
            sender=MockTelethonUser(id=111, first_name="Mario"),
            date=MockTelethonDate(1700000000000),
        )
        evt = backend._message_to_chat_event(msg)
        assert evt.payload["msg_type"] == "image"
        assert evt.payload["attachment_id"] == "tgref:111:3"

    def test_message_sticker_type(self):
        backend = _make_backend()
        msg = MockTelethonMessage(
            id=4,
            chat_id=111,
            document=MockTelethonDocument(
                attributes=[DocumentAttributeSticker()], mime_type="image/webp"
            ),
            out=False,
            sender=MockTelethonUser(id=111, first_name="Mario"),
            date=MockTelethonDate(1700000000000),
        )
        evt = backend._message_to_chat_event(msg)
        assert evt.payload["msg_type"] == "sticker"
        assert evt.payload["attachment_id"] == "tgref:111:4"

    def test_message_document_type(self):
        backend = _make_backend()
        msg = MockTelethonMessage(
            id=5,
            chat_id=111,
            document=MockTelethonDocument(
                attributes=[DocumentAttributeFilename("report.pdf")],
                mime_type="application/pdf",
            ),
            out=False,
            sender=MockTelethonUser(id=111, first_name="Mario"),
            date=MockTelethonDate(1700000000000),
        )
        evt = backend._message_to_chat_event(msg)
        assert evt.payload["msg_type"] == "attachment"
        assert evt.payload["attachment_id"] == "tgref:111:5"

    def test_message_video_audio_types(self):
        backend = _make_backend()
        media_attributes = (
            ("video", DocumentAttributeVideo(), "video/mp4"),
            ("audio", DocumentAttributeAudio(), "audio/mpeg"),
            ("voice", DocumentAttributeVoice(), "audio/ogg"),
        )
        for media, attribute, mime_type in media_attributes:
            msg = MockTelethonMessage(
                id=7,
                chat_id=111,
                document=MockTelethonDocument(
                    attributes=[attribute], mime_type=mime_type
                ),
                out=False,
                sender=MockTelethonUser(id=111, first_name="Mario"),
                date=MockTelethonDate(1700000000000),
            )
            evt = backend._message_to_chat_event(msg)
            assert evt.payload["msg_type"] == "attachment", media
            assert evt.payload["attachment_id"] == "tgref:111:7", media

    def test_message_sender_fallback_to_id(self):
        backend = _make_backend()
        msg = MockTelethonMessage(
            id=9,
            chat_id=111,
            text="Hey",
            out=False,
            sender=MockTelethonUser(id=777),
            date=MockTelethonDate(1700000000000),
        )
        evt = backend._message_to_chat_event(msg)
        assert evt.payload["sender"] == "777"

    def test_timestamp_conversion(self):
        backend = _make_backend()
        msg = MockTelethonMessage(
            id=10,
            chat_id=111,
            text=".",
            out=False,
            sender=MockTelethonUser(id=111, first_name="T"),
            date=MockTelethonDate(1700000000123),
        )
        evt = backend._message_to_chat_event(msg)
        assert evt.payload["timestamp"] == 1700000000123
        assert isinstance(evt.payload["timestamp"], int)

    def test_message_quote_no_crash(self):
        backend = _make_backend()
        msg = MockTelethonMessage(
            id=8,
            chat_id=111,
            text="Risposta",
            out=False,
            sender=MockTelethonUser(id=111, first_name="Mario"),
            date=MockTelethonDate(1700000000000),
            reply_to=MockReplyTo(reply_to_msg_id=5),
        )
        evt = backend._message_to_chat_event(msg)
        assert evt is not None
        assert evt.payload["id"] == "8"


# ─── Typing event ─────────────────────────────────────────────────────────


class TestTypingEvent:
    """⌨️  Evento typing da ChatAction → ChatEvent."""

    def test_typing_started_event(self):
        backend = _make_backend()
        backend._events.put(
            ChatEvent(
                type="typing",
                protocol=PROTOCOL_TELEGRAM,
                contact_id="222",
                payload={"action": "STARTED"},
            )
        )
        events = backend.poll_once()
        assert len(events) == 1
        assert events[0].type == "typing"
        assert events[0].contact_id == "222"
        assert events[0].payload["action"] == "STARTED"


# ─── Event queue ──────────────────────────────────────────────────────────


class TestEventQueue:
    """📬 Contratto coda eventi: .put() thread-safe, poll_once() svuota."""

    def test_poll_once_drains_queue(self):
        backend = _make_backend()
        for i in range(3):
            backend._events.put(
                ChatEvent(
                    type="message",
                    protocol=PROTOCOL_TELEGRAM,
                    contact_id=str(i),
                    payload={},
                )
            )
        events = backend.poll_once()
        assert len(events) == 3
        assert [e.contact_id for e in events] == ["0", "1", "2"]
        assert backend.poll_once() == []

    def test_poll_once_empty_queue(self):
        backend = _make_backend()
        assert backend.poll_once() == []

    def test_queue_thread_safe(self):
        backend = _make_backend()
        errors = []

        def producer():
            try:
                for i in range(50):
                    backend._events.put(
                        ChatEvent(
                            type="message",
                            protocol=PROTOCOL_TELEGRAM,
                            contact_id=str(i),
                            payload={"n": i},
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=producer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(errors) == 0

        all_events: list[ChatEvent] = []
        while True:
            batch = backend.poll_once()
            if not batch:
                break
            all_events.extend(batch)
        assert len(all_events) == 200


# ─── Cache and dedup ──────────────────────────────────────────────────────


class TestCacheDedup:
    """🗄️  Cache in-memory e dedup messaggi (dal thread TUI)."""

    def test_ingest_message_adds_to_cache(self):
        backend = _make_backend()
        data = {
            "id": "msg-1",
            "contact_id": "111",
            "protocol": PROTOCOL_TELEGRAM,
            "text": "Ciao!",
            "is_mine": False,
            "sender": "Mario",
            "timestamp": 1700000000000,
            "quote_text": None,
            "msg_type": "text",
            "attachment_info": None,
            "attachment_id": None,
        }
        added = backend.ingest_message("111", data, 1700000000000)
        assert added is True
        assert "111" in backend.cache
        assert len(backend.cache["111"]) == 1
        assert backend.cache["111"][0]["text"] == "Ciao!"

    def test_ingest_message_dedup_same_id(self):
        backend = _make_backend()
        data = {
            "id": "msg-1",
            "contact_id": "111",
            "protocol": PROTOCOL_TELEGRAM,
            "text": "Ciao!",
            "is_mine": False,
            "sender": "Mario",
            "timestamp": 1700000000000,
            "msg_type": "text",
        }
        assert backend.ingest_message("111", data, 1700000000000) is True
        assert backend.ingest_message("111", data, 1700000000000) is False
        assert len(backend.cache["111"]) == 1

    def test_ingest_message_different_contact(self):
        backend = _make_backend()
        data_a = {
            "id": "a",
            "text": "Ok",
            "timestamp": 1000,
            "is_mine": False,
            "sender": "A",
            "msg_type": "text",
            "protocol": PROTOCOL_TELEGRAM,
        }
        data_b = {
            "id": "b",
            "text": "Ok",
            "timestamp": 1000,
            "is_mine": False,
            "sender": "B",
            "msg_type": "text",
            "protocol": PROTOCOL_TELEGRAM,
        }
        assert backend.ingest_message("111", data_a, 1000) is True
        assert backend.ingest_message("222", data_b, 1000) is True
        assert len(backend.cache["111"]) == 1
        assert len(backend.cache["222"]) == 1

    def test_ingest_message_distinct_ids_same_second(self):
        backend = _make_backend()
        data1 = {
            "id": "tg-1",
            "text": "A",
            "timestamp": 1000,
            "is_mine": False,
            "sender": "X",
            "msg_type": "text",
            "protocol": PROTOCOL_TELEGRAM,
        }
        data2 = {
            "id": "tg-2",
            "text": "B",
            "timestamp": 1000,
            "is_mine": False,
            "sender": "X",
            "msg_type": "text",
            "protocol": PROTOCOL_TELEGRAM,
        }
        assert backend.ingest_message("111", data1, 1000) is True
        assert backend.ingest_message("111", data2, 1000) is True
        assert len(backend.cache["111"]) == 2

    def test_optimistic_echo_upgrades_id_and_persists_keeps_ts(self):
        """🛡️ Regressione \"inviati Telegram doppi dopo Ctrl+L\": l'echo di un
        invio ottimistico (id=None) deve attaccare l'id reale alla entry
        esistente e PERSISTERLO in SQLite via ``_update_message_id``, SENZA
        cambiare il timestamp ottimistico.  Prima l'upgrade restava solo in
        memoria (id reale + ts server) mentre il DB tratteneva (id='', ts
        client): al reload della cache (Ctrl+L) il merge per identità esatta
        non trovava il match e raddoppiava il messaggio inviato.
        """
        backend = _make_backend()
        with patch("backend._update_message_id") as mock_upd:
            # 1) Invio ottimistico dalla TUI: nessun id, ts client.
            added = backend.ingest_message(
                "111",
                {
                    "text": "ciao",
                    "is_mine": True,
                    "sender": "You",
                    "timestamp": 1000,
                    "quote_text": None,
                    "msg_type": "text",
                    "attachment_info": None,
                    "attachment_id": None,
                },
                1000,
            )
            assert added is True

            # 2) Echo reale entro la finestra di dedup.
            added_echo = backend.ingest_message(
                "111",
                {
                    "id": "42",
                    "text": "ciao",
                    "is_mine": True,
                    "sender": "You",
                    "timestamp": 1500,
                    "quote_text": None,
                    "msg_type": "text",
                    "attachment_info": None,
                    "attachment_id": None,
                },
                1500,
            )

        # Non deve essere aggiunto come nuovo messaggio.
        assert added_echo is False
        cached = backend.cache["111"]
        assert len(cached) == 1
        # Id reale attaccato, timestamp ottimistico PRESERVATO (niente drift).
        assert cached[0]["id"] == "42"
        assert cached[0]["timestamp"] == 1000
        # L'upgrade è stato persistito in SQLite (id reale, stesso ts client).
        mock_upd.assert_called_once_with(
            "111", "ciao", True, 1000, "42", protocol=PROTOCOL_TELEGRAM
        )


# ─── Thread boundary contract ─────────────────────────────────────────────


class TestThreadBoundary:
    """🧵 Vincolo: gli handler NON toccano SQLite o cache."""

    def test_handler_only_normalizes_and_puts(self):
        backend = _make_backend()
        sender = MockTelethonUser(id=111, first_name="Mario")
        msg = MockTelethonMessage(
            id=42,
            chat_id=111,
            text="Test",
            out=False,
            sender=sender,
            date=MockTelethonDate(1700000000000),
        )
        with patch.object(backend, "ingest_message") as mock_ingest:
            evt = backend._message_to_chat_event(msg)
            if evt:
                backend._events.put(evt)
            mock_ingest.assert_not_called()
        events = backend.poll_once()
        assert len(events) == 1

    def test_cache_written_only_after_poll_once(self):
        backend = _make_backend()
        evt = ChatEvent(
            type="message",
            protocol=PROTOCOL_TELEGRAM,
            contact_id="111",
            payload={
                "id": "msg-1",
                "text": "Ciao",
                "is_mine": False,
                "sender": "Mario",
                "timestamp": 1700000000000,
                "msg_type": "text",
            },
        )
        backend._events.put(evt)
        assert backend.cache == {}
        events = backend.poll_once()
        assert len(events) == 1
        backend.ingest_message("111", events[0].payload, 1700000000000)
        assert "111" in backend.cache
        assert len(backend.cache["111"]) == 1


# ─── Disconnect ───────────────────────────────────────────────────────────


class TestDisconnect:
    """🔌 disconnect_sync ferma il thread e il client."""

    def test_disconnect_sync_sets_running_false(self):
        backend = _make_backend()
        backend._running = True
        backend._connected = True
        backend.disconnect_sync()
        assert backend._running is False
        assert backend._connected is False

    def test_disconnect_sync_noop_when_not_connected(self):
        backend = _make_backend()
        backend.disconnect_sync()  # no crash

    def test_connect_sync_disconnects_previous_client_first(self):
        """🛡️ Regressione \"niente messaggi live dopo Ctrl+L\": `_connect_sync`
        deve smontare il client/loop precedente PRIMA di crearne uno nuovo.
        Due TelegramClient concorrenti sulla stessa session corrompono lo
        stato update e bloccano la ricezione dei messaggi live.
        """
        backend = _make_backend()
        # Simula un client già connesso (es. avvio precedente o Ctrl+L).
        backend._client = object()
        backend._loop = MagicMock()
        backend._loop_thread = MagicMock()
        backend._running = True

        with (
            patch.object(backend, "disconnect_sync") as mock_disc,
            patch.object(
                backend, "_load_protocol_cache", side_effect=RuntimeError("stop early")
            ),
            pytest.raises(RuntimeError),
        ):
            backend._connect_sync()

        # `disconnect_sync` è stato chiamato PRIMA di `_load_protocol_cache`
        # (che solleva: se l'ordine fosse invertito non verrebbe mai chiamato).
        mock_disc.assert_called_once()


# ─── Protocol constant ────────────────────────────────────────────────────


class TestProtocolConstant:
    """🏷️  PROTOCOL_TELEGRAM e TelegramBackend."""

    def test_protocol_telegram_constant(self):
        assert PROTOCOL_TELEGRAM == "telegram"

    def test_protocol_telegram_in_emoji_map(self):
        from models import PROTOCOL_EMOJI, protocol_emoji

        assert PROTOCOL_TELEGRAM in PROTOCOL_EMOJI
        assert protocol_emoji(PROTOCOL_TELEGRAM) == "📨"

    def test_telegram_backend_protocol(self):
        from backends.telegram import TelegramBackend

        assert TelegramBackend.protocol == PROTOCOL_TELEGRAM

    def test_is_chat_backend_subclass(self):
        from backends.base import ChatBackend
        from backends.telegram import TelegramBackend

        assert issubclass(TelegramBackend, ChatBackend)

    def test_instantiable(self):
        from backends.telegram import TelegramBackend

        backend = TelegramBackend()
        assert backend.protocol == PROTOCOL_TELEGRAM
        assert isinstance(backend.contacts, list)
        assert isinstance(backend.cache, dict)
