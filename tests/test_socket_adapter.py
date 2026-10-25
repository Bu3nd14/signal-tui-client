"""Unix-socket JSON-RPC adapter (design §4.2): real sockets in tmp dirs."""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
import time
from base64 import b64decode
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from client_harness import api_for
from pydantic import BaseModel

import protocols.db as backend_db
import server.api as server_api
import server.socket_adapter as sa
from models import ChatContact
from server.api import ApiError, Operation
from server.bus import EventBus

_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


# -- fakes ------------------------------------------------------------------------


class _Backend:
    def __init__(self) -> None:
        self.contacts = [ChatContact("alice", "Alice", "signal")]

    def find_contact(self, contact_id):
        return next((c for c in self.contacts if c.id == contact_id), None)

    def register_contact(self, contact):
        pass


class _Manager:
    def __init__(self) -> None:
        self.backend = _Backend()
        self.send_calls: list[tuple] = []
        self.attachment_calls: list[dict] = []
        self.attachments_error: BaseException | None = None

    def get(self, protocol):
        return self.backend

    def get_attachment_path(self, protocol, attachment_id):
        return None

    def send_message_sync(self, protocol, contact_id, text, **kwargs):
        self.send_calls.append((protocol, contact_id, text, kwargs))
        return "sent-id"

    def send_attachments_sync(self, protocol, contact_id, file_paths, **kwargs):
        self.attachment_calls.append(
            {
                "paths": [Path(p) for p in file_paths],
                "bytes": [Path(p).read_bytes() for p in file_paths],
            }
        )
        if self.attachments_error is not None:
            raise self.attachments_error
        return ["id"] * len(file_paths)


class _Empty(BaseModel):
    pass


class _Args(BaseModel):
    code: str = "internal"
    seconds: float = 0


@pytest.fixture
def test_ops(monkeypatch):
    """Throw-away operations (removed after the test): raise, block, sleep."""
    gate = threading.Event()
    started = threading.Event()

    def raising(api, req: _Args):
        if req.code == "crash":
            raise RuntimeError("secret internal detail")
        raise ApiError(req.code, f"msg-{req.code}", details={"x": [1]})

    def blocking(api, req: _Empty):
        started.set()
        gate.wait(10)
        return {"done": True}

    async def aslow(api, req: _Args):
        import asyncio

        await asyncio.sleep(req.seconds)
        return {"slept": req.seconds}

    def big(api, req: _Empty):
        return {"blob": "é" * 3_000_000}

    for name, handler, request, response in (
        ("test.big", big, _Empty, dict),
        ("test.raise", raising, _Args, dict),
        ("test.block", blocking, _Empty, dict),
        ("test.aslow", aslow, _Args, dict),
    ):
        monkeypatch.setitem(
            server_api._REGISTRY,
            name,
            Operation(name, request, response, handler, "test"),
        )
    return SimpleNamespace(gate=gate, started=started)


class Rpc:
    """Blocking test client speaking the wire protocol by hand."""

    def __init__(self, path: Path) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(str(path))
        self.buf = b""
        self.notifications: list[dict] = []
        self.pending: dict[Any, dict] = {}
        self._next = 0

    def send_raw(self, data: bytes) -> None:
        self.sock.sendall(data)

    def send(self, obj: Any) -> None:
        self.send_raw(json.dumps(obj).encode() + b"\n")

    def recv(self, timeout: float = 5) -> dict | None:
        """Next message (response or notification); ``None`` on EOF/reset."""
        self.sock.settimeout(timeout)
        while b"\n" not in self.buf:
            try:
                chunk = self.sock.recv(65536)
            except ConnectionResetError:
                return None
            if not chunk:
                return None
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def response(self, req_id: Any, timeout: float = 5) -> dict:
        """Next response with *req_id*; notifications are set aside."""
        deadline = time.monotonic() + timeout
        while True:
            if req_id in self.pending:
                return self.pending.pop(req_id)
            msg = self.recv(max(0.01, deadline - time.monotonic()))
            assert msg is not None, "connection closed"
            if "method" in msg and "id" not in msg:
                self.notifications.append(msg)
            else:
                self.pending[msg.get("id")] = msg  # answered out of order

    def request(self, method: str, params: Any = None, **envelope: Any) -> int:
        self._next += 1
        msg = {"jsonrpc": "2.0", "id": self._next, "method": method, **envelope}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        return self._next

    def call(self, method: str, params: Any = None, **envelope: Any) -> dict:
        return self.response(self.request(method, params, **envelope))

    def event(self, timeout: float = 5) -> dict:
        """Next ``event`` notification."""
        deadline = time.monotonic() + timeout
        while True:
            for i, note in enumerate(self.notifications):
                if note["method"] == "event":
                    return self.notifications.pop(i)["params"]
            msg = self.recv(max(0.01, deadline - time.monotonic()))
            assert msg is not None
            self.notifications.append(msg)

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def env(tmp_path, monkeypatch, test_ops):
    short = tmp_path / "run"
    monkeypatch.setattr(backend_db, "CACHE_DIR", tmp_path / "cache")
    manager = _Manager()
    bus = EventBus()
    api = api_for(manager, bus)
    path = short / "server.sock"
    handle = sa.start(api, bus, path)
    clients: list[Rpc] = []

    def connect() -> Rpc:
        client = Rpc(path)
        clients.append(client)
        return client

    yield SimpleNamespace(
        api=api,
        bus=bus,
        manager=manager,
        path=path,
        handle=handle,
        connect=connect,
        uploads=tmp_path / "cache" / "web-uploads",
        tmp=tmp_path,
    )
    for client in clients:
        client.close()
    sa.stop(handle)


def _uploads(env) -> list[Path]:
    return list(env.uploads.iterdir()) if env.uploads.exists() else []


# -- basics -----------------------------------------------------------------------


def test_hello_lists_operations(env):
    reply = env.connect().call("rpc.hello")
    result = reply["result"]
    assert reply["jsonrpc"] == "2.0"
    assert result["api_version"] == sa.API_VERSION
    assert result["server_version"] == sa.SERVER_VERSION
    assert result["operations"] == sorted(op.name for op in env.api.operations())
    assert "messages.send" in result["operations"]


def test_ping(env):
    result = env.connect().call("server.ping")["result"]
    assert result["pong"] is True and isinstance(result["ts"], float)


def test_sync_operation_matches_call_json(env):
    reply = env.connect().call("emoji.list")
    assert reply["result"] == env.api.call_json("emoji.list")
    assert reply["id"] == 1


def test_string_id_and_null_params(env):
    client = env.connect()
    client.send({"jsonrpc": "2.0", "id": "abc", "method": "emoji.list", "params": None})
    assert client.response("abc")["result"] == env.api.call_json("emoji.list")


def test_async_operation_messages_send(env):
    reply = env.connect().call(
        "messages.send",
        {
            "protocol": "signal",
            "contact_id": "alice",
            "text": "Ciao",
            "client_msg_id": "c1",
        },
    )
    assert reply["result"] == {"ok": True}
    assert env.manager.send_calls[0][:3] == ("signal", "alice", "Ciao")


def test_notification_is_executed_but_not_answered(env):
    client = env.connect()
    client.send(
        {
            "jsonrpc": "2.0",
            "method": "messages.send",
            "params": {"protocol": "signal", "contact_id": "alice", "text": "n"},
        }
    )
    assert client.call("server.ping")["result"]["pong"]  # no stray response first
    deadline = time.monotonic() + 5
    while not env.manager.send_calls and time.monotonic() < deadline:
        time.sleep(0.02)
    assert env.manager.send_calls[0][2] == "n"


# -- errors -----------------------------------------------------------------------


@pytest.mark.parametrize(("code", "rpc_code"), list(sa.RPC_CODES.items()))
def test_every_api_error_code_is_mapped(env, test_ops, code, rpc_code):
    assert set(sa.RPC_CODES) == set(server_api.ERROR_CODES)
    error = env.connect().call("test.raise", {"code": code})["error"]
    assert error["code"] == rpc_code
    assert error["message"] == f"msg-{code}"
    assert error["data"] == {
        "code": code,
        "message": f"msg-{code}",
        "details": {"x": [1]},
    }


def test_application_codes_are_distinct_and_in_range():
    codes = list(sa.RPC_CODES.values())
    assert len(set(codes)) == len(codes)
    assert all(-32099 <= c <= -32000 or c in (-32602, -32603) for c in codes)


def test_invalid_params_from_validation(env):
    error = env.connect().call("messages.page", {"nonsense": 1})["error"]
    assert error["code"] == -32602
    assert error["data"]["code"] == "invalid_request"
    for detail in error["data"]["details"]:
        assert set(detail) == {"type", "loc", "msg"}  # no echoed input


def test_unexpected_exception_is_internal_and_hides_details(env, test_ops):
    reply = env.connect().call("test.raise", {"code": "crash"})
    assert reply["error"]["code"] == -32603
    assert "secret" not in json.dumps(reply)


def test_unknown_method(env):
    error = env.connect().call("nope.nothing")["error"]
    assert error["code"] == -32601
    assert error["data"]["code"] == "method_not_found"


def test_params_must_be_an_object(env):
    assert env.connect().call("emoji.list", [1, 2])["error"]["code"] == -32602


def test_malformed_json_keeps_the_connection(env):
    client = env.connect()
    client.send_raw(b"{not json\n")
    reply = client.recv()
    assert reply["error"]["code"] == -32700 and reply["id"] is None
    client.send_raw(b"\xff\xfe\n")  # not UTF-8
    assert client.recv()["error"]["code"] == -32700
    client.send_raw(b"\n\n")  # blank lines are ignored
    assert client.call("server.ping")["result"]["pong"]


@pytest.mark.parametrize(
    "message",
    [
        [],
        [{"jsonrpc": "2.0", "id": 1, "method": "server.ping"}],
        "text",
        {"id": 1, "method": "server.ping"},  # no jsonrpc
        {"jsonrpc": "2.0", "id": 1.5, "method": "server.ping"},
        {"jsonrpc": "2.0", "id": True, "method": "server.ping"},
        {"jsonrpc": "2.0", "id": 1, "method": 5},
        {"jsonrpc": "2.0", "id": 1, "method": "server.ping", "timeout": -1},
        {"jsonrpc": "2.0", "id": 1, "method": "server.ping", "timeout": "5"},
        {"jsonrpc": "2.0", "id": 1, "method": "server.ping", "timeout": 10**6},
    ],
)
def test_invalid_envelopes(env, message):
    client = env.connect()
    client.send(message)
    reply = client.recv()
    assert reply["error"]["code"] == -32600
    assert client.call("server.ping")["result"]["pong"]


def test_oversize_line_gets_an_error_and_closes(env, monkeypatch):
    monkeypatch.setattr(sa, "MAX_LINE_BYTES", 4096)
    small = sa.start(env.api, env.bus, env.tmp / "small" / "s.sock")
    try:
        client = Rpc(env.tmp / "small" / "s.sock")
        client.send_raw(
            b'{"jsonrpc":"2.0","id":1,"method":"x","params":{"a":"' + b"x" * 8000
        )
        client.send_raw(b'"}}\n')
        reply = client.recv()
        assert reply["error"]["code"] == sa.RPC_CODES["too_large"]
        assert reply["error"]["data"]["code"] == "too_large"
        assert client.recv() is None  # connection closed
        # A line within the limit still works on a fresh connection.
        again = Rpc(env.tmp / "small" / "s.sock")
        assert again.call("server.ping")["result"]["pong"]
        again.close()
        client.close()
    finally:
        sa.stop(small)


def test_large_response_and_large_request_round_trip(env):
    big = "x" * 300_000  # above the inline-parse threshold
    reply = env.connect().call(
        "messages.send",
        {"protocol": "signal", "contact_id": "alice", "text": big[:4000]},
    )
    assert reply["result"] == {"ok": True}
    client = env.connect()
    client.send(
        {"jsonrpc": "2.0", "id": 9, "method": "server.ping", "params": {"pad": big}}
    )
    assert client.response(9)["result"]["pong"]


def test_half_closed_client_still_gets_its_answers(env):
    client = env.connect()
    client.request("server.ping")
    client.sock.shutdown(socket.SHUT_WR)
    assert client.response(1)["result"]["pong"]


def test_large_response_is_flushed_before_close(env):
    client = env.connect()
    client.request("test.big")
    client.sock.shutdown(socket.SHUT_WR)
    reply = client.response(1, timeout=20)
    assert len(reply["result"]["blob"]) == 3_000_000
    assert client.recv() is None  # then the server closes


# -- concurrency, timeouts ----------------------------------------------------------


def test_out_of_order_answers_and_other_connections_not_blocked(env, test_ops):
    client, other = env.connect(), env.connect()
    slow_id = client.request("test.block")
    assert test_ops.started.wait(5)
    fast_id = client.request("server.ping")
    # The ping overtakes the blocked call, on the same and on another connection.
    assert client.response(fast_id)["result"]["pong"]
    assert other.call("emoji.list")["result"]
    test_ops.gate.set()
    assert client.response(slow_id)["result"] == {"done": True}


def test_events_flow_while_a_call_is_blocked(env, test_ops):
    client = env.connect()
    client.call("events.subscribe")
    slow_id = client.request("test.block")
    assert test_ops.started.wait(5)
    env.bus.publish("message", {"n": 1})
    assert client.event()["payload"] == {"n": 1}
    test_ops.gate.set()
    assert client.response(slow_id)["result"] == {"done": True}


def test_timeout_member_and_operation_keeps_running(env, test_ops):
    client = env.connect()
    t0 = time.monotonic()
    reply = client.call("test.block", timeout=0.2)
    assert time.monotonic() - t0 < 3
    assert reply["error"]["code"] == sa.TIMEOUT
    assert reply["error"]["data"]["code"] == "timeout"
    assert client.call("server.ping")["result"]["pong"]
    test_ops.gate.set()  # the worker finishes; nothing breaks afterwards
    assert client.call("emoji.list")["result"]


def test_async_operation_timeout(env, test_ops):
    client = env.connect()
    assert (
        client.call("test.aslow", {"seconds": 5}, timeout=0.2)["error"]["code"]
        == sa.TIMEOUT
    )
    assert client.call("test.aslow", {"seconds": 0.05}, timeout=5)["result"] == {
        "slept": 0.05
    }


def test_default_timeout_applies(env, test_ops, monkeypatch):
    monkeypatch.setattr(sa, "DEFAULT_TIMEOUT_S", 0.2)
    assert env.connect().call("test.block")["error"]["code"] == sa.TIMEOUT
    test_ops.gate.set()


def test_too_many_requests_in_flight(env, test_ops, monkeypatch):
    monkeypatch.setattr(sa, "MAX_INFLIGHT_PER_CONNECTION", 2)
    client = env.connect()
    first, second = client.request("test.block"), client.request("test.block")
    assert test_ops.started.wait(5)
    time.sleep(0.2)
    third = client.request("server.ping")
    assert client.response(third)["error"]["code"] == sa.TOO_MANY_REQUESTS
    test_ops.gate.set()
    assert client.response(first)["result"] == {"done": True}
    assert client.response(second)["result"] == {"done": True}


def test_too_many_connections(env, monkeypatch):
    monkeypatch.setattr(sa, "MAX_CONNECTIONS", 1)
    first = env.connect()
    assert first.call("server.ping")["result"]["pong"]
    second = env.connect()
    assert second.recv()["error"]["code"] == sa.TOO_MANY_CONNECTIONS
    assert first.call("server.ping")["result"]["pong"]


# -- events -----------------------------------------------------------------------


def test_subscribe_live_events_in_order(env):
    client = env.connect()
    result = client.call("events.subscribe")["result"]
    assert result == {"subscribed": True, "last_seq": 0}
    for n in range(3):
        env.bus.publish("message", {"n": n})
    got = [client.event() for _ in range(3)]
    assert [e["seq"] for e in got] == [1, 2, 3]
    assert [e["payload"]["n"] for e in got] == [0, 1, 2]
    assert got[0]["type"] == "message" and isinstance(got[0]["ts"], float)
    assert client.notifications == []


def test_no_events_before_subscribe(env):
    client = env.connect()
    env.bus.publish("message", {"n": 1})
    assert client.call("server.ping")["result"]["pong"]
    assert client.notifications == []


def test_subscribe_replays_from_since(env):
    for n in range(5):
        env.bus.publish("message", {"n": n})
    client = env.connect()
    result = client.call("events.subscribe", {"since": 2})["result"]
    assert result["last_seq"] == 5
    assert [client.event()["seq"] for _ in range(3)] == [3, 4, 5]
    env.bus.publish("message", {"n": 5})
    assert client.event()["seq"] == 6  # replay then live, no duplicates


def test_subscribe_gap_when_since_left_the_buffer(env):
    small = EventBus(buffer_size=3)
    api = api_for(env.manager, small)
    handle = sa.start(api, small, env.tmp / "gap" / "g.sock")
    try:
        for n in range(10):
            small.publish("message", {"n": n})
        client = Rpc(env.tmp / "gap" / "g.sock")
        client.call("events.subscribe", {"since": 1})
        first = client.event()
        assert first["type"] == "gap" and first["payload"]["reason"] == "buffer"
        assert [client.event()["seq"] for _ in range(3)] == [8, 9, 10]
        client.close()
    finally:
        sa.stop(handle)


def test_slow_consumer_overflow_becomes_gap(env):
    small = EventBus(buffer_size=2, subscriber_queue_size=2)
    sub = small.subscribe()
    for n in range(10):
        small.publish("message", {"n": n})
    assert sub.get(0).type == "gap"  # the bus contract the adapter relies on
    sub.close()


@pytest.mark.parametrize("since", [-1, "3", 1.5, True])
def test_subscribe_rejects_bad_since(env, since):
    assert (
        env.connect().call("events.subscribe", {"since": since})["error"]["code"]
        == -32602
    )


def test_unsubscribe_stops_events_and_resubscribe_restarts(env):
    client = env.connect()
    client.call("events.subscribe")
    assert client.call("events.unsubscribe")["result"] == {"subscribed": False}
    assert client.call("events.unsubscribe")["result"] == {"subscribed": False}
    env.bus.publish("message", {"n": 1})
    assert client.call("server.ping")["result"]["pong"]
    assert client.notifications == []
    client.call("events.subscribe", {"since": 0})
    assert client.event()["seq"] == 1
    client.call("events.subscribe", {"since": 1})  # restarts: no duplicate stream
    env.bus.publish("message", {"n": 2})
    assert client.event()["seq"] == 2
    assert client.call("server.ping")["result"]["pong"]
    assert client.notifications == []


def test_heartbeat_when_idle(env, monkeypatch):
    monkeypatch.setattr(sa, "HEARTBEAT_INTERVAL_S", 0.1)
    client = env.connect()
    client.call("events.subscribe")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        msg = client.recv()
        if msg and msg.get("method") == "heartbeat":
            assert isinstance(msg["params"]["ts"], float)
            return
    pytest.fail("no heartbeat")


def test_subscription_closed_when_client_disconnects(env):
    client = env.connect()
    client.call("events.subscribe")
    assert len(env.bus._subs) == 1
    client.close()
    deadline = time.monotonic() + 5
    while env.bus._subs and time.monotonic() < deadline:
        env.bus.publish("message", {})  # a write to the dead peer notices it
        time.sleep(0.05)
    assert env.bus._subs == []


# -- location, permissions, lifecycle -------------------------------------------------


def test_permissions(env):
    assert stat.S_IMODE(env.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(env.path.parent.stat().st_mode) == 0o700
    runtime = env.path.with_suffix(".json")
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o600


def test_existing_loose_default_directory_is_tightened(tmp_path):
    directory = tmp_path / "megachat"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    handle = sa.start(api_for(_Manager(), EventBus()), None, directory / "server.sock")
    try:
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    finally:
        sa.stop(handle)


def test_socket_is_never_group_or_world_accessible_under_a_loose_umask(tmp_path):
    old = os.umask(0)
    try:
        handle = sa.start(
            api_for(_Manager(), EventBus()), None, tmp_path / "u" / "s.sock"
        )
    finally:
        os.umask(old)
    try:
        assert stat.S_IMODE(handle.path.stat().st_mode) == 0o600
    finally:
        sa.stop(handle)


def test_foreign_uid_peer_is_refused(env, monkeypatch):
    monkeypatch.setattr(sa, "_peer_uid", lambda _sock: os.geteuid() + 1)
    client = env.connect()
    # The server drops the connection as soon as it sees the peer uid, so the
    # write may already hit a closed socket (EPIPE) or the read a reset: every
    # outcome is "closed without an answer", none may be a response.
    try:
        client.request("server.ping")
        reply = client.recv()
    except (BrokenPipeError, ConnectionResetError):
        reply = None
    assert reply is None  # closed without an answer


def test_same_uid_peer_is_accepted_and_uid_is_read_from_the_kernel(env):
    seen = []
    real = sa._peer_uid

    def spy(sock):
        seen.append(real(sock))
        return seen[-1]

    sa._peer_uid = spy
    try:
        assert env.connect().call("server.ping")["result"]["pong"]
    finally:
        sa._peer_uid = real
    if hasattr(socket, "SO_PEERCRED"):
        assert seen == [os.geteuid()]


def test_runtime_file_lifecycle(tmp_path):
    api = api_for(_Manager(), EventBus())
    path = tmp_path / "rt" / "server.sock"
    runtime = tmp_path / "rt" / "server.json"
    before = time.time()
    handle = sa.start(api, None, path)
    data = json.loads(runtime.read_text())
    assert set(data) == {"pid", "socket", "version", "api_version", "started_at"}
    assert data["pid"] == os.getpid()
    assert data["socket"] == str(path)
    assert (
        data["version"] == sa.SERVER_VERSION and data["api_version"] == sa.API_VERSION
    )
    assert before - 1 <= data["started_at"] <= time.time()
    assert not list(path.parent.glob(".*.tmp"))
    assert path.exists()
    sa.stop(handle)
    assert not path.exists() and not runtime.exists()
    assert handle.status == "down"


def test_stop_drops_connections_and_is_idempotent(env):
    client = env.connect()
    client.call("events.subscribe")
    sa.stop(env.handle)
    sa.stop(env.handle)
    sa.stop(None)
    assert client.recv() is None
    with pytest.raises(OSError):
        Rpc(env.path)


def test_stop_leaves_a_successors_files_alone(tmp_path):
    api = api_for(_Manager(), EventBus())
    path = tmp_path / "s" / "server.sock"
    first = sa.start(api, None, path)
    path.unlink()  # something replaced our socket (and runtime file)...
    path.with_suffix(".json").write_text('{"pid": 1}')
    other = socket.socket(socket.AF_UNIX)
    other.bind(str(path))
    try:
        sa.stop(first)
        assert path.exists() and path.with_suffix(".json").exists()
    finally:
        other.close()


def test_stale_socket_and_runtime_file_are_cleaned(tmp_path):
    directory = tmp_path / "st"
    directory.mkdir()
    path = directory / "server.sock"
    dead = socket.socket(socket.AF_UNIX)
    dead.bind(str(path))
    dead.close()  # a crashed server leaves the file behind
    assert path.exists()
    path.with_suffix(".json").write_text('{"pid": 999999}')
    handle = sa.start(api_for(_Manager(), EventBus()), None, path)
    try:
        client = Rpc(path)
        assert client.call("server.ping")["result"]["pong"]
        client.close()
        assert json.loads(path.with_suffix(".json").read_text())["pid"] == os.getpid()
    finally:
        sa.stop(handle)


def test_live_server_on_the_path_blocks_startup(env):
    with pytest.raises(sa.SocketAdapterError, match="already listening"):
        sa.start(env.api, env.bus, env.path)
    assert env.connect().call("server.ping")["result"]["pong"]  # untouched
    assert env.path.with_suffix(".json").exists()


def test_regular_file_on_the_path_is_never_removed(tmp_path):
    path = tmp_path / "f" / "server.sock"
    path.parent.mkdir()
    path.write_text("precious")
    with pytest.raises(sa.SocketAdapterError, match="not our socket"):
        sa.start(api_for(_Manager(), EventBus()), None, path)
    assert path.read_text() == "precious"


def test_too_long_path_is_a_clear_error(tmp_path):
    with pytest.raises(sa.SocketAdapterError, match="too long"):
        sa.start(
            api_for(_Manager(), EventBus()), None, tmp_path / ("d" * 120) / "s.sock"
        )


def test_bind_failure_cleans_up(tmp_path, monkeypatch):
    path = tmp_path / "b" / "server.sock"

    async def boom(*_a, **_k):
        raise OSError("boom")

    monkeypatch.setattr(sa.asyncio, "start_unix_server", boom)
    with pytest.raises(sa.SocketAdapterError, match="boom"):
        sa.start(api_for(_Manager(), EventBus()), None, path)
    assert not path.exists() and not path.with_suffix(".json").exists()


def test_default_socket_path_resolution(tmp_path, monkeypatch):
    env = {"XDG_RUNTIME_DIR": str(tmp_path)}
    assert sa.default_socket_path(env) == tmp_path / "megachat" / "server.sock"
    assert sa.default_socket_path({SOCKET_ENV: "/x/y.sock"}) == Path("/x/y.sock")
    assert sa.default_socket_path(
        {SOCKET_ENV: "/x/y.sock", "XDG_RUNTIME_DIR": str(tmp_path)}
    ) == Path("/x/y.sock")
    monkeypatch.setenv("HOME", str(tmp_path))
    fallback = sa.default_socket_path({})
    assert fallback == tmp_path / ".local" / "state" / "megachat" / "server.sock"
    assert sa.default_socket_path({"XDG_RUNTIME_DIR": "relative"}) == fallback
    assert sa.runtime_file_path(fallback) == fallback.with_name("server.json")


SOCKET_ENV = sa.SOCKET_ENV


# -- attachments (mc-7kp) -------------------------------------------------------------


def _send(client: Rpc, *paths: Any, **extra: Any) -> dict:
    attachments = [
        p if isinstance(p, dict) else {"path": str(p), "filename": Path(p).name}
        for p in paths
    ]
    return client.call(
        "messages.send",
        {
            "protocol": "signal",
            "contact_id": "alice",
            "text": "",
            "attachments": attachments,
            **extra,
        },
    )


@pytest.fixture
def png(tmp_path):
    path = tmp_path / "pic.png"
    path.write_bytes(_PNG)
    return path


def test_attachment_is_copied_and_the_copy_cleaned_up(env, png):
    reply = _send(env.connect(), png)
    assert reply["result"] == {"ok": True}
    (call,) = env.manager.attachment_calls
    (staged,) = call["paths"]
    assert staged != png and staged.parent == env.uploads  # the op saw our copy
    assert call["bytes"] == [_PNG]
    assert png.read_bytes() == _PNG  # the client's file is untouched
    assert _uploads(env) == []
    assert stat.S_IMODE(env.uploads.stat().st_mode) == 0o700


def test_staged_copy_is_private_and_removed_when_the_send_fails(env, png):
    env.manager.attachments_error = RuntimeError("backend exploded")
    reply = _send(env.connect(), png)
    assert reply["error"]["code"] == sa.RPC_CODES["backend_error"]
    assert _uploads(env) == []


def test_missing_filename_defaults_to_the_basename(env, png):
    reply = _send(env.connect(), {"path": str(png)})
    assert reply["result"] == {"ok": True}


def test_wrong_extension_still_rejected(env, tmp_path):
    bad = tmp_path / "pic.txt"
    bad.write_bytes(_PNG)
    assert _send(env.connect(), bad)["error"]["code"] == -32602
    assert env.manager.attachment_calls == [] and _uploads(env) == []


def test_non_media_file_is_rejected_and_not_kept(env, tmp_path):
    secret = tmp_path / "id_rsa.png"
    secret.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
    reply = _send(env.connect(), secret)
    assert reply["error"]["data"]["code"] == "invalid_request"
    assert env.manager.attachment_calls == [] and _uploads(env) == []


def test_symlink_is_refused(env, png, tmp_path):
    link = tmp_path / "link.png"
    link.symlink_to(png)
    reply = _send(env.connect(), link)
    assert reply["error"]["data"]["code"] == "invalid_request"
    assert env.manager.attachment_calls == []
    # ...but a path through a symlinked *directory* is the user's own layout.
    real = tmp_path / "real"
    real.mkdir()
    (real / "p.png").write_bytes(_PNG)
    (tmp_path / "dirlink").symlink_to(real)
    assert _send(env.connect(), tmp_path / "dirlink" / "p.png")["result"] == {
        "ok": True
    }


def test_special_files_and_directories_are_refused(env, tmp_path):
    fifo = tmp_path / "pipe.png"
    os.mkfifo(fifo)
    directory = tmp_path / "dir.png"
    directory.mkdir()
    client = env.connect()
    for target in (fifo, directory, Path("/dev/null"), Path("/dev/zero")):
        reply = _send(client, {"path": str(target), "filename": "x.png"})
        assert reply["error"]["data"]["code"] == "invalid_request", target
    assert env.manager.attachment_calls == [] and _uploads(env) == []


@pytest.mark.parametrize("path", ["relative/p.png", "", "~/p.png", "../p.png"])
def test_relative_or_empty_paths_are_refused(env, path):
    reply = _send(env.connect(), {"path": path, "filename": "p.png"})
    assert reply["error"]["data"]["code"] == "invalid_request"
    assert env.manager.attachment_calls == []


def test_missing_file(env, tmp_path):
    reply = _send(env.connect(), tmp_path / "nope.png")
    assert reply["error"]["data"]["code"] == "invalid_request"


def test_invalid_payload_is_rejected_before_any_copy(env, png, monkeypatch):
    calls = []
    monkeypatch.setattr(sa, "stage_local_file", lambda *a, **k: calls.append(a))
    client = env.connect()
    reply = client.call(
        "messages.send",
        {
            "protocol": "signal",
            "contact_id": "ghost",
            "text": "",
            "attachments": [{"path": str(png), "filename": "pic.png"}],
        },
    )
    assert reply["error"]["data"]["code"] == "not_found"
    assert calls == []


def test_too_many_attachments_copy_nothing(env, png, monkeypatch):
    calls = []
    monkeypatch.setattr(sa, "stage_local_file", lambda *a, **k: calls.append(a))
    reply = _send(env.connect(), *([png] * 11))
    assert reply["error"]["data"]["message"] == "Too many attachments"
    assert calls == []


def test_oversized_total_is_too_large_and_cleans_up(env, png, monkeypatch):
    monkeypatch.setattr(sa, "MAX_STAGED_TOTAL_BYTES", len(_PNG) + 4)
    reply = _send(env.connect(), png, png)
    assert reply["error"]["data"]["code"] == "too_large"
    assert env.manager.attachment_calls == [] and _uploads(env) == []


def test_malformed_attachments_are_invalid_params(env):
    client = env.connect()
    for bad in ("x", [1], [{"path": 5}]):
        reply = client.call(
            "messages.send",
            {
                "protocol": "signal",
                "contact_id": "alice",
                "text": "t",
                "attachments": bad,
            },
        )
        assert reply["error"]["code"] == -32602


def test_rejected_slot_passes_through(env):
    reply = _send(env.connect(), {"rejected": "too_large"})
    assert reply["error"]["data"]["code"] == "too_large"


def test_check_operation_never_sees_client_paths(env, png):
    reply = env.connect().call(
        "messages.send.check",
        {
            "protocol": "signal",
            "contact_id": "alice",
            "text": "x",
            "attachments": [{"path": "/etc/passwd"}],
        },
    )
    assert reply["result"] == {"ok": True}


def test_attachment_policy_covers_every_operation_with_attachments(env):
    """A new operation taking AttachmentIn must get an explicit decision."""
    assert sa.attachment_operations(env.api) == {
        "messages.send",
        "messages.send.check",
        "gmail.messages.send",
        "gmail.messages.send.check",
    }
    assert sa.attachment_operations(env.api) == (
        sa.STAGING_OPERATIONS | sa.IGNORED_ATTACHMENT_OPERATIONS
    )


# -- Gmail attachments (mc-im6) -------------------------------------------------------


@pytest.fixture
def mail(monkeypatch):
    from server import gmail

    calls: list[dict[str, Any]] = []

    def fake_send(**kwargs: Any) -> str:
        kwargs["attachments"] = list(kwargs["attachments"])
        calls.append(kwargs)
        return "<id@example.com>"

    monkeypatch.setattr(gmail, "send_email", fake_send)
    return calls


def _mail(client: Rpc, *paths: Any, **extra: Any) -> dict:
    attachments = [
        p if isinstance(p, dict) else {"path": str(p), "filename": Path(p).name}
        for p in paths
    ]
    return client.call(
        "gmail.messages.send",
        {"to": "a@example.com", "text": "hi", "attachments": attachments, **extra},
    )


def test_gmail_attachments_are_copied_read_and_cleaned_up(env, mail, tmp_path):
    zipped = tmp_path / "archive.zip"
    zipped.write_bytes(b"PK\x03\x04" + b"z" * 100)
    # Not media: a "png" above the 20 MB media limit is a fine mail attachment.
    big = tmp_path / "big.png"
    big.write_bytes(_PNG + b"0" * (21 * 1024 * 1024))
    reply = _mail(env.connect(), zipped, big)
    assert reply["result"] == {"ok": True, "message_id": "<id@example.com>"}
    (call,) = mail
    assert [(n, len(d)) for n, d in call["attachments"]] == [
        ("archive.zip", 104),
        ("big.png", len(_PNG) + 21 * 1024 * 1024),
    ]
    assert zipped.read_bytes().startswith(b"PK")  # the client's file is untouched
    assert _uploads(env) == []


def test_gmail_attachment_checks_run_before_any_copy(env, mail, tmp_path, monkeypatch):
    copies = []
    monkeypatch.setattr(sa, "stage_local_file", lambda *a, **k: copies.append(a))
    note = tmp_path / "n.txt"
    note.write_text("x")
    reply = _mail(env.connect(), *([note] * 11))
    assert reply["error"]["data"] == {
        "code": "invalid_request",
        "message": "Troppi allegati",
        "details": None,
    }
    assert copies == [] and mail == []


def test_gmail_attachment_over_the_gmail_limit_is_too_large(env, mail, tmp_path):
    big = tmp_path / "big.bin"
    with big.open("wb") as handle:
        handle.truncate(25 * 1024 * 1024 + 1)
    reply = _mail(env.connect(), big)
    assert reply["error"]["data"]["code"] == "too_large"
    assert mail == [] and _uploads(env) == []


def test_gmail_attachment_symlink_and_missing_paths_are_refused(env, mail, tmp_path):
    target = tmp_path / "t.txt"
    target.write_text("x")
    link = tmp_path / "l.txt"
    link.symlink_to(target)
    client = env.connect()
    for path in (link, tmp_path / "nope.txt", Path("relative.txt")):
        reply = _mail(client, {"path": str(path), "filename": "x.txt"})
        assert reply["error"]["data"]["code"] == "invalid_request", path
    assert mail == [] and _uploads(env) == []


def test_unknown_attachment_operation_blocks_startup(tmp_path, monkeypatch):
    class _Req(BaseModel):
        attachments: list[sa.AttachmentIn] = []

    monkeypatch.setitem(
        server_api._REGISTRY,
        "test.files",
        Operation("test.files", _Req, dict, lambda api, req: {}, ""),
    )
    with pytest.raises(sa.SocketAdapterError, match="no attachment policy"):
        sa.start(api_for(_Manager(), EventBus()), None, tmp_path / "x" / "s.sock")


def test_attachment_field_with_another_name_blocks_startup(tmp_path, monkeypatch):
    class _Req(BaseModel):
        files: list[sa.AttachmentIn] = []

    monkeypatch.setitem(
        server_api._REGISTRY,
        "test.files",
        Operation("test.files", _Req, dict, lambda api, req: {}, ""),
    )
    with pytest.raises(sa.SocketAdapterError, match="outside 'attachments'"):
        sa.start(api_for(_Manager(), EventBus()), None, tmp_path / "x" / "s.sock")


def test_stale_staged_files_are_pruned_at_start(env, tmp_path):
    env.uploads.mkdir(parents=True, exist_ok=True)
    old, fresh = env.uploads / "old.png", env.uploads / "new.png"
    old.write_bytes(_PNG)
    fresh.write_bytes(_PNG)
    past = time.time() - 7200
    os.utime(old, (past, past))
    handle = sa.start(env.api, env.bus, tmp_path / "pr" / "s.sock")
    try:
        assert not old.exists() and fresh.exists()
    finally:
        sa.stop(handle)
