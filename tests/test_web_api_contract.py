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
