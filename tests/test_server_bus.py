"""Tests for server.bus.EventBus."""

from __future__ import annotations

import threading
import time

from server.bus import GAP, EventBus


def test_seq_monotonic_and_defaults() -> None:
    bus = EventBus()
    assert bus.last_seq == 0
    a = bus.publish("a")
    b = bus.publish("b", {"x": 1})
    assert (a.seq, b.seq) == (1, 2)
    assert a.payload == {}
    assert b.payload == {"x": 1}
    assert bus.last_seq == 2
    assert a.ts <= b.ts


def test_fanout_and_no_history_without_since() -> None:
    bus = EventBus()
    bus.publish("old")
    s1, s2 = bus.subscribe(), bus.subscribe()
    bus.publish("new")
    assert [e.type for e in s1.drain()] == ["new"]
    assert [e.type for e in s2.drain()] == ["new"]


def test_since_replay() -> None:
    bus = EventBus()
    for i in range(5):
        bus.publish("e", {"i": i})
    sub = bus.subscribe(since=2)
    assert [e.seq for e in sub.drain()] == [3, 4, 5]
    bus.publish("live")
    assert [e.seq for e in sub.drain()] == [6]
    assert bus.subscribe(since=6).drain() == []
    assert bus.subscribe(since=99).drain() == []


def test_replay_racing_publishers() -> None:
    bus = EventBus(buffer_size=5000, subscriber_queue_size=5000)
    n_threads, per = 4, 500
    start = threading.Barrier(n_threads + 1)

    def producer() -> None:
        start.wait()
        for _ in range(per):
            bus.publish("e")

    threads = [threading.Thread(target=producer) for _ in range(n_threads)]
    for t in threads:
        t.start()
    start.wait()
    subs = [bus.subscribe(since=0) for _ in range(5)]
    for t in threads:
        t.join()
    total = n_threads * per
    for sub in subs:
        seqs = [e.seq for e in sub.drain()]
        assert seqs == list(range(1, total + 1))


def test_buffer_eviction_gap() -> None:
    bus = EventBus(buffer_size=3)
    for _ in range(10):
        bus.publish("e")
    sub = bus.subscribe(since=2)
    events = sub.drain()
    assert events[0].type == GAP
    assert events[0].seq == 10
    assert events[0].payload == {"reason": "buffer", "since": 2}
    assert [e.seq for e in events[1:]] == [8, 9, 10]
    # since exactly at oldest-1: no gap
    events = bus.subscribe(since=7).drain()
    assert [e.seq for e in events] == [8, 9, 10]


def test_overflow_gap_then_recovery() -> None:
    bus = EventBus(buffer_size=3, subscriber_queue_size=3)
    sub = bus.subscribe()
    for _ in range(3):
        bus.publish("e")
    bus.publish("e")  # seq 4 overflows
    bus.publish("e")  # seq 5 after the gap
    events = sub.drain()
    assert [e.type for e in events] == [GAP, "e"]
    assert events[0].payload == {"reason": "overflow"}
    assert events[0].seq == 4
    assert events[1].seq == 5
    bus.publish("e")
    assert sub.get(timeout=1).seq == 6


def test_close_unblocks_get() -> None:
    bus = EventBus()
    sub = bus.subscribe()
    result: list[object] = ["unset"]

    def waiter() -> None:
        result[0] = sub.get(timeout=5)

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.05)
    sub.close()
    t.join(1)
    assert not t.is_alive()
    assert result[0] is None
    assert sub.closed
    sub.close()  # idempotent
    bus.publish("e")
    assert sub.get(timeout=0.01) is None


def test_get_timeout_and_context_manager() -> None:
    bus = EventBus()
    with bus.subscribe() as sub:
        assert sub.get(timeout=0.01) is None
        bus.publish("e")
        assert sub.get(timeout=1).type == "e"
    assert sub.closed


def test_bus_close_closes_subscriptions() -> None:
    bus = EventBus()
    sub = bus.subscribe()
    bus.close()
    assert sub.closed
    assert bus.subscribe().closed


def test_drain_order() -> None:
    bus = EventBus()
    sub = bus.subscribe()
    for i in range(4):
        bus.publish("e", {"i": i})
    assert [e.payload["i"] for e in sub.drain()] == [0, 1, 2, 3]
    assert sub.drain() == []


async def test_aget() -> None:
    bus = EventBus()
    sub = bus.subscribe()
    assert await sub.aget(timeout=0.01) is None
    threading.Timer(0.02, lambda: bus.publish("later")).start()
    event = await sub.aget(timeout=1)
    assert event is not None and event.type == "later"


def test_publish_never_blocks_with_stuck_subscriber() -> None:
    bus = EventBus(buffer_size=10, subscriber_queue_size=10)
    stuck = bus.subscribe()
    start = time.monotonic()
    for _ in range(5000):
        bus.publish("e")
    assert time.monotonic() - start < 1.0
    assert stuck.drain()[0].type == GAP or stuck.drain() == []
