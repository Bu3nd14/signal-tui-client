"""
Regression tests for backend.py — sending messages (subprocess and RPC).
"""

from __future__ import annotations

import pytest
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend import _send_subprocess, SignalRPCClient


class TestSendSubprocess:
    """📤 Invio messaggi via subprocess."""

    def test_send_basic(self):
        """Send base → comando con -m e destinatario."""
        with patch("backend._run_subprocess") as mock_run:
            _send_subprocess("Ciao!", "+391234567890")
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "send" in args
        assert "-m" in args
        assert "Ciao!" in args
        assert "+391234567890" in args

    def test_send_with_quote(self):
        """Send con quote → include flag quote."""
        with patch("backend._run_subprocess") as mock_run:
            _send_subprocess(
                "Ciao!",
                "+391234567890",
                quote_timestamp=1000000,
                quote_author="+391234567890",
                quote_message="Messaggio originale",
            )
        args = mock_run.call_args[0][0]
        assert "--quote-timestamp" in args
        assert "1000000" in args
        assert "--quote-author" in args
        assert "--quote-message" in args

    def test_send_with_partial_quote(self):
        """Send con solo quote_timestamp → ok."""
        with patch("backend._run_subprocess") as mock_run:
            _send_subprocess(
                "Ciao!",
                "+391234567890",
                quote_timestamp=1000000,
            )
        args = mock_run.call_args[0][0]
        assert "--quote-timestamp" in args
        assert "--quote-author" not in args


class TestSendRPC:
    """📤 Invio messaggi via RPC."""

    def test_send_message_basic(self):
        """Send RPC base → parametri corretti."""
        client = SignalRPCClient("http://localhost:9999")

        with patch.object(client, "_call") as mock_call:
            mock_call.return_value = {"result": {}}
            client.send_message("Ciao!", "+391234567890")

        mock_call.assert_called_once_with("send", {
            "message": "Ciao!",
            "recipient": ["+391234567890"],
        })

    def test_send_message_with_timestamp(self):
        """Send RPC con timestamp esplicito."""
        client = SignalRPCClient("http://localhost:9999")

        with patch.object(client, "_call") as mock_call:
            mock_call.return_value = {"result": {}}
            client.send_message("Ciao!", "+391234567890", timestamp=1000000)

        mock_call.assert_called_once_with("send", {
            "message": "Ciao!",
            "recipient": ["+391234567890"],
            "timestamp": 1000000,
        })

    def test_send_message_with_quote(self):
        """Send RPC con quote."""
        client = SignalRPCClient("http://localhost:9999")

        with patch.object(client, "_call") as mock_call:
            mock_call.return_value = {"result": {}}
            client.send_message(
                "Ciao!",
                "+391234567890",
                quote_timestamp=1000000,
                quote_author="+391234567890",
                quote_message="Originale",
            )

        mock_call.assert_called_once_with("send", {
            "message": "Ciao!",
            "recipient": ["+391234567890"],
            "quoteTimestamp": 1000000,
            "quoteAuthor": "+391234567890",
            "quoteMessage": "Originale",
        })
