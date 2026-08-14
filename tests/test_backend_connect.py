"""Regression tests for ``BackendConnectMixin`` auto-selection.

L'auto-selezione del primo contatto attende che TUTTI i backend abbiano
riportato un esito (ready o fallito): solo allora, se non c'è ancora una
selezione e ci sono contatti, seleziona il contatto in cima alla lista.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from models import PROTOCOL_SIGNAL, ChatContact
from signal_tui import SignalTUI


def _make_app() -> SignalTUI:
    """App with the heavy/UI-touching methods of _on_backend_ready stubbed."""
    app = SignalTUI()
    app._render_contact_list = MagicMock()
    app._update_unread_badges = MagicMock()
    app._status = MagicMock()

    # Real _select_contact touches the DOM; emulate only its selection effect.
    def _select(contact: ChatContact) -> None:
        app.selected_contact = contact

    app._select_contact = MagicMock(side_effect=_select)
    return app


def _make_backend(contacts: list[ChatContact], protocol: str = PROTOCOL_SIGNAL) -> MagicMock:
    backend = MagicMock()
    backend.protocol = protocol
    backend.cache = {}
    backend.contacts = contacts
    return backend


class TestBackendReadyAutoSelect:
    def test_no_selection_until_all_backends_reported(self):
        app = _make_app()
        anna = ChatContact(id="+1", display_name="Anna", protocol=PROTOCOL_SIGNAL)
        mario = ChatContact(id="+2", display_name="Mario", protocol=PROTOCOL_SIGNAL)
        # Un altro backend (WhatsApp) è ancora in connessione.
        app._pending_backends = {"whatsapp"}

        app._on_backend_ready(_make_backend([mario, anna]))

        # Signal è pronto, ma WA non ha ancora riportato → nessuna selezione.
        app._select_contact.assert_not_called()
        assert app.selected_contact is None

        # L'ultimo backend riporta (ready o fallito) → seleziona il top.
        app._mark_backend_done("whatsapp")

        app._select_contact.assert_called_once_with(anna)
        assert app.selected_contact is anna

    def test_last_backend_failure_still_selects_first_contact(self):
        app = _make_app()
        anna = ChatContact(id="+1", display_name="Anna", protocol=PROTOCOL_SIGNAL)
        app.contacts = [anna]
        # L'ultimo backend (fallito) non ha ancora riportato.
        app._pending_backends = {"signal"}

        app._mark_backend_done("signal")

        app._select_contact.assert_called_once_with(anna)
        assert app.selected_contact is anna

    def test_no_selection_when_no_contacts(self):
        app = _make_app()
        app._pending_backends = {"signal"}

        app._mark_backend_done("signal")

        app._select_contact.assert_not_called()
        assert app.selected_contact is None

    def test_mark_backend_connecting_adds_to_pending(self):
        app = _make_app()

        app._mark_backend_connecting("signal")
        app._mark_backend_connecting("whatsapp")

        assert app._pending_backends == {"signal", "whatsapp"}



class TestTelegramConnectGuard:
    """🛡️ _connect_telegram non deve avviare due worker in parallelo.

    Due Ctrl+L→Esc ravvicinati avviavano due worker concorrenti che gareggiavano
    sullo stesso stato del backend (client/loop), rompendo la ricezione live.
    """

    def _make_app(self) -> SignalTUI:
        app = _make_app()
        app.call_from_thread = MagicMock()
        app.telegram_backend = MagicMock()
        app.telegram_backend.protocol = PROTOCOL_SIGNAL
        return app

    def test_skips_when_already_connecting(self):
        app = self._make_app()
        app._tg_connecting = True

        app._connect_telegram()

        app.telegram_backend._connect_sync.assert_not_called()

    def test_runs_and_resets_guard(self):
        app = self._make_app()
        app._tg_connecting = False

        app._connect_telegram()

        app.telegram_backend._connect_sync.assert_called_once()
        assert app._tg_connecting is False

    def test_guard_reset_on_failure(self):
        app = self._make_app()
        app._tg_connecting = False
        app.telegram_backend._connect_sync.side_effect = RuntimeError("boom")

        app._connect_telegram()

        assert app._tg_connecting is False



class TestReconnectTouchedBackends:
    """🎯 _reconnect_touched_backends riconnette solo i backend toccati."""

    def _make_app(self) -> SignalTUI:
        app = _make_app()
        app.run_worker = MagicMock()
        app._connect_signal = MagicMock()
        app._connect_whatsapp = MagicMock()
        app._connect_telegram = MagicMock()
        app.signal_backend = MagicMock()
        app.whatsapp_backend = MagicMock()
        app.telegram_backend = MagicMock()
        return app

    def test_empty_set_reconnects_nothing(self):
        app = self._make_app()
        app._reconnect_touched_backends(set())
        app.run_worker.assert_not_called()

    def test_telegram_only(self):
        app = self._make_app()
        app._reconnect_touched_backends({"telegram"})
        app.run_worker.assert_called_once_with(
            app._connect_telegram, exclusive=False, thread=True)

    def test_signal_only(self):
        app = self._make_app()
        app._reconnect_touched_backends({"signal"})
        app.run_worker.assert_called_once_with(
            app._connect_signal, exclusive=False, thread=True)

    def test_whatsapp_only(self):
        app = self._make_app()
        app._reconnect_touched_backends({"whatsapp"})
        app.run_worker.assert_called_once_with(
            app._connect_whatsapp, exclusive=False, thread=True)

    def test_multiple(self):
        app = self._make_app()
        app._reconnect_touched_backends({"signal", "telegram"})
        assert app.run_worker.call_count == 2
        app.run_worker.assert_any_call(app._connect_signal, exclusive=False, thread=True)
        app.run_worker.assert_any_call(app._connect_telegram, exclusive=False, thread=True)

    def test_missing_backend_skipped(self):
        app = self._make_app()
        app.telegram_backend = None
        app._reconnect_touched_backends({"telegram"})
        app.run_worker.assert_not_called()
