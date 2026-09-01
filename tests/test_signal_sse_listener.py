from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from protocols import SignalBackend


def _running_backend() -> SignalBackend:
    backend = SignalBackend(user_number="+391234567890")
    backend._polling_active = True
    backend._sse_thread = MagicMock()
    return backend


def _stop_after_backoff(backend: SignalBackend) -> None:
    backend._polling_active = False


def test_empty_stream_logs_connection_lost_no_unbound_error(caplog):
    backend = _running_backend()
    backend._rpc.listen_events = MagicMock(return_value=iter(()))

    with (
        caplog.at_level(logging.INFO, logger="protocols.signal"),
        patch(
            "protocols.signal.time.sleep",
            side_effect=lambda _seconds: _stop_after_backoff(backend),
        ),
    ):
        backend._sse_listener()

    assert "connection lost" in caplog.text
    assert "cannot access local variable" not in caplog.text


def test_envelopes_processed_and_logged(caplog):
    backend = _running_backend()
    event = MagicMock()
    backend._rpc.listen_events = MagicMock(
        return_value=iter([{"envelope": {"source": "+390000000000"}}])
    )
    backend.envelope_to_event = MagicMock(return_value=[event])

    with (
        caplog.at_level(logging.INFO, logger="protocols.signal"),
        patch(
            "protocols.signal.time.sleep",
            side_effect=lambda _seconds: _stop_after_backoff(backend),
        ),
    ):
        backend._sse_listener()

    assert backend._event_queue.get_nowait() is event
    assert "received 1 events" in caplog.text
    assert "connection lost" not in caplog.text


def test_internal_error_logged_as_error(caplog):
    backend = _running_backend()
    backend._rpc.listen_events = MagicMock(return_value=iter([{"envelope": {}}]))
    backend.envelope_to_event = MagicMock(side_effect=RuntimeError("broken parser"))

    with (
        caplog.at_level(logging.ERROR, logger="protocols.signal"),
        patch(
            "protocols.signal.time.sleep",
            side_effect=lambda _seconds: _stop_after_backoff(backend),
        ),
    ):
        backend._sse_listener()

    assert any(
        record.levelno == logging.ERROR
        and "unexpected listener error" in record.getMessage()
        and record.exc_info is not None
        for record in caplog.records
    )
    assert "connection lost" not in caplog.text
