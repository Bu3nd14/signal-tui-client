"""Headless ``serve`` mode: lock, core + web adapter, ordered shutdown."""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from server import main as server_main
from server.bus import EventBus
from server.core import ChatCore


@pytest.fixture
def lock_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "signal-tui.lock"
    monkeypatch.setattr("server.lock.LOCK_FILE", str(path))
    monkeypatch.setattr(server_main, "LOG_FILE", str(tmp_path / "serve.log"))
    return path


@pytest.fixture
def fake_backends():
    """Same backend patching as tests/conftest.py (no real daemons/WAHA)."""
    with (
        patch("server.core.BackendManager"),
        # a real ``protocol`` string: it travels in the (JSON) backend_* events
        patch(
            "server.core.SignalBackend",
            return_value=MagicMock(protocol="signal"),
        ),
        patch("server.core.whatsapp_enabled", return_value=False),
        patch("server.core.telegram_enabled", return_value=False),
        patch("server.core.signal_enabled", return_value=False),
    ):
        yield


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_serve_end_to_end_with_web(lock_file, fake_backends):
    port = _free_port()
    stop = threading.Event()
    result: list[int] = []
    prune = MagicMock()
    with patch("protocols.db._prune_cache", prune):
        t = threading.Thread(
            target=lambda: result.append(
                server_main.serve(
                    [
                        "--web-port",
                        str(port),
                        "--web-host",
                        "127.0.0.1",
                        "--web-no-auth",
                    ],
                    stop_event=stop,
                )
            )
        )
        t.start()
        try:

            def healthy() -> bool:
                try:
                    url = f"http://127.0.0.1:{port}/health"
                    with urllib.request.urlopen(url, timeout=1) as r:
                        return r.status == 200
                except OSError:
                    return False

            assert _wait(healthy)
            assert lock_file.read_text().strip() == str(os.getpid())
        finally:
            stop.set()
            t.join(15)
    assert not t.is_alive()
    assert result == [0]
    assert not lock_file.exists()
    prune.assert_called_once()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1)


def test_lock_contention_exits_1(lock_file, capsys):
    lock_file.write_text(str(os.getpid()))  # a live process holds the lock
    with patch.object(server_main, "ChatCore") as core_cls:
        rc = server_main.serve(["--no-web"], stop_event=threading.Event())
    assert rc == 1
    assert "already running" in capsys.readouterr().err
    core_cls.assert_not_called()
    assert lock_file.read_text().strip() == str(os.getpid())  # untouched


def _recording_core(calls: list[str], telegram=None):
    """A real ``ChatCore`` (fake backends) whose lifecycle steps are recorded."""
    core = ChatCore(EventBus())
    core.telegram_backend = telegram
    core.start = lambda: calls.append("core.start")  # type: ignore[method-assign]
    core.stop = lambda: calls.append("core.stop")  # type: ignore[method-assign]
    return core


def test_shutdown_order(lock_file, fake_backends):
    calls: list[str] = []
    telegram = MagicMock()
    telegram.disconnect_sync.side_effect = lambda: calls.append("telegram.disconnect")
    core = _recording_core(calls, telegram)
    stop = threading.Event()
    stop.set()
    handle = MagicMock(status="up")
    with (
        patch.object(server_main, "ChatCore", return_value=core),
        patch(
            "web.server.start_web_server",
            side_effect=lambda *a, **k: calls.append("web.start") or handle,
        ) as start_web,
        patch(
            "web.server.stop_web_server", side_effect=lambda h: calls.append("web.stop")
        ),
        patch("protocols.db._prune_cache", side_effect=lambda: calls.append("prune")),
        patch.object(
            server_main,
            "_release_lock",
            side_effect=lambda: calls.append("release"),
        ),
    ):
        rc = server_main.serve(
            ["--web-port", "4999", "--web-host", "0.0.0.0"], stop_event=stop
        )
    assert rc == 0
    assert calls == [
        "core.start",
        "web.start",
        "core.stop",
        "web.stop",
        "telegram.disconnect",
        "prune",
        "release",
    ]
    args, kwargs = start_web.call_args
    assert args[1] == 4999
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["require_auth"] is True
    assert isinstance(kwargs["bus"], EventBus)
    assert args[0] is core.api


def test_shutdown_failures_are_non_fatal(lock_file, fake_backends):
    core = ChatCore(EventBus())
    core.start = lambda: None  # type: ignore[method-assign]
    core.stop = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    stop = threading.Event()
    stop.set()
    prune = MagicMock(side_effect=RuntimeError("prune boom"))
    with (
        patch.object(server_main, "ChatCore", return_value=core),
        patch("protocols.db._prune_cache", prune),
    ):
        rc = server_main.serve(["--no-web"], stop_event=stop)
    assert rc == 0
    core.stop.assert_called_once()
    prune.assert_called_once()
    assert not lock_file.exists()


def test_no_web_and_no_socket_warns_that_nobody_can_connect(
    lock_file, fake_backends, caplog, capsys
):
    stop = threading.Event()
    stop.set()
    with (
        caplog.at_level(logging.WARNING, logger="signal_tui"),
        patch("web.server.start_web_server") as start_web,
        patch("server.main.socket_adapter.start") as start_socket,
        patch("protocols.db._prune_cache"),
    ):
        rc = server_main.serve(["--no-web", "--no-socket"], stop_event=stop)
    assert rc == 0
    start_web.assert_not_called()
    start_socket.assert_not_called()
    assert any("no client can connect" in r.message for r in caplog.records)
    assert "no client can connect" in capsys.readouterr().err
    assert not lock_file.exists()


def test_no_web_still_serves_the_socket(lock_file, fake_backends, capsys, tmp_path):
    path = tmp_path / "nw" / "server.sock"
    seen: dict = {}
    stop = threading.Event()

    def run() -> None:
        with (
            patch("web.server.start_web_server") as start_web,
            patch("protocols.db._prune_cache"),
        ):
            seen["rc"] = server_main.serve(
                ["--no-web", "--socket", str(path)], stop_event=stop
            )
            seen["web_started"] = start_web.called

    t = threading.Thread(target=run)
    t.start()
    try:
        # Wait for the startup messages, not just the socket file: the file is
        # created before the two lines are printed.
        out = err = ""

        def saw_startup() -> bool:
            nonlocal out, err
            chunk = capsys.readouterr()
            out += chunk.out
            err += chunk.err
            return f"Socket: {path}" in out and "--no-web" in err

        assert _wait(saw_startup)
        assert "--no-web" in err and str(path) in err
        assert f"Socket: {path}" in out
    finally:
        stop.set()
        t.join(15)
    assert seen == {"rc": 0, "web_started": False}


def _rpc(path: Path, method: str, params=None) -> dict:
    import json

    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(5)
        s.connect(str(path))
        message = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        s.sendall(json.dumps(message).encode() + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            data += s.recv(65536)
    return json.loads(data)


def test_serve_starts_and_stops_the_socket_by_default(
    lock_file, fake_backends, tmp_path, isolate_server_socket
):
    stop = threading.Event()
    result: list[int] = []
    runtime = isolate_server_socket.with_suffix(".json")
    with patch("protocols.db._prune_cache"):
        t = threading.Thread(
            target=lambda: result.append(
                server_main.serve(["--no-web"], stop_event=stop)
            )
        )
        t.start()
        try:
            assert _wait(runtime.exists)  # written last: the socket listens
            hello = _rpc(isolate_server_socket, "rpc.hello")["result"]
            assert "messages.send" in hello["operations"]
            status = _rpc(isolate_server_socket, "server.status")
            assert "result" in status
        finally:
            stop.set()
            t.join(15)
    assert result == [0]
    assert not isolate_server_socket.exists() and not runtime.exists()
    assert not lock_file.exists()


def test_server_shutdown_op_stops_serve_cleanly(
    lock_file, fake_backends, tmp_path, isolate_server_socket
):
    """``server.shutdown`` is acknowledged, then serve exits 0 like on SIGINT."""
    stop = threading.Event()
    result: list[int] = []
    runtime = isolate_server_socket.with_suffix(".json")
    prune = MagicMock()
    with patch("protocols.db._prune_cache", prune):
        t = threading.Thread(
            target=lambda: result.append(
                server_main.serve(["--no-web"], stop_event=stop)
            )
        )
        t.start()
        try:
            assert _wait(runtime.exists)
            clients = _rpc(isolate_server_socket, "server.clients")["result"]
            assert clients["connections"] == 1 and clients["self_included"] is True
            answer = _rpc(isolate_server_socket, "server.shutdown")
            assert answer["result"] == {"status": "ok"}  # flushed to the caller
            t.join(15)
            assert not t.is_alive()
        finally:
            stop.set()
            t.join(15)
    assert result == [0]
    prune.assert_called_once()
    assert not isolate_server_socket.exists() and not runtime.exists()
    assert not lock_file.exists()


def test_no_socket_flag_skips_the_adapter(lock_file, fake_backends):
    stop = threading.Event()
    stop.set()
    with (
        patch("server.main.socket_adapter.start") as start_socket,
        patch("protocols.db._prune_cache"),
    ):
        assert server_main.serve(["--no-web", "--no-socket"], stop_event=stop) == 0
    start_socket.assert_not_called()


def test_socket_flags_are_exclusive():
    with pytest.raises(SystemExit):
        server_main.serve(["--no-socket", "--socket", "/tmp/x.sock"])


def test_socket_path_flag_is_passed_through_and_expanded(
    lock_file, fake_backends, monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    stop = threading.Event()
    stop.set()
    handle = MagicMock(path=tmp_path / "x.sock")
    with (
        patch("server.main.socket_adapter.start", return_value=handle) as start,
        patch("server.main.socket_adapter.stop"),
        patch("protocols.db._prune_cache"),
    ):
        rc = server_main.serve(["--no-web", "--socket", "~/x.sock"], stop_event=stop)
    assert rc == 0
    args = start.call_args.args
    assert args[2] == tmp_path / "x.sock"


def test_socket_failure_exits_1_and_cleans_up(
    lock_file, fake_backends, capsys, tmp_path
):
    # A live server already listens on the path (lock bypassed): refuse.
    path = tmp_path / "live" / "server.sock"
    path.parent.mkdir()
    live = socket.socket(socket.AF_UNIX)
    live.bind(str(path))
    live.listen()
    prune = MagicMock()
    try:
        with patch("protocols.db._prune_cache", prune):
            rc = server_main.serve(
                ["--no-web", "--socket", str(path)], stop_event=threading.Event()
            )
    finally:
        live.close()
    assert rc == 1
    assert "Socket adapter failed" in capsys.readouterr().err
    prune.assert_called_once()
    assert not lock_file.exists()
    assert path.exists()  # the other server's socket is left alone


def test_shutdown_stops_the_socket_before_the_web_adapter(lock_file, fake_backends):
    calls: list[str] = []
    core = _recording_core(calls)
    stop = threading.Event()
    stop.set()
    web = MagicMock(status="up")
    sock = MagicMock(path=Path("/x/s.sock"))
    with (
        patch.object(server_main, "ChatCore", return_value=core),
        patch(
            "server.main.socket_adapter.start",
            side_effect=lambda *a, **k: calls.append("socket.start") or sock,
        ),
        patch(
            "server.main.socket_adapter.stop",
            side_effect=lambda h: calls.append("socket.stop"),
        ),
        patch(
            "web.server.start_web_server",
            side_effect=lambda *a, **k: calls.append("web.start") or web,
        ),
        patch(
            "web.server.stop_web_server", side_effect=lambda h: calls.append("web.stop")
        ),
        patch("protocols.db._prune_cache", side_effect=lambda: calls.append("prune")),
    ):
        assert server_main.serve(["--web-port", "4999"], stop_event=stop) == 0
    assert calls == [
        "core.start",
        "socket.start",
        "web.start",
        "core.stop",
        "socket.stop",
        "web.stop",
        "prune",
    ]


def test_web_failure_exits_1_and_cleans_up(lock_file, fake_backends, capsys):
    prune = MagicMock()
    with (
        patch("web.server.start_web_server", return_value=None),
        patch("protocols.db._prune_cache", prune),
    ):
        rc = server_main.serve(["--web-port", "4999"], stop_event=threading.Event())
    assert rc == 1
    assert "Web adapter failed" in capsys.readouterr().err
    prune.assert_called_once()
    assert not lock_file.exists()


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_signal_stops_server(lock_file, fake_backends, sig):
    previous = signal.getsignal(sig)
    timer = threading.Timer(0.5, lambda: os.kill(os.getpid(), sig))
    with patch("protocols.db._prune_cache") as prune:
        timer.start()
        try:
            rc = server_main.serve(["--no-web"])
        finally:
            timer.cancel()
    assert rc == 0
    prune.assert_called_once()
    assert not lock_file.exists()
    assert signal.getsignal(sig) is previous


def test_invalid_port_rejected(lock_file):
    with pytest.raises(SystemExit):
        server_main.serve(["--web-port", "70000"])
    assert not lock_file.exists()
