"""Regression tests for the per-backend unread totals in the status bar.

The status bar's DEFAULT content is ``📱 N  💬 N  📨 N`` (N = sum of unread
counts per protocol, ``-`` when 0).  Transient and persistent status messages
overwrite it; when they are cleared, the default is restored (and refreshed).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from textual.widgets import Static

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from models import (
    PROTOCOL_SIGNAL,
    PROTOCOL_TELEGRAM,
    PROTOCOL_WHATSAPP,
    ChatContact,
)
from signal_tui import SignalTUI

DEFAULT_ALL_ZERO = "📱 -  💬 -  📨 -"


class _FakeStatusBar:
    """Minimal ``StatusBar`` double exposing the widget's public API.

    ``show_message``/``show_default``/``set_counts`` mirror the real
    ``StatusBar`` methods so the app code under test runs unchanged.  The
    formatted default text is produced by the SAME pure helper used in
    production (``SignalTUI._backend_unread_text``), so the format assertions
    stay coupled to the real code.
    """

    def __init__(self) -> None:
        self.content = ""
        self.counts: dict[str, int] = {}

    def show_message(self, text: str) -> None:
        self.content = text

    def set_counts(self, counts: dict[str, int]) -> None:
        self.counts = dict(counts)

    def show_default(self, totals: dict[str, int]) -> None:
        self.counts = dict(totals)
        self.content = SignalTUI._backend_unread_text(totals)


def _contact(protocol: str, cid: str, name: str = "X") -> ChatContact:
    return ChatContact(id=cid, display_name=name, protocol=protocol)


def _make_app(*contacts: ChatContact) -> tuple[SignalTUI, _FakeStatusBar]:
    """Build a bare ``SignalTUI`` with ``query_one("#status-bar")`` stubbed."""
    app = SignalTUI()
    app.contacts = list(contacts)
    bar = _FakeStatusBar()
    app.query_one = MagicMock(return_value=bar)
    return app, bar


class TestRenderBackendUnreadStatus:
    def test_default_all_zero(self):
        app, bar = _make_app(
            _contact(PROTOCOL_SIGNAL, "+1"),
            _contact(PROTOCOL_WHATSAPP, "wa1"),
            _contact(PROTOCOL_TELEGRAM, "tg1"),
        )
        app._unread_counts = {}

        app._render_backend_unread_status()

        assert bar.content == DEFAULT_ALL_ZERO

    def test_counts_on_single_backend(self):
        app, bar = _make_app(
            _contact(PROTOCOL_SIGNAL, "+1"),
            _contact(PROTOCOL_SIGNAL, "+2"),
        )
        app._unread_counts = {"signal:+1": 2, "signal:+2": 3}

        app._render_backend_unread_status()

        assert bar.content == "📱 5  💬 -  📨 -"

    def test_counts_on_all_backends_fixed_order(self):
        app, bar = _make_app(
            _contact(PROTOCOL_TELEGRAM, "tg1"),
            _contact(PROTOCOL_SIGNAL, "+1"),
            _contact(PROTOCOL_WHATSAPP, "wa1"),
        )
        app._unread_counts = {
            "telegram:tg1": 7,
            "signal:+1": 1,
            "whatsapp:wa1": 3,
        }

        app._render_backend_unread_status()

        # Order is always Signal → WhatsApp → Telegram, regardless of the
        # order contacts appear in ``self.contacts``.
        assert bar.content == "📱 1  💬 3  📨 7"

    def test_backend_unread_total_sums_contacts(self):
        app, _ = _make_app(
            _contact(PROTOCOL_SIGNAL, "+1"),
            _contact(PROTOCOL_SIGNAL, "+2"),
            _contact(PROTOCOL_WHATSAPP, "wa1"),
        )
        app._unread_counts = {
            "signal:+1": 2,
            "signal:+2": 3,
            "whatsapp:wa1": 5,
        }

        assert app._backend_unread_total(PROTOCOL_SIGNAL) == 5
        assert app._backend_unread_total(PROTOCOL_WHATSAPP) == 5
        assert app._backend_unread_total(PROTOCOL_TELEGRAM) == 0

    def test_contacts_without_unread_entry_count_as_zero(self):
        app, _ = _make_app(
            _contact(PROTOCOL_SIGNAL, "+1"),
            _contact(PROTOCOL_SIGNAL, "+2"),
        )
        app._unread_counts = {"signal:+1": 4}

        assert app._backend_unread_total(PROTOCOL_SIGNAL) == 4

    def test_render_survives_query_one_failure(self):
        app, _ = _make_app(_contact(PROTOCOL_SIGNAL, "+1"))
        app.query_one = MagicMock(side_effect=RuntimeError("no DOM in test"))

        # Must not raise: the render is best-effort like _status/_status_clear.
        app._render_backend_unread_status()


class TestStatusClearRestoresDefault:
    def test_status_clear_restores_default(self):
        app, bar = _make_app(_contact(PROTOCOL_SIGNAL, "+1"))
        app._unread_counts = {"signal:+1": 2}
        app._status("Errore", 0)  # persistent
        assert bar.content == "Errore"
        assert app._status_active is True

        app._status_clear()

        assert bar.content == "📱 2  💬 -  📨 -"
        assert app._status_active is False
        assert app._status_timer is None

    def test_status_sets_active_and_overwrites(self):
        app, bar = _make_app(_contact(PROTOCOL_SIGNAL, "+1"))
        app._status("Prima", 0)
        assert app._status_active is True
        assert bar.content == "Prima"

        app._status("Seconda")  # transient
        assert app._status_active is True
        assert bar.content == "Seconda"

    def test_permanent_error_hidden_then_cleared(self):
        app, bar = _make_app(_contact(PROTOCOL_SIGNAL, "+1"))
        app._unread_counts = {"signal:+1": 3}
        app._status("❌ backend non disponibile", 0)

        assert bar.content == "❌ backend non disponibile"
        assert app._status_active is True

        app._status_clear()

        assert bar.content == "📱 3  💬 -  📨 -"


class TestRefreshBackendStatusIfIdle:
    def test_does_not_touch_bar_when_active(self):
        app, bar = _make_app(_contact(PROTOCOL_SIGNAL, "+1"))
        app._status_active = True
        bar.content = "SENTINEL"

        app._refresh_backend_status_if_idle()

        assert bar.content == "SENTINEL"

    def test_updates_bar_when_idle(self):
        app, bar = _make_app(_contact(PROTOCOL_SIGNAL, "+1"))
        app._status_active = False
        app._unread_counts = {"signal:+1": 4}

        app._refresh_backend_status_if_idle()

        assert bar.content == "📱 4  💬 -  📨 -"


class TestOnBackendReadyInitsDefault:
    def test_on_backend_ready_renders_default_when_idle(self):
        app = SignalTUI()
        app.contacts = [
            _contact(PROTOCOL_SIGNAL, "+1"),
            _contact(PROTOCOL_WHATSAPP, "wa1"),
        ]
        app._unread_counts = {"signal:+1": 2, "whatsapp:wa1": 3}
        # Stub the DOM/status side effects so the unread default is not
        # overwritten by the trailing ``_status("✅ Signal: ...")`` call.
        app._sync_last_ts = MagicMock()
        app._sort_contacts = MagicMock()
        app._render_contact_list = MagicMock()
        app._update_unread_badges = MagicMock()
        app._status = MagicMock()
        app._mark_backend_done = MagicMock()
        bar = _FakeStatusBar()
        app.query_one = MagicMock(return_value=bar)

        backend = MagicMock()
        backend.protocol = PROTOCOL_SIGNAL
        backend.cache = {}
        backend.contacts = []

        app._on_backend_ready(backend)

        assert bar.content == "📱 2  💬 3  📨 -"
        assert app._status_active is False


@pytest.mark.integration
async def test_status_transient_restores_default_after_timer(app_for_test):
    """A transient status overwrites the default, then the timer restores it."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()
        status_text = app.query_one("#status-text", Static)

        app._unread_counts["signal:+391234567890"] = 2
        app._status("Ciao", 0.05)
        assert status_text.content == "Ciao"

        await pilot.pause(0.2)

        assert app.query_one("#status-signal", Static).content == "📱 2"
        assert app.query_one("#status-whatsapp", Static).content == "💬 -"
        assert app.query_one("#status-telegram", Static).content == "📨 -"
        assert app._status_active is False


@pytest.mark.integration
async def test_select_contact_zeroes_unread_in_default(app_for_test):
    """Selecting a contact zeroes its unread and refreshes the default."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()

        mario_key = "signal:+391234567890"
        luigi_key = "signal:+391111111111"
        app._unread_counts[mario_key] = 5
        app._unread_counts[luigi_key] = 3

        app._select_contact(app.contacts[0])  # Mario

        assert app._unread_counts[mario_key] == 0
        # Only Luigi's 3 remain; Mario's unread was zeroed.
        assert app.query_one("#status-signal", Static).content == "📱 3"
        assert app.query_one("#status-whatsapp", Static).content == "💬 -"
        assert app.query_one("#status-telegram", Static).content == "📨 -"
