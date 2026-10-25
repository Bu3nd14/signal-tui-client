"""Really spawn ``megachat serve`` (isolated HOME, lock and socket) and attach
headless TUIs to it, also from two TUIs started at the same moment."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from server import attach, lock
from server.paths import runtime_file_path


@pytest.fixture
def isolated(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="mc"))  # short: sun_path limit
    (root / "home").mkdir()
    sock = root / "s.sock"
    lock_file = root / "l"
    for key, value in {
        "HOME": str(root / "home"),
        "XDG_DATA_HOME": str(root / "home" / "share"),
        "MEGACHAT_SOCKET": str(sock),
        "MEGACHAT_LOCK_FILE": str(lock_file),
        "SIGNAL_ENABLED": "0",
        "WHATSAPP_API_PORT": "1",  # nothing listens: no WAHA auto-detect
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(lock, "LOCK_FILE", str(lock_file))
    yield SimpleNamespace(root=root, sock=sock, lock=lock_file)
    # Whatever happened, no server may outlive the test.
    try:
        pid = int(lock_file.read_text())
        os.kill(pid, signal.SIGKILL)
    except (OSError, ValueError):
        pass
    shutil.rmtree(root, ignore_errors=True)


def _stop_server(env) -> None:
    pid = int(env.lock.read_text())
    os.kill(pid, signal.SIGINT)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the server did not stop on SIGINT")
    assert not env.lock.exists()
    assert not env.sock.exists()


def _server_pid(env) -> int:
    return json.loads(runtime_file_path(env.sock).read_text())["pid"]


@pytest.mark.integration
async def test_spawn_then_attach_two_tuis_and_stop(isolated):
    from tui.app import SignalTUI

    log = isolated.root / "serve.log"
    first = attach.attach_or_spawn(serve_args=["--no-web"], log_path=log)
    second = None
    try:
        assert first.spawned is True
        pid = _server_pid(isolated)
        assert int(isolated.lock.read_text()) == pid  # the lock is the server's

        # A second TUI attaches to the same server and takes no lock.
        second = attach.attach_or_spawn(serve_args=["--no-web"], log_path=log)
        assert second.spawned is False
        assert _server_pid(isolated) == pid
        assert int(isolated.lock.read_text()) == pid
        assert first.client.call("server.status")
        assert second.client.call("server.status")

        # Headless TUI on the spawned server.
        app = SignalTUI(client=first.client, server_spawned=first.spawned)
        assert app.server_spawned is True
        async with app.run_test() as pilot:
            await pilot.pause(0.5)
            app._polling_active = False
            app.client.close()
        # The first TUI is gone, the second still works.
        assert second.client.call("server.status")
    finally:
        first.client.close()
        if second is not None:
            second.client.close()
    _stop_server(isolated)


@pytest.mark.integration
def test_two_tuis_started_together_end_up_with_one_server(isolated):
    log = isolated.root / "serve.log"
    results: list[attach.Attached | BaseException] = []
    barrier = threading.Barrier(2)

    def start():
        barrier.wait()
        try:
            results.append(
                attach.attach_or_spawn(serve_args=["--no-web"], log_path=log)
            )
        except BaseException as exc:  # noqa: BLE001
            results.append(exc)

    threads = [threading.Thread(target=start) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    try:
        assert all(isinstance(r, attach.Attached) for r in results), results
        attached = [r for r in results if isinstance(r, attach.Attached)]
        assert len(attached) == 2
        # Both are clients of the same single server.
        assert int(isolated.lock.read_text()) == _server_pid(isolated)
        assert sum(a.spawned for a in attached) == 1
        for a in attached:
            assert a.client.call("server.status")
    finally:
        for r in results:
            if isinstance(r, attach.Attached):
                r.client.close()
    _stop_server(isolated)


async def _quit_with(app, pilot, key: str, default_focus: str) -> None:
    """Ctrl+Q, check the dialog's default button, press *key*."""
    from tui.exit_dialog import ExitDialogScreen

    await pilot.press("ctrl+q")
    deadline = time.monotonic() + 10
    while not isinstance(app.screen, ExitDialogScreen):
        assert time.monotonic() < deadline, "the exit dialog did not open"
        await pilot.pause(0.05)
    assert app.screen.focused.id == default_focus
    await pilot.press(key)
    deadline = time.monotonic() + 40  # Quit waits for the server process
    while not app._exit:
        assert time.monotonic() < deadline, "the TUI did not exit"
        await pilot.pause(0.05)


@pytest.mark.integration
async def test_detach_keeps_the_server_and_quit_from_another_tui_stops_it(isolated):
    from tui.app import SignalTUI

    log = isolated.root / "serve.log"
    first = attach.attach_or_spawn(serve_args=["--no-web"], log_path=log)
    second = None
    try:
        assert first.spawned is True
        pid = _server_pid(isolated)

        # The spawning TUI defaults to Quit; the user picks Detach instead.
        app1 = SignalTUI(client=first.client, server_spawned=first.spawned)
        async with app1.run_test() as pilot:
            await pilot.pause(0.5)
            await _quit_with(app1, pilot, "d", "exit-quit")
        assert first.client.state == "closed"
        await asyncio.sleep(0.5)
        os.kill(pid, 0)  # the server is still running
        assert isolated.lock.exists() and isolated.sock.exists()

        # A second TUI attaches (default: Detach) and Quits the server.
        second = attach.attach_or_spawn(serve_args=["--no-web"], log_path=log)
        assert second.spawned is False and _server_pid(isolated) == pid
        app2 = SignalTUI(client=second.client, server_spawned=second.spawned)
        async with app2.run_test() as pilot:
            await pilot.pause(0.5)
            await _quit_with(app2, pilot, "q", "exit-detach")
        assert second.client.state == "closed"
        # The TUI waited for the server process to be gone.
        with pytest.raises(OSError):
            os.kill(pid, 0)
        assert not isolated.lock.exists()
        assert not isolated.sock.exists()
        assert not runtime_file_path(isolated.sock).exists()
    finally:
        first.client.close()
        if second is not None:
            second.client.close()
