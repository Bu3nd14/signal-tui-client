"""Regression tests: strumentazione timing di ``send_message_sync`` (PR #29).

Copre il blocco di misurazione della durata in ``_send_message_worker``:
warning oltre 1s, debug sotto 1s.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from models import PROTOCOL_WHATSAPP
from tui.send import SendMixin


def _handler(backend) -> SimpleNamespace:
    handler = SimpleNamespace()
    handler.manager = SimpleNamespace(get=MagicMock(return_value=backend))
    handler._transition_outgoing_status = MagicMock(return_value=True)
    handler._update_outgoing_message_id = MagicMock()
    handler.call_from_thread = MagicMock()
    handler._status = MagicMock()
    return handler


def _backend(send_result: str | None = "msg123", delay: float = 0.0):
    backend = MagicMock()
    backend.ingest_message = MagicMock(return_value=True)

    def _delayed_send(*_args, **_kwargs):
        if delay:
            time.sleep(delay)
        return send_result

    backend.send_message_sync = MagicMock(side_effect=_delayed_send)
    return backend


class TestSendTimingInstrumentation:
    def test_slow_send_logs_warning(self, caplog):
        backend = _backend(delay=1.05)
        handler = _handler(backend)
        with caplog.at_level(logging.WARNING, logger="tui.send"):
            SendMixin._send_message_worker(
                handler,
                "ciao",
                1000,
                None,
                None,
                protocol=PROTOCOL_WHATSAPP,
                contact_id="123@g.us",
            )
        assert any("send_message_sync slow" in rec.message for rec in caplog.records)

    def test_fast_send_logs_debug(self, caplog):
        backend = _backend()
        handler = _handler(backend)
        with caplog.at_level(logging.DEBUG, logger="tui.send"):
            SendMixin._send_message_worker(
                handler,
                "ciao",
                1000,
                None,
                None,
                protocol=PROTOCOL_WHATSAPP,
                contact_id="123@g.us",
            )
        assert any("send_message_sync took" in rec.message for rec in caplog.records)

    def test_transition_called_after_send(self):
        backend = _backend()
        handler = _handler(backend)
        SendMixin._send_message_worker(
            handler,
            "ciao",
            1000,
            None,
            None,
            protocol=PROTOCOL_WHATSAPP,
            contact_id="123@g.us",
        )
        handler._transition_outgoing_status.assert_called_once_with(
            PROTOCOL_WHATSAPP,
            "123@g.us",
            1000,
            "ciao",
            "sent",
            ("pending",),
        )
        backend.ingest_message.assert_called_once()
