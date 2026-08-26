"""Non-blocking bridge from backend ingestion to the web event loop."""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any

logger = logging.getLogger(__name__)

_QUEUE_SIZE = 1000
_push_queue: queue.Queue[dict[str, Any]] | None = None
_dropped_events = 0
_lock = threading.Lock()


def init_bridge() -> queue.Queue[dict[str, Any]]:
    """Activate a fresh bounded queue for one web-server lifecycle."""
    global _dropped_events, _push_queue
    with _lock:
        _push_queue = queue.Queue(maxsize=_QUEUE_SIZE)
        _dropped_events = 0
        return _push_queue


def close_bridge() -> None:
    """Deactivate event forwarding."""
    global _push_queue
    with _lock:
        _push_queue = None


def get_push_queue() -> queue.Queue[dict[str, Any]] | None:
    """Return the active push queue, if the web server is enabled."""
    return _push_queue


def push_event(event: dict[str, Any]) -> bool:
    """Enqueue an event without ever blocking its producer."""
    global _dropped_events
    push_queue = _push_queue
    if push_queue is None:
        return False
    try:
        push_queue.put_nowait(event)
        return True
    except queue.Full:
        with _lock:
            _dropped_events += 1
            dropped = _dropped_events
        logger.warning("Web push queue full; dropped event (total=%d)", dropped)
        return False


def dropped_events() -> int:
    """Return the number of events dropped in the current lifecycle."""
    return _dropped_events
