"""Regression tests for sending from the web UI to an address-book-only contact.

Design: ``docs/DESIGN_FIX_WEB_SEND_ADDRESS_BOOK.md`` (v2.2).

``POST /api/send`` resolves contacts cache-only (zero rete): active chats via
``backend.contacts``, then the in-memory address-book cache populated by
``/api/contacts/book``.  A ghost found in the book is registered before the
send; WhatsApp also aliases ``@lid`` → the ``@c.us`` contact object.
"""

from __future__ import annotations

import threading
import time
from base64 import b64decode
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from models import (
    PROTOCOL_SIGNAL,
    PROTOCOL_TELEGRAM,
    PROTOCOL_WHATSAPP,
    ChatContact,
    ChatEvent,
)
from protocols.base import ChatBackend
from protocols.signal import SignalBackend
from protocols.telegram import TelegramBackend
from protocols.whatsapp import WhatsAppBackend
from tui.events import EventHandlingMixin
from web.api import create_api_router

_PNG_1X1 = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _MinimalBackend(ChatBackend):
    """Concrete ChatBackend implementing every abstract method trivially."""

    protocol = "test"

    def __init__(self, contacts: list[ChatContact] | None = None):
        self.contacts: list[ChatContact] = list(contacts or [])

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def list_contacts(self) -> list[ChatContact]:
        return []

    async def send_message(self, *args, **kwargs) -> str:
        return ""

    async def mark_read(self, contact_id: str) -> None:
        pass

    async def receive(self):
        if False:
            yield


def _wa_book_contact(phone: str, jid: str | None = None, name: str = "Book"):
    jid = jid or f"{phone}@c.us"
    return ChatContact(
        id=jid,
        display_name=name,
        protocol=PROTOCOL_WHATSAPP,
        extras={
            "phone": phone,
            "jid": jid,
            "address_book": True,
            "source": "wa_book",
        },
    )


def _wa_backend(*, book=None, contacts=None, lid_map=None):
    backend = WhatsAppBackend(api_url="http://api.test")
    backend.contacts = list(contacts or [])
    backend._contacts_by_jid = {contact.id: contact for contact in backend.contacts}
    backend._address_book = None if book is None else list(book)
    backend._address_book_ts = time.monotonic()
    #: ``{}`` means "cache already loaded": no disk I/O in the unit tests.
    backend._lid_map = {} if lid_map is None else dict(lid_map)
    return backend


class _RecordingManager:
    """Minimal manager that records the send surface used by ``/api/send``."""

    def __init__(self, backend):
        self.backend = backend
        self.send_calls: list[tuple] = []
        self.attachments_calls: list[dict] = []
        self.paths: dict[tuple, Path] = {}
        self.list_contacts_calls = 0

    def get(self, protocol):
        if self.backend is not None and self.backend.protocol == protocol:
            return self.backend
        return None

    def list_contacts(self):
        self.list_contacts_calls += 1
        return list(self.backend.contacts) if self.backend else []

    def get_attachment_path(self, protocol, attachment_id):
        return self.paths.get((protocol, attachment_id))

    def send_message_sync(self, protocol, contact_id, text, **kwargs):
        self.send_calls.append((protocol, contact_id, text, kwargs))
        return "sent-id"

    def send_attachments_sync(
        self,
        protocol,
        contact_id,
        file_paths,
        *,
        batch_id=None,
        captions=(),
        mime_types=(),
        media_kinds=(),
        filenames=(),
        **kwargs,
    ):
        self.attachments_calls.append(
            {
                "protocol": protocol,
                "contact_id": contact_id,
                "paths": [Path(path) for path in file_paths],
                "batch_id": batch_id,
                "captions": list(captions),
                "mime_types": list(mime_types),
                "media_kinds": list(media_kinds),
                "filenames": list(filenames),
                "kwargs": dict(kwargs),
            }
        )
        return [f"id-{index}" for index in range(len(file_paths))]


def _app(manager) -> FastAPI:
    app = FastAPI()
    app.state.manager = manager
    app.include_router(create_api_router())
    return app


# ─── §5.1 find_contact / find_address_book_contact ────────────────────────────


class TestFindContact:
    def test_base_defaults(self):
        contact = ChatContact("alice", "Alice", "test")
        backend = _MinimalBackend([contact])

        assert backend.find_contact("alice") is contact
        assert backend.find_contact("nobody") is None
        assert backend.find_address_book_contact("alice") is None

    def test_signal_address_book_is_contacts(self):
        backend = SignalBackend("+3901")
        contact = ChatContact("+3902", "Bob", PROTOCOL_SIGNAL)
        backend._set_contacts([contact])

        assert backend.find_contact("+3902") is contact
        assert backend.find_address_book_contact("+3902") is contact
        assert backend.find_contact("+3999") is None

    def test_whatsapp_prefers_active_chat_over_book(self):
        active = ChatContact("393@c.us", "Active", PROTOCOL_WHATSAPP)
        book = _wa_book_contact("393", name="Book")
        backend = _wa_backend(book=[book], contacts=[active])

        assert backend.find_contact("393@c.us") is active
        assert backend.find_address_book_contact("393@c.us") is book

    def test_whatsapp_falls_back_to_book(self):
        book = _wa_book_contact("393")
        backend = _wa_backend(book=[book])

        assert backend.find_contact("393@c.us") is book

    def test_telegram_falls_back_to_book(self):
        backend = TelegramBackend()
        book = ChatContact(
            "42",
            "Mamma",
            PROTOCOL_TELEGRAM,
            extras={"address_book": True},
        )
        backend._address_book = [book]

        assert backend.find_address_book_contact("42") is book
        assert backend.find_contact("42") is book
        assert backend.find_contact("99") is None


# ─── §5.2 zero network in the lookup ─────────────────────────────────────────


class TestCacheOnlyLookup:
    def test_send_never_consults_manager_list_contacts(self):
        book = _wa_book_contact("393")
        backend = _wa_backend(book=[book])
        manager = _RecordingManager(backend)

        with patch("web.api.push_event"), TestClient(_app(manager)) as client:
            response = client.post(
                "/api/send",
                json={
                    "protocol": "whatsapp",
                    "contact_id": "393@c.us",
                    "text": "Ciao",
                },
            )

        assert response.status_code == 200
        assert manager.list_contacts_calls == 0

    def test_unit_lookup_touches_no_network(self, monkeypatch):
        book = _wa_book_contact("393")
        backend = _wa_backend(book=[book])
        backend.list_address_book_sync = MagicMock()
        backend._rest.list_all_contacts = MagicMock()
        backend._lid_resolve_remote = MagicMock()
        to_thread = MagicMock()
        monkeypatch.setattr("web.api.asyncio.to_thread", to_thread)

        assert backend.find_contact("393@c.us") is book

        backend.list_address_book_sync.assert_not_called()
        backend._rest.list_all_contacts.assert_not_called()
        backend._lid_resolve_remote.assert_not_called()
        to_thread.assert_not_called()

    def test_send_book_only_never_fetches(self):
        book = _wa_book_contact("393")
        backend = _wa_backend(book=[book])
        backend.list_address_book_sync = MagicMock()
        backend._rest.list_all_contacts = MagicMock()
        backend._lid_resolve_remote = MagicMock()
        manager = _RecordingManager(backend)

        with patch("web.api.push_event"), TestClient(_app(manager)) as client:
            response = client.post(
                "/api/send",
                json={
                    "protocol": "whatsapp",
                    "contact_id": "393@c.us",
                    "text": "Ciao",
                },
            )

        assert response.status_code == 200
        assert len(manager.send_calls) == 1
        assert manager.list_contacts_calls == 0
        backend.list_address_book_sync.assert_not_called()
        backend._rest.list_all_contacts.assert_not_called()
        backend._lid_resolve_remote.assert_not_called()


# ─── §5.3 concurrency and dedup ──────────────────────────────────────────────


def _concurrent_backend(name):
    if name == "minimal":
        return _MinimalBackend(), PROTOCOL_SIGNAL
    if name == "signal":
        return SignalBackend("+3901"), PROTOCOL_SIGNAL
    if name == "whatsapp":
        return _wa_backend(), PROTOCOL_WHATSAPP
    if name == "telegram":
        return TelegramBackend(), PROTOCOL_TELEGRAM
    raise AssertionError(name)


@pytest.mark.parametrize("name", ["minimal", "signal", "whatsapp", "telegram"])
def test_register_contact_barrier_single_append(name):
    backend, protocol = _concurrent_backend(name)
    contact_id = "12345@c.us" if name == "whatsapp" else "12345"
    extra = {"phone": "12345"} if name == "whatsapp" else {}

    first = ChatContact(contact_id, "First", protocol, extras=dict(extra))
    second = ChatContact(contact_id, "Second", protocol, extras=dict(extra))
    barrier = threading.Barrier(2)
    results: list[bool] = []

    def worker(contact):
        barrier.wait()
        results.append(backend.register_contact(contact))

    threads = [
        threading.Thread(target=worker, args=(contact,)) for contact in (first, second)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [False, True]
    assert len(backend.contacts) == 1
    winner = backend.contacts[0]
    if name == "signal":
        assert backend._contacts_by_key[winner.cache_key] is winner
    elif name == "whatsapp":
        assert backend._contacts_by_jid[winner.id] is winner
    elif name == "telegram":
        assert backend._contacts_by_id[int(winner.id)] is winner


def test_register_lock_is_class_scoped():
    lock = ChatBackend._register_lock
    assert hasattr(lock, "acquire") and hasattr(lock, "release")
    for backend_cls in (
        _MinimalBackend,
        SignalBackend,
        WhatsAppBackend,
        TelegramBackend,
    ):
        assert backend_cls._register_lock is lock


# ─── §5.4 active-chat regression ─────────────────────────────────────────────


class TestActiveChatRegression:
    def test_find_contact_does_not_touch_book(self):
        active = ChatContact("alice", "Alice", PROTOCOL_WHATSAPP)
        backend = _wa_backend(book=[_wa_book_contact("393")], contacts=[active])
        backend.find_address_book_contact = MagicMock()

        assert backend.find_contact("alice") is active
        backend.find_address_book_contact.assert_not_called()

    def test_send_active_chat_skips_registration_and_keeps_id(self):
        active = ChatContact("alice", "Alice", PROTOCOL_WHATSAPP)
        backend = _wa_backend(book=[_wa_book_contact("393")], contacts=[active])
        backend.find_address_book_contact = MagicMock()
        backend.register_contact = MagicMock()
        manager = _RecordingManager(backend)

        with patch("web.api.push_event") as pushed, TestClient(_app(manager)) as client:
            response = client.post(
                "/api/send",
                json={
                    "protocol": "whatsapp",
                    "contact_id": "alice",
                    "text": "Ciao",
                },
            )

        assert response.status_code == 200
        backend.find_address_book_contact.assert_not_called()
        backend.register_contact.assert_not_called()
        assert manager.send_calls[0][1] == "alice"
        assert pushed.call_args.args[0]["payload"]["contact_id"] == "alice"


# ─── §5.5 WhatsApp @c.us/@lid identity ───────────────────────────────────────


class TestWhatsAppIdentity:
    def test_book_only_send_keeps_client_id_everywhere(self):
        book = _wa_book_contact("393")
        backend = _wa_backend(book=[book], lid_map={})
        manager = _RecordingManager(backend)

        with patch("web.api.push_event") as pushed, TestClient(_app(manager)) as client:
            response = client.post(
                "/api/send",
                json={
                    "protocol": "whatsapp",
                    "contact_id": "393@c.us",
                    "text": "Ciao",
                },
            )

        assert response.status_code == 200
        assert manager.send_calls[0][1] == "393@c.us"
        payload = pushed.call_args.args[0]["payload"]
        assert payload["contact_id"] == "393@c.us"
        assert backend.contacts == [book]
        assert book.extras["ghost"] is True

    def test_book_only_attachment_keeps_client_id(self):
        book = _wa_book_contact("393")
        backend = _wa_backend(book=[book], lid_map={})
        manager = _RecordingManager(backend)

        with patch("web.api.push_event") as pushed, TestClient(_app(manager)) as client:
            response = client.post(
                "/api/send",
                data={"protocol": "whatsapp", "contact_id": "393@c.us", "text": ""},
                files={"file": ("clipboard.png", _PNG_1X1, "image/png")},
            )

        assert response.status_code == 200
        assert manager.attachments_calls[0]["contact_id"] == "393@c.us"
        assert manager.send_calls == []
        assert pushed.call_args.args[0]["payload"]["contact_id"] == "393@c.us"

    def test_alias_points_to_same_client_contact(self):
        now = int(time.time())
        book = _wa_book_contact("393")
        backend = _wa_backend(
            book=[book],
            lid_map={"393@lid": {"phone": "393", "resolved_at": now}},
        )

        assert backend.register_contact(book) is True
        assert backend._identify_contact("393@lid") is book
        assert book.id == "393@c.us"
        assert backend.contacts == [book]

    def test_alias_setdefault_never_overwrites_real_lid(self):
        now = int(time.time())
        real_lid = ChatContact("393@lid", "Real", PROTOCOL_WHATSAPP)
        book = _wa_book_contact("393")
        backend = _wa_backend(
            book=[book],
            lid_map={"393@lid": {"phone": "393", "resolved_at": now}},
        )
        backend._contacts_by_jid["393@lid"] = real_lid

        backend.register_contact(book)

        assert backend._identify_contact("393@lid") is real_lid

    def test_alias_is_created_when_cache_fills_later(self):
        now = int(time.time())
        book = _wa_book_contact("393")
        backend = _wa_backend(book=[book], lid_map={})

        assert backend.register_contact(book) is True
        assert backend._identify_contact("393@lid") is None

        backend._lid_map["393@lid"] = {"phone": "393", "resolved_at": now}
        assert backend.register_contact(book) is False

        assert backend._identify_contact("393@lid") is book
        assert backend.contacts == [book]

    def test_alias_retried_on_second_send_after_lid_cache_fills(self):
        now = int(time.time())
        book = _wa_book_contact("393")
        backend = _wa_backend(book=[book], lid_map={})
        manager = _RecordingManager(backend)

        def send():
            with patch("web.api.push_event"), TestClient(_app(manager)) as client:
                return client.post(
                    "/api/send",
                    json={
                        "protocol": "whatsapp",
                        "contact_id": "393@c.us",
                        "text": "Ciao",
                    },
                )

        assert send().status_code == 200
        assert backend._identify_contact("393@lid") is None

        backend._lid_map["393@lid"] = {"phone": "393", "resolved_at": now}

        assert send().status_code == 200
        assert backend._identify_contact("393@lid") is book
        assert backend.contacts == [book]

    def test_register_lid_alias_early_return_without_phone_or_c_us(self):
        backend = _wa_backend()

        backend._register_lid_alias(ChatContact("393@lid", "Lid", PROTOCOL_WHATSAPP))
        backend._register_lid_alias(
            ChatContact("393@c.us", "NoPhone", PROTOCOL_WHATSAPP, extras={})
        )

        assert backend._contacts_by_jid == {}

    def test_incoming_lid_event_ingests_under_client_contact(self):
        now = int(time.time())
        book = _wa_book_contact("393")
        backend = _wa_backend(
            book=[book],
            lid_map={"393@lid": {"phone": "393", "resolved_at": now}},
        )
        backend.register_contact(book)
        backend.ingest_message = MagicMock(return_value=True)

        harness = EventHandlingMixin()
        harness.manager = SimpleNamespace(get=lambda protocol: backend)
        harness.contacts = [book]
        harness.selected_contact = None
        harness._contact_list_dirty = False
        harness._dirty_contact_keys = set()
        harness._cache = {book.cache_key: []}
        harness._web_enabled = True
        harness._typing_contacts = {}
        harness._typing_mumbling = {}
        harness._TYPING_MUMBLING_DURATION = 100
        harness._seen_message_ids = set()
        harness._seen_timestamps = set()
        harness.call_from_thread = MagicMock()

        event = ChatEvent(
            type="message",
            protocol=PROTOCOL_WHATSAPP,
            contact_id="393@lid",
            payload={
                "id": "m1",
                "text": "ciao",
                "is_mine": False,
                "timestamp": 123,
                "msg_type": "text",
            },
        )
        with patch("web.bridge.push_event") as pushed:
            assert harness._handle_message_event(event) is True

        assert backend.ingest_message.call_args.args[0] == "393@c.us"
        assert pushed.call_args.args[0]["payload"]["contact_id"] == "393@c.us"

    def test_phone_to_lid_applies_ttl(self):
        backend = _wa_backend()
        ttl_seconds = 30 * 86400
        now = int(time.time())

        backend._lid_map = {
            "393@lid": {"phone": "393", "resolved_at": now - ttl_seconds - 60}
        }
        assert backend._phone_to_lid("393") is None

        backend._lid_map = {"393@lid": {"phone": "393", "resolved_at": now}}
        assert backend._phone_to_lid("393") == "393@lid"


# ─── §5.5b Telegram book-only E2E ────────────────────────────────────────────


class TestTelegramBookOnlySend:
    def test_book_only_send_registers_and_indexes_id(self):
        book = ChatContact(
            "42",
            "Mamma",
            PROTOCOL_TELEGRAM,
            extras={"address_book": True},
        )
        backend = TelegramBackend()
        backend._address_book = [book]
        backend._address_book_ts = time.monotonic()
        manager = _RecordingManager(backend)

        with patch("web.api.push_event") as pushed, TestClient(_app(manager)) as client:
            response = client.post(
                "/api/send",
                json={
                    "protocol": "telegram",
                    "contact_id": "42",
                    "text": "Ciao",
                },
            )

        assert response.status_code == 200
        assert manager.send_calls[0][1] == "42"
        assert backend.contacts == [book]
        assert backend._contacts_by_id[42] is book
        assert pushed.call_args.args[0]["payload"]["contact_id"] == "42"


# ─── §5.6 stale cache / missing cache ────────────────────────────────────────


class TestCacheSemantics:
    def test_stale_cache_is_usable(self):
        book = _wa_book_contact("393")
        backend = _wa_backend(book=[book])
        backend._address_book_ts = 0.0

        assert backend.find_address_book_contact("393@c.us") is book
        assert backend.find_contact("393@c.us") is book

    def test_missing_cache_returns_none_and_404(self):
        backend = _wa_backend(book=None)
        assert backend.find_address_book_contact("393@c.us") is None
        manager = _RecordingManager(backend)

        with TestClient(_app(manager)) as client:
            response = client.post(
                "/api/send",
                json={
                    "protocol": "whatsapp",
                    "contact_id": "393@c.us",
                    "text": "Ciao",
                },
            )

        assert response.status_code == 404
        assert manager.send_calls == []

    def test_unknown_id_with_populated_book_404(self):
        book = _wa_book_contact("393")
        backend = _wa_backend(book=[book])
        manager = _RecordingManager(backend)

        with TestClient(_app(manager)) as client:
            response = client.post(
                "/api/send",
                json={
                    "protocol": "whatsapp",
                    "contact_id": "999@c.us",
                    "text": "Ciao",
                },
            )

        assert response.status_code == 404
        assert manager.send_calls == []


# ─── §5.7 register_contact bool contract ─────────────────────────────────────


class TestRegisterContactContract:
    def test_returns_true_then_false(self):
        backend = _MinimalBackend()
        contact = ChatContact("alice", "Alice", "test")

        assert backend.register_contact(contact) is True
        assert backend.register_contact(contact) is False
        assert backend.contacts == [contact]

    def test_dedup_is_by_cache_key_not_eq(self):
        first = ChatContact("alice", "Alice", "test", extras={"a": 1})
        second = ChatContact("alice", "Alice", "test", extras={"a": 2})
        backend = _MinimalBackend([first])

        assert backend.register_contact(second) is False
        assert backend.contacts == [first]

    def test_overrides_update_index_only_on_append(self):
        backend = _wa_backend()
        contact = _wa_book_contact("393")

        assert backend.register_contact(contact) is True
        assert backend._contacts_by_jid["393@c.us"] is contact

        other = _wa_book_contact("393", name="Other")
        assert backend.register_contact(other) is False
        assert backend._contacts_by_jid["393@c.us"] is contact
