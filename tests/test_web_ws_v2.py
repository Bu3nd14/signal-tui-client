"""``/ws/v2``: the core's wire events with resume, authenticated like ``/ws``."""

from __future__ import annotations

import base64
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.bus import GAP, EventBus
from web import ws

TOKEN = "v2-secret"
# Starlette's test client adds handshake headers to the dict it is given:
# always pass a copy.
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _app(bus: EventBus | None, *, required: bool = False) -> FastAPI:
    app = FastAPI()
    app.state.websocket_connections = set()
    if bus is not None:
        app.state.bus = bus
    ws.install_websocket(app, TOKEN, required=required)
    return app


@pytest.fixture(autouse=True)
def fast_timers(monkeypatch):
    monkeypatch.setattr(ws, "HEARTBEAT_INTERVAL_S", 0.05)
    monkeypatch.setattr(ws, "SUBSCRIBE_GRACE_S", 0.05)


def _events(sock, count: int, limit: int = 200) -> list[dict]:
    """The next *count* non-heartbeat frames."""
    frames: list[dict] = []
    for _ in range(limit):
        frame = sock.receive_json()
        if frame.get("type") != "heartbeat":
            frames.append(frame)
            if len(frames) == count:
                return frames
    raise AssertionError(f"only {frames} received")


def _typing(bus: EventBus, who: str):
    return bus.publish(
        "typing", {"protocol": "signal", "contact_id": who, "action": "STARTED"}
    )


def test_live_stream_sends_every_event_type_as_a_wire_frame():
    bus = EventBus()
    with TestClient(_app(bus)) as client, client.websocket_connect("/ws/v2") as sock:
        assert sock.receive_json() == {"type": "heartbeat"}
        sent = [
            bus.publish("backend_status", {"protocol": "signal", "text": "x"}),
            _typing(bus, "+39"),
            bus.publish("web_push", {"event": {"type": "message", "payload": {}}}),
        ]
        frames = _events(sock, 3)
    assert frames == [
        {"seq": e.seq, "type": e.type, "payload": e.payload, "ts": e.ts} for e in sent
    ]
    assert [f["seq"] for f in frames] == sorted(f["seq"] for f in frames)
    assert list(frames[0]) == ["seq", "type", "payload", "ts"]


def test_since_query_replays_missed_events_then_goes_live():
    bus = EventBus()
    first = [_typing(bus, f"c{i}") for i in range(4)]
    with (
        TestClient(_app(bus)) as client,
        client.websocket_connect(f"/ws/v2?since={first[1].seq}") as sock,
    ):
        live = _typing(bus, "live")
        frames = _events(sock, 3)
    assert [f["seq"] for f in frames] == [3, 4, live.seq]
    assert frames[-1]["payload"]["contact_id"] == "live"


def test_subscribe_frame_replays_from_since_without_losing_or_repeating():
    bus = EventBus()
    for i in range(3):
        _typing(bus, f"c{i}")
    with TestClient(_app(bus)) as client, client.websocket_connect("/ws/v2") as sock:
        sock.send_json({"type": "subscribe", "since": 1})
        frames = _events(sock, 2)
        # Events published while waiting for the frame are not lost either.
        _typing(bus, "next")
        frames += _events(sock, 1)
    assert [f["seq"] for f in frames] == [2, 3, 4]


def test_events_published_during_the_grace_period_are_delivered():
    bus = EventBus()
    with TestClient(_app(bus)) as client, client.websocket_connect("/ws/v2") as sock:
        event = _typing(bus, "early")
        assert _events(sock, 1)[0]["seq"] == event.seq


def test_gap_when_since_fell_out_of_the_buffer():
    bus = EventBus(buffer_size=3)
    for i in range(10):
        _typing(bus, f"c{i}")
    with (
        TestClient(_app(bus)) as client,
        client.websocket_connect("/ws/v2?since=2") as sock,
    ):
        frames = _events(sock, 4)
    assert frames[0]["type"] == GAP
    assert frames[0]["payload"] == {"reason": "buffer", "since": 2}
    assert frames[0]["seq"] == 10
    assert [f["seq"] for f in frames[1:]] == [8, 9, 10]


def test_gap_when_the_connection_queue_overflows():
    bus = EventBus(buffer_size=2, subscriber_queue_size=2)
    with TestClient(_app(bus)) as client, client.websocket_connect("/ws/v2") as sock:
        assert sock.receive_json() == {"type": "heartbeat"}
        for i in range(60):
            _typing(bus, f"c{i}")
        seen: list[dict] = []
        while not seen or seen[-1]["seq"] < 60:
            seen += _events(sock, 1)
    assert any(
        f["type"] == GAP and f["payload"] == {"reason": "overflow"} for f in seen
    )


def test_since_beyond_the_head_is_clamped_to_live():
    bus = EventBus()
    _typing(bus, "old")
    with (
        TestClient(_app(bus)) as client,
        client.websocket_connect("/ws/v2?since=999") as sock,
    ):
        event = _typing(bus, "new")
        assert _events(sock, 1)[0]["seq"] == event.seq


def test_heartbeat_when_idle_and_without_a_bus():
    for bus in (EventBus(), None):
        with (
            TestClient(_app(bus)) as client,
            client.websocket_connect("/ws/v2") as sock,
        ):
            assert sock.receive_json() == {"type": "heartbeat"}
            assert sock.receive_json() == {"type": "heartbeat"}
        assert client.app is not None


def test_malformed_requests():
    bus = EventBus()
    with TestClient(_app(bus)) as client:
        for query in ("since=abc", "since=-1", "since=1.5"):
            with (
                pytest.raises(WebSocketDisconnect) as refused,
                client.websocket_connect(f"/ws/v2?{query}"),
            ):
                pass
            assert refused.value.code == 1008
        with client.websocket_connect("/ws/v2") as sock:
            sock.send_json({"type": "subscribe", "since": "nope"})
            assert _events_or_error(sock) == {
                "type": "error",
                "payload": {"reason": "invalid_subscribe"},
            }
            sock.send_text("not json")  # ignored
            sock.send_json({"type": "unknown"})  # ignored
            event = _typing(bus, "still-alive")
            assert _events(sock, 1)[0]["seq"] == event.seq


def _events_or_error(sock, limit: int = 50) -> dict:
    for _ in range(limit):
        frame = sock.receive_json()
        if frame.get("type") != "heartbeat":
            return frame
    raise AssertionError("no frame")


# -- authentication: identical to /ws -----------------------------------------


def _subprotocol_token(token: str) -> str:
    encoded = base64.urlsafe_b64encode(token.encode()).decode().rstrip("=")
    return f"signal-tui-token.{encoded}"


@pytest.mark.parametrize("path", ["/ws", "/ws/v2"])
def test_unauthenticated_connection_is_refused(path):
    with TestClient(_app(EventBus(), required=True)) as client:
        for headers in (
            {},
            {"Authorization": "Bearer wrong"},
            {"Authorization": TOKEN},
        ):
            with (
                pytest.raises(WebSocketDisconnect) as refused,
                client.websocket_connect(path, headers=headers),
            ):
                pass
            assert refused.value.code == 1008


def test_refused_connection_gets_no_events_even_with_a_since():
    bus = EventBus()
    _typing(bus, "secret")
    with (
        TestClient(_app(bus, required=True)) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/ws/v2?since=0"),
    ):
        pass


@pytest.mark.parametrize("path", ["/ws", "/ws/v2"])
def test_bearer_header_and_browser_subprotocol_are_accepted(path):
    with TestClient(_app(EventBus(), required=True)) as client:
        with client.websocket_connect(path, headers=dict(AUTH)) as sock:
            assert sock.receive_json() == {"type": "heartbeat"}
        with client.websocket_connect(
            path, subprotocols=[_subprotocol_token(TOKEN), "signal-tui-bearer"]
        ) as sock:
            assert sock.accepted_subprotocol == "signal-tui-bearer"
            assert sock.receive_json() == {"type": "heartbeat"}
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(path, subprotocols=[_subprotocol_token("wrong")]),
        ):
            pass


# -- the legacy socket is unaffected ------------------------------------------


def test_legacy_socket_still_gets_legacy_frames_next_to_v2():
    bus = EventBus()
    with (
        TestClient(_app(bus)) as client,
        client.websocket_connect("/ws") as legacy,
        client.websocket_connect("/ws/v2") as v2,
    ):
        assert legacy.receive_json() == {"type": "heartbeat"}
        assert v2.receive_json() == {"type": "heartbeat"}
        event = _typing(bus, "+39")
        legacy_frame = _events(legacy, 1)[0]
        v2_frame = _events(v2, 1)[0]
    assert legacy_frame == {
        "type": "typing",
        "payload": {"protocol": "signal", "contact_id": "+39", "action": "STARTED"},
    }
    assert v2_frame["seq"] == event.seq and v2_frame["payload"] == event.payload


def test_legacy_socket_ignores_since_and_never_replays():
    bus = EventBus()
    _typing(bus, "old")
    with (
        TestClient(_app(bus)) as client,
        client.websocket_connect("/ws?since=0") as legacy,
    ):
        assert legacy.receive_json() == {"type": "heartbeat"}
        _typing(bus, "new")
        assert _events(legacy, 1)[0]["payload"]["contact_id"] == "new"


def test_connections_are_tracked_for_shutdown_and_released():
    app = _app(EventBus())
    with TestClient(app) as client:
        with client.websocket_connect("/ws/v2") as sock:
            assert sock.receive_json() == {"type": "heartbeat"}
            assert len(app.state.websocket_v2_connections) == 1
            # never in the legacy set, whose broadcaster writes legacy frames
            assert app.state.websocket_connections == set()
        for _ in range(100):
            if not app.state.websocket_v2_connections:
                break
            time.sleep(0.02)
        assert app.state.websocket_v2_connections == set()


# -- a real server: start_web_server(api, ...) ---------------------------------


def test_real_server_streams_v2_resumes_and_closes_on_stop():
    import asyncio
    import json
    import socket

    from client_harness import api_for

    websockets = pytest.importorskip("websockets")
    from web.server import start_web_server, stop_web_server

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    bus = EventBus()
    first = _typing(bus, "before")
    handle = start_web_server(api_for(SimpleNamespace(), bus), port, TOKEN, bus=bus)
    assert handle is not None
    deadline = time.monotonic() + 3
    while handle.status != "up" and time.monotonic() < deadline:
        time.sleep(0.02)
    try:

        async def scenario() -> list[dict]:
            url = f"ws://127.0.0.1:{port}/ws/v2?since={first.seq - 1}"
            with pytest.raises(Exception):  # noqa: B017 - 1008 refusal
                async with websockets.connect(url) as refused:
                    await refused.recv()
            async with websockets.connect(url, additional_headers=dict(AUTH)) as sock:
                got = [json.loads(await sock.recv())]
                live = _typing(bus, "after")
                while got[-1].get("seq") != live.seq:
                    got.append(json.loads(await sock.recv()))
                return [f for f in got if f.get("type") != "heartbeat"]

        frames = asyncio.run(scenario())
    finally:
        stop_web_server(handle)
    assert [f["payload"]["contact_id"] for f in frames] == ["before", "after"]
    assert not handle.thread.is_alive()
    assert handle.websocket_v2_connections == set()
