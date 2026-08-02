"""
Regression tests for the emoji-picker chat refresh bug.

When a chat has more than 20 messages, only the last 20 are shown and
``_seen_timestamps`` only contains those 20 timestamps.  Closing the emoji
picker calls ``_refresh_chat()`` which used to re-add *all* cached messages
whose timestamp was not in ``_seen_timestamps`` — i.e. all the older messages
beyond the 20-message window — causing the chat to jump to old messages.

These tests verify that ``_refresh_chat()`` only adds messages *newer* than
the last one already shown.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from signal_tui import SignalTUI
from backend import Contact


def _make_message(text: str, ts: int, is_mine: bool = False) -> dict:
    """Build a single cached message dict."""
    return {
        "text": text,
        "is_mine": is_mine,
        "sender": "You" if is_mine else "Mario",
        "timestamp": ts,
        "quote_text": None,
        "msg_type": "text",
        "attachment_info": None,
        "attachment_id": None,
        "read": is_mine,
        "status": "sent" if is_mine else "read",
    }


class _FakeChatLog:
    """Minimal stand-in for the #chat-log widget used by _refresh_chat."""

    def __init__(self) -> None:
        self.scrolled = False

    def scroll_end(self, animate: bool = False) -> None:
        self.scrolled = True



class TestRefreshChat:
    """🔄 Verifica che _refresh_chat non ri-aggiunga messaggi vecchi."""

    def _make_app(self, n_messages: int = 25) -> SignalTUI:
        """Build an app with a cache of *n_messages* for one contact."""
        app = SignalTUI()
        contact = Contact(number="+391234567890", name="Mario", aci="uuid-123")
        app.selected_contact = contact
        # Timestamps strictly increasing from 1..n
        app._cache = {
            contact.number: [
                _make_message(f"msg-{i}", ts=i) for i in range(1, n_messages + 1)
            ]
        }
        return app

    def test_refresh_chat_does_not_readd_old_messages(self):
        """Con >20 messaggi, _refresh_chat non deve ri-aggiungere i vecchi."""
        app = self._make_app(n_messages=25)

        # Simulate the initial load: only the last 20 messages are shown and
        # only their timestamps are recorded in _seen_timestamps.
        shown = app._cache[app.selected_contact.number][-20:]
        app._seen_timestamps = {m["timestamp"] for m in shown}

        # Track how many messages _add_message would mount.
        added: list[str] = []

        def fake_add_message(text, *args, **kwargs):
            added.append(text)

        with patch.object(app, "_add_message", side_effect=fake_add_message):
            app._refresh_chat()

        # No older messages should be re-added.
        assert added == []

    def test_refresh_chat_adds_only_newer_messages(self):
        """_refresh_chat deve aggiungere solo messaggi più recenti dell'ultimo."""
        app = self._make_app(n_messages=25)

        # Initial load shows the last 20 (timestamps 6..25).
        shown = app._cache[app.selected_contact.number][-20:]
        app._seen_timestamps = {m["timestamp"] for m in shown}

        # A new message arrives while the picker is open (timestamp 26).
        app._cache[app.selected_contact.number].append(
            _make_message("nuovo", ts=26)
        )

        added: list[str] = []

        def fake_add_message(text, *args, **kwargs):
            added.append(text)

        fake_chat_log = _FakeChatLog()

        with patch.object(app, "_add_message", side_effect=fake_add_message), \
             patch.object(app, "query_one", return_value=fake_chat_log):
            app._refresh_chat()

        # Only the new message should be added, not the old ones.
        assert added == ["nuovo"]
        # The chat should have been scrolled to the end.
        assert fake_chat_log.scrolled


    def test_refresh_chat_no_selected_contact(self):
        """Senza contatto selezionato, _refresh_chat non fa nulla."""
        app = SignalTUI()
        app.selected_contact = None
        app._cache = {"+391234567890": [_make_message("x", ts=1)]}

        with patch.object(app, "_add_message") as mock_add:
            app._refresh_chat()

        mock_add.assert_not_called()

    def test_refresh_chat_empty_seen_timestamps(self):
        """Con _seen_timestamps vuoto, aggiunge tutti i messaggi (nessun crash)."""
        app = self._make_app(n_messages=3)
        app._seen_timestamps = set()

        added: list[str] = []

        def fake_add_message(text, *args, **kwargs):
            added.append(text)

        fake_chat_log = _FakeChatLog()

        with patch.object(app, "_add_message", side_effect=fake_add_message), \
             patch.object(app, "query_one", return_value=fake_chat_log):
            app._refresh_chat()

        # All 3 messages are newer than max_seen=0, so all are added.
        assert len(added) == 3
        assert fake_chat_log.scrolled

