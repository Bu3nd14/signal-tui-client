"""ChatClient / InProcessClient: operations as JSON, events with resume + GAP."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from unittest.mock import MagicMock

import pytest

from models import PROTOCOL_SIGNAL, ChatContact
from server.api import ApiError
from server.bus import GAP, EventBus
from server.client import ChatClient, ClientSubscription, InProcessClient
from server.core import ChatCore
from server.events_api import contact_to_wire
from signal_tui import SignalTUI


@pytest.fixture
def core() -> ChatCore:
    return ChatCore(EventBus())


@pytest.fixture
def client(core: ChatCore):
    c = InProcessClient(core)
    yield c
    c.close()


def test_in_process_client_is_a_chat_client(client):
    assert isinstance(client, ChatClient)
    assert isinstance(client.subscribe(), ClientSubscription)


def test_call_returns_plain_json(client):
    out = client.call("server.status")
    assert json.loads(json.dumps(out)) == out
    assert client.call("server.status", {}) == out


def test_call_validation_error_is_an_api_error(client):
    with pytest.raises(ApiError) as err:
        client.call("messages.edit", {"nonsense": 1})
    assert err.value.code == "invalid_request"


def test_call_unknown_operation(client):
    with pytest.raises(ApiError) as err:
        client.call("no.such.op")
    assert err.value.code == "not_found"


async def test_acall_matches_call_and_raises_api_error(client):
    assert await client.acall("server.status") == client.call("server.status")
    with pytest.raises(ApiError):
        await client.acall("messages.edit", {})


def test_subscribe_yields_envelopes_with_json_payloads(core, client):
    sub = client.subscribe()
    core.bus.publish(
        "typing", {"protocol": "signal", "contact_id": "1", "action": "STARTED"}
    )
    event = sub.get(timeout=1)
    assert event is not None
    assert (event.seq, event.type) == (1, "typing")
    assert json.loads(json.dumps(event.payload)) == event.payload
    assert sub.get(timeout=0.01) is None
    core.bus.publish("a")
    core.bus.publish("b")
    assert [e.type for e in sub.drain()] == ["a", "b"]


def test_subscribe_since_replays_missed_events(core, client):
    core.bus.publish("one")
    core.bus.publish("two")
    core.bus.publish("three")
    sub = client.subscribe(since=1)
    assert [(e.seq, e.type) for e in sub.drain()] == [(2, "two"), (3, "three")]


def test_subscribe_since_outside_buffer_signals_gap():
    bus = EventBus(buffer_size=2)
    core = ChatCore(bus)
    for i in range(5):
        bus.publish("e", {"i": i})
    sub = InProcessClient(core).subscribe(since=0)
    events = sub.drain()
    assert events[0].type == GAP
    assert events[0].payload["reason"] == "buffer"
    assert [e.payload["i"] for e in events[1:]] == [3, 4]


def test_subscription_close_and_client_close(core, client):
    sub = client.subscribe()
    other = client.subscribe()
    sub.close()
    assert sub.closed
    assert sub.get(timeout=0.01) is None
    client.close()
    assert other.closed
    core.bus.publish("x")
    assert other.drain() == []


def test_blocked_get_wakes_on_close(client):
    sub = client.subscribe()
    result: list = []
    t = threading.Thread(target=lambda: result.append(sub.get(timeout=5)))
    t.start()
    time.sleep(0.05)
    sub.close()
    t.join(2)
    assert not t.is_alive()
    assert result == [None]


async def test_aget(core, client):
    sub = client.subscribe()
    asyncio.get_running_loop().call_later(0.05, core.bus.publish, "later")
    event = await sub.aget(timeout=2)
    assert event is not None and event.type == "later"
    assert await sub.aget(timeout=0.01) is None


# -- the TUI consumes events through its client ------------------------------


def _wait(predicate, timeout=3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_tui_uses_an_in_process_client_for_events():
    app = SignalTUI()
    assert isinstance(app.client, InProcessClient)
    app.call_from_thread = MagicMock()
    app._core_sub = app.client.subscribe()
    app._polling_active = True
    thread = threading.Thread(target=app._poll_worker, daemon=True)
    thread.start()
    contact = ChatContact(id="+39123", display_name="Ada", protocol=PROTOCOL_SIGNAL)
    try:
        # A wire-form message for a contact the TUI list does not have yet.
        app.bus.publish(
            "message",
            {
                "protocol": PROTOCOL_SIGNAL,
                "contact_id": contact.id,
                "contact": contact_to_wire(contact),
                "was_placeholder": False,
                "message": {"id": "m", "text": "hi", "is_mine": False, "timestamp": 5},
                "timestamp": 5,
                "added": True,
                "changed": False,
            },
        )
        assert _wait(lambda: len(app.contacts) == 1)
    finally:
        app._polling_active = False
        thread.join(3)
    built = app.contacts[0]
    assert built == ChatContact(
        id="+39123",
        display_name="Ada",
        protocol=PROTOCOL_SIGNAL,
        extras={"last_message_ts": 5},
    )
    assert built is not contact
    assert app._cache[contact.cache_key][0]["text"] == "hi"
