from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from backends.manager import BackendManager
from backends.signal import SignalBackend
from backends.telegram import TelegramBackend


def _backend(protocol: str):
    if protocol == "signal":
        backend = SignalBackend()
        backend.send_message_sync = MagicMock(return_value=str(int(time.time() * 1000)))
    else:
        backend = TelegramBackend()
        backend.send_message_sync = MagicMock(return_value="77")
    return backend


@pytest.mark.parametrize("protocol", ["signal", "telegram"])
def test_facade_send_enqueues_outgoing_event_that_is_ingested(protocol):
    backend = _backend(protocol)
    manager = BackendManager()
    manager.register(backend)

    result = manager.send_message_sync(protocol, "42", "from web")
    event = backend.poll_once()[0]
    added = backend.ingest_message(
        event.contact_id, event.payload, event.payload["timestamp"], persist=False
    )

    assert result == backend.send_message_sync.return_value
    assert added is True
    assert backend.cache["42"][0]["text"] == "from web"
    assert backend.cache["42"][0]["is_mine"] is True


@pytest.mark.parametrize("protocol", ["signal", "telegram"])
def test_facade_send_echo_upgrades_optimistic_without_duplicate(protocol):
    backend = _backend(protocol)
    manager = BackendManager()
    manager.register(backend)
    optimistic_ts = int(time.time() * 1000)
    optimistic = {
        "text": "from tui",
        "is_mine": True,
        "sender": "You",
        "quote_text": None,
        "msg_type": "text",
        "attachment_info": None,
        "status": "pending",
    }
    assert backend.ingest_message("42", optimistic, optimistic_ts, persist=False)

    manager.send_message_sync(protocol, "42", "from tui")
    event = backend.poll_once()[0]
    added = backend.ingest_message(
        event.contact_id, event.payload, event.payload["timestamp"], persist=False
    )

    assert added is False
    assert len(backend.cache["42"]) == 1
    assert backend.cache["42"][0]["id"] == event.payload["id"]
