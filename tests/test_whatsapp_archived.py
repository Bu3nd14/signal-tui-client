"""Archived WhatsApp chats: flag from /chats, archive actions, chat.archive."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from client_harness import api_for
from fastapi import FastAPI
from fastapi.testclient import TestClient

from protocols.whatsapp import WhatsAppBackend
from protocols.whatsapp_rest import WhatsAppRESTClient
from web.api import create_api_router

ARCHIVED = "391111@c.us"
ACTIVE = "392222@c.us"


def _backend() -> WhatsAppBackend:
    backend = WhatsAppBackend(api_url="http://api.test", media_dir="")
    backend._rest = MagicMock()
    backend._rest.list_contacts.return_value = [
        {
            "id": ARCHIVED,
            "name": "Old",
            "isGroup": False,
            "last_ts": 1,
            "archived": True,
        },
        {
            "id": ACTIVE,
            "name": "New",
            "isGroup": False,
            "last_ts": 2,
            "archived": False,
        },
    ]
    backend._rest.list_all_contacts.return_value = []
    backend._connected = True
    with patch.object(backend, "start_lid_resolver"):
        backend._load_contacts()
    return backend


def _archived_flags(backend: WhatsAppBackend) -> dict[str, bool]:
    return {c.id: bool(c.extras.get("archived")) for c in backend.contacts}


def _drain(backend: WhatsAppBackend) -> list:
    events = []
    while not backend._events.empty():
        event = backend._events.get_nowait()
        if event is not None:
            events.append(event)
    return events


def test_normalize_chat_reads_archived_flag():
    client = WhatsAppRESTClient("http://api.test")
    assert client._normalize_chat({"id": "1@c.us", "archived": True})["archived"]
    assert not client._normalize_chat({"id": "1@c.us"})["archived"]


def test_set_chat_archived_calls_waha_endpoint():
    client = WhatsAppRESTClient("http://api.test")
    with patch.object(client, "_request", return_value={}) as request:
        assert client.set_chat_archived("39@c.us", True)
        assert client.set_chat_archived("39@c.us", False)
    paths = [call.args[1] for call in request.call_args_list]
    assert paths[0].endswith("/chats/39%40c.us/archive")
    assert paths[1].endswith("/chats/39%40c.us/unarchive")


def test_contacts_carry_archived_flag():
    assert _archived_flags(_backend()) == {ARCHIVED: True, ACTIVE: False}


def test_archive_action_updates_contact_and_notifies():
    backend = _backend()
    _drain(backend)
    backend._rest.set_chat_archived.return_value = True
    assert backend.set_chat_archived_sync(ACTIVE, True)
    backend._rest.set_chat_archived.assert_called_once_with(ACTIVE, True)
    assert _archived_flags(backend)[ACTIVE] is True
    events = _drain(backend)
    assert [(e.type, e.contact_id) for e in events] == [("contact_update", ACTIVE)]


def test_archive_action_failure_leaves_flag():
    backend = _backend()
    backend._rest.set_chat_archived.return_value = False
    assert not backend.set_chat_archived_sync(ACTIVE, True)
    assert _archived_flags(backend)[ACTIVE] is False


def test_chat_archive_webhook_from_phone():
    backend = _backend()
    _drain(backend)
    handled = backend.handle_webhook(
        {"event": "chat.archive", "payload": {"id": ARCHIVED, "archived": False}}
    )
    assert handled
    assert _archived_flags(backend)[ARCHIVED] is False
    assert [e.type for e in _drain(backend)] == ["contact_update"]
    # Same state again: no duplicate notification.
    backend.handle_webhook(
        {"event": "chat.archive", "payload": {"id": ARCHIVED, "archived": False}}
    )
    assert _drain(backend) == []


def test_refresh_updates_archived_flag_in_place():
    backend = _backend()
    backend._rest.list_contacts.return_value = [
        {
            "id": ARCHIVED,
            "name": "Old",
            "isGroup": False,
            "last_ts": 1,
            "archived": False,
        },
        {"id": ACTIVE, "name": "New", "isGroup": False, "last_ts": 2, "archived": True},
    ]
    with patch.object(backend, "start_lid_resolver"):
        backend.refresh_contacts_sync(force=False)
    assert _archived_flags(backend) == {ARCHIVED: False, ACTIVE: True}


def _client(backend) -> TestClient:
    app = FastAPI()
    app.state.api = api_for(SimpleNamespace(get=lambda proto: backend))
    app.include_router(create_api_router())
    return TestClient(app)


def test_archive_endpoint():
    backend = MagicMock()
    backend.set_chat_archived_sync.return_value = True
    client = _client(backend)
    response = client.post(
        "/api/chats/archive",
        content=json.dumps(
            {"protocol": "whatsapp", "contact_id": ACTIVE, "archived": True}
        ),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json() == {"archived": True}
    backend.set_chat_archived_sync.assert_called_once_with(ACTIVE, True)


def test_archive_endpoint_rejects_bad_input_and_reports_failure():
    backend = MagicMock()
    backend.set_chat_archived_sync.return_value = False
    client = _client(backend)
    bad = [
        {"protocol": "signal", "contact_id": ACTIVE, "archived": True},
        {"protocol": "whatsapp", "contact_id": "", "archived": True},
        {"protocol": "whatsapp", "contact_id": ACTIVE, "archived": "yes"},
    ]
    for payload in bad:
        assert client.post("/api/chats/archive", json=payload).status_code == 400
    failed = client.post(
        "/api/chats/archive",
        json={"protocol": "whatsapp", "contact_id": ACTIVE, "archived": True},
    )
    assert failed.status_code == 502
