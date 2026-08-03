"""
Tests for the typing-indicator feature.

Typing indicators arrive from signal-cli as envelopes with a
``typingMessage`` field whose ``action`` is ``"STARTED"`` or ``"STOPPED"``.
They are ephemeral: they must never be saved to the message cache nor shown
as messages in the chat log.  Instead they toggle a ``✍️`` icon next to the
contact in the contact list.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend import _process_typing
from signal_tui import SignalTUI
from backend import Contact


def _typing_envelope(source: str, action: str) -> dict:
    """Build a typing-indicator envelope."""
    return {
        "source": source,
        "sourceNumber": source,
        "sourceUuid": "uuid-" + source,
        "timestamp": 1234567890000,
        "typingMessage": {
            "action": action,
            "timestamp": 1234567890000,
        },
    }


class TestBackendProcessTyping:
    """🧪 Verifica che _process_typing estragga correttamente i dati."""

    def test_started(self):
        result = _process_typing(_typing_envelope("+391234567890", "STARTED"))
        assert result == ("+391234567890", "STARTED")

    def test_stopped(self):
        result = _process_typing(_typing_envelope("+391234567890", "STOPPED"))
        assert result == ("+391234567890", "STOPPED")

    def test_not_typing_envelope(self):
        # A normal message envelope has no typingMessage → None
        envelope = {
            "source": "+391234567890",
            "dataMessage": {"message": "ciao"},
        }
        assert _process_typing(envelope) is None

    def test_unknown_action(self):
        envelope = _typing_envelope("+391234567890", "UNKNOWN")
        assert _process_typing(envelope) is None

    def test_missing_source(self):
        envelope = {
            "typingMessage": {"action": "STARTED", "timestamp": 1},
        }
        assert _process_typing(envelope) is None


class TestSignalTUITyping:
    """✍️ Verifica la gestione degli indicatori di typing nella UI."""

    def _make_app(self) -> SignalTUI:
        app = SignalTUI()
        contact = Contact(number="+391234567890", name="Mario", aci="uuid-123")
        app.contacts = [contact]
        app._unread_counts = {}
        app._typing_contacts = {}
        return app

    def test_started_adds_to_typing_contacts(self):
        app = self._make_app()
        with patch.object(app, "call_from_thread"):
            result = app._process_typing_envelope(
                _typing_envelope("+391234567890", "STARTED")
            )
        assert result is True
        assert "+391234567890" in app._typing_contacts

    def test_stopped_removes_from_typing_contacts(self):
        app = self._make_app()
        app._typing_contacts["+391234567890"] = 100.0
        with patch.object(app, "call_from_thread"):
            result = app._process_typing_envelope(
                _typing_envelope("+391234567890", "STOPPED")
            )
        assert result is True
        assert "+391234567890" not in app._typing_contacts

    def test_typing_envelope_not_saved_to_cache(self):
        """Un envelope di typing non deve finire nella cache messaggi."""
        app = self._make_app()
        app._cache = {}
        with patch.object(app, "call_from_thread"):
            app._process_envelope(_typing_envelope("+391234567890", "STARTED"))
        # The cache must remain empty — typing is ephemeral.
        assert app._cache == {}

    def test_contact_label_includes_typing_icon(self):
        app = self._make_app()
        contact = app.contacts[0]
        # No typing → no icon
        assert "✍️" not in app._contact_label(contact)
        # Typing → icon present
        app._typing_contacts[contact.number] = 100.0
        assert "✍️" in app._contact_label(contact)

    def test_contact_label_icon_after_unread_badge(self):
        """L'icona ✍️ va a destra del badge *N quando presente."""
        app = self._make_app()
        contact = app.contacts[0]
        app._unread_counts[contact.number] = 3
        app._typing_contacts[contact.number] = 100.0
        label = app._contact_label(contact)
        # Badge first, then typing icon
        assert label.index("*3") < label.index("✍️")

    def test_typing_timeout_expires_indicator(self):
        """Dopo il timeout, l'indicatore sparisce."""
        app = self._make_app()
        app._typing_contacts["+391234567890"] = 0.0  # started long ago
        with patch.object(app, "call_from_thread"):
            # Simulate the poll loop's timeout check
            now = 100.0
            expired = [
                num for num, started_at in app._typing_contacts.items()
                if now - started_at > app._TYPING_TIMEOUT
            ]
            for num in expired:
                app._typing_contacts.pop(num, None)
        assert app._typing_contacts == {}
