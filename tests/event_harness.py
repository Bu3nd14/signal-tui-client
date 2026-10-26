"""Shared harness: a minimal view host, and ``feed`` to run raw backend events
through the real ``ChatCore.process``, the bus and the TUI's own bus consumer."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from models import ChatEvent
from server.bus import Event, EventBus
from server.core import ChatCore
from tui.events import EventHandlingMixin
from tui.polling import PollingMixin
from web.legacy_events import to_legacy


class EventApp(EventHandlingMixin, PollingMixin):
    """Exposes only the attributes the view handlers touch."""

    def __init__(self, backend: Any, contacts=None, **attrs: Any) -> None:
        self.bus = EventBus()
        self.core = ChatCore(self.bus)
        self.manager = SimpleNamespace(get=lambda _protocol: backend)
        self.core.manager = self.manager
        self.contacts = list(contacts or [])
        self.selected_contact = None
        self._contact_list_dirty = False
        self._dirty_contact_keys: set[str] = set()
        self._cache: dict[str, list[dict]] = {}
        self._typing_contacts: dict[str, float] = {}
        self._typing_mumbling: dict[str, float] = {}
        self._seen_message_ids: set[tuple] = set()
        self._seen_timestamps: set[tuple] = set()
        self._TYPING_MUMBLING_DURATION = 5.0
        self._web_enabled = False
        self.calls: list[tuple] = []
        for key, value in attrs.items():
            setattr(self, key, value)

    def call_from_thread(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))


def legacy_frames(events: list[Event]) -> list[dict]:
    """The ``/ws`` frames the web would send for *events*."""
    return [frame for frame in map(to_legacy, events) if frame is not None]


def feed(app: Any, raw: ChatEvent) -> bool:
    """Deliver a raw backend event the way production does, synchronously.

    ``app.core.process(raw)`` -> each result published on ``app.bus`` -> the
    events are read back from a subscription on that bus and handed to the
    app's real consumer step (``_route_bus_event``, the one the poll worker
    uses).  Exceptions from the view handlers propagate.  Returns ``True``
    when at least one event was handled.
    """
    results = app.core.process(raw)
    with app.bus.subscribe() as sub:
        published = [app.bus.publish(etype, payload) for etype, payload in results]
        received = sub.drain()
    assert [e.seq for e in received] == [e.seq for e in published]
    handled = False
    for event in received:
        if app._route_bus_event(event):
            handled = True
    return handled
