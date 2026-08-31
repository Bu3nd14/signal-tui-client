"""Unit tests for TelegramBackend without connecting to Telegram."""

from __future__ import annotations

import asyncio
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.tl.types import (
    Channel,
    Chat,
    PeerUser,
    SendMessageCancelAction,
    SendMessageRecordAudioAction,
    SendMessageRecordVideoAction,
    SendMessageTypingAction,
    SendMessageUploadPhotoAction,
    UpdateChannelUserTyping,
    UpdateChatUserTyping,
    UpdateUserTyping,
    User,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backends.telegram import TelegramBackend
from models import PROTOCOL_TELEGRAM, ChatContact, ChatEvent


def _backend() -> TelegramBackend:
    backend = TelegramBackend()
    backend._api_id = 123
    backend._api_hash = "hash"
    return backend


def _message(**overrides):
    fields = {
        "chat_id": 42,
        "text": "hello",
        "out": False,
        "sender": SimpleNamespace(first_name="Ada", last_name="Lovelace", id=7),
        "date": datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC),
        "photo": None,
        "document": None,
        "sticker": None,
        "video": None,
        "voice": None,
        "audio": None,
        "reply_to": None,
        "id": 99,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class DocumentAttributeSticker:
    pass


class DocumentAttributeFilename:
    def __init__(self, file_name):
        self.file_name = file_name


class DocumentAttributeVideo:
    pass


class DocumentAttributeVoice:
    pass


class DocumentAttributeAudio:
    def __init__(self, *, voice=False):
        self.voice = voice


class DocumentAttributeAnimated:
    pass


class TestTelegramContacts:
    def test_attachment_path_returns_existing_file_only(self, tmp_path):
        backend = _backend()
        existing = tmp_path / "attachment.txt"
        existing.write_text("data")

        assert backend.get_attachment_path(str(existing)) == existing
        assert backend.get_attachment_path(str(tmp_path / "missing")) is None

    def test_get_attachment_path_tgref_not_connected(self):
        backend = _backend()

        assert backend.get_attachment_path("tgref:42:99") is None

    def test_get_attachment_path_tgref_loop_not_running(self, monkeypatch):
        backend = _backend()
        backend._client = SimpleNamespace()
        backend._connected = True
        backend._loop = MagicMock()
        backend._loop.is_running.return_value = False
        run_threadsafe = MagicMock()
        monkeypatch.setattr(
            "backends.telegram.asyncio.run_coroutine_threadsafe", run_threadsafe
        )

        assert backend.get_attachment_path("tgref:42:99") is None
        run_threadsafe.assert_not_called()

    def _ready_backend_with_media_msg(self, monkeypatch, tmp_path, msg):
        backend = _backend()
        backend._connected = True
        backend._loop = MagicMock()
        backend._loop.is_running.return_value = True
        backend._client = SimpleNamespace(
            get_input_entity=AsyncMock(return_value="entity"),
            get_messages=AsyncMock(return_value=msg),
        )
        monkeypatch.setattr("backends.telegram._media_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "backends.telegram.asyncio.run_coroutine_threadsafe",
            lambda coro, _loop: SimpleNamespace(
                result=lambda timeout: asyncio.run(coro)
            ),
        )
        return backend

    def test_get_attachment_path_tgref_lazy_download_success(
        self, monkeypatch, tmp_path
    ):
        target = tmp_path / "42-99-photo.jpg"
        msg = SimpleNamespace(
            id=99,
            photo=True,
            document=None,
            file=SimpleNamespace(name="photo.jpg"),
            download_media=AsyncMock(return_value=str(target)),
        )
        backend = self._ready_backend_with_media_msg(monkeypatch, tmp_path, msg)

        assert backend.get_attachment_path("tgref:42:99") == target
        backend._client.get_messages.assert_awaited_once_with("entity", ids=99)
        msg.download_media.assert_awaited_once_with(file=str(target))

    def test_get_attachment_path_tgref_dedup_existing_file(self, monkeypatch, tmp_path):
        target = tmp_path / "42-99-photo.jpg"
        target.write_text("data")
        msg = SimpleNamespace(
            id=99,
            photo=True,
            document=None,
            file=SimpleNamespace(name="photo.jpg"),
            download_media=AsyncMock(),
        )
        backend = self._ready_backend_with_media_msg(monkeypatch, tmp_path, msg)

        assert backend.get_attachment_path("tgref:42:99") == target
        msg.download_media.assert_not_awaited()

    def test_get_attachment_path_tgref_message_gone(self, monkeypatch):
        backend = _backend()
        backend._connected = True
        backend._loop = MagicMock()
        backend._loop.is_running.return_value = True
        backend._client = SimpleNamespace(
            get_input_entity=AsyncMock(return_value="entity"),
            get_messages=AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            "backends.telegram.asyncio.run_coroutine_threadsafe",
            lambda coro, _loop: SimpleNamespace(
                result=lambda timeout: asyncio.run(coro)
            ),
        )

        assert backend.get_attachment_path("tgref:42:99") is None

    def test_get_attachment_path_tgref_no_media(self, monkeypatch):
        msg = SimpleNamespace(
            id=99,
            photo=None,
            document=None,
            file=SimpleNamespace(name="photo.jpg"),
            download_media=AsyncMock(),
        )
        backend = _backend()
        backend._connected = True
        backend._loop = MagicMock()
        backend._loop.is_running.return_value = True
        backend._client = SimpleNamespace(
            get_input_entity=AsyncMock(return_value="entity"),
            get_messages=AsyncMock(return_value=msg),
        )
        monkeypatch.setattr(
            "backends.telegram.asyncio.run_coroutine_threadsafe",
            lambda coro, _loop: SimpleNamespace(
                result=lambda timeout: asyncio.run(coro)
            ),
        )

        assert backend.get_attachment_path("tgref:42:99") is None
        msg.download_media.assert_not_awaited()

    def test_get_attachment_path_tgref_download_fails(self, monkeypatch, tmp_path):
        msg = SimpleNamespace(
            id=99,
            photo=True,
            document=None,
            file=SimpleNamespace(name="photo.jpg"),
            download_media=AsyncMock(side_effect=RuntimeError("boom")),
        )
        backend = self._ready_backend_with_media_msg(monkeypatch, tmp_path, msg)

        assert backend.get_attachment_path("tgref:42:99") is None

    def test_get_attachment_path_tgref_future_raises(self, monkeypatch):
        msg = SimpleNamespace(
            id=99,
            photo=True,
            document=None,
            file=SimpleNamespace(name="photo.jpg"),
            download_media=AsyncMock(),
        )
        backend = _backend()
        backend._connected = True
        backend._loop = MagicMock()
        backend._loop.is_running.return_value = True
        backend._client = SimpleNamespace(
            get_input_entity=AsyncMock(return_value="entity"),
            get_messages=AsyncMock(return_value=msg),
        )

        def raise_now(coro, _loop):
            coro.close()

            def result(timeout):
                raise TimeoutError("timeout")

            return SimpleNamespace(result=result)

        monkeypatch.setattr(
            "backends.telegram.asyncio.run_coroutine_threadsafe", raise_now
        )

        assert backend.get_attachment_path("tgref:42:99") is None

    def test_entity_to_contact_handles_duck_types_and_real_telethon_entities(self):
        user = TelegramBackend._entity_to_contact(
            SimpleNamespace(
                id=1, first_name="Ada", last_name="Lovelace", username="ada", phone="1"
            )
        )
        fallback = TelegramBackend._entity_to_contact(
            SimpleNamespace(
                id=2, first_name="", last_name="", username="anonymous", phone=""
            )
        )
        group = TelegramBackend._entity_to_contact(
            SimpleNamespace(id=-42, title="Group")
        )
        channel = TelegramBackend._entity_to_contact(
            SimpleNamespace(id=-10042, title="Channel")
        )
        unknown = TelegramBackend._entity_to_contact(SimpleNamespace(id=5))
        real_user = TelegramBackend._entity_to_contact(User(id=6, first_name="Real"))
        real_chat = TelegramBackend._entity_to_contact(
            Chat(
                id=7,
                title="Real group",
                photo=None,
                participants_count=1,
                date=None,
                version=1,
            )
        )
        real_channel = TelegramBackend._entity_to_contact(
            Channel(id=8, title="Real channel", photo=None, date=None)
        )

        assert (user.display_name, user.extras) == (
            "Ada Lovelace",
            {"username": "ada", "phone": "1", "is_group": False},
        )
        assert fallback.display_name == "anonymous"
        assert group.extras["is_group"] is True
        assert channel.extras == {"is_group": False, "is_channel": True}
        assert unknown.display_name == "5"
        assert real_user.display_name == "Real"
        assert real_chat.extras["is_group"] is True
        assert real_channel.extras["is_channel"] is True

    def test_identify_and_list_contacts_return_known_contact_and_copy(self):
        backend = _backend()
        contact = ChatContact(id="42", display_name="Ada", protocol=PROTOCOL_TELEGRAM)
        backend.contacts = [contact]
        backend._contacts_by_id = {42: contact}

        assert backend._identify_contact("not-an-id") is None
        assert backend._identify_contact("42") is contact
        listed = asyncio.run(backend.list_contacts())
        assert listed == [contact]
        assert listed is not backend.contacts

    def test_load_contacts_populates_contacts_and_handles_errors(self):
        backend = _backend()
        dialog = SimpleNamespace(
            entity=SimpleNamespace(
                id=42, first_name="Ada", last_name="", username="", phone=""
            ),
            message=SimpleNamespace(date=datetime(2025, 1, 1, tzinfo=UTC)),
        )
        backend._client = SimpleNamespace(get_dialogs=AsyncMock(return_value=[dialog]))

        asyncio.run(backend._load_contacts())
        assert backend.contacts[0].last_message_ts > 0
        assert backend._contacts_by_id[42] is backend.contacts[0]

        backend._client.get_dialogs = AsyncMock(side_effect=RuntimeError("offline"))
        asyncio.run(backend._load_contacts())
        assert backend.contacts == []
        assert backend._contacts_by_id == {}


class TestTelegramMessages:
    def test_message_to_event_normalizes_text_media_reply_and_contact(self):
        backend = _backend()
        contact = ChatContact(id="42", display_name="Ada", protocol=PROTOCOL_TELEGRAM)
        backend._contacts_by_id = {42: contact}
        backend.cache = {"42": [{"id": "12", "text": "quoted"}]}
        message = _message(out=True, reply_to=SimpleNamespace(reply_to_msg_id=12))

        event = backend._message_to_chat_event(message)

        assert isinstance(event, ChatEvent)
        assert event.payload == {
            "id": "99",
            "text": "hello",
            "is_mine": True,
            "sender": "Ada Lovelace",
            "timestamp": 1735787045000,
            "quote_text": "quoted",
            "quote_attachment_id": None,
            "quote_content_type": None,
            "reply_to_message_id": "12",
            "msg_type": "text",
            "attachment_info": None,
            "attachment_id": None,
            "media_kind": None,
            "content_type": None,
            "protocol": PROTOCOL_TELEGRAM,
            "contact": contact,
        }

        assert backend._message_to_chat_event(_message(chat_id=None)) is None

    def test_quote_resolves_target_attachment_metadata(self):
        """Una quote verso un target cached media popola quote_attachment_*.

        Il path NON viene risolto qui: resta None (il download lazy è di
        ``get_attachment_path``); vengono copiati solo id + content_type.
        """
        backend = _backend()
        contact = ChatContact(id="42", display_name="Ada", protocol=PROTOCOL_TELEGRAM)
        backend._contacts_by_id = {42: contact}
        backend.cache = {
            "42": [
                {
                    "id": "12",
                    "text": "",
                    "msg_type": "image",
                    "attachment_info": "Photo",
                    "attachment_id": "tgref:42:12",
                    "content_type": "image/png",
                }
            ]
        }
        message = _message(reply_to=SimpleNamespace(reply_to_msg_id=12))

        event = backend._message_to_chat_event(message)

        assert event.payload["quote_text"] == "🖼️ Immagine"
        assert event.payload["quote_attachment_id"] == "tgref:42:12"
        assert event.payload["quote_content_type"] == "image/png"

    @pytest.mark.parametrize(
        ("kind", "attribute", "mime", "expected_info"),
        [
            ("sticker", DocumentAttributeSticker(), "image/webp", "🎨 Sticker"),
            ("video", DocumentAttributeVideo(), "video/mp4", "🎬 Video"),
            ("voice", DocumentAttributeVoice(), "audio/ogg", "🎤 Voice"),
            ("audio", DocumentAttributeAudio(), "audio/mpeg", "🎵 Audio"),
            ("gif", DocumentAttributeAnimated(), "video/mp4", "📎 Document"),
        ],
    )
    def test_message_to_event_normalizes_media(
        self, kind, attribute, mime, expected_info
    ):
        backend = _backend()
        document = SimpleNamespace(mime_type=mime, attributes=[attribute])
        event = backend._message_to_chat_event(
            _message(document=document, text=""), "/tmp/media"
        )

        expected_type = (
            "sticker"
            if kind == "sticker"
            else "image"
            if kind == "gif"
            else "attachment"
        )
        assert event.payload["msg_type"] == expected_type
        assert event.payload["media_kind"] == kind
        assert event.payload["content_type"] == mime
        assert event.payload["attachment_info"] == expected_info
        assert event.payload["attachment_id"] == "/tmp/media"

    def test_message_photo_with_caption_uses_text_as_info(self):
        backend = _backend()

        with_caption = backend._message_to_chat_event(
            _message(photo=object(), text="che bello")
        )
        assert with_caption.payload["msg_type"] == "image"
        assert with_caption.payload["attachment_info"] == "che bello"
        assert with_caption.payload["attachment_id"] == "tgref:42:99"

        without_text = backend._message_to_chat_event(_message(photo=object(), text=""))
        assert without_text.payload["msg_type"] == "image"
        assert without_text.payload["attachment_info"] == "Photo"

    def test_message_photo_without_download_gets_tgref(self):
        backend = _backend()

        event = backend._message_to_chat_event(_message(photo=object(), text=""))

        assert event.payload["msg_type"] == "image"
        assert event.payload["attachment_info"] == "Photo"
        assert event.payload["attachment_id"] == "tgref:42:99"

    def test_message_photo_with_caption_gets_tgref(self):
        backend = _backend()

        event = backend._message_to_chat_event(
            _message(photo=object(), text="che bello")
        )

        assert event.payload["msg_type"] == "image"
        assert event.payload["attachment_info"] == "che bello"
        assert event.payload["attachment_id"] == "tgref:42:99"

    def test_message_to_event_uses_document_filename_and_id_fallback(self):
        backend = _backend()
        document = SimpleNamespace(
            attributes=[DocumentAttributeFilename("report.pdf")],
            mime_type="application/pdf",
        )

        event = backend._message_to_chat_event(_message(document=document, text=""))

        assert event.payload["msg_type"] == "attachment"
        assert event.payload["attachment_info"] == "📎 report.pdf"
        assert event.payload["attachment_id"] == "tgref:42:99"

    def test_receipts_polling_and_ingest_deduplication(self, monkeypatch):
        backend = _backend()
        backend.cache = {
            "42": [
                {"id": "1", "is_mine": True, "status": "sent", "timestamp": 1},
                {"id": "2", "is_mine": True, "status": "read", "timestamp": 2},
            ]
        }
        persist_status = MagicMock()
        persist_status_by_id = MagicMock()
        monkeypatch.setattr("backend._update_message_status", persist_status)
        monkeypatch.setattr(
            "backend._update_message_status_by_id", persist_status_by_id
        )

        assert backend.process_receipt({}) == []
        updated = backend.process_receipt({"message_ids": ["1", "2"]})
        assert [item["id"] for item in updated] == ["1"]
        assert updated[0]["status"] == "delivered"
        assert backend.cache["42"][0]["status"] == "delivered"
        assert (
            backend.process_receipt({"message_ids": ["1"], "is_read": True})[0][
                "status"
            ]
            == "read"
        )
        assert persist_status.call_count == 0
        assert persist_status_by_id.call_count == 2

        persisted = MagicMock()
        updated_id = MagicMock()
        monkeypatch.setattr(backend, "_persist_message", persisted)
        monkeypatch.setattr("backend._update_message_id", updated_id)
        data = {"id": None, "text": "optimistic", "is_mine": True}
        assert backend.ingest_message("7", data, 100) is True
        assert backend.ingest_message("7", {**data, "id": "server"}, 101) is False
        assert backend.cache["7"][0]["id"] == "server"
        updated_id.assert_called_once()
        assert (
            backend.ingest_message("7", {"id": "server", "text": "optimistic"}, 102)
            is False
        )
        assert persisted.call_count == 1

    def test_ingest_bounds_cache_and_persist_message_passes_protocol(self, monkeypatch):
        backend = _backend()
        backend.cache["42"] = [
            {"id": str(i), "text": str(i), "timestamp": i} for i in range(50)
        ]
        assert backend.ingest_message(
            "42", {"id": "new", "text": "new"}, 100, persist=False
        )
        assert len(backend.cache["42"]) == 50
        assert backend.cache["42"][0]["id"] == "1"

        add = MagicMock()
        monkeypatch.setattr("backend._add_message_to_cache", add)
        backend._persist_message("42", {"id": "1", "text": "text", "is_mine": True}, 5)
        assert add.call_args.kwargs["protocol"] == PROTOCOL_TELEGRAM
        assert add.call_args.kwargs["msg_id"] == "1"

    def test_poll_once_drains_events_and_receive_is_empty(self):
        backend = _backend()
        assert backend.poll_once() == []
        event = ChatEvent(
            type="message", protocol=PROTOCOL_TELEGRAM, contact_id="1", payload={}
        )
        backend._events.put(event)
        assert backend.poll_once() == [event]

        async def collect():
            return [item async for item in backend.receive()]

        assert asyncio.run(collect()) == []


class TestTelegramTyping:
    def test_user_typing_is_started(self):
        backend = _backend()
        asyncio.run(
            backend._handle_typing_update(
                UpdateUserTyping(user_id=42, action=SendMessageTypingAction())
            )
        )
        events = backend.poll_once()
        assert len(events) == 1
        assert events[0].type == "typing"
        assert events[0].protocol == PROTOCOL_TELEGRAM
        assert events[0].contact_id == "42"
        assert events[0].payload == {"action": "STARTED"}

    def test_user_typing_cancel_is_stopped(self):
        backend = _backend()
        asyncio.run(
            backend._handle_typing_update(
                UpdateUserTyping(user_id=42, action=SendMessageCancelAction())
            )
        )
        assert backend.poll_once()[0].payload == {"action": "STOPPED"}

    @pytest.mark.parametrize(
        "action",
        [
            SendMessageUploadPhotoAction(progress=0),
            SendMessageRecordAudioAction(),
            SendMessageRecordVideoAction(),
        ],
    )
    def test_non_cancel_send_actions_are_started(self, action):
        backend = _backend()
        asyncio.run(
            backend._handle_typing_update(UpdateUserTyping(user_id=42, action=action))
        )
        assert backend.poll_once()[0].payload == {"action": "STARTED"}

    def test_chat_and_channel_contact_id_conventions(self):
        backend = _backend()
        asyncio.run(
            backend._handle_typing_update(
                UpdateChatUserTyping(
                    chat_id=123,
                    from_id=PeerUser(user_id=9),
                    action=SendMessageTypingAction(),
                )
            )
        )
        assert backend.poll_once()[0].contact_id == "-123"

        channel_id = str(-1000000000000 - 456)
        asyncio.run(
            backend._handle_typing_update(
                UpdateChannelUserTyping(
                    channel_id=456,
                    from_id=PeerUser(user_id=9),
                    action=SendMessageTypingAction(),
                )
            )
        )
        assert backend.poll_once()[0].contact_id == channel_id

    def test_typing_is_pure_translator_no_cache_mutation(self):
        backend = _backend()
        backend.cache = {"42": [{"id": "1", "status": "sent"}]}
        asyncio.run(
            backend._handle_typing_update(
                UpdateUserTyping(user_id=42, action=SendMessageTypingAction())
            )
        )
        assert backend.cache == {"42": [{"id": "1", "status": "sent"}]}

    def test_unknown_update_returns_none_no_event(self):
        backend = _backend()
        result = asyncio.run(backend._handle_typing_update(SimpleNamespace(user_id=42)))
        assert result is None
        assert backend.poll_once() == []


class TestTelegramBackendOperations:
    def test_voice_attachment_sets_voice_note(self, monkeypatch, tmp_path):
        backend = _backend()
        backend._loop = MagicMock()
        backend._client = SimpleNamespace(
            send_file=AsyncMock(return_value=SimpleNamespace(id=77))
        )
        backend._resolve_input_entity = AsyncMock(return_value="entity")

        class CompletedFuture:
            def __init__(self, value):
                self.value = value

            def result(self, timeout):
                return self.value

        monkeypatch.setattr(
            "backends.telegram.asyncio.run_coroutine_threadsafe",
            lambda coro, _loop: CompletedFuture(asyncio.run(coro)),
        )

        result = backend.send_attachment_sync(
            "42",
            tmp_path / "voice.ogg",
            mime_type="audio/ogg",
            media_kind="voice",
        )

        assert result == "77"
        backend._client.send_file.assert_awaited_once_with(
            "entity",
            str(tmp_path / "voice.ogg"),
            caption=None,
            reply_to=None,
            force_document=False,
            voice_note=True,
        )

    def test_attachment_send_timeout_is_120_and_configurable(
        self, monkeypatch, tmp_path
    ):
        backend = _backend()
        backend._loop = MagicMock()
        backend._client = MagicMock()
        future = MagicMock()
        future.result.return_value = "77"

        def schedule(coro, _loop):
            coro.close()
            return future

        monkeypatch.setattr(
            "backends.telegram.asyncio.run_coroutine_threadsafe", schedule
        )
        backend.send_attachment_sync(
            "42", tmp_path / "photo.png", mime_type="image/png"
        )
        future.result.assert_called_once_with(timeout=120)

        backend.attachment_send_timeout = 45
        backend.send_attachment_sync(
            "42", tmp_path / "photo.png", mime_type="image/png"
        )
        assert future.result.call_args_list[-1].kwargs == {"timeout": 45}

    def test_load_cache_mark_read_and_pairing(self, monkeypatch, tmp_path):
        backend = _backend()
        monkeypatch.setattr("backend._load_cache", MagicMock(return_value={"1": []}))
        assert backend._load_protocol_cache() == {"1": []}
        monkeypatch.setattr("backend._load_cache", MagicMock(side_effect=RuntimeError))
        assert backend._load_protocol_cache() == {}

        mark_read = MagicMock()
        monkeypatch.setattr("backend._mark_as_read", mark_read)
        backend.mark_read_sync("42")
        mark_read.assert_called_once_with("42", protocol=PROTOCOL_TELEGRAM)
        monkeypatch.setattr(
            "backend._mark_as_read", MagicMock(side_effect=RuntimeError)
        )
        backend.mark_read_sync("42")

        backend._api_id = 0
        assert backend.needs_pairing is False
        backend._api_id, backend._api_hash = 1, ""
        assert backend.needs_pairing is False
        backend._api_hash, backend._connected = "hash", True
        assert backend.needs_pairing is False
        backend._connected = False
        backend._session_path = str(tmp_path / "missing.session")
        assert backend.needs_pairing is True

    def test_send_methods_validate_connection_ids_and_send(self, monkeypatch):
        backend = _backend()
        with pytest.raises(RuntimeError):
            asyncio.run(backend.send_message("42", "hi"))
        with pytest.raises(RuntimeError):
            backend.send_message_sync("42", "hi")

        backend._loop = MagicMock()
        backend._client = SimpleNamespace(
            get_input_entity=AsyncMock(return_value="entity"),
            send_message=AsyncMock(return_value=SimpleNamespace(id=77)),
        )

        def run_now(coro, _loop):
            outcome = {}

            def execute():
                outcome["value"] = asyncio.run(coro)

            worker = threading.Thread(target=execute)
            worker.start()
            worker.join()

            def result(timeout):
                return outcome["value"]

            return SimpleNamespace(result=result)

        def invalid_id(coro, _loop):
            coro.close()

            def result(timeout):
                raise ValueError("Invalid Telegram contact id: bad")

            return SimpleNamespace(result=result)

        monkeypatch.setattr(
            "backends.telegram.asyncio.run_coroutine_threadsafe", run_now
        )
        assert asyncio.run(backend.send_message("42", "hi")) == "77"
        assert backend.send_message_sync("42", "hi") == "77"
        monkeypatch.setattr(
            "backends.telegram.asyncio.run_coroutine_threadsafe", invalid_id
        )
        with pytest.raises(ValueError):
            asyncio.run(backend.send_message("bad", "hi"))

    def test_send_reply_uses_positive_original_message_id(self, monkeypatch):
        backend = _backend()
        backend._loop = MagicMock()
        backend._client = SimpleNamespace(
            get_input_entity=AsyncMock(return_value="entity"),
            send_message=AsyncMock(return_value=SimpleNamespace(id=77)),
        )
        monkeypatch.setattr(
            "backends.telegram.asyncio.run_coroutine_threadsafe",
            lambda coro, _loop: SimpleNamespace(
                result=lambda timeout: asyncio.run(coro)
            ),
        )

        assert backend.send_message_sync("42", "hi", reply_to_message_id="12") == "77"
        backend._client.send_message.assert_awaited_once_with(
            "entity", "hi", reply_to=12
        )
        with pytest.raises(ValueError, match="reply message id"):
            backend.send_message_sync("42", "hi", reply_to_message_id="0")

    def test_validated_reply_to_message_id_rejects_non_numeric_string(self):
        with pytest.raises(ValueError, match="reply message id"):
            TelegramBackend._validated_reply_to_message_id("not-a-message-id")

    def test_disconnect_fetch_history_and_complete_2fa(self, monkeypatch):
        backend = _backend()
        backend._client = SimpleNamespace(disconnect=AsyncMock())
        backend._loop = MagicMock()
        thread = MagicMock(is_alive=MagicMock(return_value=True))
        backend._loop_thread = thread
        future = MagicMock()

        def discard(coro, _loop):
            coro.close()
            return future

        run_threadsafe = MagicMock(side_effect=discard)
        monkeypatch.setattr(
            "backends.telegram.asyncio.run_coroutine_threadsafe", run_threadsafe
        )
        backend.disconnect_sync()
        thread.join.assert_called_once_with(timeout=5)
        run_threadsafe.assert_called_once()

        backend = _backend()
        assert backend.fetch_recent_history() == 0
        backend._connected, backend._loop = True, MagicMock()
        backend.contacts = [
            ChatContact(id="42", display_name="Ada", protocol=PROTOCOL_TELEGRAM)
        ]
        backend._client = SimpleNamespace(
            get_input_entity=AsyncMock(return_value="entity"),
            get_messages=AsyncMock(return_value=[_message()]),
        )
        monkeypatch.setattr(backend, "ingest_message", MagicMock())
        monkeypatch.setattr(
            "backends.telegram.asyncio.run_coroutine_threadsafe",
            lambda coro, _loop: SimpleNamespace(
                result=lambda timeout: asyncio.run(coro)
            ),
        )
        assert backend.fetch_recent_history() == 1

        backend._needs_2fa = False
        assert backend.complete_2fa("password") is False
        backend._needs_2fa = True
        backend._client = SimpleNamespace(sign_in=AsyncMock(), disconnect=MagicMock())
        backend._loop = SimpleNamespace(
            run_until_complete=lambda coro: asyncio.run(coro), close=MagicMock()
        )
        assert backend.complete_2fa("password") is True
        assert backend._connected is True

    def test_fetch_history_photo_persists_tgref(self, monkeypatch):
        backend = _backend()
        backend._connected = True
        backend._loop = MagicMock()
        backend.contacts = [
            ChatContact(id="42", display_name="Ada", protocol=PROTOCOL_TELEGRAM)
        ]
        photo_msg = _message(photo=object(), text="")
        photo_msg.download_media = AsyncMock()
        backend._client = SimpleNamespace(
            get_input_entity=AsyncMock(return_value="entity"),
            get_messages=AsyncMock(return_value=[photo_msg]),
        )
        ingest = MagicMock()
        monkeypatch.setattr(backend, "ingest_message", ingest)
        monkeypatch.setattr(
            "backends.telegram.asyncio.run_coroutine_threadsafe",
            lambda coro, _loop: SimpleNamespace(
                result=lambda timeout: asyncio.run(coro)
            ),
        )

        assert backend.fetch_recent_history() == 1
        assert ingest.call_count == 1
        assert ingest.call_args.args[1]["attachment_id"] == "tgref:42:99"
        photo_msg.download_media.assert_not_awaited()

    def test_complete_2fa_failure_and_qr_missing_configuration(self):
        backend = _backend()
        backend._api_id = 0
        assert backend.get_pairing_qr() is None
        backend._needs_2fa = True
        backend._client = SimpleNamespace(sign_in=AsyncMock(side_effect=RuntimeError))
        backend._loop = SimpleNamespace(
            run_until_complete=lambda coro: asyncio.run(coro)
        )
        assert backend.complete_2fa("password") is False
        assert backend._needs_2fa is False


class TestTelegramQuoteMedia:
    """🖼️ Bug #37 — quote di un media Telegram risolta dalla cache locale."""

    def test_tg_quote_media_from_cache_placeholder(self, cached_media_target):
        """Target foto senza caption in cache → "🖼️ Immagine"."""
        from backends.telegram import _tg_quote_text_from_cached

        assert _tg_quote_text_from_cached(cached_media_target) == "🖼️ Immagine"

    @pytest.mark.parametrize(
        ("msg_type", "info", "expected"),
        [
            ("image", "Photo", "🖼️ Immagine"),
            ("sticker", "🎨 Sticker", "🎨 Sticker"),
            ("attachment", "🎬 Video", "🎬 Video"),
            ("attachment", "🎤 Voice", "🎵 Audio"),
            ("attachment", "🎵 Audio", "🎵 Audio"),
            ("attachment", "📎 Document", "📎 File"),
        ],
    )
    def test_tg_quote_media_placeholder_variants(self, msg_type, info, expected):
        """Etichette Telegram fini mappano al segnaposto canonico."""
        from backends.telegram import _tg_quote_text_from_cached

        target = {"id": "12", "text": "", "msg_type": msg_type, "attachment_info": info}
        assert _tg_quote_text_from_cached(target) == expected

    @pytest.mark.parametrize(
        ("msg_type", "info", "expected"),
        [
            # Documento con filename reale (shape reale di _message_to_chat_event).
            ("attachment", "📎 report.pdf", "📎 report.pdf — 📎 File"),
            # Filename nudo non mappato: composto col segnaposto, come Signal.
            ("attachment", "report.pdf", "report.pdf — 📎 File"),
        ],
    )
    def test_tg_quote_unmapped_filename_enriches_placeholder(
        self, msg_type, info, expected
    ):
        """Un ``attachment_info`` non mappato compone ``filename — segnaposto``."""
        from backends.telegram import _tg_quote_text_from_cached

        target = {"id": "12", "text": "", "msg_type": msg_type, "attachment_info": info}
        assert _tg_quote_text_from_cached(target) == expected

    def test_tg_quote_caption_from_cache(self):
        """La caption (``text``) di un media quotato ha priorità."""
        from backends.telegram import _tg_quote_text_from_cached

        target = {
            "id": "12",
            "text": "che bello",
            "msg_type": "image",
            "attachment_info": "Photo",
        }
        assert _tg_quote_text_from_cached(target) == "che bello"

    def test_tg_quote_no_cache_hit_is_none(self):
        """Target non in cache → quote_text None (limitazione documentata)."""
        backend = _backend()
        backend.cache = {"42": []}
        message = _message(reply_to=SimpleNamespace(reply_to_msg_id=999))

        event = backend._message_to_chat_event(message)

        assert event.payload["quote_text"] is None
        assert event.payload["reply_to_message_id"] == "999"

    def test_tg_quote_media_through_message_to_event(self):
        """Percorso completo: reply_to risolto contro la cache → segnaposto."""
        backend = _backend()
        backend.cache = {
            "42": [
                {
                    "id": "12",
                    "text": "",
                    "msg_type": "image",
                    "attachment_info": "Photo",
                }
            ]
        }
        message = _message(reply_to=SimpleNamespace(reply_to_msg_id=12))

        event = backend._message_to_chat_event(message)

        assert event.payload["quote_text"] == "🖼️ Immagine"
        assert event.payload["reply_to_message_id"] == "12"


def test_migrate_legacy_media_dir(tmp_path, monkeypatch):
    """La migrazione one-shot sposta i media da /tmp a CACHE_DIR persistente
    e riscrive i riferimenti nel DB."""
    import sqlite3

    import backend
    from backends import telegram as tg

    monkeypatch.setattr(tg.tempfile, "gettempdir", lambda: str(tmp_path / "oldtmp"))
    monkeypatch.setattr(backend, "CACHE_DIR", tmp_path / "share")
    monkeypatch.setattr(backend, "DB_FILE", str(tmp_path / "db.sqlite"))

    old_dir = tmp_path / "oldtmp" / "telegram-media"
    old_dir.mkdir(parents=True)
    (old_dir / "photo.jpg").write_bytes(b"x")
    (old_dir / "882-1-sent.png").write_bytes(b"y")

    connection = sqlite3.connect(str(tmp_path / "db.sqlite"))
    connection.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, contact_number TEXT, "
        "text TEXT, is_mine INTEGER, timestamp INTEGER, attachment_id TEXT, "
        "quote_attachment_id TEXT, quote_attachment_path TEXT, protocol TEXT, "
        "quote_text TEXT, quote_timestamp INTEGER, quote_author TEXT, "
        "msg_type TEXT, attachment_info TEXT, content_type TEXT, read INTEGER, "
        "status TEXT, msg_id TEXT, quote_content_type TEXT, edited INTEGER, "
        "local_id TEXT, reply_to_message_id TEXT, message_part_key TEXT)"
    )
    connection.execute(
        "INSERT INTO messages (protocol, attachment_id, quote_attachment_path) "
        "VALUES ('telegram', ?, ?)",
        (str(old_dir / "photo.jpg"), str(old_dir / "882-1-sent.png")),
    )
    connection.commit()
    connection.close()

    tg._migrate_legacy_media_dir()

    new_dir = tmp_path / "share" / "telegram-media"
    assert (new_dir / "photo.jpg").is_file()
    assert (new_dir / "882-1-sent.png").is_file()
    assert not old_dir.exists()

    connection = sqlite3.connect(str(tmp_path / "db.sqlite"))
    row = connection.execute(
        "SELECT attachment_id, quote_attachment_path FROM messages"
    ).fetchone()
    connection.close()
    assert row[0] == str(new_dir / "photo.jpg")
    assert row[1] == str(new_dir / "882-1-sent.png")


class TestTelegramDisconnectLifecycle:
    @staticmethod
    def _real_loop_thread():
        loop = asyncio.new_event_loop()
        started = threading.Event()

        def run():
            asyncio.set_event_loop(loop)
            started.set()
            loop.run_forever()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        assert started.wait(timeout=1)
        return loop, thread

    def test_disconnect_sync_handles_telethon_144_sync_wrapper(self):
        import time

        backend = _backend()
        loop, thread = self._real_loop_thread()
        disconnected = threading.Event()

        class Telethon144StyleClient:
            def disconnect(self):
                async def perform_disconnect():
                    disconnected.set()

                return asyncio.shield(loop.create_task(perform_disconnect()))

        backend._loop = loop
        backend._loop_thread = thread
        backend._client = Telethon144StyleClient()
        started = time.monotonic()
        try:
            backend.disconnect_sync()
            elapsed = time.monotonic() - started

            assert disconnected.is_set()
            assert not thread.is_alive()
            assert elapsed < 1
            assert backend._loop is None
            assert backend._loop_thread is None
            assert backend._client is None
            backend.disconnect_sync()  # idempotent no-op
        finally:
            if thread.is_alive():
                loop.call_soon_threadsafe(loop.stop)
                thread.join(timeout=1)
            if not loop.is_closed():
                loop.close()

    def test_disconnect_sync_closed_loop_skips_client_cleanly(self):
        backend = _backend()
        loop = asyncio.new_event_loop()
        loop.close()
        client = SimpleNamespace(disconnect=MagicMock())
        backend._loop = loop
        backend._client = client

        backend.disconnect_sync()

        client.disconnect.assert_not_called()
        assert backend._loop is None
        assert backend._client is None
