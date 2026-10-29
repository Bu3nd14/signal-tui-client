"""Contract: ``GET /api/messages`` returns exactly what ``messages.page`` does.

Real SQLite rows (isolated by the autouse conftest fixture), fake manager and
backends.  Each case compares the HTTP body with ``api.call_json`` on the same
manager, and also pins the behaviour itself.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from client_harness import api_for
from fastapi import FastAPI
from fastapi.testclient import TestClient

from protocols.db import _add_message_to_cache
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

    def get(self, protocol):
        return self._backends.get(protocol)

    def list_contacts(self):
        return list(self._contacts)

    def list_address_book_sync(self, protocols=None, force=False):
        return list(self._book)


def contact(cid, name, proto="whatsapp", phone="", **extras):
    if phone:
        extras["phone"] = phone
    return SimpleNamespace(
        id=cid, display_name=name, protocol=proto, extras=extras, phone=phone
    )


def make(manager):
    app = FastAPI()
    app.state.api = api_for(manager)
    install_auth(app, TOKEN)
    app.include_router(create_api_router())
    return TestClient(app), get_api(app)


def add(contact_id, text, ts, *, mine=False, sender="", protocol="signal", **kw):
    _add_message_to_cache(contact_id, text, mine, sender, ts, protocol=protocol, **kw)


def get(client, protocol, contact_id, **params):
    return client.get(
        "/api/messages",
        params={"proto": protocol, "contact_id": contact_id, **params},
        headers=AUTH,
    )


def op_args(protocol, contact_id, **extra):
    return {"protocol": protocol, "contact_id": contact_id, **extra}


def test_one_to_one_matches_operation():
    add("+1", "hello", 1000, sender="Alice", msg_id="m1", quote_author="+1")
    add("+1", "yo", 2000, mine=True, sender="You", msg_id="m2")
    client, api = make(Manager())
    response = get(client, "signal", "+1")
    assert response.status_code == 200
    assert response.json() == api.call_json("messages.page", op_args("signal", "+1"))
    body = response.json()
    assert [m["id"] for m in body] == ["m1", "m2"]
    assert [m["direction"] for m in body] == ["in", "out"]
    assert [m["quote_author"] for m in body] == ["", ""]
    assert "is_group" not in body[0]
    assert body[1]["status"] == "sent" and body[0]["status"] is None


def test_unknown_chat_is_empty():
    client, api = make(Manager())
    response = get(client, "signal", "nobody")
    assert (
        response.json()
        == []
        == api.call_json("messages.page", op_args("signal", "nobody"))
    )


def test_limit_and_since_semantics():
    for index in range(1, 6):
        add("+1", f"t{index}", index * 1000, msg_id=f"m{index}")
    client, api = make(Manager())
    for params, expected in (
        ({"limit": 2}, ["t4", "t5"]),
        ({"since": 3000}, ["t3", "t4", "t5"]),
        ({"since": 3000, "limit": 2}, ["t4", "t5"]),
        ({"limit": 2000}, ["t1", "t2", "t3", "t4", "t5"]),
    ):
        response = get(client, "signal", "+1", **params)
        assert response.status_code == 200
        assert [m["text"] for m in response.json()] == expected
        assert response.json() == api.call_json(
            "messages.page", op_args("signal", "+1", **params)
        )


@pytest.mark.parametrize("limit", [0, -1, 2001])
def test_invalid_limit_is_400_in_route_and_operation(limit):
    from server.api import ApiError

    client, api = make(Manager())
    response = get(client, "signal", "+1", limit=limit)
    assert (response.status_code, response.json()) == (
        400,
        {"detail": "Invalid limit"},
    )
    with pytest.raises(ApiError) as excinfo:
        api.call_json("messages.page", op_args("signal", "+1", limit=limit))
    assert (excinfo.value.code, excinfo.value.message) == (
        "invalid_request",
        "Invalid limit",
    )


def test_invalid_protocol_is_rejected_by_route():
    client, _ = make(Manager())
    assert get(client, "irc", "+1").status_code == 422


def test_group_resolves_sender_and_quote_author():
    group = "120@g.us"
    add(
        group,
        "hi",
        1000,
        protocol="whatsapp",
        sender="111@lid",
        msg_id="g1",
        quote_author="222@lid",
    )
    add(
        group,
        "ok",
        2000,
        protocol="whatsapp",
        sender="222@lid",
        msg_id="g2",
        quote_author="333@lid",
        quote_timestamp=1000,
    )
    book = [
        contact("111@c.us", "Mario", phone="111", lid="111@lid"),
        contact("222@c.us", "Luisa", phone="222", lid="222@lid"),
        # A book row whose display name is itself an unresolved lid.
        contact("333@lid", "333@lid"),
    ]
    client, api = make(Manager(book=book))
    response = get(client, "whatsapp", group)
    assert response.json() == api.call_json("messages.page", op_args("whatsapp", group))
    first, second = response.json()
    assert first["is_group"] and second["is_group"]
    assert (first["sender"], first["quote_author"]) == ("Mario", "Luisa")
    # 333@lid is unknown: fall back to the sender of the quoted message.
    assert (second["sender"], second["quote_author"]) == ("Luisa", "Mario")


def test_group_detected_from_contact_extras():
    add("room", "x", 1000, protocol="telegram", sender="a", msg_id="t1")
    manager = Manager(
        {"telegram": Backend(fetch_history=lambda *_: [])},
        contacts=[contact("room", "Room", "telegram", is_group=True)],
    )
    client, api = make(manager)
    response = get(client, "telegram", "room")
    assert response.json() == api.call_json(
        "messages.page", op_args("telegram", "room")
    )
    assert response.json()[0]["is_group"] is True


def test_whatsapp_ghost_id_is_canonicalized_and_unioned():
    canonical, alias = "39123@lid", "39123@c.us"
    add(alias, "old", 1000, protocol="whatsapp", msg_id="M1")
    add(canonical, "new", 2000, protocol="whatsapp", msg_id="M2")
    fetched: list[tuple[str, int]] = []
    active = contact(canonical, "Ann", phone="39123", lid=canonical)

    def find_contact(contact_id):
        return active if contact_id in {canonical, alias} else None

    backend = Backend(
        find_contact=find_contact,
        fetch_history=lambda cid, n, **_kw: fetched.append((cid, n)) or [],
    )
    client, api = make(Manager({"whatsapp": backend}))
    response = get(client, "whatsapp", alias, refresh="true")
    assert response.status_code == 200
    # Refresh runs on the ACTIVE (@lid) chat, not the ghost id.
    assert fetched == [(canonical, 20)]
    # Reading by the ghost id must union both keys too: the refresh just wrote
    # the live history under @lid, which a ghost-only read would hide.
    assert [m["text"] for m in response.json()] == ["old", "new"]
    assert response.json() == api.call_json(
        "messages.page", op_args("whatsapp", alias, refresh=True)
    )
    assert fetched == [(canonical, 20)] * 2
    # Reading by the canonical id unions the history split over both keys.
    response = get(client, "whatsapp", canonical)
    assert [m["text"] for m in response.json()] == ["old", "new"]
    assert response.json() == api.call_json(
        "messages.page", op_args("whatsapp", canonical)
    )


@pytest.mark.parametrize(
    ("protocol", "params", "calls"),
    [
        ("telegram", {}, 1),
        ("telegram", {"refresh": "true"}, 1),
        ("signal", {}, 0),
        ("whatsapp", {}, 0),
        ("whatsapp", {"refresh": "true"}, 1),
        ("signal", {"refresh": "true"}, 1),
    ],
)
def test_history_refresh_policy(protocol, params, calls):
    seen = []
    backend = Backend(fetch_history=lambda cid, n, **_kw: seen.append((cid, n)) or [])
    client, api = make(Manager({protocol: backend}))
    response = get(client, protocol, "42", **params)
    assert response.status_code == 200
    assert seen == [("42", 20)] * calls
    op_params = {k: v == "true" for k, v in params.items()}
    assert response.json() == api.call_json(
        "messages.page", op_args(protocol, "42", **op_params)
    )


def test_telegram_refresh_runs_off_the_event_loop_and_errors_are_swallowed():
    import threading

    main = threading.get_ident()
    threads = []

    def boom(cid, n):
        threads.append(threading.get_ident())
        raise RuntimeError("telegram down")

    add("42", "cached", 1000, protocol="telegram", msg_id="t1")
    client, _ = make(Manager({"telegram": Backend(fetch_history=boom)}))
    response = get(client, "telegram", "42")
    assert response.status_code == 200
    assert [m["text"] for m in response.json()] == ["cached"]
    assert threads and main not in threads
