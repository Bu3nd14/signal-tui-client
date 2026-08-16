"""
Regression tests for contact_picker.py — search_contacts helper.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from textual.widgets import Input, ListView, Static

# Ensure the project root is on sys.path so we can import the modules
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from contact_picker import ContactPickerScreen, search_contacts
from models import PROTOCOL_SIGNAL, ChatContact


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
