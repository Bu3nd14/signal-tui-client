"""
Tests for ``signal_tui.py`` startup ordering.

Verifies that the poll worker starts before WhatsApp connect_sync,
so Signal messages are processed even when WhatsApp is slow to connect.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class TestStartupOrder:
    """Verify poll worker starts before WhatsApp connect_sync."""

    def test_polling_active_before_whatsapp_connect(self):
        """_polling_active must be True when WhatsApp connect_sync runs."""
        from signal_tui import SignalTUI

        app = SignalTUI()
        app._add_message = MagicMock()
        app._update_contacts_ui = MagicMock()
        # on_mount sets _polling_active and starts poll worker
        app._polling_active = True
        app.query_one = MagicMock()

        # Mock Signal backend
        app.signal_backend = MagicMock()
        app.signal_backend._connect_sync = MagicMock()
        app.signal_backend.cache = {}
        app.signal_backend.contacts = []
        app.signal_backend.protocol = "signal"
        app.signal_backend._use_daemon = False

        # Mock WhatsApp backend
        wa = MagicMock()
        wa.protocol = "whatsapp"
        wa.cache = {}
        wa.contacts = []
        wa.connect_sync = MagicMock()

        captured: dict[str, bool] = {}

        def _capture():
            captured["polling_active"] = app._polling_active

        wa.connect_sync.side_effect = _capture

        app.whatsapp_backend = wa
        app.manager._backends = {
            "signal": app.signal_backend,
            "whatsapp": wa,
        }

        with patch("signal_tui.ensure_webhook_server"), \
             patch("signal_tui._dedup_messages", return_value=0), \
             patch.object(app, "_sync_last_ts"), \
             patch.object(app, "_resync_wa_history"), \
             patch.object(app, "call_from_thread"), \
             patch.object(app, "run_worker"):
            app._startup()

        assert captured.get("polling_active") is True, (
            "Poll worker must be running BEFORE WhatsApp connect_sync. "
            "If this fails, Signal messages won't arrive until WhatsApp connects."
        )
