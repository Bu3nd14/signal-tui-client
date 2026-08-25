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

from models import (
    PROTOCOL_SIGNAL,
    PROTOCOL_WHATSAPP,
    ChatContact,
    contact_cache_key,
)
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


def _make_backend(
    contacts: list[ChatContact], protocol: str = PROTOCOL_SIGNAL
) -> MagicMock:
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
            app._connect_telegram, exclusive=False, thread=True
        )

    def test_signal_only(self):
        app = self._make_app()
        app._reconnect_touched_backends({"signal"})
        app.run_worker.assert_called_once_with(
            app._connect_signal, exclusive=False, thread=True
        )

    def test_whatsapp_only(self):
        app = self._make_app()
        app._reconnect_touched_backends({"whatsapp"})
        app.run_worker.assert_called_once_with(
            app._connect_whatsapp, exclusive=False, thread=True
        )

    def test_multiple(self):
        app = self._make_app()
        app._reconnect_touched_backends({"signal", "telegram"})
        assert app.run_worker.call_count == 2
        app.run_worker.assert_any_call(
            app._connect_signal, exclusive=False, thread=True
        )
        app.run_worker.assert_any_call(
            app._connect_telegram, exclusive=False, thread=True
        )

    def test_missing_backend_skipped(self):
        app = self._make_app()
        app.telegram_backend = None
        app._reconnect_touched_backends({"telegram"})
        app.run_worker.assert_not_called()


def _media_msg(
    *,
    mid: str,
    att: str | None,
    text: str,
    is_mine: bool = True,
    ts: int = 1787648916285,
    msg_type: str = "image",
    status: str = "sent",
) -> dict:
    """Build a cached media-message dict coherent with the UI cache."""
    return {
        "id": mid,
        "text": text,
        "is_mine": is_mine,
        "sender": "You" if is_mine else "Mario",
        "timestamp": ts,
        "msg_type": msg_type,
        "attachment_info": text,
        "attachment_id": att,
        "content_type": "image/jpeg",
        "read": is_mine,
        "status": status,
    }


class TestBackendReadyMergeDedup:
    """🖼️ Il merge ``_on_backend_ready`` tiene ENTRAMBI gli allegati di un
    messaggio multi-allegato (stesso ``id``, ``attachment_id`` diversi)."""

    @staticmethod
    def _make_app() -> SignalTUI:
        app = SignalTUI()
        app._render_contact_list = MagicMock()
        app._update_unread_badges = MagicMock()
        app._status = MagicMock()
        app._sync_last_ts = MagicMock()
        app._sort_contacts = MagicMock()
        app._refresh_backend_status_if_idle = MagicMock()
        app.contacts = []
        app._pending_backends = set()
        app.selected_contact = None
        return app

    @staticmethod
    def _make_backend(protocol: str, cache: dict) -> MagicMock:
        backend = MagicMock()
        backend.protocol = protocol
        backend.cache = cache
        backend.contacts = []
        return backend

    def test_two_attachments_same_id_both_kept(self):
        """Due allegati con lo stesso msg_id ma attachment_id diversi restano
        entrambi nella UI cache dopo il merge (bug "una sola immagine")."""
        app = self._make_app()
        cid = "+393356912240"
        msg_id = "1787648916285"
        backend = self._make_backend(
            PROTOCOL_SIGNAL,
            {
                cid: [
                    _media_msg(
                        mid=msg_id,
                        att="att-0115",
                        text="Image: IMG_0115.jpg",
                    ),
                    _media_msg(
                        mid=msg_id,
                        att="att-0114",
                        text="Image: IMG_0114.jpg",
                    ),
                ]
            },
        )

        app._on_backend_ready(backend)

        key = contact_cache_key(PROTOCOL_SIGNAL, cid)
        entries = app._cache[key]
        assert len(entries) == 2
        assert {e["attachment_id"] for e in entries} == {"att-0115", "att-0114"}

    def test_same_id_same_attachment_is_deduped(self):
        """Redelivery dello stesso allegato (id + attachment_id identici) → un solo."""
        app = self._make_app()
        cid = "+393356912240"
        msg_id = "1787648916285"
        msg = _media_msg(mid=msg_id, att="att-0115", text="Image: IMG_0115.jpg")
        backend = self._make_backend(PROTOCOL_SIGNAL, {cid: [msg, dict(msg)]})

        app._on_backend_ready(backend)

        key = contact_cache_key(PROTOCOL_SIGNAL, cid)
        assert len(app._cache[key]) == 1

    def test_ack_echo_without_attachment_id_still_deduped(self):
        """#36: l'ack-echo (stesso id, nessun attachment_id, text=caption) non
        diventa un nuovo messaggio accanto al media uscente."""
        app = self._make_app()
        cid = "1@c.us"
        msg_id = "true_189025889575055"
        media = _media_msg(
            mid=msg_id,
            att="https://wa.to/img/abc123.jpg",
            text="Media: https://wa.to/img/abc123.jpg",
        )
        ack = {
            "id": msg_id,
            "text": "Yes, nice",
            "is_mine": True,
            "sender": "You",
            "timestamp": 1700000001,
            "msg_type": "text",
            "attachment_info": None,
            "attachment_id": None,
            "read": True,
            "status": "sent",
        }
        backend = self._make_backend(PROTOCOL_WHATSAPP, {cid: [media, ack]})

        app._on_backend_ready(backend)

        key = contact_cache_key(PROTOCOL_WHATSAPP, cid)
        entries = app._cache[key]
        assert len(entries) == 1
        assert entries[0]["msg_type"] == "image"
