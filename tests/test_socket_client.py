"""SocketClient: the ChatClient contract over a real adapter, plus reconnect,
resume, liveness and version handling (design §4.2)."""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import tempfile
import threading
import time
from base64 import b64decode
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel
from test_socket_adapter import _Manager

import protocols.db as backend_db
import server.api as server_api
import server.socket_adapter as sa
import server.socket_client as sc
from server.api import ApiError, ChatAPI, Operation
from server.bus import GAP, EventBus
from server.client import ChatClient, ClientSubscription, InProcessClient
from server.socket_client import (
    CONNECTION_EVENT,
    IncompatibleServer,
    OperationTimeout,
    ServerUnavailable,
    SocketClient,
)

_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
_FAST = {"backoff": (0.02, 0.1)}


class _Empty(BaseModel):
    pass


class _Args(BaseModel):
    code: str = "internal"
    seconds: float = 0


# -- environment ------------------------------------------------------------------


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A real core pieces (api over a fake manager, bus) behind a real adapter."""
    gate = threading.Event()
    finished: list[float] = []

    def raising(api, req: _Args):
        raise ApiError(req.code, f"msg-{req.code}", details={"x": [1]})

    def blocking(api, req: _Empty):
        gate.wait(10)
        return {"done": True}

    async def aslow(api, req: _Args):
        await asyncio.sleep(req.seconds)
        finished.append(req.seconds)
        return {"slept": req.seconds}

    for name, handler, request in (
        ("test.raise", raising, _Args),
        ("test.block", blocking, _Empty),
        ("test.aslow", aslow, _Args),
    ):
        monkeypatch.setitem(
            server_api._REGISTRY,
            name,
            Operation(name, request, dict, handler, "test"),
        )
    monkeypatch.setattr(backend_db, "CACHE_DIR", tmp_path / "cache")
    # Socket paths are limited to ~100 bytes: pytest's tmp_path can be too long.
    short = Path(tempfile.mkdtemp(prefix="mc"))
    path = short / "s.sock"
    monkeypatch.setenv("MEGACHAT_SOCKET", str(path))
    manager = _Manager()
    core = SimpleNamespace(manager=manager, bus=EventBus())
    core.api = ChatAPI(core)
    env = SimpleNamespace(
        core=core,
        bus=core.bus,
        manager=manager,
        path=path,
        gate=gate,
        finished=finished,
        handle=sa.start(core.api, core.bus, path),
        uploads=tmp_path / "cache" / "web-uploads",
        clients=[],
    )

    def restart(bus: EventBus | None = None) -> None:
        sa.stop(env.handle)
        if bus is not None:
            core.bus = env.bus = bus
        env.handle = sa.start(core.api, core.bus, path)

    def drop_connections() -> None:
        """Server side: cut every client connection (the server stays up)."""
        loop, adapter = env.handle.loop, env.handle.adapter
        for conn in list(adapter.connections):
            loop.call_soon_threadsafe(conn.close)

    def connect(**options: Any) -> SocketClient:
        client = SocketClient.connect(path, **{**_FAST, **options})
        env.clients.append(client)
        return client

    env.restart, env.drop, env.connect = restart, drop_connections, connect
    yield env
    gate.set()
    for client in env.clients:
        client.close()
    sa.stop(env.handle)
    shutil.rmtree(short, ignore_errors=True)


@pytest.fixture(params=["in-process", "socket"])
def client(request, server):
    if request.param == "in-process":
        c: ChatClient = InProcessClient(server.core)  # type: ignore[arg-type]
    else:
        c = server.connect()
    yield c
    c.close()


def _types(events) -> list[str]:
    return [e.type for e in events]


def _wait_for(sub, predicate, timeout=5.0):
    """Collect events until *predicate(events)* holds."""
    events = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event = sub.get(timeout=0.1)
        if event is not None:
            events.append(event)
        if predicate(events):
            return events
    raise AssertionError(f"timed out; got {[(e.seq, e.type) for e in events]}")


# -- contract: runs against both transports ------------------------------------------


def test_is_a_chat_client(client):
    assert isinstance(client, ChatClient)
    assert isinstance(client.subscribe(), ClientSubscription)


def test_call_returns_plain_json(client):
    out = client.call("server.status")
    assert json.loads(json.dumps(out)) == out
    assert client.call("server.status", {}) == out
    assert client.call("server.status", timeout=5) == out


def test_validation_error(client):
    with pytest.raises(ApiError) as err:
        client.call("messages.edit", {"nonsense": 1})
    assert err.value.code == "invalid_request"
    assert err.value.details


def test_unknown_operation(client):
    with pytest.raises(ApiError) as err:
        client.call("no.such.op")
    assert err.value.code == "not_found"
    assert "no.such.op" in err.value.message


def test_api_error_keeps_code_message_and_details(client):
    with pytest.raises(ApiError) as err:
        client.call("test.raise", {"code": "conflict"})
    assert (err.value.code, err.value.message) == ("conflict", "msg-conflict")
    assert err.value.details == {"x": [1]}


def test_non_serializable_request(server):
    client = server.connect()
    with pytest.raises(ApiError) as err:
        client.call("server.status", {"x": object()})
    assert err.value.code == "invalid_request"
    assert client.call("server.status")  # nothing was half-sent


async def test_acall(client):
    assert await client.acall("server.status") == client.call("server.status")
    with pytest.raises(ApiError) as err:
        await client.acall("test.raise", {"code": "forbidden"})
    assert err.value.code == "forbidden"
    assert await client.acall("test.aslow", {"seconds": 0.01}, timeout=5) == {
        "slept": 0.01
    }


def test_async_operation(client):
    assert client.call("test.aslow", {"seconds": 0.01}) == {"slept": 0.01}


def test_many_threads_get_their_own_answers(client):
    results: dict[int, Any] = {}
    errors: list[BaseException] = []

    def work(i: int) -> None:
        try:
            # Different durations: the answers come back out of order.
            seconds = 0.001 * ((i * 7) % 13)
            results[i] = client.call("test.aslow", {"seconds": seconds})
            assert client.call("test.raise", {"code": "not_found"}) is None
        except ApiError as exc:
            results[i] = (results[i], exc.code)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert errors == []
    assert len(results) == 40
    for i, (answer, code) in results.items():
        assert answer == {"slept": 0.001 * ((i * 7) % 13)} and code == "not_found"


def test_subscribe_events_drain_and_timeout(server, client):
    sub = client.subscribe()
    server.bus.publish("typing", {"contact_id": "1"})
    event = sub.get(timeout=2)
    assert event is not None and (event.seq, event.type) == (1, "typing")
    assert event.payload == {"contact_id": "1"} and event.ts > 0
    assert sub.get(timeout=0.05) is None
    for name in "abc":
        server.bus.publish(name)
    got = []
    deadline = time.monotonic() + 3
    while len(got) < 3 and time.monotonic() < deadline:
        got += sub.drain()
        time.sleep(0.01)
    assert _types(got) == ["a", "b", "c"]


def test_subscribe_since_replays(server, client):
    for name in ("one", "two", "three"):
        server.bus.publish(name)
    sub = client.subscribe(since=1)
    events = _wait_for(sub, lambda evs: len(evs) >= 2)
    assert [(e.seq, e.type) for e in events] == [(2, "two"), (3, "three")]


def test_subscribe_since_outside_buffer_is_a_gap(server, monkeypatch, client):
    small = EventBus(buffer_size=2)
    for i in range(5):
        small.publish("e", {"i": i})
    # A client subscribing to a bus whose buffer no longer covers 'since'.
    server.core.bus = small
    if isinstance(client, InProcessClient):
        sub = client.subscribe(since=0)
    else:
        server.restart(small)
        c = server.connect()
        sub = c.subscribe(since=0)
    events = _wait_for(sub, lambda evs: len(evs) >= 3)
    assert events[0].type == GAP and events[0].payload["reason"] == "buffer"
    assert [e.payload["i"] for e in events[1:]] == [3, 4]


def test_close_wakes_getters_and_closes_subscriptions(client):
    sub = client.subscribe()
    other = client.subscribe()
    got: list = []
    t = threading.Thread(target=lambda: got.append(sub.get(timeout=10)))
    t.start()
    time.sleep(0.1)
    sub.close()
    t.join(3)
    assert not t.is_alive() and got == [None]
    assert sub.closed and not other.closed
    client.close()
    assert other.closed
    client.close()  # idempotent


async def test_aget(server, client):
    sub = client.subscribe()
    asyncio.get_running_loop().call_later(0.05, server.bus.publish, "later")
    event = await sub.aget(timeout=3)
    assert event is not None and event.type == "later"
    assert await sub.aget(timeout=0.05) is None


def test_send_with_an_attachment_path(server, client, tmp_path):
    pic = tmp_path / "pic.png"
    pic.write_bytes(_PNG)
    reply = client.call(
        "messages.send",
        {
            "protocol": "signal",
            "contact_id": "alice",
            "text": "",
            "attachments": [{"path": str(pic), "filename": "pic.png"}],
        },
    )
    assert reply == {"ok": True}
    (call,) = server.manager.attachment_calls
    assert call["bytes"] == [_PNG]
    assert pic.read_bytes() == _PNG
    if isinstance(client, SocketClient):  # the server worked on a staged copy
        assert call["paths"][0] != pic
        assert list(server.uploads.iterdir()) == []


# -- errors specific to the socket -----------------------------------------------------


def test_operation_timeout_does_not_cancel_the_operation(server):
    client = server.connect()
    with pytest.raises(OperationTimeout) as err:
        client.call("test.aslow", {"seconds": 0.6}, timeout=0.2)
    assert err.value.code == "timeout" and isinstance(err.value, ApiError)
    assert "may still complete" in err.value.message
    assert client.connected
    assert client.call("server.status")  # the late answer is dropped quietly
    deadline = time.monotonic() + 3
    while not server.finished and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.finished == [0.6]


def test_blocked_operation_times_out_in_time(server):
    client = server.connect()
    t0 = time.monotonic()
    with pytest.raises(OperationTimeout):
        client.call("test.block", timeout=0.1)  # server answers -32020 itself
    assert time.monotonic() - t0 < 3


async def test_acall_cancellation_leaves_the_connection_usable(server):
    client = server.connect()
    task = asyncio.ensure_future(client.acall("test.aslow", {"seconds": 0.3}))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await client.acall("server.status")


def test_wire_constants_match_the_adapter():
    assert sc._RPC_TIMEOUT == sa.TIMEOUT
    assert sc._RPC_TOO_MANY_REQUESTS == sa.TOO_MANY_REQUESTS
    assert sc._RPC_TOO_MANY_CONNECTIONS == sa.TOO_MANY_CONNECTIONS
    assert set(sc._RPC_PROTOCOL_CODES) == {
        sa.PARSE_ERROR,
        sa.INVALID_REQUEST,
        sa.INVALID_PARAMS,
    }
    assert sc._MAX_TIMEOUT_S == sa.MAX_TIMEOUT_S
    assert sc.HEARTBEAT_INTERVAL_S == sa.HEARTBEAT_INTERVAL_S


def test_connect_resolves_the_default_path(server):
    client = SocketClient.connect(timeout=2)  # $MEGACHAT_SOCKET
    try:
        assert client.path == server.path and client.connected
        assert client.server_info["api_version"] == sa.API_VERSION
        assert "messages.send" in client.server_info["operations"]
    finally:
        client.close()


def test_connect_without_a_server_raises_server_unavailable(tmp_path):
    t0 = time.monotonic()
    with pytest.raises(ServerUnavailable) as err:
        SocketClient.connect(tmp_path / "nothing.sock", timeout=0.3)
    assert err.value.code == "server_unavailable"
    assert time.monotonic() - t0 < 3


def test_connect_waits_for_a_server_that_is_starting(server):
    sa.stop(server.handle)
    threading.Timer(
        0.3,
        lambda: setattr(
            server, "handle", sa.start(server.core.api, server.bus, server.path)
        ),
    ).start()
    client = SocketClient.connect(server.path, timeout=5)
    try:
        assert client.call("server.status")
    finally:
        client.close()


# -- version ------------------------------------------------------------------------------


def _fake_server(path: Path, script, ready: threading.Event | None = None):
    """A one-connection-at-a-time raw server running *script(conn_file, sock)*."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(4)
    stop = threading.Event()

    conns: list[socket.socket] = []

    def serve():
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            conns.append(conn)
            threading.Thread(target=script, args=(conn, stop), daemon=True).start()
        listener.close()

    server_thread = threading.Thread(target=serve, daemon=True)
    server_thread.start()

    def close():
        stop.set()
        server_thread.join(2)
        for conn in conns:
            conn.close()

    return close


def _lines(conn):
    f = conn.makefile("rb")
    for line in f:
        yield json.loads(line)


def _reply(conn, req, result):
    conn.sendall(
        json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}).encode()
        + b"\n"
    )


def test_major_version_mismatch_is_refused(tmp_path):
    path = Path(tempfile.mkdtemp(prefix="mc")) / "v.sock"

    def script(conn, stop):
        for req in _lines(conn):
            _reply(
                conn,
                req,
                {
                    "api_version": sa.API_VERSION + 1,
                    "server_version": "9",
                    "operations": [],
                },
            )

    close = _fake_server(path, script)
    try:
        with pytest.raises(IncompatibleServer) as err:
            SocketClient.connect(path, timeout=2)
        assert err.value.code == "incompatible_server"
        assert err.value.details == {
            "server": sa.API_VERSION + 1,
            "client": sa.API_VERSION,
        }
    finally:
        close()
        shutil.rmtree(path.parent, ignore_errors=True)


def test_version_change_on_reconnect_is_final(server, monkeypatch):
    client = server.connect()
    sub = client.subscribe()
    monkeypatch.setattr(sa, "API_VERSION", sa.API_VERSION + 1)
    server.restart()
    deadline = time.monotonic() + 5
    while client.state != "closed" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert client.state == "closed"
    with pytest.raises(IncompatibleServer):
        client.call("server.status")
    deadline = time.monotonic() + 3
    while not sub.closed and time.monotonic() < deadline:
        time.sleep(0.02)
    assert sub.closed


# -- connection loss, reconnect, resume ------------------------------------------------------


def test_in_flight_call_fails_with_server_unavailable_and_client_recovers(server):
    client = server.connect()
    outcome: list = []

    def work():
        try:
            outcome.append(client.call("test.block"))
        except BaseException as exc:  # noqa: BLE001
            outcome.append(exc)

    t = threading.Thread(target=work)
    t.start()
    time.sleep(0.2)
    server.drop()
    t.join(5)
    assert not t.is_alive()
    assert isinstance(outcome[0], ServerUnavailable)
    server.gate.set()
    deadline = time.monotonic() + 5
    while not client.connected and time.monotonic() < deadline:
        time.sleep(0.02)
    assert client.call("server.status")


def test_calls_fail_fast_while_disconnected(server, monkeypatch):
    client = server.connect()
    sa.stop(server.handle)
    deadline = time.monotonic() + 5
    while client.connected and time.monotonic() < deadline:
        time.sleep(0.02)
    assert client.state == "reconnecting"
    t0 = time.monotonic()
    with pytest.raises(ServerUnavailable):
        client.call("server.status")
    assert time.monotonic() - t0 < 0.5
    server.restart()
    deadline = time.monotonic() + 5
    while not client.connected and time.monotonic() < deadline:
        time.sleep(0.02)
    assert client.call("server.status")


def test_subscription_resumes_after_a_lost_connection(server):
    client = server.connect()
    sub = client.subscribe()
    server.bus.publish("one")
    assert sub.get(timeout=3).type == "one"
    server.drop()
    server.bus.publish("two")
    server.bus.publish("three")
    events = _wait_for(sub, lambda evs: len(evs) >= 4)
    assert _types(events) == [CONNECTION_EVENT, CONNECTION_EVENT, "two", "three"]
    lost, restored = events[:2]
    assert lost.payload["state"] == "lost" and restored.payload["state"] == "restored"
    assert lost.seq == restored.seq == 1  # the stream position, not a new seq
    assert [e.seq for e in events[2:]] == [2, 3]
    server.bus.publish("four")  # and the stream keeps flowing
    assert _wait_for(sub, lambda evs: len(evs) == 1)[0].type == "four"
    assert client.connected


def test_resume_without_events_missed(server):
    client = server.connect()
    sub = client.subscribe()
    server.drop()
    events = _wait_for(sub, lambda evs: len(evs) >= 2)
    assert [e.payload["state"] for e in events] == ["lost", "restored"]
    server.bus.publish("x")
    assert _wait_for(sub, lambda evs: len(evs) == 1)[0].type == "x"


def test_gap_when_the_outage_outlasted_the_server_buffer(server):
    server.restart(EventBus(buffer_size=3))
    client = server.connect()
    sub = client.subscribe()
    server.bus.publish("one")
    assert sub.get(timeout=3).type == "one"
    server.drop()
    for i in range(10):
        server.bus.publish("e", {"i": i})
    events = _wait_for(sub, lambda evs: len(evs) >= 6)
    assert _types(events)[:3] == [CONNECTION_EVENT, CONNECTION_EVENT, GAP]
    assert events[2].payload["reason"] == "buffer"
    assert [e.payload["i"] for e in events[3:]] == [7, 8, 9]


def test_server_restart_gives_a_gap_event(server):
    client = server.connect()
    sub = client.subscribe()
    for _ in range(3):
        server.bus.publish("old")
    _wait_for(sub, lambda evs: len(evs) >= 3)
    time.sleep(0.01)
    server.restart(EventBus())  # a new server: seq starts again at 1
    events = _wait_for(sub, lambda evs: any(e.type == GAP for e in evs))
    assert _types(events) == [CONNECTION_EVENT, CONNECTION_EVENT, GAP]
    assert events[2].payload == {"reason": "restart"}
    server.bus.publish("new")
    new = _wait_for(sub, lambda evs: len(evs) == 1)[0]
    assert (new.seq, new.type) == (1, "new")


def test_restart_is_detected_from_the_seq_alone(server, monkeypatch):
    client = server.connect()
    monkeypatch.setattr(client, "_read_identity", lambda: None)
    sub = client.subscribe()
    for _ in range(3):
        server.bus.publish("old")
    _wait_for(sub, lambda evs: len(evs) >= 3)
    server.restart(EventBus())
    events = _wait_for(sub, lambda evs: any(e.type == GAP for e in evs))
    assert events[-1].payload == {"reason": "restart"}


def test_late_subscription_is_served_from_the_ring(server):
    client = server.connect()
    first = client.subscribe()
    for name in ("a", "b", "c"):
        server.bus.publish(name)
    _wait_for(first, lambda evs: len(evs) >= 3)
    second = client.subscribe(since=1)
    assert [(e.seq, e.type) for e in second.drain()] == [(2, "b"), (3, "c")]
    third = client.subscribe()
    assert third.drain() == []
    server.bus.publish("d")
    assert _wait_for(third, lambda evs: len(evs) == 1)[0].type == "d"
    assert _wait_for(second, lambda evs: len(evs) == 1)[0].type == "d"


def test_subscribing_while_disconnected_starts_the_stream_on_reconnect(server):
    client = server.connect()
    sa.stop(server.handle)
    deadline = time.monotonic() + 5
    while client.connected and time.monotonic() < deadline:
        time.sleep(0.02)
    sub = client.subscribe()
    server.restart()
    assert _wait_for(sub, lambda evs: len(evs) >= 1)[0].payload["state"] == "restored"
    server.bus.publish("fresh")
    events = _wait_for(sub, lambda evs: len(evs) >= 1)
    assert events[-1].type == "fresh"


def test_no_reconnect_option_makes_a_loss_final(server):
    client = server.connect(reconnect=False)
    sub = client.subscribe()
    server.drop()
    deadline = time.monotonic() + 5
    while not sub.closed and time.monotonic() < deadline:
        time.sleep(0.02)
    assert sub.closed and client.state == "closed"
    with pytest.raises(ServerUnavailable):
        client.call("server.status")


def test_reconnect_attempts_are_bounded(server, monkeypatch):
    client = server.connect(backoff=(0.1, 0.4))
    attempts: list[float] = []
    real = client._open

    def counting(deadline):
        attempts.append(time.monotonic())
        return real(deadline)

    monkeypatch.setattr(client, "_open", counting)
    sa.stop(server.handle)
    time.sleep(2.5)
    assert 3 <= len(attempts) <= 14
    server.restart()
    deadline = time.monotonic() + 5
    while not client.connected and time.monotonic() < deadline:
        time.sleep(0.02)
    assert client.connected


def test_reconnect_timeout_gives_up(server):
    client = server.connect(backoff=(0.02, 0.05), reconnect_timeout=0.3)
    sa.stop(server.handle)
    deadline = time.monotonic() + 5
    while client.state != "closed" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert client.state == "closed"
    with pytest.raises(ServerUnavailable, match="did not come back"):
        client.call("server.status")


# -- liveness -----------------------------------------------------------------------------------


def test_silent_server_is_detected_by_the_missing_heartbeat(tmp_path):
    path = Path(tempfile.mkdtemp(prefix="mc")) / "h.sock"

    def script(conn, stop):
        for req in _lines(conn):
            if req["method"] == "rpc.hello":
                _reply(
                    conn,
                    req,
                    {
                        "api_version": sa.API_VERSION,
                        "server_version": "x",
                        "operations": [],
                    },
                )
            elif req["method"] == "events.subscribe":
                _reply(conn, req, {"subscribed": True, "last_seq": 0})
            # ...then nothing: no heartbeat, no answers.

    close = _fake_server(path, script)
    client = SocketClient.connect(path, timeout=2, heartbeat_timeout=0.6, **_FAST)
    try:
        sub = client.subscribe()
        t0 = time.monotonic()
        lost = _wait_for(sub, lambda evs: len(evs) >= 1, timeout=5)[0]
        assert lost.type == CONNECTION_EVENT and lost.payload["state"] == "lost"
        assert "no data" in lost.payload["reason"]
        assert 0.4 < time.monotonic() - t0 < 3
    finally:
        client.close()
        close()
        shutil.rmtree(path.parent, ignore_errors=True)


def test_idle_connection_without_subscription_is_kept_alive_by_pings(server):
    client = server.connect(heartbeat_timeout=0.6, ping_interval=0.1)
    time.sleep(1.8)  # three times the dead-after window, nothing but pings
    assert client.connected
    assert client.call("server.status")


def test_heartbeats_keep_a_subscribed_connection_alive(server, monkeypatch):
    monkeypatch.setattr(sa, "HEARTBEAT_INTERVAL_S", 0.1)
    client = server.connect(heartbeat_timeout=0.6)
    sub = client.subscribe()
    time.sleep(1.5)
    assert client.connected and sub.drain() == []


# -- closing -----------------------------------------------------------------------------------


def test_close_stops_the_threads_and_fails_later_calls(server):
    client = server.connect()
    sub = client.subscribe()
    thread = client._thread
    assert thread is not None and thread.is_alive()
    client.close()
    assert not thread.is_alive()
    assert sub.closed and not client.connected and client.state == "closed"
    with pytest.raises(ServerUnavailable, match="closed"):
        client.call("server.status")
    with pytest.raises(ServerUnavailable):
        client.subscribe()
    client.close()
    assert not [t for t in threading.enumerate() if t.name == "megachat-sock-client"]


def test_close_during_reconnect_does_not_leak_the_thread(server):
    client = server.connect()
    thread = client._thread
    sa.stop(server.handle)
    time.sleep(0.3)
    client.close()
    assert thread is not None and not thread.is_alive()


def test_close_fails_calls_in_flight(server):
    client = server.connect()
    outcome: list = []

    def work():
        try:
            outcome.append(client.call("test.block"))
        except BaseException as exc:  # noqa: BLE001
            outcome.append(exc)

    t = threading.Thread(target=work)
    t.start()
    time.sleep(0.2)
    client.close()
    t.join(5)
    assert isinstance(outcome[0], ServerUnavailable)


def test_events_and_responses_from_many_threads_stay_ordered(server):
    client = server.connect()
    sub = client.subscribe()
    stop = threading.Event()

    def publisher():
        while not stop.is_set():
            server.bus.publish("tick")
            time.sleep(0.001)

    t = threading.Thread(target=publisher)
    t.start()
    try:
        workers = [
            threading.Thread(
                target=lambda: [client.call("server.status") for _ in range(20)]
            )
            for _ in range(8)
        ]
        for w in workers:
            w.start()
        for w in workers:
            w.join(20)
    finally:
        stop.set()
        t.join(3)
    time.sleep(0.3)
    seqs = [e.seq for e in sub.drain() if e.type == "tick"]
    assert seqs and seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))  # nothing lost


def test_a_server_that_drops_every_connection_is_not_hammered():
    path = Path(tempfile.mkdtemp(prefix="mc")) / "f.sock"
    accepted: list[float] = []

    def script(conn, stop):
        for req in _lines(conn):
            accepted.append(time.monotonic())
            hello = {
                "api_version": sa.API_VERSION,
                "server_version": "x",
                "operations": [],
            }
            _reply(conn, req, hello)
            break
        conn.close()  # accept, greet, hang up

    close = _fake_server(path, script)
    client = SocketClient.connect(path, timeout=2, backoff=(0.1, 0.4))
    try:
        time.sleep(2.0)
        assert 3 <= len(accepted) <= 12, len(accepted)
    finally:
        client.close()
        close()
        shutil.rmtree(path.parent, ignore_errors=True)
