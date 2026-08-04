"""
Tests for Step 3 — UI protocol-aware labels, message accents, and the
Ctrl+W protocol filter (ALL / SIGNAL / WHATSAPP).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from models import ChatContact, PROTOCOL_SIGNAL, PROTOCOL_WHATSAPP
from signal_tui import SignalTUI
from ui_components import MessageWidget


def _signal(cid: str = "+391", name: str = "Mario") -> ChatContact:
    return ChatContact(id=cid, display_name=name, protocol=PROTOCOL_SIGNAL)


def _whatsapp(cid: str = "wa:1@s.whatsapp.net", name: str = "Anna") -> ChatContact:
    return ChatContact(id=cid, display_name=name, protocol=PROTOCOL_WHATSAPP)


def _make_app(*contacts) -> SignalTUI:
    app = SignalTUI()
    app.contacts = list(contacts)
    return app


class _FakeListView:
    def __init__(self):
        self.items = []
        self.index = None

    def clear(self):
        self.items = []

    def append(self, item):
        self.items.append(item)


class TestProtocolLabel:
    """🏷️ Label di contatto protocol-aware (emoji)."""

    def test_signal_label_emoji(self):
        app = _make_app(_signal())
        label = app._contact_label(app.contacts[0])
        assert "📱" in label
        assert "Mario" in label

    def test_whatsapp_label_emoji(self):
        app = _make_app(_whatsapp())
        label = app._contact_label(app.contacts[0])
        assert "💬" in label
        assert "Anna" in label

    def test_protocol_class(self):
        app = _make_app()
        assert app._protocol_class(_signal()) == "protocol-signal"
        assert app._protocol_class(_whatsapp()) == "protocol-whatsapp"


class TestProtocolFilter:
    """🎛️ Filtro Ctrl+W sulla lista unificata."""

    def test_filtered_contacts_default_all(self):
        app = _make_app(_signal(), _whatsapp())
        assert len(app._filtered_contacts()) == 2

    def test_filtered_contacts_signal(self):
        app = _make_app(_signal(), _whatsapp())
        app._protocol_filter = "signal"
        result = app._filtered_contacts()
        assert len(result) == 1
        assert result[0].protocol == PROTOCOL_SIGNAL

    def test_filtered_contacts_whatsapp(self):
        app = _make_app(_signal(), _whatsapp())
        app._protocol_filter = "whatsapp"
        result = app._filtered_contacts()
        assert len(result) == 1
        assert result[0].protocol == PROTOCOL_WHATSAPP

    def test_cycle_filter_order(self):
        app = _make_app()
        app._apply_contact_filter = lambda: None  # avoid UI touches
        with patch.object(app, "_add_message"):
            observed = []
            for _ in range(3):
                app.action_cycle_protocol_filter()
                observed.append(app._protocol_filter)
        assert observed == ["signal", "whatsapp", "all"]

    def test_filter_render_applies_to_view(self):
        """Il filtro aggiorna dinamicamente la ListView (senza reload DB)."""
        app = _make_app(_signal(), _whatsapp())
        fake_list = _FakeListView()
        app.query_one = MagicMock(return_value=fake_list)
        app._protocol_filter = "whatsapp"
        # _render_contact_list uses the in-memory contact list, not the DB.
        app._render_contact_list(app._filtered_contacts())
        assert len(fake_list.items) == 1
        item = fake_list.items[0]
        assert item.has_class("protocol-whatsapp")

    def test_filter_title_suffix(self):
        app = _make_app()
        assert app._filter_title_suffix() == ""
        app._protocol_filter = "signal"
        assert app._filter_title_suffix() == " — Signal"
        app._protocol_filter = "whatsapp"
        assert app._filter_title_suffix() == " — WhatsApp"


class TestMessageAccent:
    """🎨 Accent per-protocollo sul MessageWidget."""

    def test_signal_accent_class(self):
        w = MessageWidget("ciao", timestamp=1, sender="Mario", protocol=PROTOCOL_SIGNAL)
        assert w.has_class("msg-signal")
        assert not w.has_class("msg-whatsapp")

    def test_whatsapp_accent_class(self):
        w = MessageWidget("ciao", timestamp=1, sender="Anna", protocol=PROTOCOL_WHATSAPP)
        assert w.has_class("msg-whatsapp")

    def test_no_protocol_no_accent(self):
        w = MessageWidget("ciao", timestamp=1, sender="X")
        assert not w.has_class("msg-signal")
        assert not w.has_class("msg-whatsapp")

    def test_selected_drops_accent_to_keep_highlight(self):
        w = MessageWidget("ciao", timestamp=1, sender="Mario", protocol=PROTOCOL_SIGNAL)
        w.set_selected(True)
        assert not w.has_class("msg-signal")
        w.set_selected(False)
        assert w.has_class("msg-signal")
