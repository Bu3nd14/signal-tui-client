"""Contract: each migrated HTTP route returns exactly what the operation does.

For every migrated operation the route output (status 200) equals
``ChatAPI.call_json`` for the same input, on the same manager.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.api import create_api_router
from web.auth import install_auth
from web.ops import get_api

TOKEN = "contract-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class Backend:
    def __init__(self, **attrs):
        self.__dict__.update(attrs)


class Manager:
    def __init__(self, backends=None, contacts=None, book=None):
        self._backends = backends or {}
        self._contacts = contacts or []
        self._book = book or []
        self.read_calls = []

    def get(self, protocol):
        return self._backends.get(protocol)

    def list_contacts(self):
        return list(self._contacts)

    def list_address_book_sync(self, force=False):
        return list(self._book)

    async def mark_read(self, protocol, contact_id):
        self.read_calls.append((protocol, contact_id))


def make(manager):
    app = FastAPI()
    app.state.manager = manager
    install_auth(app, TOKEN)
    app.include_router(create_api_router())
    return TestClient(app), get_api(app)


def contact(cid, name, proto="signal", **extras):
    return SimpleNamespace(
        id=cid,
        display_name=name,
        protocol=proto,
        extras=extras,
        phone="",
        cache_key=f"{proto}:{cid}",
    )


@pytest.fixture
def no_db(monkeypatch):
    monkeypatch.setattr(
        "server.ops.contacts._unread_counts", lambda: {("signal", "+1"): 4}
    )


def test_emoji_matches_operation():
    client, api = make(Manager())
    response = client.get("/api/emoji", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == api.call_json("emoji.list")
    assert response.headers["cache-control"] == "public, max-age=3600"
    assert list(response.json()[0]) == ["category", "icon", "emojis", "aliases"]


@pytest.mark.parametrize("protocol", ["signal", "telegram", "bogus"])
def test_reactions_matches_operation(protocol):
    manager = Manager(
        {"telegram": Backend(get_available_reactions=lambda: ["👍", "❤"])}
    )
    client, api = make(manager)
    response = client.get("/api/reactions", params={"protocol": protocol}, headers=AUTH)
    assert response.json() == api.call_json(
        "reactions.available", {"protocol": protocol}
    )
    expected = ["👍", "❤"] if protocol == "telegram" else []
    assert response.json() == {"emojis": expected}


def test_status_matches_operation():
    client, api = make(Manager({"signal": Backend(is_connected=True)}))
    response = client.get("/api/status", headers=AUTH)
    assert response.json() == api.call_json("server.status")
    assert response.json() == {"signal": True, "whatsapp": False, "telegram": False}


def _contacts_manager(reconcile=None):
    chats = [
        contact("+1", "Alice", last_message_ts=1700, avatar="a.png"),
        contact("+2", "Bob"),
    ]
    book = [contact("+1", "Alice"), contact("+3", "Alberto"), contact("+4", "Zed")]
    backends = {"whatsapp": Backend(reconcile_unread=reconcile)} if reconcile else {}
    return Manager(backends, contacts=chats, book=book)


@pytest.mark.parametrize("params", [{}, {"q": "ali"}, {"q": "  "}, {"q": "zzz"}])
def test_contacts_matches_operation(no_db, params):
    client, api = make(_contacts_manager())
    response = client.get("/api/contacts", params=params, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == api.call_json("contacts.list", params)
    if not params:
        assert response.json()[0] == {
            "id": "+1",
            "display_name": "Alice",
            "protocol": "signal",
            "extras": {"last_message_ts": 1700, "avatar": "a.png"},
            "last_message_ts": 1700,
            "unread": 4,
        }
        assert list(response.json()[0]) == [
            "id",
            "display_name",
            "protocol",
            "extras",
            "last_message_ts",
            "unread",
        ]


def test_contacts_reconcile_failure_is_swallowed(no_db):
    def boom():
        raise RuntimeError("phone offline")

    client, _ = make(_contacts_manager(boom))
    assert len(client.get("/api/contacts", headers=AUTH).json()) == 2


@pytest.mark.parametrize(
    "params", [{"q": "al"}, {"q": "al", "force": "true"}, {"q": " "}]
)
def test_contacts_book_matches_operation(no_db, params):
    client, api = make(_contacts_manager())
    response = client.get("/api/contacts/book", params=params, headers=AUTH)
    assert response.status_code == 200
    data = {"q": params["q"], "force": params.get("force") == "true"}
    assert response.json() == api.call_json("contacts.book", data)
    assert len(response.json()) == (2 if params["q"] == "al" else 0)


def test_contacts_book_requires_q():
    client, _ = make(_contacts_manager())
    assert client.get("/api/contacts/book", headers=AUTH).status_code == 422


READ = {"protocol": "signal", "contact_id": "+1"}


def test_messages_read_matches_operation_and_marks_read():
    manager = Manager()
    client, api = make(manager)
    response = client.post("/api/messages/read", json=READ, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == api.call_json("chats.mark_read", READ) == {"status": "ok"}
    assert manager.read_calls == [("signal", "+1")] * 2


def test_messages_read_strips_and_swallows_backend_errors():
    manager = Manager()

    async def boom(protocol, contact_id):
        raise RuntimeError("down")

    manager.mark_read = boom
    client, _ = make(manager)
    response = client.post(
        "/api/messages/read",
        json={"protocol": " signal ", "contact_id": " +1 "},
        headers=AUTH,
    )
    assert (response.status_code, response.json()) == (200, {"status": "ok"})


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"protocol": "signal"},
        {"protocol": "irc", "contact_id": "x"},
        {"protocol": "signal", "contact_id": "  "},
        {"protocol": None, "contact_id": 5},
    ],
)
def test_messages_read_invalid_is_400(payload):
    client, _ = make(Manager())
    response = client.post("/api/messages/read", json=payload, headers=AUTH)
    assert (response.status_code, response.json()) == (
        400,
        {"detail": "Invalid request"},
    )


ARCHIVE = {"protocol": "whatsapp", "contact_id": "1@c.us", "archived": True}


def test_chats_archive_matches_operation():
    calls = []

    def archive(contact_id, archived):
        calls.append((contact_id, archived))
        return True

    client, api = make(Manager({"whatsapp": Backend(set_chat_archived_sync=archive)}))
    response = client.post("/api/chats/archive", json=ARCHIVE, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == api.call_json("chats.archive", ARCHIVE)
    assert response.json() == {"archived": True}
    assert calls == [("1@c.us", True)] * 2


@pytest.mark.parametrize(
    "payload",
    [
        {**ARCHIVE, "protocol": "signal"},
        {**ARCHIVE, "contact_id": ""},
        {**ARCHIVE, "archived": "yes"},
        {**ARCHIVE, "archived": 1},
        {"protocol": "whatsapp", "contact_id": "1@c.us"},
    ],
)
def test_chats_archive_invalid_is_400(payload):
    client, _ = make(
        Manager({"whatsapp": Backend(set_chat_archived_sync=lambda *a: 1)})
    )
    response = client.post("/api/chats/archive", json=payload, headers=AUTH)
    assert (response.status_code, response.json()) == (
        400,
        {"detail": "Invalid request"},
    )


@pytest.mark.parametrize(
    "backends, status, detail",
    [
        ({}, 503, "Backend unavailable"),
        ({"whatsapp": Backend()}, 503, "Backend unavailable"),
        (
            {"whatsapp": Backend(set_chat_archived_sync=lambda *a: False)},
            502,
            "Archive failed",
        ),
    ],
)
def test_chats_archive_backend_errors(backends, status, detail):
    client, _ = make(Manager(backends))
    response = client.post("/api/chats/archive", json=ARCHIVE, headers=AUTH)
    assert (response.status_code, response.json()) == (status, {"detail": detail})
