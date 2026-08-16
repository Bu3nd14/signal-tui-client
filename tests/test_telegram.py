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
from telethon.tl.types import Channel, Chat, User

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


class TestTelegramContacts:
    def test_attachment_path_returns_existing_file_only(self, tmp_path):
        backend = _backend()
        existing = tmp_path / "attachment.txt"
        existing.write_text("data")

        assert backend.get_attachment_path(str(existing)) == existing
        assert backend.get_attachment_path(str(tmp_path / "missing")) is None

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
            "msg_type": "text",
            "attachment_info": None,
            "attachment_id": None,
            "status": "sent",
            "protocol": PROTOCOL_TELEGRAM,
            "contact": contact,
        }

        assert backend._message_to_chat_event(_message(chat_id=None)) is None

    @pytest.mark.parametrize(
        ("field", "expected_type", "expected_info"),
        [
            ("photo", "image", "🖼️ Photo"),
            ("sticker", "sticker", "🎨 Sticker"),
            ("video", "attachment", "🎬 Video"),
            ("voice", "attachment", "🎤 Voice"),
            ("audio", "attachment", "🎵 Audio"),
        ],
    )
    def test_message_to_event_normalizes_media(
        self, field, expected_type, expected_info
    ):
        backend = _backend()
        event = backend._message_to_chat_event(
            _message(**{field: object()}), "/tmp/media"
        )

        assert event.payload["msg_type"] == expected_type
        assert event.payload["attachment_info"] == expected_info
        assert event.payload["attachment_id"] == "/tmp/media"

    def test_message_to_event_uses_document_filename_and_id_fallback(self):
        backend = _backend()
        document = SimpleNamespace(attributes=[SimpleNamespace(file_name="report.pdf")])

        event = backend._message_to_chat_event(_message(document=document))

        assert event.payload["msg_type"] == "attachment"
        assert event.payload["attachment_info"] == "📎 report.pdf"
        assert event.payload["attachment_id"] == "99"

    def test_receipts_polling_and_ingest_deduplication(self, monkeypatch):
        backend = _backend()
        backend.cache = {
            "42": [
                {"id": "1", "is_mine": True, "status": "sent", "timestamp": 1},
                {"id": "2", "is_mine": True, "status": "read", "timestamp": 2},
            ]
        }
        persist_status = MagicMock()
        monkeypatch.setattr("backend._update_message_status", persist_status)

        assert backend.process_receipt({}) == []
        assert [
            item["id"] for item in backend.process_receipt({"message_ids": ["1", "2"]})
        ] == ["1"]
        assert backend.cache["42"][0]["status"] == "delivered"
        assert (
            backend.process_receipt({"message_ids": ["1"], "is_read": True})[0][
                "status"
            ]
            == "read"
        )
        assert persist_status.call_count == 2

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


class TestTelegramBackendOperations:
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
        backend._client = SimpleNamespace(sign_in=AsyncMock(), disconnect=AsyncMock())
        backend._loop = SimpleNamespace(
            run_until_complete=lambda coro: asyncio.run(coro), close=MagicMock()
        )
        assert backend.complete_2fa("password") is True
        assert backend._connected is True

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
