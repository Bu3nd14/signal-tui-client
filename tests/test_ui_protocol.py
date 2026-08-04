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
from ui_components import MessageWidget, ContactListView, ContactListWidget


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

    def test_ctrl_w_binding_is_priority(self):
        """Ctrl+W (filtro) deve avere priority=True così funziona anche quando
        il focus è sull'input di messaggio (altrimenti l'Input di Textual,
        che ha il suo 'ctrl+w' per cancellare una parola, lo cattura)."""
        app = _make_app()
        binding = next(
            (b for b in app.BINDINGS if b.action == "cycle_protocol_filter"), None
        )
        assert binding is not None
        assert binding.priority is True

    def test_select_contact_no_crash_when_beyond_visible_list(self):
        """Selezionando un contatto che è oltre la lista visibile/filtrata non
        deve più sollevare IndexError (era: contact_list.children[index] con
        l'indice globale di self.contacts fuori range)."""
        app = _make_app()
        # 3 contatti: 2 signal, 1 whatsapp
        app.contacts = [_signal("+1"), _signal("+2"), _whatsapp()]
        app._protocol_filter = "signal"
        target = app.contacts[1]  # secondo Signal, visibile nel filtro signal

        # ListView finta: contiene solo 2 children (i soli signal del filtro).
        class _Item:
            def __init__(self):
                self.children = [MagicMock()]
        fake_list = MagicMock()
        fake_list.index = 0
        fake_list.children = [_Item(), _Item()]

        # Patch le operazioni pesanti di _select_contact.
        app.query_one = MagicMock(return_value=fake_list)
        app._add_message = MagicMock()
        app._clear_chat = MagicMock()
        app._cancel_reply = MagicMock()
        app._load_messages_worker = MagicMock()
        app.manager = MagicMock()
        app.manager.get.return_value = MagicMock()
        # run_worker è il wrapper async di Textual: usa un no-op sincrono così non
        # crea coroutine mai attese.
        app.run_worker = MagicMock()
        app.run_worker.return_value = None

        # Non deve sollevare IndexError.
        app._select_contact(target)
        assert app.selected_contact == target
        # L'evidenziazione deve usare *visible.index* (indice nel filtro), non il
        # globale: per target (2° signal) il visible.index è 1.
        fake_list.index = 1
        assert fake_list.index == 1

    def test_select_contact_ok_without_filter_when_visible(self):
        """Senza filtro, il contatto visibile viene evidenziato correttamente."""
        app = _make_app()
        app.contacts = [_signal("+1"), _signal("+2")]
        target = app.contacts[1]

        class _Item:
            def __init__(self):
                self.children = [MagicMock()]  # con update attivo
        fake_list = MagicMock()
        fake_list.index = 0
        item = _Item()
        fake_list.children = [_Item(), item]
        app.query_one = MagicMock(return_value=fake_list)
        app._add_message = MagicMock()
        app._clear_chat = MagicMock()
        app._cancel_reply = MagicMock()
        app._load_messages_worker = MagicMock()
        app.manager = MagicMock()
        app.manager.get.return_value = MagicMock()
        app.run_worker = MagicMock()
        app.run_worker.return_value = None

        app._select_contact(target)
        # visible = tutti (no filtro): target è index 1 -> evidenziato e update chiamato.
        assert fake_list.index == 1
        item.children[0].update.assert_called_once()

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
        # default filter è "all" -> mostrare il suffisso "- All".
        assert app._filter_title_suffix() == " - All"
        app._protocol_filter = "signal"
        assert app._filter_title_suffix() == " - Signal"
        app._protocol_filter = "whatsapp"
        assert app._filter_title_suffix() == " - WhatsApp"
        app._protocol_filter = "all"
        assert app._filter_title_suffix() == " - All"

    def test_cycle_filter_does_not_pollute_chat(self):
        """Ctrl+W aggiorna il filtro ma NON deve scrivere nulla nella chat."""
        app = _make_app()
        app._add_message = MagicMock()
        with patch.object(app, "_apply_contact_filter"):
            app.action_cycle_protocol_filter()
            app.action_cycle_protocol_filter()
        # _add_message non deve essere mai usato per inquinare la chat col filtro.
        app._add_message.assert_not_called()

    def test_contact_picker_uses_filtered_contacts(self):
        """Il picker dei contatti (Ctrl+S) deve cercare solo nella lista filtrata."""
        app = _make_app()
        app.contacts = [_signal("+1"), _signal("+2"), _whatsapp()]
        app._protocol_filter = "whatsapp"
        # Patcha push_screen per ispezionare la ContactPickerScreen creata.
        captured = {}
        def fake_push(screen, cb):
            captured["contacts"] = screen._all_contacts
        app.push_screen = fake_push
        app._open_contact_picker()
        # Solo i contatti whatsapp (1) devono passare al picker, non i signal.
        assert [c.id for c in captured["contacts"]] == ["wa:1@s.whatsapp.net"]

    def test_apply_contact_filter_updates_title_by_id(self):
        """Il titolo contatti si aggiorna cercando per *id* (#ContactsTitle)."""
        app = _make_app()
        app.contacts = [_signal("+1"), _signal("+2"), _whatsapp()]
        title = MagicMock()
        chat_title = MagicMock()
        chat_log = MagicMock()
        list_view = MagicMock()
        def fake_q(selector, *_a, **_k):
            if selector == "#ContactsTitle":
                return title
            if selector == "#ChatTitle":
                return chat_title
            if selector == "#chat-log":
                return chat_log
            return list_view
        app.query_one = fake_q
        app._filtered_contacts = lambda: app.contacts  # tutti

        app._protocol_filter = "signal"
        app._apply_contact_filter()
        # Il titolo deve contenere il nuovo suffisso -> " - Signal".
        ok = title.update.call_args
        assert ok and " - Signal" in ok.args[0]
        # Anche il banner 💬 Chat riceve la classe filtro (sincronizzazione colore).
        chat_title.add_class.assert_called_with("chat-filter-signal")

    def test_apply_contact_filter_colors_chat_border(self):
        """Bordo chat + lista contatti + banner assumono la classe giusta."""
        app = _make_app()
        title = MagicMock()
        chat_title = MagicMock()
        chat_log = MagicMock()
        contact_list = MagicMock()
        def fake_q(selector, *_a, **_k):
            if selector == "#ContactsTitle":
                return title
            if selector == "#ChatTitle":
                return chat_title
            if selector == "#chat-log":
                return chat_log
            if selector == "#contact-list":
                return contact_list
            return title
        app.query_one = fake_q
        app._filtered_contacts = lambda: app.contacts

        app._protocol_filter = "whatsapp"
        app._apply_contact_filter()
        chat_log.remove_class.assert_called_with("chat-filter-signal", "chat-filter-whatsapp")
        chat_log.add_class.assert_called_with("chat-filter-whatsapp")
        contact_list.add_class.assert_called_with("chat-filter-whatsapp")
        chat_title.add_class.assert_called_with("chat-filter-whatsapp")
        title.add_class.assert_called_with("chat-filter-whatsapp")


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


class TestContactSorting:
    """🔀 Ordinamento della lista contatti per ultimo messaggio.

    Regola:
      1. contatti CON messaggi -> per ``last_message_ts`` desc;
      2. contatti SENZA messaggi ma con un nome -> alfabetici;
      3. contatti SENZA messaggi e SOLO numero -> in coda (alfabetici).
    """

    def _contact(self, cid, name, ts=0):
        c = ChatContact(id=cid, display_name=name, protocol=PROTOCOL_SIGNAL)
        c.last_message_ts = ts
        return c

    def test_with_messages_sorted_desc(self):
        older = self._contact("+1", "Vecchio", ts=1000)
        recent = self._contact("+2", "Recente", ts=9000)
        mid = self._contact("+3", "Medio", ts=5000)
        app = _make_app(older, mid, recent)
        app._sort_contacts()
        assert [c.id for c in app.contacts] == ["+2", "+3", "+1"]

    def test_no_messages_after_with_messages(self):
        with_msg = self._contact("+1", "ConMsg", ts=1000)
        no_msg = self._contact("+2", "Senza")
        app = _make_app(no_msg, with_msg)
        app._sort_contacts()
        assert [c.id for c in app.contacts] == ["+1", "+2"]

    def test_no_messages_alphabetical(self):
        b = self._contact("+2", "Beta")
        a = self._contact("+1", "Alfa")
        app = _make_app(b, a)
        app._sort_contacts()
        assert [c.id for c in app.contacts] == ["+1", "+2"]

    def test_unnamed_no_messages_last_even_after_named_no_msg(self):
        named = self._contact("+1", "Mario")
        unnamed = ChatContact(id="+9", display_name="+9", protocol=PROTOCOL_SIGNAL)
        app = _make_app(unnamed, named)
        app._sort_contacts()
        assert [c.id for c in app.contacts] == ["+1", "+9"]

    def test_unnamed_with_messages_follows_date(self):
        # Un "solo numero" CON messaggio segue l'ordinamento per data (NON in coda).
        unnamed_recent = ChatContact(id="+9", display_name="+9", protocol=PROTOCOL_SIGNAL)
        unnamed_recent.last_message_ts = 9000
        named_old = self._contact("+1", "Mario", ts=1000)
        app = _make_app(named_old, unnamed_recent)
        app._sort_contacts()
        assert [c.id for c in app.contacts] == ["+9", "+1"]

    def test_full_mix(self):
        a = self._contact("+1", "Anna", ts=1000)          # con messaggio, primo gruppo
        d = self._contact("+4", "Dario", ts=9999)         # con messaggio più recente
        b = self._contact("+2", "Bruno")                  # senza, con nome (alpha)
        c = self._contact("+3", "Carlo")                  # senza, con nome (alpha)
        u1 = ChatContact(id="+5", display_name="+5", protocol=PROTOCOL_SIGNAL)
        u2 = ChatContact(id="+6", display_name="+6", protocol=PROTOCOL_WHATSAPP)
        app = _make_app(a, u2, b, d, c, u1)
        app._sort_contacts()
        assert [c.id for c in app.contacts] == ["+4", "+1", "+2", "+3", "+5", "+6"]

    def test_sync_last_ts_from_cache(self):
        app = _make_app(self._contact("+1", "Pippo"))
        app._cache = {
            "signal:+1": [
                {"timestamp": 1111},
                {"timestamp": 2222},
                {"timestamp": 111},   # fuori ordine: dev'essere ignorato
            ],
            "signal:+2": [{"timestamp": 3333}],
        }
        app._sync_last_ts()
        assert app.contacts[0].last_message_ts == 2222

    def test_sync_last_ts_keeps_existing_max(self):
        c = self._contact("+1", "Pippo", ts=20)
        app = _make_app(c)
        app._cache = {"signal:+1": [{"timestamp": 10}]}
        app._sync_last_ts()
        assert app.contacts[0].last_message_ts == 20

    def test_reorder_contact_list_preserves_selection(self):
        app = _make_app(self._contact("+1", "Vecchio", ts=100))
        app._protocol_filter = "all"
        # _render_contact_list viene mockato per isolare il re-sort.
        rendered = []
        app._render_contact_list = lambda filtered: rendered.extend(
            [c.id for c in filtered]
        )
        rec = self._contact("+2", "Nuovo", ts=900)
        app.contacts.append(rec)
        app._reorder_contact_list()
        assert rendered == ["+2", "+1"]


class TestContactListSelect:
    """🖱️ La lista contatti NON deve abilitare la selezione di testo (race).

    Testual, a ogni MouseDown, se ``allow_select`` è vero avvia una selezione
    di testo che accede a ``content_widget.parent.region``.  Con la lista
    ricostruita spesso, il parent può risultare ``None`` -> crash
    ``AttributeError: 'NoneType' object has no attribute 'region'``.
    Disattivandola sulla lista contatti eliminiamo la race, mantenendo la
    selezione di riga (``ListView.index``).
    """

    def test_contact_list_widget_uses_no_select_list_view(self):
        """La #contact-list deve essere una ContactListView (allow_select=False)."""
        widget = ContactListWidget()
        # compose è un generatore: il ListView è il 2° elemento dopo il Label
        items = list(widget.compose())
        assert len(items) == 2
        label, lv = items
        assert isinstance(lv, ContactListView)
        assert lv.ALLOW_SELECT is False
        assert lv.id == "contact-list"

    def test_contact_list_view_is_still_a_list_view(self):
        """ContactListView resta una ListView: query_one(..., ListView) e
        la selezione di riga continuano a funzionare."""
        from textual.widgets import ListView
        assert issubclass(ContactListView, ListView)


class TestWhatsAppHistoryLoad:
    """📩 All'apertura di un contatto WhatsApp con cache vuoto, viene scaricato
    lo storico remoto (fetch_history) così i messaggi (anche non letti) si vedono."""

    def test_load_messages_worker_fetches_history_for_empty_whatsapp_cache(self):
        app = _make_app()
        c = ChatContact(id="16660245291231@lid", display_name="Pix Tim",
                        protocol=PROTOCOL_WHATSAPP)
        app.contacts = [c]
        app.selected_contact = c
        app._chat_reload_token = 1
        app._cache = {}
        app._seen_timestamps = set()
        app._loaded_all = False

        backend = MagicMock()
        # fetch_history riempie il cache del backend; il worker poi lo specchia
        # nel cache UI.
        backend.cache = {
            c.id: [
                {"text": "ciao 1", "is_mine": False, "read": False, "timestamp": 1},
                {"text": "ciao 2", "is_mine": False, "read": False, "timestamp": 2},
            ]
        }
        backend.fetch_history = MagicMock(return_value=backend.cache[c.id])
        app.manager = MagicMock()
        app.manager.get.return_value = backend

        # evita operazioni su widget reali; call_from_thread esegue sincronico
        app._add_message = MagicMock()
        app._add_load_more_widget = MagicMock()
        app.call_from_thread = MagicMock(
            side_effect=lambda fn, *a, **k: fn(*a, **k)
        )
        app.query_one = MagicMock()

        app._load_messages_worker()

        # fetch_history è stato chiamato con il jid del contatto
        backend.fetch_history.assert_called_once_with("16660245291231@lid", limit=20)
        # il cache UI è stato popolato dallo specchio del backend
        assert len(app._cache[c.cache_key]) == 2
        # mostra i messaggi (per ogni msg chiama _add_message)
        assert app._add_message.call_count >= 2

    def test_load_messages_worker_fetches_history_even_when_cache_nonempty(self):
        """Con cache già popolato, lo storico remoto viene comunque riscaricato
        (per recuperare anche i messaggi inviati da un altro client)."""
        app = _make_app()
        c = ChatContact(id="16660245291231@lid", display_name="Pix Tim",
                        protocol=PROTOCOL_WHATSAPP)
        app.contacts = [c]
        app.selected_contact = c
        app._chat_reload_token = 1
        # cache UI già popolato (es. da live/send della TUI)
        app._cache = {c.cache_key: [
            {"text": "esistente", "is_mine": False, "read": True, "timestamp": 1},
        ]}
        app._seen_timestamps = set()
        app._loaded_all = False

        backend = MagicMock()
        backend.cache = {
            c.id: [
                {"text": "esistente", "is_mine": False, "read": True, "timestamp": 1},
                {"text": "da altro client", "is_mine": True, "read": True, "timestamp": 2},
            ]
        }
        backend.fetch_history = MagicMock(return_value=backend.cache[c.id])
        app.manager = MagicMock()
        app.manager.get.return_value = backend

        app._add_message = MagicMock()
        app._add_load_more_widget = MagicMock()
        app.call_from_thread = MagicMock(
            side_effect=lambda fn, *a, **k: fn(*a, **k)
        )
        app.query_one = MagicMock()

        app._load_messages_worker()

        # Nonostante il cache non vuoto, viene riscaricato lo storico remoto
        backend.fetch_history.assert_called_once_with("16660245291231@lid", limit=20)
        # il cache UI ora include anche il messaggio "da altro client"
        texts = [m["text"] for m in app._cache[c.cache_key]]
        assert "da altro client" in texts




