"""The TUI over a remote client: no UI-thread calls, timeouts, errors, resync."""

from __future__ import annotations

import ast
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from client_harness import StubClient, inline_client_calls, view_app

from models import ChatContact
from server.api import ApiError
from server.bus import GAP, Event, EventBus
from server.client import ChatClient, InProcessSubscription
from server.socket_client import (
    CONNECTION_EVENT,
    IncompatibleServer,
    OperationTimeout,
    ServerUnavailable,
)
from tui.client_calls import (
    MSG_INCOMPATIBLE,
    MSG_RESTORED,
    MSG_UNAVAILABLE,
)
from tui.client_timeouts import OP_TIMEOUTS, WINDOW_REFRESH_TIMEOUT, timeout_for

ROOT = Path(__file__).resolve().parent.parent
UI_FILES = sorted((ROOT / "tui").glob("*.py")) + [ROOT / "device_link_screen.py"]
OP_NAME = r"^(server|link|contacts|messages|media|chats|download)\.[a-z_0-9]+$"


# -- static guards ------------------------------------------------------------------


def _literals(path: Path) -> set[str]:
    import re

    tree = ast.parse(path.read_text())
    return {
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and re.match(OP_NAME, n.value)
    }


def test_every_operation_the_tui_names_has_a_timeout():
    used: set[str] = set()
    for path in UI_FILES:
        if path.name != "client_timeouts.py":
            used |= _literals(path)
    assert used - set(OP_TIMEOUTS) == set()
    # No stale entries either: each configured operation is called somewhere.
    assert set(OP_TIMEOUTS) - used == set()


def test_no_direct_client_call_outside_the_timeout_helper():
    for path in UI_FILES:
        if path.name == "client_timeouts.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("call", "acall")
                and "client" in ast.unparse(node.func.value)
            ):
                raise AssertionError(f"{path.name}:{node.lineno} calls the client")


def test_timeout_values():
    assert timeout_for("messages.send") == 90.0
    assert timeout_for("messages.window", {"refresh": False}) == 15.0
    assert timeout_for("messages.window", {"refresh": True}) == WINDOW_REFRESH_TIMEOUT
    assert timeout_for("server.config") < timeout_for("messages.send")
    with pytest.raises(KeyError):
        timeout_for("nope.nothing")


# -- error mapping ------------------------------------------------------------------


def _send(app, contact, client_error):
    app.client = StubClient(client_error)
    app._schedule_resync = MagicMock()
    app._cache[contact.cache_key] = [
        {"is_mine": True, "text": "hi", "timestamp": 5, "status": "pending"}
    ]
    app._send_message_worker(
        "hi", 5, None, protocol=contact.protocol, contact_id=contact.id
    )
    return app.client


def test_send_passes_its_timeout():
    app, contact = view_app(StubClient({"ok": True, "status_applied": True}))
    app._cache[contact.cache_key] = []
    app._send_message_worker(
        "hi", 5, None, protocol=contact.protocol, contact_id=contact.id
    )
    assert app.client.timeouts == [90.0]


def test_timed_out_send_stays_pending_and_resyncs():
    app, contact = view_app(StubClient())
    client = _send(app, contact, OperationTimeout("slow"))
    assert client.timeouts == [90.0]
    assert app._cache[contact.cache_key][0]["status"] == "pending"
    app._update_message_widgets_status.assert_not_called()
    app._schedule_resync.assert_called_once_with("timeout")
    assert "non confermato" in app._status.call_args.args[0]


def test_unreachable_server_during_send_stays_pending_without_resync():
    app, contact = view_app(StubClient())
    _send(app, contact, ServerUnavailable("gone"))
    assert app._cache[contact.cache_key][0]["status"] == "pending"
    app._schedule_resync.assert_not_called()  # the restored connection resyncs
    assert "non raggiungibile" in app._status.call_args.args[0]


def test_incompatible_server_fails_the_send_with_a_persistent_message():
    app, contact = view_app(StubClient())
    _send(app, contact, IncompatibleServer("v2"))
    assert app._cache[contact.cache_key][0]["status"] == "failed"
    assert app._status.call_args_list[-1].args == (MSG_INCOMPATIBLE, 0)


def test_api_error_on_send_is_unchanged():
    app, contact = view_app(StubClient())
    _send(app, contact, ApiError("backend_down", "down", details={"error": "No"}))
    assert app._cache[contact.cache_key][0]["status"] == "failed"
    app._status.assert_called_with("❌ Send error: No", 0)


def test_timed_out_edit_keeps_the_new_text_and_resyncs():
    app, contact = view_app(StubClient(OperationTimeout("slow")))
    app._schedule_resync = MagicMock()
    app._restore_local_edit = MagicMock()
    snap = {
        "protocol": "signal",
        "contact_id": contact.id,
        "message_id": "m1",
    }
    app._edit_message_worker(snap, "old", "new")
    assert app.client.timeouts == [30.0]
    app._restore_local_edit.assert_not_called()
    app._schedule_resync.assert_called_once_with("timeout")


class TestReporting:
    def _app(self):
        app, _ = view_app(StubClient())
        return app

    def test_unavailable_is_reported_once_per_interval(self):
        app = self._app()
        for _ in range(5):
            assert app._report_client_error(ServerUnavailable("x")) is True
        assert app._status.call_count == 1
        assert app._status.call_args.args[0] == MSG_UNAVAILABLE

    def test_nothing_replaces_the_persistent_lost_message(self):
        app = self._app()
        app._on_client_connection({"state": "lost", "reason": "eof"})
        assert app._status.call_args.args == (MSG_UNAVAILABLE, 0)
        app._status.reset_mock()
        app._report_client_error(ServerUnavailable("x"))
        app._status.assert_not_called()

    def test_incompatible_is_persistent_and_shown_once(self):
        app = self._app()
        app._report_client_error(IncompatibleServer("v"))
        app._report_client_error(IncompatibleServer("v"))
        app._status.assert_called_once_with(MSG_INCOMPATIBLE, 0)

    def test_timeout_message_names_the_action(self):
        app = self._app()
        app._report_client_error(OperationTimeout("t"), "rubrica")
        assert "rubrica" in app._status.call_args.args[0]

    def test_other_errors_are_left_to_the_caller(self):
        app = self._app()
        assert app._report_client_error(ApiError("not_found", "x")) is False
        assert app._report_client_error(RuntimeError("x")) is False
        app._status.assert_not_called()

    def test_restored_clears_lost_and_resyncs(self):
        app = self._app()
        app._schedule_resync = MagicMock()
        app._on_client_connection({"state": "lost", "reason": "eof"})
        assert app._server_lost is True
        app._on_client_connection({"state": "restored", "reason": ""})
        assert app._server_lost is False
        app._status.assert_called_with(MSG_RESTORED)
        app._schedule_resync.assert_called_once_with("restored")


class TestEventRouting:
    def _app(self):
        app, _ = view_app(StubClient())
        app._schedule_resync = MagicMock()
        app._on_client_connection = MagicMock()
        return app

    def test_connection_event_goes_to_the_ui_handler(self):
        app = self._app()
        payload = {"state": "lost", "reason": "eof"}
        assert app._route_bus_event(Event(1, CONNECTION_EVENT, payload, 0.0))
        app._on_client_connection.assert_called_once_with(payload)

    def test_gap_resyncs(self):
        app = self._app()
        assert app._route_bus_event(Event(1, GAP, {"reason": "restart"}, 0.0))
        app._schedule_resync.assert_called_once_with("gap")
        assert app._contact_list_dirty is True


# -- resync -------------------------------------------------------------------------


class Script(ChatClient):
    """A client answering from a table, recording ``(op, thread, timeout)``."""

    def __init__(self, answers):
        self.answers = answers
        self.calls: list[tuple[str, int, float | None]] = []

    def call(self, name, data=None, *, timeout=None):
        self.calls.append((name, threading.get_ident(), timeout))
        answer = self.answers[name]
        if callable(answer):
            answer = answer(data)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def acall(self, name, data=None, *, timeout=None):
        return self.call(name, data, timeout=timeout)

    def subscribe(self, since=None):
        return InProcessSubscription(EventBus().subscribe())

    def close(self):
        pass


def _wire(cid, name, proto="signal"):
    return {"id": cid, "display_name": name, "protocol": proto, "extras": {}}


def _resync_app(answers):
    client = Script(answers)
    app, contact = view_app(client)
    for name in (
        "_render_contact_list",
        "_sort_contacts",
        "_update_unread_badges",
        "_refresh_backend_status_if_idle",
        "_sync_last_ts",
        "_rerender_open_chat",
        "_client_call_background",
    ):
        setattr(app, name, MagicMock())
    return app, contact, client


def _answers(rows):
    return {
        "server.status": {"signal": True, "whatsapp": False, "telegram": False},
        "messages.snapshot": {"+39123": rows},
        "contacts.for_protocol": [_wire("+39123", "Mario"), _wire("+3999", "Luigi")],
        "messages.window": {"messages": rows, "refreshed": False},
        "server.config": {
            "thumbnail_max_lines": 3,
            "thumbnail_max_cols": 4,
            "picker_max_results": 50,
            "picker_preferred_backend": "",
        },
    }


def _row(text, ts, **extra):
    return {"is_mine": False, "text": text, "timestamp": ts, "read": False, **extra}


def test_resync_fetches_everything_with_timeouts():
    app, _contact, client = _resync_app(_answers([_row("a", 1)]))
    assert app._resync_once() is True
    ops = [(name, timeout) for name, _t, timeout in client.calls]
    assert ops == [
        ("server.status", 5.0),
        ("messages.snapshot", 30.0),
        ("contacts.for_protocol", 15.0),
        ("messages.window", 15.0),
        ("server.config", 5.0),
    ]


def test_resync_twice_adds_no_duplicate_contacts_or_messages():
    app, contact, _ = _resync_app(_answers([_row("a", 1), _row("b", 2)]))
    for _ in range(2):
        assert app._resync_once() is True
    assert sorted(c.cache_key for c in app.contacts) == [
        "signal:+39123",
        "signal:+3999",
    ]
    assert app.contacts[0] is contact or contact in app.contacts  # identity kept
    texts = [m["text"] for m in app._cache[contact.cache_key]]
    assert texts == ["a", "b"]


def test_resync_marks_the_open_chat_read():
    app, contact, _ = _resync_app(_answers([_row("a", 1)]))
    app._resync_once()
    assert all(m["read"] for m in app._cache[contact.cache_key])
    app._client_call_background.assert_called_once_with(
        "chats.mark_read", {"protocol": "signal", "contact_id": contact.id}
    )


def test_resync_rerenders_only_when_the_open_chat_changed():
    app, _contact, _ = _resync_app(_answers([_row("a", 1)]))
    app._resync_once()
    app._rerender_open_chat.assert_called_once()
    app._rerender_open_chat.reset_mock()
    app._resync_once()
    app._rerender_open_chat.assert_not_called()


def test_resync_reports_and_aborts_on_a_transport_error():
    answers = _answers([])
    answers["messages.snapshot"] = ServerUnavailable("gone")
    app, _, client = _resync_app(answers)
    assert app._resync_once() is False
    assert [n for n, _t, _o in client.calls] == ["server.status", "messages.snapshot"]
    assert app.contacts[0].display_name == "Mario"  # nothing applied
    assert app._status.call_args.args[0] == MSG_UNAVAILABLE


def test_resync_skips_a_protocol_the_server_refuses():
    answers = _answers([])
    answers["contacts.for_protocol"] = ApiError("backend_down", "x")
    app, _, _ = _resync_app(answers)
    assert app._resync_once() is True


class TestPendingSends:
    def _app(self, rows):
        app, contact, _ = _resync_app(_answers(rows))
        app._cache[contact.cache_key] = [
            {"is_mine": True, "text": "hi", "timestamp": 1000, "status": "pending"}
        ]
        return app, contact

    def test_unknown_to_the_server_becomes_failed(self):
        app, contact = self._app([])
        app._resync_once()
        assert app._cache[contact.cache_key][0]["status"] == "failed"
        app._update_message_widgets_status.assert_any_call(
            [{"timestamp": 1000, "text": "hi", "status": "failed"}]
        )

    def test_known_and_sent_is_upgraded_not_failed(self):
        sent = {
            "is_mine": True,
            "text": "hi",
            "timestamp": 1000,
            "status": "sent",
            "id": "srv",
        }
        app, contact = self._app([sent])
        app._resync_once()
        (msg,) = [m for m in app._cache[contact.cache_key] if m["text"] == "hi"]
        assert msg["status"] == "sent"

    def test_known_and_still_pending_stays_pending(self):
        pending = {
            "is_mine": True,
            "text": "hi",
            "timestamp": 1000,
            "status": "pending",
        }
        app, contact = self._app([pending])
        app._resync_once()
        (msg,) = [m for m in app._cache[contact.cache_key] if m["text"] == "hi"]
        assert msg["status"] == "pending"

    def test_known_and_failed_on_the_server_becomes_failed(self):
        failed = {"is_mine": True, "text": "hi", "timestamp": 1000, "status": "failed"}
        app, contact = self._app([failed])
        app._resync_once()
        (msg,) = [m for m in app._cache[contact.cache_key] if m["text"] == "hi"]
        assert msg["status"] == "failed"


def test_resyncs_coalesce():
    gate = threading.Event()
    started = threading.Event()
    answers = _answers([])

    def status(_data):
        started.set()
        gate.wait(5)
        return {"signal": False}

    answers["server.status"] = status
    app, _, client = _resync_app(answers)
    runner = threading.Thread(target=lambda: app._schedule_resync("a"))
    runner.start()
    assert started.wait(2)
    app._schedule_resync("b")  # while the first runs: remembered, not started
    app._schedule_resync("c")
    gate.set()
    runner.join(5)
    names = [n for n, _t, _o in client.calls]
    assert names.count("server.status") == 2  # one more pass, not three
    assert app._resync_running is False


def test_timeouts_start_at_most_one_resync_per_interval():
    app, _ = view_app(StubClient())
    app._schedule_resync = MagicMock()
    for _ in range(3):
        app._note_unknown_outcome(OperationTimeout("t"))
    app._schedule_resync.assert_called_once_with("timeout")
    app._note_unknown_outcome(ServerUnavailable("x"))  # restored will resync
    app._schedule_resync.assert_called_once()


def test_background_failure_is_reported_not_raised():
    app, _ = view_app(StubClient(ServerUnavailable("gone")))
    inline_client_calls(app)
    app._schedule_resync = MagicMock()
    app._client_call_background("chats.mark_read", {"protocol": "signal"})
    assert app.client.timeouts == [15.0]
    assert app._status.call_args.args[0] == MSG_UNAVAILABLE


# -- the UI thread never waits for the client ---------------------------------------


class SlowClient(ChatClient):
    """Blocks the named operations on a gate; records the calling threads."""

    def __init__(self, blocked, answers):
        self.blocked = set(blocked)
        self.answers = answers
        self.gate = threading.Event()
        self.threads: dict[str, int] = {}
        self.timeouts: dict[str, float | None] = {}
        self.entered = threading.Event()

    def call(self, name, data=None, *, timeout=None):
        self.threads[name] = threading.get_ident()
        self.timeouts[name] = timeout
        if name in self.blocked:
            self.entered.set()
            self.gate.wait(10)
        answer = self.answers.get(name, {"ok": True})
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def acall(self, name, data=None, *, timeout=None):
        return self.call(name, data, timeout=timeout)

    def subscribe(self, since=None):
        return InProcessSubscription(EventBus().subscribe())

    def close(self):
        self.gate.set()


@pytest.fixture
def slow_app():
    """A running headless app (real workers) over a ``SlowClient``."""
    from textual.containers import Vertical

    from tui.app import SignalTUI

    def _mount(self):
        self._chat_log = self.query_one("#chat-log", Vertical)
        self._render_contact_list(list(self.contacts))
        self._start_server_config_fetch()

    with (
        patch("server.core.BackendManager"),
        patch("server.core.SignalBackend"),
        patch("server.core.whatsapp_enabled", return_value=False),
        patch("server.core.telegram_enabled", return_value=False),
        patch.object(SignalTUI, "on_mount", _mount),
    ):
        app = SignalTUI()
        app.contacts = [
            ChatContact(id="+391", display_name="Mario", protocol="signal"),
        ]
        yield app


async def _until(pilot, predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        await pilot.pause(0.02)
    return predicate()


@pytest.mark.integration
async def test_device_link_does_not_block_the_ui(slow_app):
    from device_link_screen import DeviceLinkPickerScreen

    status = {
        p: {"available": True, "account": "", "linked": None}
        for p in ("signal", "whatsapp", "telegram")
    }
    client = SlowClient({"link.status"}, {"link.status": {"protocols": status}})
    slow_app.client = client
    async with slow_app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+l")
        assert await _until(pilot, client.entered.is_set)
        assert client.threads["link.status"] != slow_app._thread_id
        assert client.timeouts["link.status"] == 5.0
        # The UI still handles keys while the server has not answered.
        await pilot.press("ctrl+w")
        assert slow_app._protocol_filter == "signal"
        assert not isinstance(slow_app.screen, DeviceLinkPickerScreen)
        client.gate.set()
        assert await _until(
            pilot, lambda: isinstance(slow_app.screen, DeviceLinkPickerScreen)
        )


@pytest.mark.integration
async def test_opening_an_unknown_contact_does_not_block_the_ui(slow_app):
    wire = {
        "id": "+399",
        "display_name": "New",
        "protocol": "signal",
        "extras": {"ghost": True},
    }
    client = SlowClient({"contacts.register"}, {"contacts.register": wire})
    slow_app.client = client
    async with slow_app.run_test() as pilot:
        await pilot.pause()
        slow_app._select_contact(
            ChatContact(id="+399", display_name="New", protocol="signal")
        )
        assert await _until(pilot, client.entered.is_set)
        assert client.threads["contacts.register"] != slow_app._thread_id
        assert slow_app.selected_contact is None  # not opened yet
        await pilot.press("ctrl+w")
        assert slow_app._protocol_filter == "signal"
        client.gate.set()
        assert await _until(
            pilot,
            lambda: (
                slow_app.selected_contact is not None
                and slow_app.selected_contact.id == "+399"
            ),
        )
        assert [c.id for c in slow_app.contacts].count("+399") == 1


@pytest.mark.integration
async def test_download_does_not_block_the_ui(slow_app):
    client = SlowClient({"download.serve_text"}, {"download.serve_text": {"url": "u"}})
    slow_app.client = client
    async with slow_app.run_test() as pilot:
        await pilot.pause()
        slow_app._download_mode = True
        slow_app._start_download("hello", timestamp=1)
        assert await _until(pilot, client.entered.is_set)
        assert client.threads["download.serve_text"] != slow_app._thread_id
        await pilot.press("ctrl+w")
        assert slow_app._protocol_filter == "signal"
        assert slow_app._download_mode is True  # left when the server answers
        client.gate.set()
        assert await _until(pilot, lambda: slow_app._download_mode is False)


@pytest.mark.integration
async def test_server_config_is_fetched_in_a_worker_at_mount(slow_app):
    client = SlowClient(
        {"server.config"},
        {
            "server.config": {
                "thumbnail_max_lines": 11,
                "thumbnail_max_cols": 22,
                "picker_max_results": 50,
                "picker_preferred_backend": "",
            }
        },
    )
    slow_app.client = client
    async with slow_app.run_test() as pilot:
        await pilot.pause()
        assert await _until(pilot, client.entered.is_set)
        assert client.threads["server.config"] != slow_app._thread_id
        assert slow_app._thumbnail_limits() != (11, 22)  # defaults until answered
        client.gate.set()
        assert await _until(pilot, lambda: slow_app._thumbnail_limits() == (11, 22))
