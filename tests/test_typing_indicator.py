"""
Tests for the typing-indicator feature.

Typing indicators arrive from signal-cli as envelopes with a
``typingMessage`` field whose ``action`` is ``"STARTED"`` or ``"STOPPED"``.
They are ephemeral: they must never be saved to the message cache nor shown
as messages in the chat log.  Instead they toggle a ``✍️`` icon next to the
contact in the contact list.

A contact that stops typing without sending a message, or that sends a
message while typing, moves to a "mumbling" state (💭).  The contact list is
always kept in alphabetical order: the ✍️/💭 icons and the unread *N badges
are shown in the label but never reorder the list, so it doesn't jump around.

"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend import _process_typing
from models import PROTOCOL_SIGNAL, ChatContact, contact_cache_key
from signal_tui import SignalTUI


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


def _message_envelope(source: str, text: str = "ciao") -> dict:
    """Build a real message envelope (no typingMessage)."""
    return {
        "source": source,
        "sourceNumber": source,
        "timestamp": 1234567890000,
        "dataMessage": {"message": text, "timestamp": 1234567890000},
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

    @pytest.fixture(autouse=True)
    def _temp_db(self, tmp_path):
        """Isolate the SQLite cache for this test class."""
        import backend as backend_mod

        db_file = tmp_path / "messages.db"
        with (
            patch.object(backend_mod, "DB_FILE", db_file),
            patch.object(backend_mod, "CACHE_DIR", tmp_path),
        ):
            yield

    def _make_app(self) -> SignalTUI:
        app = SignalTUI()
        contact = ChatContact(
            id="+391234567890",
            display_name="Mario",
            protocol=PROTOCOL_SIGNAL,
            extras={"aci": "uuid-123"},
        )
        app.contacts = [contact]
        app.signal_backend.contacts = [contact]
        app.signal_backend._set_contacts([contact])
        app._unread_counts = {}
        app._typing_contacts = {}
        app._typing_mumbling = {}
        return app

    def _dispatch(self, app, envelope: dict) -> None:
        """Convert an envelope to ChatEvents and route them through _handle_event."""
        events = app.signal_backend.envelope_to_event(envelope)
        for event in events:
            with patch.object(app, "call_from_thread"):
                app._handle_event(event)

    def _cache_key(self) -> str:
        return contact_cache_key(PROTOCOL_SIGNAL, "+391234567890")

    def test_started_adds_to_typing_contacts(self):
        app = self._make_app()
        self._dispatch(app, _typing_envelope("+391234567890", "STARTED"))
        assert self._cache_key() in app._typing_contacts

    def test_stopped_moves_to_mumbling(self):
        """STOPPED rimuove da _typing_contacts e passa allo stato mumbling."""
        app = self._make_app()
        app._typing_contacts[self._cache_key()] = 100.0
        self._dispatch(app, _typing_envelope("+391234567890", "STOPPED"))
        assert self._cache_key() not in app._typing_contacts
        # The contact moves to the mumbling state (💭)
        assert self._cache_key() in app._typing_mumbling

    def test_typing_envelope_not_saved_to_cache(self):
        """Un envelope di typing non deve finire nella cache messaggi."""
        app = self._make_app()
        app._cache = {}
        self._dispatch(app, _typing_envelope("+391234567890", "STARTED"))
        # The cache must remain empty — typing is ephemeral.
        assert app._cache == {}

    def test_message_moves_typing_contact_to_mumbling(self):
        """Quando arriva un messaggio da chi stava scrivendo, il contatto
        passa allo stato mumbling (💭)."""
        app = self._make_app()
        app._cache = {}
        app._typing_contacts[self._cache_key()] = 100.0  # sta scrivendo

        self._dispatch(app, _message_envelope("+391234567890"))

        # The ✍️ indicator is gone, the contact moves to the mumbling state (💭).
        assert self._cache_key() not in app._typing_contacts
        assert self._cache_key() in app._typing_mumbling

    def test_message_from_non_typing_contact_no_mumbling(self):
        """Un messaggio da un contatto che NON stava scrivendo non crea
        alcuno stato mumbling."""
        app = self._make_app()
        app._cache = {}
        self._dispatch(app, _message_envelope("+391234567890"))
        assert self._cache_key() not in app._typing_mumbling
        assert self._cache_key() not in app._typing_contacts

    def test_message_refreshes_existing_mumbling(self):
        """Un messaggio da un contatto già in mumbling aggiorna il timer."""
        app = self._make_app()
        app._cache = {}
        app._typing_mumbling[self._cache_key()] = 100.0  # already mumbling
        self._dispatch(app, _message_envelope("+391234567890"))
        # Still mumbling, with a fresh (future) expiry.
        assert self._cache_key() in app._typing_mumbling
        assert app._typing_mumbling[self._cache_key()] > 100.0

    def test_new_started_after_message_readds_indicator(self):
        """Dopo un messaggio, un nuovo STARTED riattiva l'indicatore ✍️."""
        app = self._make_app()
        app._cache = {}

        # Contact starts typing → indicator appears
        self._dispatch(app, _typing_envelope("+391234567890", "STARTED"))
        assert self._cache_key() in app._typing_contacts

        # Contact sends a message → moves to mumbling (💭)
        self._dispatch(app, _message_envelope("+391234567890"))
        assert self._cache_key() not in app._typing_contacts
        assert self._cache_key() in app._typing_mumbling

        # Contact starts typing again → back to ✍️, mumbling cleared
        self._dispatch(app, _typing_envelope("+391234567890", "STARTED"))
        assert self._cache_key() in app._typing_contacts
        assert self._cache_key() not in app._typing_mumbling

    def test_contact_label_includes_typing_icon(self):
        app = self._make_app()
        contact = app.contacts[0]
        # No typing → no icon
        assert "✍️" not in app._member_label(contact)
        # Typing → icon present
        app._typing_contacts[contact.cache_key] = 100.0
        assert "✍️" in app._member_label(contact)

    def test_contact_label_icon_after_unread_badge(self):
        """L'icona ✍️ va a destra del badge *N quando presente."""
        app = self._make_app()
        contact = app.contacts[0]
        app._unread_counts[contact.cache_key] = 3
        app._typing_contacts[contact.cache_key] = 100.0
        label = app._member_label(contact)
        # Badge first, then typing icon
        assert label.index("*3") < label.index("✍️")

    def test_contact_label_includes_mumbling_icon(self):
        """Un contatto in stato mumbling mostra 💭 (non ✍️)."""
        app = self._make_app()
        contact = app.contacts[0]
        app._typing_mumbling[contact.cache_key] = 100.0
        label = app._member_label(contact)
        assert "💭" in label
        assert "✍️" not in label

    def test_typing_timeout_moves_to_mumbling(self):
        """Dopo il timeout, l'indicatore ✍️ sparisce ma il contatto passa
        allo stato mumbling (💭) invece di sparire del tutto."""
        app = self._make_app()
        app._typing_contacts[self._cache_key()] = 0.0  # started long ago
        with patch.object(app, "call_from_thread"):
            # Simulate the poll loop's timeout check
            now = 100.0
            expired = [
                num
                for num, started_at in app._typing_contacts.items()
                if now - started_at > app._TYPING_TIMEOUT
            ]
            for num in expired:
                app._typing_contacts.pop(num, None)
                app._typing_mumbling[num] = now + app._TYPING_MUMBLING_DURATION
        assert app._typing_contacts == {}
        assert self._cache_key() in app._typing_mumbling

    def test_mumbling_expiry_removes_indicator(self):
        """Dopo la scadenza del mumbling, il contatto viene rimosso."""
        app = self._make_app()
        app._typing_mumbling[self._cache_key()] = 100.0  # already expired
        with patch.object(app, "call_from_thread"):
            # Simulate the poll loop's mumbling-expiry check
            now = 200.0
            expired = [
                num
                for num, expires_at in app._typing_mumbling.items()
                if now >= expires_at
            ]
            for num in expired:
                app._typing_mumbling.pop(num, None)
        assert app._typing_mumbling == {}

    def test_started_clears_mumbling(self):
        """Un nuovo STARTED rimuove lo stato mumbling (sta scrivendo di nuovo)."""
        app = self._make_app()
        app._typing_mumbling[self._cache_key()] = 100.0
        self._dispatch(app, _typing_envelope("+391234567890", "STARTED"))
        assert self._cache_key() not in app._typing_mumbling
        assert self._cache_key() in app._typing_contacts

    def _two_contacts(self, app):
        """Replace app.contacts with Anna + Mario, returning them."""
        anna = ChatContact(
            id="+391111111111", display_name="Anna", protocol=PROTOCOL_SIGNAL
        )
        mario = ChatContact(
            id="+391234567890",
            display_name="Mario",
            protocol=PROTOCOL_SIGNAL,
            extras={"aci": "uuid-123"},
        )
        app.contacts = [mario, anna]
        app._unread_counts = {}
        return anna, mario

    def test_sort_keeps_alphabetical_when_typing(self):
        """La lista resta in ordine alfabetico anche quando un contatto sta
        scrivendo (niente riordino)."""
        app = self._make_app()
        anna, mario = self._two_contacts(app)
        app._typing_contacts[mario.cache_key] = 100.0

        app._sort_contacts()

        # Anna < Mario alfabeticamente, indipendentemente dal typing.
        assert app.contacts[0].id == anna.id
        assert app.contacts[1].id == mario.id

    def test_sort_keeps_alphabetical_with_unread(self):
        """La lista resta in ordine alfabetico anche con messaggi non letti."""
        app = self._make_app()
        anna, mario = self._two_contacts(app)
        app._unread_counts = {mario.cache_key: 2}
        app._typing_contacts[mario.cache_key] = 100.0

        app._sort_contacts()

        # Anna < Mario alfabeticamente, indipendentemente da non letti/typing.
        assert app.contacts[0].id == anna.id
        assert app.contacts[1].id == mario.id

    def test_sort_keeps_alphabetical_when_mumbling(self):
        """La lista resta in ordine alfabetico anche in stato mumbling."""
        app = self._make_app()
        anna, mario = self._two_contacts(app)
        app._typing_mumbling[mario.cache_key] = 100.0

        app._sort_contacts()

        # Anna < Mario alfabeticamente, indipendentemente dal mumbling.
        assert app.contacts[0].id == anna.id
        assert app.contacts[1].id == mario.id

    def test_sort_selected_typing_not_reordered(self):
        """Il contatto selezionato che sta scrivendo NON viene spostato in
        cima: resta nel suo posto alfabetico."""
        app = self._make_app()
        selected = ChatContact(
            id="+391234567890",
            display_name="Mario",
            protocol=PROTOCOL_SIGNAL,
            extras={"aci": "uuid-123"},
        )
        other = ChatContact(
            id="+391111111111", display_name="Anna", protocol=PROTOCOL_SIGNAL
        )
        app.contacts = [selected, other]
        app._unread_counts = {}
        app.selected_contact = selected
        app._typing_contacts[selected.cache_key] = 100.0

        app._sort_contacts()

        # Mario (selected, typing) stays in alphabetical order, not at the top.
        assert app.contacts[0].id == other.id  # Anna
        assert app.contacts[1].id == selected.id  # Mario

    def test_sort_selected_mumbling_not_reordered(self):
        """Il contatto selezionato in stato mumbling NON viene spostato in cima."""
        app = self._make_app()
        selected = ChatContact(
            id="+391234567890",
            display_name="Mario",
            protocol=PROTOCOL_SIGNAL,
            extras={"aci": "uuid-123"},
        )
        other = ChatContact(
            id="+391111111111", display_name="Anna", protocol=PROTOCOL_SIGNAL
        )
        app.contacts = [selected, other]
        app._unread_counts = {}
        app.selected_contact = selected
        app._typing_mumbling[selected.cache_key] = 100.0

        app._sort_contacts()

        assert app.contacts[0].id == other.id  # Anna
        assert app.contacts[1].id == selected.id  # Mario

    def test_selected_typing_still_shows_icon(self):
        """Il contatto selezionato che sta scrivendo mostra comunque l'icona
        ✍️ nella label (solo il riordino è disabilitato)."""
        app = self._make_app()
        contact = app.contacts[0]
        app.selected_contact = contact
        app._typing_contacts[contact.cache_key] = 100.0
        assert "✍️" in app._member_label(contact)

    def test_selected_mumbling_still_shows_icon(self):
        """Il contatto selezionato in stato mumbling mostra comunque 💭."""
        app = self._make_app()
        contact = app.contacts[0]
        app.selected_contact = contact
        app._typing_mumbling[contact.cache_key] = 100.0
        assert "💭" in app._member_label(contact)


class TestUpdateTypingLabel:
    """🎯 _update_typing_label aggiorna SOLO la riga del contatto interessato,
    senza ricostruire l'intera lista (sort + render O(N)) — causa del lag di
    digitazione con raffiche di eventi typing."""

    def _make_app(self):
        app = SignalTUI()
        contact = ChatContact(
            id="+391234567890",
            display_name="Mario",
            protocol=PROTOCOL_SIGNAL,
            extras={"aci": "uuid-123"},
        )
        app.contacts = [contact]
        app.signal_backend.contacts = [contact]
        app._unread_counts = {}
        app._typing_contacts = {}
        app._typing_mumbling = {}
        app.call_from_thread = MagicMock()
        return app

    def _render_one(self, app, cache_key, text):
        """Crea una lista finta con un solo item per il contatto."""

        class Label:
            def __init__(self):
                self._refreshed = 0

            def update(self, t):
                self._refreshed += 1
                self._text = t

        class Item:
            def __init__(self, ck):
                self._contact_id = ck
                self._label_text = text
                self._label = Label()
                self.children = [self._label]

        class List:
            def __init__(self):
                self.children = []

        lst = List()
        item = Item(cache_key)
        lst.children = [item]
        app._contact_widgets = {cache_key: item}
        app.query_one = lambda *a, **k: lst
        # la lista contatti NON deve essere riordinata né ridisegnata.
        app._sort_contacts = MagicMock()
        app._render_contact_list = MagicMock()
        return lst, app._sort_contacts, app._render_contact_list

    def test_updates_only_matching_row(self):
        app = self._make_app()
        contact = app.contacts[0]
        app._typing_contacts[contact.cache_key] = 100.0
        lst, sort_mock, render_mock = self._render_one(
            app, contact.cache_key, "vecchia etichetta"
        )
        item = lst.children[0]
        # forza il cambio: l'etichetta attuale non ha più l'icona
        item._label_text = "vecchia etichetta"

        app._update_typing_label(contact.cache_key)

        assert item._label._refreshed == 1
        assert "✍️" in item._label_text
        # niente sort/render della lista intera
        sort_mock.assert_not_called()
        render_mock.assert_not_called()

    def test_updates_mumbling_icon(self):
        app = self._make_app()
        contact = app.contacts[0]
        app._typing_mumbling[contact.cache_key] = 100.0
        lst, sort_mock, render_mock = self._render_one(
            app, contact.cache_key, "vecchia etichetta"
        )
        item = lst.children[0]
        item._label_text = "vecchia etichetta"

        app._update_typing_label(contact.cache_key)

        assert "💭" in item._label_text
        sort_mock.assert_not_called()
        render_mock.assert_not_called()

    def test_noop_when_row_not_rendered(self):
        """Se il contatto non è nella lista (es. filtrato), non fa nulla."""
        app = self._make_app()
        contact = app.contacts[0]
        app._typing_contacts[contact.cache_key] = 100.0
        lst, sort_mock, render_mock = self._render_one(
            app, "signal:+999999999", "etichetta-altro"
        )
        other = lst.children[0]
        other._label_text = "etichetta-altro"

        app._update_typing_label(contact.cache_key)

        # la riga di un altro contatto non viene toccata
        assert other._label._refreshed == 0
        sort_mock.assert_not_called()
        render_mock.assert_not_called()

    def test_noop_when_contact_unknown(self):
        app = self._make_app()
        lst, sort_mock, render_mock = self._render_one(app, "signal:+391234567890", "x")
        app._typing_contacts["signal:+0000"] = 100.0
        app._update_typing_label("signal:+0000")
        assert lst.children[0]._label._refreshed == 0
        sort_mock.assert_not_called()
        render_mock.assert_not_called()
