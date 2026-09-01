"""Unit tests for WAHA webhook handling without binding a real socket."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from protocols import webhook


def _handler(path: str, body: bytes, target=None):
    handler = webhook._WebhookHTTPHandler.__new__(webhook._WebhookHTTPHandler)
    handler.path = path
    handler.rfile = io.BytesIO(body)
    handler.headers = {"Content-Length": str(len(body))}
    handler.target = target
    handler._wh_response = MagicMock()
    return handler


class TestWebhookPost:
    def test_wrong_path_returns_404(self):
        handler = _handler("/wrong", b"{}")
        handler.do_POST()
        handler._wh_response.assert_called_once_with(404)

    def test_valid_json_forwards_to_target_and_acks(self):
        target = MagicMock()
        payload = {"event": "message"}
        handler = _handler("/webhook", json.dumps(payload).encode(), target)

        handler.do_POST()

        target.assert_called_once_with(payload)
        handler._wh_response.assert_called_once_with(200)

    def test_invalid_json_returns_400(self):
        handler = _handler("/webhook", b"not json")
        handler.do_POST()
        handler._wh_response.assert_called_once_with(400)

    def test_body_read_failure_returns_400(self):
        handler = _handler("/webhook", b"{}")
        handler.rfile.read = MagicMock(side_effect=OSError("bad stream"))
        handler.do_POST()
        handler._wh_response.assert_called_once_with(400)

    def test_target_failure_still_acks(self):
        target = MagicMock(side_effect=RuntimeError("boom"))
        handler = _handler("/webhook", b"{}", target)
        handler.do_POST()
        handler._wh_response.assert_called_once_with(200)

    def test_no_target_or_non_dict_data_still_acks(self):
        handler = _handler("/webhook", b"[]")
        handler.do_POST()
        handler._wh_response.assert_called_once_with(200)


class TestWebhookResponse:
    def test_writes_json_response(self):
        handler = webhook._WebhookHTTPHandler.__new__(webhook._WebhookHTTPHandler)
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.wfile = MagicMock()

        handler._wh_response(200)

        handler.send_response.assert_called_once_with(200)
        handler.send_header.assert_any_call("Content-Type", "application/json")
        handler.send_header.assert_any_call("Content-Length", "12")
        handler.end_headers.assert_called_once()
        handler.wfile.write.assert_called_once_with(b'{"ok": true}')
        handler.wfile.flush.assert_called_once()

    def test_response_write_failure_is_ignored(self):
        handler = webhook._WebhookHTTPHandler.__new__(webhook._WebhookHTTPHandler)
        handler.send_response = MagicMock(side_effect=OSError("closed"))
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.wfile = MagicMock()

        handler._wh_response(200)

        handler.send_response.assert_called_once_with(200)


class TestWebhookServer:
    def test_existing_server_rebinds_target(self, monkeypatch):
        backend = SimpleNamespace(handle_webhook=MagicMock())
        monkeypatch.setattr(webhook, "_WEBHOOK_SERVER", object())
        monkeypatch.setattr(webhook._WebhookHTTPHandler, "target", None)

        assert webhook.ensure_webhook_server(backend) == webhook.WEBHOOK_PORT
        assert webhook._WebhookHTTPHandler.target is backend.handle_webhook

    def test_starts_mocked_server(self, monkeypatch):
        server = MagicMock()
        thread = MagicMock()
        monkeypatch.setattr(webhook, "_WEBHOOK_SERVER", None)
        monkeypatch.setattr(
            webhook.socketserver, "TCPServer", MagicMock(return_value=server)
        )
        monkeypatch.setattr(webhook.threading, "Thread", MagicMock(return_value=thread))

        assert webhook.ensure_webhook_server(None) == webhook.WEBHOOK_PORT
        webhook.socketserver.TCPServer.assert_called_once_with(
            ("0.0.0.0", webhook.WEBHOOK_PORT), webhook._WebhookHTTPHandler
        )
        thread.start.assert_called_once()

    def test_bind_error_returns_zero(self, monkeypatch):
        monkeypatch.setattr(webhook, "_WEBHOOK_SERVER", None)
        monkeypatch.setattr(
            webhook.socketserver, "TCPServer", MagicMock(side_effect=OSError)
        )

        assert webhook.ensure_webhook_server(None) == 0
