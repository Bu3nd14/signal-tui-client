"""Regression tests: strumentazione timing del send (PR #29).

La misura della durata del send backend vive nell'operazione ``messages.send``
(il TUI la chiama dal worker): warning oltre 1s, debug sotto 1s.  La transizione
pending→sent avviene nel core dopo il send.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from client_harness import client_for

from models import PROTOCOL_WHATSAPP


def _backend(send_result: str | None = "msg123", delay: float = 0.0):
    backend = MagicMock()
    backend.ingest_message = MagicMock(return_value=True)
    backend.contacts = []

    def _delayed_send(*_args, **_kwargs):
        if delay:
            time.sleep(delay)
        return send_result

    backend.send_message_sync = MagicMock(side_effect=_delayed_send)
    return backend


def _send(backend):
    manager = MagicMock()
    manager.get.return_value = backend
    with (
        patch("server.ops.send.transition_outgoing_status", return_value=True) as t,
        patch.object(backend, "_persist_message", create=True),
    ):
        client_for(manager).call(
            "messages.send",
            {
                "protocol": PROTOCOL_WHATSAPP,
                "contact_id": "123@g.us",
                "text": "ciao",
                "retry": False,
                "optimistic": {"timestamp": 1000},
            },
        )
    return t


class TestSendTimingInstrumentation:
    def test_slow_send_logs_warning(self, caplog):
        backend = _backend(delay=1.05)
        with caplog.at_level(logging.WARNING, logger="server.ops.send"):
            _send(backend)
        assert any("send_message_sync slow" in rec.message for rec in caplog.records)

    def test_fast_send_logs_debug(self, caplog):
        backend = _backend()
        with caplog.at_level(logging.DEBUG, logger="server.ops.send"):
            _send(backend)
        assert any("send_message_sync took" in rec.message for rec in caplog.records)

    def test_transition_called_after_send(self):
        backend = _backend()
        transition = _send(backend)
        transition.assert_called_once_with(
            backend,
            PROTOCOL_WHATSAPP,
            "123@g.us",
            1000,
            "ciao",
            "sent",
            ("pending",),
        )
        # seed ottimistico + attach dell'id reale
        assert backend.ingest_message.call_count == 2
