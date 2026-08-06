"""
Regression tests for backend.py — SignalRPCClient and daemon detection.
"""

from __future__ import annotations

import pytest
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend import SignalRPCClient, _is_daemon_running, SSE_URL


class TestSignalRPCClient:
    """🔌 Chiamate RPC di base."""

    def test_call_success(self):
        """Chiamata RPC con successo → restituisce risultato."""
        client = SignalRPCClient("http://localhost:9999")

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = b'{"jsonrpc":"2.0","id":1,"result":["ok"]}'
            mock_urlopen.return_value.__enter__.return_value = mock_response

            result = client._call("listContacts")
        assert "result" in result
        assert result["result"] == ["ok"]

    def test_call_error(self):
        """Chiamata RPC con errore di connessione → dict con 'error'."""
        client = SignalRPCClient("http://localhost:9999")

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = Exception("Connection refused")

            result = client._call("listContacts")
        assert "error" in result
        assert "Connection refused" in result["error"]

    def test_list_contacts_success(self):
        """list_contacts con successo → lista."""
        client = SignalRPCClient("http://localhost:9999")

        with patch.object(client, "_call") as mock_call:
            mock_call.return_value = {"result": [{"number": "+39"}]}
            contacts = client.list_contacts()
        assert contacts == [{"number": "+39"}]

    def test_list_contacts_error(self):
        """list_contacts con errore → lista vuota."""
        client = SignalRPCClient("http://localhost:9999")

        with patch.object(client, "_call") as mock_call:
            mock_call.return_value = {"error": "timeout"}
            contacts = client.list_contacts()
        assert contacts == []

    def test_receive_success(self):
        """receive con successo → lista messaggi."""
        client = SignalRPCClient("http://localhost:9999")

        with patch.object(client, "_call") as mock_call:
            mock_call.return_value = {
                "result": [{"envelope": {"source": "+39"}}]
            }
            messages = client.receive()
        assert len(messages) == 1
        assert messages[0]["envelope"]["source"] == "+39"

    def test_receive_error(self):
        """receive con errore → lista vuota."""
        client = SignalRPCClient("http://localhost:9999")

        with patch.object(client, "_call") as mock_call:
            mock_call.return_value = {"error": "timeout"}
            messages = client.receive()
        assert messages == []


class TestSignalSSE:
    """📡 Server-Sent Events listener (real-time Signal delivery)."""

    def test_listen_events_yields_envelope(self):
        """An SSE stream with one event → yields the parsed envelope."""
        client = SignalRPCClient(SSE_URL)
        # Simulated SSE stream: event line, data line, blank terminator, keep-alive
        sse_body = (
            b'event:receive\n'
            b'data:["{\\"envelope\\":{\\"source\\":\\"+39\\"}}"]\n'
            b'\n'
            b':keep-alive\n'
        )

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.__iter__.return_value = iter(sse_body.splitlines(keepends=True))
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            results = list(client.listen_events("+391234567890"))
        assert len(results) == 1
        assert results[0]["envelope"]["source"] == "+39"

    def test_listen_events_skips_keepalive(self):
        """SSE comments (keep-alive) are ignored."""
        client = SignalRPCClient(SSE_URL)
        sse_body = (
            b':\n'                     # keep-alive
            b':\n'                     # keep-alive
            b'event:receive\n'
            b'data:["{\\"envelope\\":{\\"source\\":\\"+39\\"}}"]\n'
            b'\n'
        )

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.__iter__.return_value = iter(sse_body.splitlines(keepends=True))
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            results = list(client.listen_events("+391234567890"))
        assert len(results) == 1

    def test_listen_events_connection_error(self):
        """Connection error → generator returns empty (caller reconnects)."""
        client = SignalRPCClient(SSE_URL)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("Connection refused")
            results = list(client.listen_events("+391234567890"))
        assert results == []

    def test_listen_events_bad_json(self):
        """Malformed SSE data → event is skipped gracefully."""
        client = SignalRPCClient(SSE_URL)
        sse_body = (
            b'event:receive\n'
            b'data:not-valid-json\n'
            b'\n'
            b'event:receive\n'
            b'data:["{\\"envelope\\":{\\"source\\":\\"+39\\"}}"]\n'
            b'\n'
        )

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.__iter__.return_value = iter(sse_body.splitlines(keepends=True))
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            results = list(client.listen_events("+391234567890"))
        assert len(results) == 1
        assert results[0]["envelope"]["source"] == "+39"


class TestIsDaemonRunning:
    """🟢 Rilevamento demone in esecuzione."""

    def test_daemon_running(self):
        """Demone attivo → True."""
        with patch("backend.SignalRPCClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client._call.return_value = {"result": ["ok"]}
            mock_client_cls.return_value = mock_client

            assert _is_daemon_running() is True

    def test_daemon_not_running(self):
        """Demone non attivo → False."""
        with patch("backend.SignalRPCClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client._call.side_effect = Exception("Connection refused")
            mock_client_cls.return_value = mock_client

            assert _is_daemon_running() is False
