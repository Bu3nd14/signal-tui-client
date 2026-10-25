"""TUI poll worker as the single bus consumer + test-only ``_handle_event``."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from models import PROTOCOL_SIGNAL, ChatContact, ChatEvent
from server.bus import GAP
from signal_tui import SignalTUI

CID = "+391234567890"


def _app() -> SignalTUI:
    app = SignalTUI()
    app.call_from_thread = MagicMock()
    app._core_sub = app.client.subscribe()
    app._polling_active = True
    return app


def _run_worker(app: SignalTUI) -> threading.Thread:
    thread = threading.Thread(target=app._poll_worker, daemon=True)
    thread.start()
    return thread


def _stop(app: SignalTUI, thread: threading.Thread) -> None:
    app._polling_active = False
    thread.join(3)
    assert not thread.is_alive()


def _wait(predicate, timeout=3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_worker_applies_domain_events_with_view_handlers():
    app = _app()
    key = f"{PROTOCOL_SIGNAL}:{CID}"
    thread = _run_worker(app)
    try:
        app.bus.publish(
            "typing",
            {"protocol": PROTOCOL_SIGNAL, "contact_id": CID, "action": "STARTED"},
        )
        assert _wait(lambda: key in app._typing_contacts)
    finally:
        _stop(app, thread)
    app.call_from_thread.assert_any_call(app._update_typing_label, key)


def test_backend_events_go_to_ui_thread_dispatch():
    app = _app()
    thread = _run_worker(app)
    try:
        event = app.bus.publish("backend_done", {"protocol": "x", "ok": False})
        assert _wait(
            lambda: (
                (app._dispatch_core_event, event)
                in [c.args for c in app.call_from_thread.call_args_list]
            )
        )
    finally:
        _stop(app, thread)


def test_gap_marks_list_dirty_and_triggers_full_flush():
    app = _app()
    app._contact_list_dirty = False
    thread = _run_worker(app)
    try:
        # Force a GAP into the subscription the worker reads.
        app._core_sub._sub._push(
            SimpleNamespace(seq=1, type=GAP, payload={"reason": "overflow"}, ts=0.0)
        )
        assert _wait(
            lambda: any(
                c.args and c.args[0] == app._recompute_unread
                for c in app.call_from_thread.call_args_list
            )
        )
    finally:
        _stop(app, thread)
    # full recompute (no key argument) + one reorder
    calls = [c.args for c in app.call_from_thread.call_args_list]
    assert (app._recompute_unread,) in calls
    assert (app._reorder_contact_list,) in calls
    assert app._contact_list_dirty is False


def test_handle_event_publishes_on_bus_without_double_dispatch():
    app = _app()
    sub = app.bus.subscribe()
    thread = _run_worker(app)
    try:
        event = ChatEvent(
            type="typing",
            protocol=PROTOCOL_SIGNAL,
            contact_id=CID,
            payload={"action": "STARTED"},
        )
        app._view_typing = MagicMock(wraps=app._view_typing)
        assert app._handle_event(event) is True
        # Give the worker time to see (and skip) the published event.
        assert _wait(lambda: not app._compat_payload_ids)
        time.sleep(0.3)
    finally:
        _stop(app, thread)
    assert app._view_typing.call_count == 1
    published = sub.drain()
    assert [e.type for e in published] == ["typing"]
    assert published[0].payload["action"] == "STARTED"


def test_handle_event_returns_false_when_core_produces_nothing():
    app = _app()
    assert app._handle_event(ChatEvent("mystery", PROTOCOL_SIGNAL, CID)) is False
    assert (
        app._handle_event(
            ChatEvent("typing", PROTOCOL_SIGNAL, CID, payload={"action": "nope"})
        )
        is False
    )


def test_pump_to_view_end_to_end():
    """Raw backend event -> core pump -> bus -> TUI worker -> UI cache."""
    app = _app()
    contact = ChatContact(id=CID, display_name="Ada", protocol=PROTOCOL_SIGNAL)
    app.contacts = [contact]
    batches = [
        [
            ChatEvent(
                type="message",
                protocol=PROTOCOL_SIGNAL,
                contact_id=CID,
                payload={
                    "id": "m1",
                    "text": "ciao",
                    "is_mine": False,
                    "timestamp": 1234,
                    "contact": contact,
                },
            )
        ]
    ]
    backend = SimpleNamespace(
        protocol=PROTOCOL_SIGNAL,
        contacts=[contact],
        poll_once=lambda: batches.pop(0) if batches else [],
        ingest_message=MagicMock(return_value=True),
    )
    app.core.manager = SimpleNamespace(get=lambda _p: backend, all=lambda: [backend])
    thread = _run_worker(app)
    app.core._start_pump()
    try:
        assert _wait(lambda: bool(app._cache.get(contact.cache_key)))
    finally:
        app.core.stop()
        _stop(app, thread)
    assert app._cache[contact.cache_key][0]["text"] == "ciao"
    assert contact.last_message_ts == 1234
    backend.ingest_message.assert_called_once()
