"""Exit dialog (Detach / Quit / Cancel), quit routing per mode and the
``server.shutdown`` / ``server.clients`` operations (design §5)."""

from __future__ import annotations

import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from client_harness import api_for
from textual.app import App

import server.socket_adapter as sa
from server.api import ApiError, ChatAPI
from server.attach import server_pid, wait_server_stopped
from server.bus import EventBus
from server.paths import runtime_file_path
from server.socket_client import ServerUnavailable, SocketClient
from tui import exit_flow
from tui.exit_dialog import ExitDialogScreen, other_clients_text


async def _until(pilot, predicate, seconds=8.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        await pilot.pause(0.05)
    return predicate()


async def _exited(pilot, app):
    assert await _until(pilot, lambda: app._exit, 10), "the TUI did not exit"


def _wait(predicate, seconds=8.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# -- the modal -----------------------------------------------------------------


class Host(App):
    def __init__(self, **kwargs):
        super().__init__()
        self.kwargs = kwargs
        self.results: list[str] = []

    def on_mount(self):
        self.push_screen(ExitDialogScreen(**self.kwargs), self.results.append)


@pytest.mark.parametrize(
    ("spawned", "focused"), [(True, "exit-quit"), (False, "exit-detach")]
)
async def test_default_focus_follows_who_spawned_the_server(spawned, focused):
    host = Host(default_quit=spawned)
    async with host.run_test() as pilot:
        await pilot.pause()
        assert host.screen.focused.id == focused
        await pilot.press("enter")
        await pilot.pause()
    assert host.results == ["quit" if spawned else "detach"]


@pytest.mark.parametrize(
    ("keys", "result"),
    [
        (["d"], "detach"),
        (["q"], "quit"),
        (["escape"], "cancel"),
        (["right", "right", "enter"], "cancel"),  # detach -> quit -> cancel
        (["left", "enter"], "cancel"),  # wraps from the first button
        (["tab", "enter"], "quit"),
    ],
)
async def test_keys(keys, result):
    host = Host(default_quit=False)
    async with host.run_test() as pilot:
        await pilot.pause()
        await pilot.press(*keys)
        await pilot.pause()
    assert host.results == [result]


async def test_other_clients_are_announced_and_quit_needs_confirmation():
    host = Host(default_quit=True, other_clients=2)
    async with host.run_test() as pilot:
        await pilot.pause()
        text = str(host.screen.query_one("#exit-others").render())
        assert "Altri 2 client connessi" in text
        await pilot.press("q")
        await pilot.pause()
        assert host.results == []  # asked again, nothing decided
        assert host.screen.focused.id == "exit-cancel"  # the safe button
        assert not host.screen.query_one("#exit-detach").display
        await pilot.press("d")  # Detach is not offered any more
        await pilot.pause()
        assert host.results == []
        await pilot.press("q")
        await pilot.pause()
    assert host.results == ["quit"]


async def test_confirmation_can_be_cancelled_with_enter_and_escape():
    for keys in (["q", "enter"], ["q", "escape"]):
        host = Host(default_quit=True, other_clients=1)
        async with host.run_test() as pilot:
            await pilot.pause()
            await pilot.press(*keys)
            await pilot.pause()
        assert host.results == ["cancel"]


async def test_quit_without_other_clients_is_immediate():
    host = Host(default_quit=False, other_clients=0)
    async with host.run_test() as pilot:
        await pilot.pause()
        assert not host.screen.query_one("#exit-others").display
        await pilot.press("q")
        await pilot.pause()
    assert host.results == ["quit"]


def test_other_clients_text():
    assert other_clients_text(0) == ""
    assert other_clients_text(1) == "1 altro client connesso"
    assert other_clients_text(3) == "Altri 3 client connessi"


# -- operations ----------------------------------------------------------------


def test_shutdown_is_not_supported_without_a_host_hook():
    api = api_for(SimpleNamespace())
    with pytest.raises(ApiError) as err:
        api.call_json("server.shutdown")
    assert err.value.code == "not_supported"


def test_shutdown_acknowledges_first_then_runs_the_hook():
    api = api_for(SimpleNamespace())
    fired = threading.Event()
    api.shutdown_hook = fired.set
    with patch("server.api.SHUTDOWN_DELAY_S", 0.3):
        assert api.call_json("server.shutdown") == {"status": "ok"}
        assert not fired.is_set()  # the answer came back before the stop
        assert fired.wait(5)


@pytest.fixture
def sock_path(monkeypatch):
    short = Path(tempfile.mkdtemp(prefix="mc"))
    path = short / "s.sock"
    monkeypatch.setenv("MEGACHAT_SOCKET", str(path))
    yield path
    shutil.rmtree(short, ignore_errors=True)


class MiniServer:
    """A socket adapter over a stand-in core whose shutdown hook is recorded."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.bus = EventBus()
        core = SimpleNamespace(
            manager=SimpleNamespace(get=lambda _p: None), bus=self.bus
        )
        core.api = ChatAPI(core)
        self.api = core.api
        self.stopped = threading.Event()
        self.api.shutdown_hook = self.stopped.set
        self.handle = sa.start(self.api, self.bus, path)

    def stop(self) -> None:
        sa.stop(self.handle)


@pytest.fixture
def mini(sock_path):
    srv = MiniServer(sock_path)
    yield srv
    srv.stop()


def _client(path):
    return SocketClient.connect(path, backoff=(0.05, 0.2), reconnect=False)


def test_clients_counts_socket_connections_and_web_separately(mini):
    a = _client(mini.path)
    try:
        assert a.call("server.clients") == {
            "connections": 1,
            "self_included": True,
            "web": 0,
        }
        b = _client(mini.path)
        try:
            assert a.call("server.clients")["connections"] == 2
            mini.api.client_counters["web"] = lambda: 3
            assert a.call("server.clients")["web"] == 3
        finally:
            b.close()
        assert _wait(lambda: a.call("server.clients")["connections"] == 1)
    finally:
        a.close()


def test_clients_without_a_socket_adapter():
    api = api_for(SimpleNamespace())
    assert api.call_json("server.clients") == {
        "connections": 0,
        "self_included": False,
        "web": 0,
    }


def test_shutdown_over_the_socket_answers_before_the_stop(mini):
    client = _client(mini.path)
    try:
        assert client.call("server.shutdown") == {"status": "ok"}
        assert mini.stopped.wait(5)
    finally:
        client.close()


def test_shutdown_response_survives_a_stop_that_follows_at_once(mini):
    """The adapter drains its connections: even a stop right after the
    acknowledgement is written cannot lose it."""
    mini.api.shutdown_hook = mini.stop
    client = _client(mini.path)
    try:
        assert client.call("server.shutdown") == {"status": "ok"}
    finally:
        client.close()
    assert _wait(lambda: not mini.path.exists())


def test_adapter_unregisters_its_counter_on_stop(mini):
    assert "socket" in mini.api.client_counters
    mini.stop()
    assert "socket" not in mini.api.client_counters


# -- wait_server_stopped -------------------------------------------------------


def test_wait_server_stopped_follows_the_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert wait_server_stopped("/nonexistent/s.sock", proc.pid, 0.3) is False
        assert (
            wait_server_stopped(
                "/nonexistent/s.sock", proc.pid, 5, cancelled=lambda: True
            )
            is False
        )
        proc.kill()
        proc.wait()
        assert wait_server_stopped("/nonexistent/s.sock", proc.pid, 5) is True
    finally:
        proc.kill()


def test_wait_server_stopped_without_pid_watches_the_runtime_file(tmp_path):
    sock = tmp_path / "s.sock"
    assert wait_server_stopped(sock, None, 1) is True  # nothing there
    runtime_file_path(sock).write_text("{}")
    assert wait_server_stopped(sock, None, 0.3) is False
    assert server_pid(sock) is None
    runtime_file_path(sock).write_text('{"pid": 4242}')
    assert server_pid(sock) == 4242


# -- TUI routing ----------------------------------------------------------------


def _new_app(client, **kwargs):
    from tui.app import SignalTUI

    app = SignalTUI(client=client, **kwargs)
    app.statuses = []
    original = app._status

    def recording(text, duration=3.0):
        app.statuses.append(text)
        original(text, duration)

    app._status = recording
    return app


@pytest.fixture
def tui_client(mini):
    client = SocketClient.connect(
        mini.path, backoff=(0.05, 0.2), heartbeat_timeout=3, ping_interval=1
    )
    yield client
    client.close()


#: Threads of the in-test server (the client's is 'megachat-sock-client').
_SERVER_THREADS = ("megachat-socket", "megachat-sock-op", "megachat-sock-aux")


def _no_new_threads(before: set[int]) -> bool:
    return _wait(
        lambda: (
            not [
                t
                for t in threading.enumerate()
                if t.ident not in before
                and t.is_alive()
                and "megachat" in t.name
                and not t.name.startswith(_SERVER_THREADS)
            ]
        ),
        5,
    )


async def test_quit_key_opens_the_dialog_and_detach_leaves_the_server(mini, tui_client):
    before = {t.ident for t in threading.enumerate()}
    app = _new_app(tui_client, server_spawned=False)
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        await pilot.press("ctrl+q")
        assert await _until(pilot, lambda: isinstance(app.screen, ExitDialogScreen))
        assert app.screen.focused.id == "exit-detach"  # attached: Detach
        await pilot.press("d")
        await _exited(pilot, app)
    assert tui_client.state == "closed"
    assert not mini.stopped.is_set()  # the server was not asked to stop
    other = _client(mini.path)
    try:
        assert other.call("server.status") is not None
    finally:
        other.close()
    assert _no_new_threads(before), [t.name for t in threading.enumerate()]


async def test_quit_asks_the_server_to_stop_and_waits_for_it(mini, tui_client):
    before = {t.ident for t in threading.enumerate()}
    waited: list[tuple] = []
    app = _new_app(tui_client, server_spawned=True)
    with patch.object(
        exit_flow, "wait_server_stopped", lambda *a, **k: waited.append(a) or True
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.3)
            await pilot.press("ctrl+q")
            assert await _until(pilot, lambda: isinstance(app.screen, ExitDialogScreen))
            assert app.screen.focused.id == "exit-quit"  # spawned: Quit
            await pilot.press("enter")
            await _exited(pilot, app)
        assert mini.stopped.wait(5)
    assert len(waited) == 1 and Path(waited[0][0]) == mini.path
    assert tui_client.state == "closed"
    assert _no_new_threads(before), [t.name for t in threading.enumerate()]


async def test_other_clients_trigger_the_warning_and_second_confirmation(
    mini, tui_client
):
    others = [_client(mini.path), _client(mini.path)]
    try:
        app = _new_app(tui_client, server_spawned=True)
        with patch.object(exit_flow, "wait_server_stopped", lambda *a, **k: True):
            async with app.run_test() as pilot:
                await pilot.pause(0.3)
                await pilot.press("ctrl+q")
                assert await _until(
                    pilot, lambda: isinstance(app.screen, ExitDialogScreen)
                )
                assert "Altri 2 client connessi" in str(
                    app.screen.query_one("#exit-others").render()
                )
                await pilot.press("q")
                await pilot.pause(0.2)
                assert not mini.stopped.is_set()
                assert isinstance(app.screen, ExitDialogScreen)
                await pilot.press("q")
                await _exited(pilot, app)
            assert mini.stopped.wait(5)
    finally:
        for client in others:
            client.close()


async def test_cancel_stays_and_the_dialog_can_be_reopened(mini, tui_client):
    app = _new_app(tui_client, server_spawned=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        for _ in range(2):
            await pilot.press("ctrl+q")
            assert await _until(pilot, lambda: isinstance(app.screen, ExitDialogScreen))
            await pilot.press("escape")
            assert await _until(
                pilot, lambda: not isinstance(app.screen, ExitDialogScreen)
            )
            assert not app._exit_pending
        assert app.is_running
        assert not mini.stopped.is_set()
        app._polling_active = False
        tui_client.close()


async def test_ctrl_c_detaches_without_a_dialog_and_never_stops_the_server(
    mini, tui_client
):
    app = _new_app(tui_client, server_spawned=True)
    app._sigint_callback = lambda: app.shutdown_from_signal()
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        signal.raise_signal(signal.SIGINT)
        assert await _until(pilot, lambda: not app.is_running, 5)
    assert not mini.stopped.is_set()
    assert tui_client.state == "closed"


async def test_unreachable_server_exits_without_a_dialog(mini, tui_client):
    app = _new_app(tui_client, server_spawned=True)

    def unavailable(*_a, **_k):
        raise ServerUnavailable("gone")

    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        with patch.object(tui_client, "call", unavailable):
            await pilot.press("ctrl+q")
            await _exited(pilot, app)
    assert not app.is_running
    assert not mini.stopped.is_set()
    assert tui_client.state == "closed"


async def test_known_lost_connection_exits_without_asking(mini, tui_client):
    app = _new_app(tui_client, server_spawned=False)
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        with patch.object(type(tui_client), "connected", property(lambda _s: False)):
            await pilot.press("ctrl+q")
            await _exited(pilot, app)
    assert tui_client.state == "closed"


async def test_quit_exits_even_if_the_server_vanishes_before_shutdown(mini, tui_client):
    app = _new_app(tui_client, server_spawned=True)
    real = tui_client.call

    def flaky(name, data=None, *, timeout=None):
        if name == "server.shutdown":
            raise ServerUnavailable("gone")
        return real(name, data, timeout=timeout)

    tui_client.call = flaky
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        await pilot.press("ctrl+q")
        assert await _until(pilot, lambda: isinstance(app.screen, ExitDialogScreen))
        await pilot.press("q")
        await _exited(pilot, app)
    assert tui_client.state == "closed"


async def test_quit_failure_shows_the_error_and_stays(mini, tui_client):
    app = _new_app(tui_client, server_spawned=True)
    mini.api.shutdown_hook = None  # the server cannot stop itself
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        await pilot.press("ctrl+q")
        assert await _until(pilot, lambda: isinstance(app.screen, ExitDialogScreen))
        await pilot.press("q")
        assert await _until(
            pilot,
            lambda: any(exit_flow.MSG_STOP_FAILED[:20] in s for s in app.statuses),
        )
        assert app.is_running and not app._exit_pending
        app._polling_active = False
        tui_client.close()


async def test_in_process_tui_quits_without_a_dialog():
    from tui.app import SignalTUI

    app = SignalTUI()
    assert app._owns_core and app.server_spawned is None
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        await pilot.press("ctrl+q")
        await pilot.pause(0.5)
        assert not isinstance(app.screen, ExitDialogScreen)
