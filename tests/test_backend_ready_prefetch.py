"""``backend_ready`` data is fetched on the poll worker, merged on the UI thread.

``messages.snapshot`` copies and validates the whole protocol cache; it must
not run on the Textual UI thread.
"""

import threading
from unittest.mock import MagicMock

from server.bus import Event
from tui.app import SignalTUI


def _app() -> SignalTUI:
    app = SignalTUI.__new__(SignalTUI)
    app.client = MagicMock()
    app.client.call.side_effect = lambda name, data=None: (
        {"+39": [{"text": "hi", "timestamp": 1}]}
        if name == "messages.snapshot"
        else [{"id": "+39", "display_name": "Bob", "protocol": "signal"}]
    )
    return app


def test_ready_prefetches_on_worker_and_merges_on_ui_thread() -> None:
    app = _app()
    calls: list[tuple[str, str]] = []
    app.client.call.side_effect = (
        lambda name, data=None, _orig=app.client.call.side_effect: (
            calls.append((name, threading.current_thread().name)) or _orig(name, data)
        )
    )
    merged: list[tuple] = []
    app._on_backend_ready = lambda proto, snapshot, contacts: merged.append(
        (proto, snapshot, contacts)
    )
    ui_calls: list[str] = []

    def call_from_thread(fn, *args):
        ui_calls.append(threading.current_thread().name)
        return fn(*args)

    app.call_from_thread = call_from_thread
    worker = threading.Thread(
        target=app._dispatch_bus_event,
        args=(Event(1, "backend_ready", {"protocol": "signal", "contacts": 1}, 0.0),),
        name="poll-worker",
    )
    worker.start()
    worker.join(2)

    assert [name for name, _ in calls] == ["messages.snapshot", "contacts.for_protocol"]
    assert {thread for _, thread in calls} == {"poll-worker"}
    assert ui_calls == ["poll-worker"]  # handed over via call_from_thread
    proto, snapshot, contacts = merged[0]
    assert proto == "signal"
    assert snapshot == {"+39": [{"text": "hi", "timestamp": 1}]}
    assert [c.id for c in contacts] == ["+39"]


def test_prefetched_merge_does_not_call_the_client_again() -> None:
    app = _app()
    snapshot, contacts = app._prefetch_backend_ready("signal")
    app.client.call.reset_mock()
    app._merge_ready_backend_cache = MagicMock()
    app._merge_backend_contacts = MagicMock()
    for name in (
        "_sync_last_ts",
        "_sort_contacts",
        "_render_contact_list",
        "_update_unread_badges",
        "_refresh_backend_status_if_idle",
        "_mark_backend_done",
        "_status",
    ):
        setattr(app, name, MagicMock())
    app.contacts = []

    app._on_backend_ready("signal", snapshot, contacts)

    app.client.call.assert_not_called()
    app._merge_ready_backend_cache.assert_called_once_with("signal", snapshot)
    app._merge_backend_contacts.assert_called_once_with(contacts)


def test_prefetch_failure_degrades_to_empty() -> None:
    from server.api import ApiError

    app = _app()
    app.client.call.side_effect = ApiError("backend_down", "down")
    snapshot, contacts = app._prefetch_backend_ready("signal")
    assert snapshot == {}
    assert contacts == []
