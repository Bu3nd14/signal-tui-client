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
    assert kwargs["api"] is core.api


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


def test_no_web_warns_and_skips_adapter(lock_file, fake_backends, caplog):
    stop = threading.Event()
    stop.set()
    with (
        caplog.at_level(logging.WARNING, logger="signal_tui"),
        patch("web.server.start_web_server") as start_web,
        patch("protocols.db._prune_cache"),
    ):
        rc = server_main.serve(["--no-web"], stop_event=stop)
    assert rc == 0
    start_web.assert_not_called()
    assert any("without the web adapter" in r.message for r in caplog.records)
    assert not lock_file.exists()


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
