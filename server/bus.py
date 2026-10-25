"""
In-process event bus for the headless server core.

Provides:
- ``Event`` — immutable envelope ``{seq, type, payload, ts}``.
- ``EventBus`` — thread-safe, multi-subscriber bus with a bounded ring buffer
  of recent events.  Producers never block.
- ``Subscription`` — a consumer handle with a bounded queue, blocking
  ``get``, non-blocking ``drain`` and an asyncio-friendly ``aget``.
- ``GAP`` — synthetic event type telling a subscriber it lost events and must
  resync (reason ``"buffer"``: ``since`` fell out of the ring buffer;
  reason ``"overflow"``: the subscriber's queue overflowed).

Payloads are not copied by the bus: producers must not mutate a payload after
publishing it and consumers must treat payloads as immutable.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

logger = logging.getLogger(__name__)

GAP = "gap"


@dataclass(frozen=True)
class Event:
    """One bus event.  ``seq`` is monotonic per bus and starts at 1."""

    seq: int
    type: str
    payload: dict[str, Any]
    ts: float


class Subscription:
    """A consumer of bus events.  Created via :meth:`EventBus.subscribe`."""

    def __init__(self, bus: EventBus, queue_size: int) -> None:
        self._bus = bus
        self._maxsize = max(1, queue_size)
        self._queue: deque[Event] = deque()
        self._cond = threading.Condition()
        self._closed = False
        self._overflowed = False

    # -- producer side (called by the bus, never blocks) -------------------

    def _push(self, event: Event) -> None:
        with self._cond:
            if self._closed:
                return
            if len(self._queue) >= self._maxsize:
                if not self._overflowed:
                    self._overflowed = True
                    logger.warning(
                        "Event subscriber queue overflow (size %d); dropping "
                        "queued events, subscriber must resync",
                        self._maxsize,
                    )
                self._queue.clear()
                self._queue.append(
                    Event(event.seq, GAP, {"reason": "overflow"}, event.ts)
                )
            else:
                self._queue.append(event)
            self._cond.notify()

    # -- consumer side -----------------------------------------------------

    def get(self, timeout: float | None = None) -> Event | None:
        """Block up to ``timeout`` for the next event.

        Returns ``None`` on timeout or when the subscription is closed.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while not self._queue and not self._closed:
                if deadline is None:
                    self._cond.wait()
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._cond.wait(remaining)
            if self._closed:
                return None
            event = self._queue.popleft()
            if not self._queue:
                self._overflowed = False
            return event

    def drain(self) -> list[Event]:
        """Return all queued events in order without blocking."""
        with self._cond:
            if self._closed:
                return []
            events = list(self._queue)
            self._queue.clear()
            self._overflowed = False
            return events

    async def aget(self, timeout: float | None = None) -> Event | None:
        """Asyncio wrapper around :meth:`get` (runs it in a worker thread)."""
        return await asyncio.to_thread(self.get, timeout)

    def close(self) -> None:
        """Unregister from the bus and wake blocked getters.  Idempotent."""
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._queue.clear()
            self._cond.notify_all()
        self._bus._unregister(self)

    @property
    def closed(self) -> bool:
        with self._cond:
            return self._closed

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class EventBus:
    """Thread-safe multi-subscriber event bus with a replay ring buffer."""

    def __init__(
        self, buffer_size: int = 2000, subscriber_queue_size: int = 1000
    ) -> None:
        self._lock = threading.Lock()
        self._buffer: deque[Event] = deque(maxlen=max(1, buffer_size))
        # A full-buffer replay must fit in a fresh subscriber's queue, otherwise
        # subscribe(since=...) would immediately overflow into a GAP.
        self._queue_size = max(subscriber_queue_size, self._buffer.maxlen or 1)
        self._subs: list[Subscription] = []
        self._seq = 0
        self._closed = False

    def publish(self, type: str, payload: dict[str, Any] | None = None) -> Event:
        """Publish an event to all subscribers.  Never blocks on consumers."""
        with self._lock:
            self._seq += 1
            event = Event(
                self._seq, type, {} if payload is None else payload, time.time()
            )
            self._buffer.append(event)
            subs = list(self._subs)
            # Fan out under the lock so that ordering matches seq and a
            # concurrent subscribe(since=...) cannot miss or duplicate it.
            for sub in subs:
                sub._push(event)
        return event

    def subscribe(self, since: int | None = None) -> Subscription:
        """Register a subscriber, optionally replaying events with seq > since."""
        sub = Subscription(self, self._queue_size)
        with self._lock:
            if self._closed:
                sub._closed = True
                return sub
            if since is not None:
                last = self._seq
                since = min(since, last)
                if self._buffer:
                    oldest = self._buffer[0].seq
                    if since < oldest - 1:
                        sub._push(
                            Event(
                                last,
                                GAP,
                                {"reason": "buffer", "since": since},
                                time.time(),
                            )
                        )
                    for event in self._buffer:
                        if event.seq > since:
                            sub._push(event)
            self._subs.append(sub)
        return sub

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def close(self) -> None:
        """Close all subscriptions; later subscribes return closed ones."""
        with self._lock:
            self._closed = True
            subs = list(self._subs)
        for sub in subs:
            sub.close()

    def _unregister(self, sub: Subscription) -> None:
        with self._lock:
            try:
                self._subs.remove(sub)
            except ValueError:
                pass
