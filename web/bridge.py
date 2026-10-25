"""Bridge from legacy ``push_event(dict)`` call sites to the core event bus.

``web/api.py`` route handlers still build the legacy WebSocket frames
themselves; they are published on the active bus as ``web_push`` events and
``web.legacy_events.to_legacy`` maps them back to the unchanged dict.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from server.bus import EventBus

logger = logging.getLogger(__name__)

_bus: EventBus | None = None
_lock = threading.Lock()


def init_bridge(bus: EventBus | None = None) -> EventBus:
    """Activate forwarding on *bus* (a private bus when omitted)."""
    global _bus
    with _lock:
        _bus = bus if bus is not None else EventBus()
        return _bus


def close_bridge() -> None:
    """Deactivate event forwarding."""
    global _bus
    with _lock:
        _bus = None


def get_bus() -> EventBus | None:
    """Return the active bus, if the web server is enabled."""
    return _bus


def push_event(event: dict[str, Any]) -> bool:
    """Publish a legacy frame on the active bus; never blocks the producer."""
    bus = _bus
    if bus is None:
        return False
    bus.publish("web_push", {"event": event})
    return True
