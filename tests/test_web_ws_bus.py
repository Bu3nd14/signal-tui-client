"""``/ws`` fan-out driven by the core event bus (legacy frames + heartbeat)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.bus import EventBus
from web import bridge, ws

TOKEN = "t0k3n"


def _app(bus: EventBus | None) -> FastAPI:
    app = FastAPI()
    app.state.websocket_connections = set()
    if bus is not None:
        app.state.bus = bus
    ws.install_websocket(app, TOKEN, required=False)
    return app


def _receive_non_heartbeat(websocket, limit: int = 50) -> dict:
    for _ in range(limit):
        frame = websocket.receive_json()
        if frame.get("type") != "heartbeat":
            return frame
    raise AssertionError("only heartbeats received")


@pytest.fixture(autouse=True)
def fast_heartbeat(monkeypatch):
    monkeypatch.setattr(ws, "HEARTBEAT_INTERVAL_S", 0.05)


def test_core_events_are_sent_as_legacy_frames_in_order():
    bus = EventBus()
    with TestClient(_app(bus)) as client, client.websocket_connect("/ws") as sock:
        # Wait for the broadcaster to subscribe (first heartbeat proves it ran).
        assert sock.receive_json() == {"type": "heartbeat"}
        bus.publish("backend_status", {"protocol": "signal", "text": "x"})  # no frame
        bus.publish(
            "typing",
            {"protocol": "signal", "contact_id": "+39", "action": "STARTED"},
        )
        bus.publish(
            "message",
            {
                "protocol": "signal",
                "contact_id": "+39",
                "message": {"id": "m1", "is_mine": False},
                "timestamp": 7,
                "added": False,
                "changed": False,  # not sent
            },
        )
        bus.publish(
            "message",
            {
                "protocol": "signal",
                "contact_id": "+39",
                "message": {"id": "m2", "is_mine": True},
                "timestamp": 8,
                "added": True,
                "changed": False,
            },
        )

        assert _receive_non_heartbeat(sock) == {
            "type": "typing",
            "payload": {"protocol": "signal", "contact_id": "+39", "action": "STARTED"},
        }
        assert _receive_non_heartbeat(sock) == {
            "type": "message",
            "payload": {
                "id": "m2",
                "protocol": "signal",
                "contact_id": "+39",
                "timestamp": 8,
                "is_mine": True,
            },
        }


def test_push_event_dicts_reach_the_socket_unchanged():
    bus = EventBus()
    bridge.init_bridge(bus)
    try:
        with TestClient(_app(bus)) as client, client.websocket_connect("/ws") as sock:
            assert sock.receive_json() == {"type": "heartbeat"}
            frame = {"type": "message", "payload": {"id": "x", "contact_id": "c"}}
            assert bridge.push_event(frame)
            assert _receive_non_heartbeat(sock) == frame
    finally:
        bridge.close_bridge()


def test_heartbeat_is_sent_when_idle():
    with (
        TestClient(_app(EventBus())) as client,
        client.websocket_connect("/ws") as sock,
    ):
        assert sock.receive_json() == {"type": "heartbeat"}
        assert sock.receive_json() == {"type": "heartbeat"}


def test_without_a_bus_the_broadcaster_idles_without_error():
    bridge.close_bridge()
    with TestClient(_app(None)) as client, client.websocket_connect("/ws"):
        pass  # connects and closes cleanly; nothing is sent
    assert client.app is not None


def test_events_before_the_subscription_are_not_replayed():
    bus = EventBus()
    bus.publish(
        "typing", {"protocol": "signal", "contact_id": "old", "action": "STARTED"}
    )
    with TestClient(_app(bus)) as client, client.websocket_connect("/ws") as sock:
        assert sock.receive_json() == {"type": "heartbeat"}
        bus.publish(
            "typing", {"protocol": "signal", "contact_id": "new", "action": "STOPPED"}
        )
        assert _receive_non_heartbeat(sock)["payload"]["contact_id"] == "new"
