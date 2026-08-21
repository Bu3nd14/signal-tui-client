"""
Regression tests for contact_picker.py — search_contacts helper.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from textual.widgets import Input, ListView, Static

# Ensure the project root is on sys.path so we can import the modules
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from contact_picker import (
    BackendChoiceScreen,
    ContactPickerScreen,
    PickerEntry,
    contact_sort_key,
    entry_default_contact,
    group_by_person,
    normalize_phone,
    search_contacts,
    search_entries,
)
from models import (
    PROTOCOL_SIGNAL,
    PROTOCOL_TELEGRAM,
    PROTOCOL_WHATSAPP,
    ChatContact,
)


def _make_contacts() -> list[ChatContact]:
    """Build a small sample contact list for tests."""
    return [
        ChatContact(
            id="+391234567890", display_name="Alice Rossi", protocol=PROTOCOL_SIGNAL
        ),
        ChatContact(
            id="+391234567891", display_name="Bob Bianchi", protocol=PROTOCOL_SIGNAL
        ),
        ChatContact(
            id="+391234567892", display_name="Carla Verdi", protocol=PROTOCOL_SIGNAL
        ),
        # display_name empty → search falls back to the id (number)
        ChatContact(id="+391234567893", display_name="", protocol=PROTOCOL_SIGNAL),
    ]


def _contact(
    cid: str,
    name: str,
    protocol: str,
    *,
    phone: str = "",
    ts: int = 0,
    **extras: object,
) -> ChatContact:
    """Build a ``ChatContact`` with normalized phone / ts and extra markers."""
    ex = dict(extras)
    if phone:
        ex["phone"] = phone
    if ts:
        ex["last_message_ts"] = ts
    return ChatContact(id=cid, display_name=name, protocol=protocol, extras=ex)


class TestSearchContacts:
    """🔍 Ricerca contatti per nome o numero."""

    def test_search_by_name(self):
        """Cerca 'alice' → deve trovare Alice Rossi."""
        results = search_contacts(_make_contacts(), "alice")
        assert len(results) == 1
        assert results[0].display_name == "Alice Rossi"

    def test_search_case_insensitive(self):
        """Cerca 'ALICE' (maiuscolo) → stesso risultato di 'alice'."""
        lower = search_contacts(_make_contacts(), "alice")
        upper = search_contacts(_make_contacts(), "ALICE")
        assert [c.id for c in lower] == [c.id for c in upper]

    def test_search_by_number(self):
        """Cerca parte del numero → deve trovare il contatto."""
        results = search_contacts(_make_contacts(), "567890")
        assert len(results) == 1
        assert results[0].id == "+391234567890"

    def test_search_partial_name(self):
        """Cerca 'ross' → deve trovare Alice Rossi (substring)."""
        results = search_contacts(_make_contacts(), "ross")
        assert len(results) == 1
        assert results[0].display_name == "Alice Rossi"

    def test_search_no_results(self):
        """Cerca stringa inesistente → lista vuota."""
        results = search_contacts(_make_contacts(), "zzzznonexistentxxxx")
        assert results == []

    def test_search_empty_query(self):
        """Query vuota → restituisce tutti i contatti."""
        contacts = _make_contacts()
        results = search_contacts(contacts, "")
        assert results == contacts

    def test_search_whitespace_query(self):
        """Query di soli spazi → trattata come vuota."""
        contacts = _make_contacts()
        results = search_contacts(contacts, "   ")
        assert results == contacts

    def test_search_max_results(self):
        """Verifica il limite massimo di risultati."""
        contacts = _make_contacts()
        results = search_contacts(contacts, "", max_results=2)
        assert len(results) == 2

    def test_search_contact_without_name(self):
        """Contatto senza display name → match sul numero (id)."""
        results = search_contacts(_make_contacts(), "567893")
        assert len(results) == 1
        assert results[0].id == "+391234567893"

    def test_search_max_results_break(self):
        """max_results interrompe il loop quando il limite è raggiunto."""
        # "i" matches Alice Rossi, Bob Bianchi e Carla Verdi (3 contatti).
        results = search_contacts(_make_contacts(), "i", max_results=2)
        assert len(results) == 2


class TestContactPickerScreen:
    """🖥️ ContactPickerScreen modal (headless, integration)."""

    @pytest.mark.integration
    async def test_screen_compose_and_render(self, app_for_test):
        """The screen mounts its widgets and renders the contacts."""
        contacts = _make_contacts()
        screen = ContactPickerScreen(contacts)
        async with app_for_test.run_test() as pilot:
            await pilot.pause()
            await app_for_test.push_screen(screen)
            await pilot.pause()

            assert isinstance(app_for_test.screen, ContactPickerScreen)
            assert screen.query_one("#contact-search", Input) is not None
            assert screen.query_one("#contact-results", ListView) is not None
            assert screen.query_one("#contact-picker-title", Static) is not None
            assert screen.query_one("#contact-picker-footer", Static) is not None

            results_list = screen.query_one("#contact-results", ListView)
            assert len(results_list.children) == len(contacts)
            labels = [item.children[0].content for item in results_list.children]
            assert labels[0] == "📱 Alice Rossi"
            assert labels[1] == "📱 Bob Bianchi"

    @pytest.mark.integration
    async def test_screen_filters_on_input(self, app_for_test):
        """Typing in #contact-search filters the results."""
        contacts = _make_contacts()
        screen = ContactPickerScreen(contacts)
        async with app_for_test.run_test() as pilot:
            await pilot.pause()
            await app_for_test.push_screen(screen)
            await pilot.pause()

            search_input = screen.query_one("#contact-search", Input)
            search_input.value = "alice"
            await pilot.pause()

            assert screen._results == [contacts[0]]
            results_list = screen.query_one("#contact-results", ListView)
            assert len(results_list.children) == 1

    @pytest.mark.integration
    async def test_screen_select_enter(self, app_for_test):
        """Enter dismisses the screen with the highlighted contact."""
        contacts = _make_contacts()
        screen = ContactPickerScreen(contacts)
        received: dict[str, object] = {}

        def on_done(contact: object) -> None:
            received["value"] = contact

        async with app_for_test.run_test() as pilot:
            await pilot.pause()
            await app_for_test.push_screen(screen, on_done)
            await pilot.pause()

            results_list = screen.query_one("#contact-results", ListView)
            results_list.index = 0
            await pilot.press("enter")
            await pilot.pause()

            assert received["value"] is contacts[0]

    @pytest.mark.integration
    async def test_screen_select_click(self, app_for_test):
        """Clicking a result dismisses the screen with that contact."""
        contacts = _make_contacts()
        screen = ContactPickerScreen(contacts)
        received: dict[str, object] = {}

        def on_done(contact: object) -> None:
            received["value"] = contact

        async with app_for_test.run_test() as pilot:
            await pilot.pause()
            await app_for_test.push_screen(screen, on_done)
            await pilot.pause()

            results_list = screen.query_one("#contact-results", ListView)
            await pilot.click(results_list.children[1])
            await pilot.pause()

            assert received["value"] is contacts[1]

    @pytest.mark.integration
    async def test_screen_enter_without_selection(self, app_for_test):
        """Enter without a highlighted result does not dismiss the screen."""
        contacts = _make_contacts()
        screen = ContactPickerScreen(contacts)
        received: dict[str, object] = {}

        def on_done(contact: object) -> None:
            received["value"] = contact

        async with app_for_test.run_test() as pilot:
            await pilot.pause()
            await app_for_test.push_screen(screen, on_done)
            await pilot.pause()

            await pilot.press("enter")
            await pilot.pause()

            assert "value" not in received
            assert isinstance(app_for_test.screen, ContactPickerScreen)

    @pytest.mark.integration
    async def test_screen_selected_without_highlight(self, app_for_test):
        """A selected event with no highlighted row does not dismiss the screen."""
        screen = ContactPickerScreen(_make_contacts())
        received: dict[str, object] = {}

        def on_done(contact: object) -> None:
            received["value"] = contact

        async with app_for_test.run_test() as pilot:
            await pilot.pause()
            await app_for_test.push_screen(screen, on_done)
            await pilot.pause()

            results_list = screen.query_one("#contact-results", ListView)
            results_list.index = None
            screen.on_list_view_selected(MagicMock())
            await pilot.pause()

            assert "value" not in received

    @pytest.mark.integration
    async def test_screen_escape(self, app_for_test):
        """Escape dismisses the screen with None."""
        contacts = _make_contacts()
        screen = ContactPickerScreen(contacts)
        received: dict[str, object] = {}

        def on_done(contact: object) -> None:
            received["value"] = contact

        async with app_for_test.run_test() as pilot:
            await pilot.pause()
            await app_for_test.push_screen(screen, on_done)
            await pilot.pause()

            await pilot.press("escape")
            await pilot.pause()

            assert received["value"] is None

    def test_on_input_changed_ignores_other_inputs(self):
        """on_input_changed ignores events from inputs other than #contact-search."""
        screen = ContactPickerScreen(_make_contacts())
        event = MagicMock()
        event.input.id = "message-input"
        screen.on_input_changed(event)
        assert screen._results == []


class TestNormalizePhone:
    """📞 normalize_phone estrae le cifre (chiave di raggruppamento)."""

    def test_digits_only(self):
        assert normalize_phone("+39 333 123 4567") == "393331234567"

    def test_no_digits(self):
        assert normalize_phone("abc-@lid") == ""

    def test_empty(self):
        assert normalize_phone("") == ""


class TestGroupByPerson:
    """👥 group_by_person raggruppa la stessa persona su più backend."""

    def test_same_number_signal_and_whatsapp(self):
        sig = _contact("+393331234567", "Mario", PROTOCOL_SIGNAL, phone="393331234567")
        wa = _contact(
            "393331234567@c.us", "Mario", PROTOCOL_WHATSAPP, phone="393331234567"
        )
        entries = group_by_person([sig, wa])
        assert len(entries) == 1
        assert entries[0].key == "phone:393331234567"
        assert set(entries[0].members) == {PROTOCOL_SIGNAL, PROTOCOL_WHATSAPP}

    def test_signal_and_whatsapp_c_us_group_without_explicit_phone(self):
        """Un JID @c.us deriva il telefono dall'id → si fonde con Signal."""
        sig = _contact("+393331234567", "Mario", PROTOCOL_SIGNAL)
        wa = _contact("393331234567@c.us", "Mario", PROTOCOL_WHATSAPP)
        entries = group_by_person([sig, wa])
        assert len(entries) == 1
        assert entries[0].key == "phone:393331234567"
        assert set(entries[0].members) == {PROTOCOL_SIGNAL, PROTOCOL_WHATSAPP}

    def test_signal_and_resolved_lid_group_via_extras_phone(self):
        """Un @lid risolto (extras["phone"]) si fonde con Signal."""
        sig = _contact("+393331234567", "Mario", PROTOCOL_SIGNAL)
        wa = _contact("139153@lid", "Mario", PROTOCOL_WHATSAPP, phone="393331234567")
        entries = group_by_person([sig, wa])
        assert len(entries) == 1
        assert entries[0].key == "phone:393331234567"
        assert set(entries[0].members) == {PROTOCOL_SIGNAL, PROTOCOL_WHATSAPP}

    def test_unresolved_lid_without_phone_is_separate(self):
        """Un @lid senza phone resta standalone (raw:), Signal a parte."""
        sig = _contact("+393331234567", "Mario", PROTOCOL_SIGNAL)
        wa = _contact("139153@lid", "Mario", PROTOCOL_WHATSAPP)
        entries = group_by_person([sig, wa])
        assert len(entries) == 2
        assert {e.key for e in entries} == {
            "phone:393331234567",
            "raw:whatsapp:139153@lid",
        }

    def test_telegram_without_phone_is_single(self):
        tg = _contact("123456789", "Mamma Vod", PROTOCOL_TELEGRAM)
        entries = group_by_person([tg])
        assert len(entries) == 1
        assert entries[0].key == "raw:telegram:123456789"

    def test_unresolved_lid_is_single(self):
        lid = _contact("22098@lid", "X", PROTOCOL_WHATSAPP, lid_unresolved=True)
        entries = group_by_person([lid])
        assert entries[0].key == "raw:whatsapp:22098@lid"

    def test_groups_never_grouped(self):
        g1 = _contact("123@g.us", "G1", PROTOCOL_WHATSAPP, is_group=True)
        g2 = _contact("456@g.us", "G2", PROTOCOL_WHATSAPP, is_group=True)
        entries = group_by_person([g1, g2])
        assert len(entries) == 2
        assert all(e.key.startswith("raw:") for e in entries)

    def test_telegram_numeric_id_is_not_a_phone(self):
        tg = _contact("393331234567", "Mamma Vod", PROTOCOL_TELEGRAM)
        sig = _contact("+393331234567", "Mario", PROTOCOL_SIGNAL, phone="393331234567")
        entries = group_by_person([tg, sig])
        assert len(entries) == 2

    def test_display_name_is_best_name(self):
        sig = _contact("+391", "+391", PROTOCOL_SIGNAL, phone="391")  # solo numero
        wa = _contact("391@c.us", "Mario", PROTOCOL_WHATSAPP, phone="391")
        entry = group_by_person([sig, wa])[0]
        assert entry.display_name == "Mario"


class TestEntryDefaultContact:
    """⭐ entry_default_contact sceglie il membro più recente (o il preferred)."""

    def test_most_recent_wins(self):
        sig = _contact("+391", "A", PROTOCOL_SIGNAL, phone="391", ts=100)
        wa = _contact("391@c.us", "B", PROTOCOL_WHATSAPP, phone="391", ts=200)
        entry = group_by_person([sig, wa])[0]
        assert entry_default_contact(entry) is wa

    def test_tiebreak_signal_first(self):
        sig = _contact("+391", "A", PROTOCOL_SIGNAL, phone="391", ts=100)
        wa = _contact("391@c.us", "B", PROTOCOL_WHATSAPP, phone="391", ts=100)
        tg = _contact("391", "C", PROTOCOL_TELEGRAM, phone="391", ts=100)
        entry = group_by_person([wa, tg, sig])[0]
        assert entry_default_contact(entry) is sig

    def test_preferred_backend_override(self):
        sig = _contact("+391", "A", PROTOCOL_SIGNAL, phone="391", ts=300)
        tg = _contact("391", "C", PROTOCOL_TELEGRAM, phone="391", ts=10)
        entry = group_by_person([sig, tg])[0]
        with patch(
            "contact_picker.get_picker_preferred_backend", return_value="telegram"
        ):
            assert entry_default_contact(entry) is tg


class TestSearchEntries:
    """🔍 search_entries matcha nome, id e phone di ogni membro."""

    def _entries(self) -> list[PickerEntry]:
        return [
            group_by_person(
                [
                    _contact(
                        "393331234567@c.us",
                        "Mario Rossi",
                        PROTOCOL_WHATSAPP,
                        phone="393331234567",
                    )
                ]
            )[0],
            group_by_person(
                [
                    _contact(
                        "+39111222333", "Luigi", PROTOCOL_SIGNAL, phone="39111222333"
                    )
                ]
            )[0],
        ]

    def test_match_name(self):
        assert search_entries(self._entries(), "mario") == [self._entries()[0]]

    def test_match_id(self):
        assert search_entries(self._entries(), "567@c") == [self._entries()[0]]

    def test_match_phone(self):
        assert search_entries(self._entries(), "11222") == [self._entries()[1]]

    def test_empty_query_cap(self):
        entries = [
            group_by_person(
                [_contact(f"+39{i:09d}", "N", PROTOCOL_SIGNAL, phone=f"39{i:09d}")]
            )[0]
            for i in range(60)
        ]
        assert len(search_entries(entries, "")) == 50


class TestSearchContactsPhone:
    """🔍 search_contacts esteso al campo phone."""

    def test_match_phone(self):
        c = _contact(
            "393331234567@c.us", "Mario", PROTOCOL_WHATSAPP, phone="393331234567"
        )
        assert search_contacts([c], "3331") == [c]

    def test_no_phone_no_match(self):
        c = _contact("123@c.us", "Mario", PROTOCOL_WHATSAPP)
        assert search_contacts([c], "3331") == []


class TestContactSortKey:
    """↕️ contact_sort_key: recency → alfabetico → solo numero in coda."""

    def test_order(self):
        recent = _contact("+391", "Recent", PROTOCOL_SIGNAL, ts=200)
        older = _contact("+392", "Older", PROTOCOL_SIGNAL, ts=100)
        named_a = _contact("+393", "Anna", PROTOCOL_SIGNAL)
        named_b = _contact("+394", "Bianca", PROTOCOL_SIGNAL)
        number_only = _contact("+395", "+395", PROTOCOL_SIGNAL)  # name == id
        contacts = [number_only, named_b, recent, named_a, older]
        contacts.sort(key=contact_sort_key)
        assert contacts == [recent, older, named_a, named_b, number_only]


class TestPickerEntryLabel:
    """🏷️ Label di riga: emoji concatenate per entry multi-membro."""

    def test_single_member_label(self):
        c = _contact("+391", "Alice", PROTOCOL_SIGNAL)
        entry = group_by_person([c])[0]
        assert ContactPickerScreen._entry_label(entry) == "📱 Alice"

    def test_multi_member_label(self):
        sig = _contact("+391", "Mario", PROTOCOL_SIGNAL, phone="391", ts=100)
        wa = _contact("391@c.us", "Mario", PROTOCOL_WHATSAPP, phone="391", ts=200)
        entry = group_by_person([sig, wa])[0]
        assert ContactPickerScreen._entry_label(entry) == "💬📱 Mario"


class TestContactPickerScreenM4:
    """🖥️ Milestone 4 — loading, set_contacts, BackendChoiceScreen, Ctrl+W."""

    @pytest.mark.integration
    async def test_loading_then_set_contacts_with_query(self, app_for_test):
        """Fast path in loading → set_contacts → lista completa; query riapplicata."""
        fast = _contact("+1", "Fast", PROTOCOL_SIGNAL)
        later = _contact("+2", "Later", PROTOCOL_SIGNAL)
        screen = ContactPickerScreen([fast], loading=True)
        async with app_for_test.run_test() as pilot:
            await pilot.pause()
            await app_for_test.push_screen(screen)
            await pilot.pause()

            footer = screen.query_one("#contact-picker-footer", Static)
            assert "Caricamento" in str(footer.content)
            assert len(screen.query_one("#contact-results", ListView).children) == 1

            # query digitata durante il load (nessun match sul fast path)
            screen.query_one("#contact-search", Input).value = "later"
            await pilot.pause()
            assert screen._results == []

            # arriva la rubrica completa → la query si riapplica ai dati nuovi
            screen.set_contacts([fast, later])
            await pilot.pause()
            assert "Caricamento" not in str(footer.content)
            assert len(screen.query_one("#contact-results", ListView).children) == 1
            assert screen._results == [later]

    @pytest.mark.integration
    async def test_multi_entry_enter_opens_backend_choice(self, app_for_test):
        """Enter su entry multi-membro → BackendChoiceScreen pre-selezionato."""
        sig = _contact(
            "+393331234567", "Mario", PROTOCOL_SIGNAL, phone="393331234567", ts=100
        )
        wa = _contact(
            "393331234567@c.us",
            "Mario",
            PROTOCOL_WHATSAPP,
            phone="393331234567",
            ts=200,
        )
        screen = ContactPickerScreen([sig, wa])
        async with app_for_test.run_test() as pilot:
            await pilot.pause()
            await app_for_test.push_screen(screen)
            await pilot.pause()

            screen.query_one("#contact-results", ListView).index = 0
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app_for_test.screen, BackendChoiceScreen)
            choice = app_for_test.screen
            choice_list = choice.query_one("#backend-choice-list", ListView)
            assert choice_list.index == 0
            assert choice._members[0] is wa  # più recente (ts 200) pre-selezionato

            # Esc nel sub-modale torna al picker (non chiude tutto)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app_for_test.screen, ContactPickerScreen)

    @pytest.mark.integration
    async def test_backend_choice_enter_dismisses_picker(self, app_for_test):
        """Enter nel BackendChoiceScreen → dismiss del picker col contatto scelto."""
        sig = _contact(
            "+393331234567", "Mario", PROTOCOL_SIGNAL, phone="393331234567", ts=100
        )
        wa = _contact(
            "393331234567@c.us",
            "Mario",
            PROTOCOL_WHATSAPP,
            phone="393331234567",
            ts=200,
        )
        screen = ContactPickerScreen([sig, wa])
        received: dict[str, object] = {}

        def on_done(contact: object) -> None:
            received["value"] = contact

        async with app_for_test.run_test() as pilot:
            await pilot.pause()
            await app_for_test.push_screen(screen, on_done)
            await pilot.pause()

            screen.query_one("#contact-results", ListView).index = 0
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app_for_test.screen, BackendChoiceScreen)

            await pilot.press("enter")
            await pilot.pause()
            assert received["value"] is wa

    @pytest.mark.integration
    async def test_ctrl_w_filters_and_selects_direct_member(self, app_for_test):
        """Ctrl+W interno filtra client-side e forza il membro del protocollo."""
        sig = _contact("+391", "SignalOnly", PROTOCOL_SIGNAL)
        wa = _contact("wa@c.us", "WaOnly", PROTOCOL_WHATSAPP)
        screen = ContactPickerScreen([sig, wa])
        received: dict[str, object] = {}

        def on_done(contact: object) -> None:
            received["value"] = contact

        async with app_for_test.run_test() as pilot:
            await pilot.pause()
            await app_for_test.push_screen(screen, on_done)
            await pilot.pause()

            assert len(screen.query_one("#contact-results", ListView).children) == 2

            await pilot.press("ctrl+w")
            await pilot.pause()
            assert screen._protocol_filter == "signal"
            assert len(screen.query_one("#contact-results", ListView).children) == 1

            screen.query_one("#contact-results", ListView).index = 0
            await pilot.press("enter")
            await pilot.pause()
            assert received["value"] is sig

    def test_set_contacts_ignored_when_not_mounted(self):
        """set_contacts su uno schermo mai montato è un no-op (guard is_mounted)."""
        fast = _contact("+1", "Fast", PROTOCOL_SIGNAL)
        screen = ContactPickerScreen([fast])
        screen.set_contacts([_contact("+2", "Late", PROTOCOL_SIGNAL)])
        assert screen._all_contacts == [fast]

    @pytest.mark.integration
    async def test_stale_worker_token_ignored(self, app_for_test):
        """Worker con token stantio (dopo dismiss) → set_contacts NON applicato."""
        fast = _contact("+1", "Fast", PROTOCOL_SIGNAL)
        screen = ContactPickerScreen([fast], loading=True)
        async with app_for_test.run_test() as pilot:
            await pilot.pause()
            await app_for_test.push_screen(screen)
            await pilot.pause()

            app_for_test.manager.list_address_book_sync.return_value = [
                _contact("+2", "Late", PROTOCOL_SIGNAL)
            ]
            app_for_test.manager.address_book_errors = {}
            app_for_test.call_from_thread = MagicMock(
                side_effect=lambda fn, *a, **k: fn(*a, **k)
            )

            # token valido all'apertura, poi invalidato dal dismiss
            app_for_test._address_book_token = 1
            app_for_test._address_book_token = 2

            app_for_test._address_book_worker(1, None, screen)
            await pilot.pause()
            assert screen._all_contacts == [fast]

    @pytest.mark.integration
    async def test_current_worker_token_applies(self, app_for_test):
        """Worker con token corrente → set_contacts applicato."""
        fast = _contact("+1", "Fast", PROTOCOL_SIGNAL)
        late = _contact("+2", "Late", PROTOCOL_SIGNAL)
        screen = ContactPickerScreen([fast], loading=True)
        async with app_for_test.run_test() as pilot:
            await pilot.pause()
            await app_for_test.push_screen(screen)
            await pilot.pause()

            app_for_test.manager.list_address_book_sync.return_value = [fast, late]
            app_for_test.manager.address_book_errors = {}
            app_for_test.call_from_thread = MagicMock(
                side_effect=lambda fn, *a, **k: fn(*a, **k)
            )

            app_for_test._address_book_token = 1
            app_for_test._address_book_worker(1, None, screen)
            await pilot.pause()
            assert screen._all_contacts == [fast, late]
            assert not screen._loading
