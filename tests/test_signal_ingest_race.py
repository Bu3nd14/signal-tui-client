from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

import backend as backend_mod
from backend import _add_message_to_cache
from backends import SignalBackend

CONTACT = "+391234567890"
TIMESTAMP = 1_787_000_000_000


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    db_file = tmp_path / "messages.db"
    with (
        patch.object(backend_mod, "DB_FILE", db_file),
        patch.object(backend_mod, "CACHE_DIR", tmp_path),
    ):
        yield db_file


def _data() -> dict:
    return {
        "text": "Ciao Mikeli",
        "is_mine": False,
        "sender": "Mikeli",
        "quote_text": None,
        "msg_type": "text",
        "attachment_info": None,
        "attachment_id": None,
    }


def _row_count(db_file: Path) -> int:
    with sqlite3.connect(db_file) as conn:
        return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


def test_ingest_dedup_survives_cache_swap(tmp_db: Path):
    backend = SignalBackend()
    assert backend.ingest_message(CONTACT, _data(), TIMESTAMP) is True

    backend.cache = {}

    assert backend.ingest_message(CONTACT, _data(), TIMESTAMP) is False
    assert len(backend.cache[CONTACT]) == 1
    assert _row_count(tmp_db) == 1


def test_add_message_to_cache_idempotent(tmp_db: Path):
    first = _add_message_to_cache(CONTACT, "Ciao Mikeli", False, "Mikeli", TIMESTAMP)
    second = _add_message_to_cache(CONTACT, "Ciao Mikeli", False, "Mikeli", TIMESTAMP)

    assert first is None
    assert second == 1
    assert _row_count(tmp_db) == 1


def test_ingest_concurrent_check_then_act(tmp_db: Path):
    backend = SignalBackend()
    barrier = threading.Barrier(3)
    results: list[bool | str] = []

    def ingest() -> None:
        barrier.wait()
        results.append(backend.ingest_message(CONTACT, _data(), TIMESTAMP))

    threads = [threading.Thread(target=ingest) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(results) == [False, True]
    assert len(backend.cache[CONTACT]) == 1
    assert _row_count(tmp_db) == 1


def test_connect_merge_preserves_inflight():
    backend = SignalBackend(user_number=CONTACT)
    inflight = {**_data(), "timestamp": TIMESTAMP}
    backend.cache = {CONTACT: [inflight]}

    with (
        patch.object(backend, "_load_protocol_cache", return_value={}),
        patch("backends.signal._is_daemon_running", return_value=True),
        patch.object(backend, "_load_contacts_rpc"),
        patch.object(backend, "_start_sse_listener"),
        patch.object(backend._rpc, "_call", return_value={"result": {}}),
    ):
        backend._connect_sync()

    assert backend.cache == {CONTACT: [inflight]}


def test_signal_quote_timestamp_extraction():
    """Il timestamp del messaggio quotato è esposto da id/targetSentTimestamp."""
    from backends.signal import _signal_quote_timestamp

    assert _signal_quote_timestamp({"id": "1787948503599"}) == 1787948503599
    assert (
        _signal_quote_timestamp({"targetSentTimestamp": 1787948503599})
        == 1787948503599
    )
    assert _signal_quote_timestamp({"targetSentTimestamp": "1787948503599"}) == (
        1787948503599
    )
    assert _signal_quote_timestamp(None) is None
    assert _signal_quote_timestamp({}) is None
    assert _signal_quote_timestamp({"id": "not-a-number"}) is None
