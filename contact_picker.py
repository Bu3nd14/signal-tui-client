"""
Contact Picker for Signal TUI Client.

Provides:
- ``ContactPickerScreen`` — a modal screen with a search bar and a live-updating
  list of contacts.  The user types to filter, navigates with ↑/↓, and presses
  Enter to select a contact (dismissing the screen with the chosen ``Contact``).
- ``search_contacts`` — a pure helper that filters a list of contacts by name
  or number (case-insensitive), used both by the screen and by tests.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, ListView, ListItem, Label, Static

from models import ChatContact, protocol_emoji

logger = logging.getLogger(__name__)


# ─── Search helper ───────────────────────────────────────────────────────────

def search_contacts(contacts: list[ChatContact], query: str, max_results: int = 50) -> list[ChatContact]:
    """Filter *contacts* by *query* (case-insensitive) on name or id.

    A contact matches if the query is a substring of its display name or its
    contact id (e.g. phone number).  The query is stripped; an empty query
    returns all contacts (up to ``max_results``).

    Parameters
    ----------
    contacts:
        The full list of contacts to search.
    query:
        The search string typed by the user.
    max_results:
        Maximum number of results to return (default 50).

    Returns
    -------
    list[ChatContact]
        The matching contacts, in their original order.
    """
    q = query.strip().lower()
    if not q:
        return contacts[:max_results]

    results: list[ChatContact] = []
    for contact in contacts:
        name = contact.display_name.lower()
        cid = contact.id.lower()
        if q in name or q in cid:
            results.append(contact)
            if len(results) >= max_results:
                break
    return results


# ─── Contact Picker Screen ───────────────────────────────────────────────────

class ContactPickerScreen(ModalScreen[ChatContact]):
    """Modal screen to search and select a contact.

    When the user selects a contact, the screen dismisses and returns the
    selected ``Contact`` via ``self.dismiss(contact)``.  Pressing Escape
    dismisses with ``None``.
    """

    DEFAULT_CSS = """
    ContactPickerScreen {
        align: center middle;
        background: $surface 80%;
    }

    #contact-picker-container {
        width: 60;
        height: 70%;
        min-height: 15;
        border: thick $accent;
        background: $surface;
        padding: 0 1;
    }

    #contact-picker-title {
        text-style: bold;
        text-align: center;
        padding: 0 0 1 0;
        color: $text;
    }

    #contact-search {
        width: 100%;
        height: 3;
        margin: 0 0 1 0;
    }

    #contact-results {
        height: 1fr;
        overflow-y: auto;
        overflow-x: hidden;
        padding: 0;
        scrollbar-gutter: stable;
    }

    #contact-results ListItem {
        padding: 0 1;
    }

    #contact-results ListItem:hover {
        background: $accent 20%;
    }

    #contact-results ListItem:focus {
        background: $accent 40%;
    }

    #contact-picker-footer {
        dock: bottom;
        height: 1;
        width: 100%;
        text-align: center;
        color: $text-muted;
        padding: 0 1;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss(None)", "Close", priority=True),
        Binding("enter", "select_contact", "Select", priority=True),
    ]

    def __init__(self, contacts: list[ChatContact]) -> None:
        """Initialise the picker with the full list of contacts.

        Parameters
        ----------
        contacts:
            The contacts to search through (usually ``app.contacts``).
        """
        super().__init__()
        self._all_contacts = contacts
        self._results: list[ChatContact] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="contact-picker-container"):
            yield Static("🔍 Search Contacts", id="contact-picker-title")
            yield Input(
                placeholder="Type to search contacts...",
                id="contact-search",
            )
            yield ListView(id="contact-results")
            yield Static(
                "Tab switch · ↑/↓ navigate · Enter/click select · Esc close",
                id="contact-picker-footer",
            )


    def on_mount(self) -> None:
        """Render the initial (unfiltered) list and focus the search input."""
        self._render_results(self._all_contacts)
        self.query_one("#contact-search", Input).focus()

    # ── Rendering ────────────────────────────────────────────────────────

    def _render_results(self, contacts: list[ChatContact]) -> None:
        """Fill the results ListView with the given contacts."""
        self._results = contacts
        results_list = self.query_one("#contact-results", ListView)
        results_list.clear()
        for contact in contacts:
            results_list.append(ListItem(Label(self._contact_label(contact))))

    @staticmethod
    def _contact_label(contact: ChatContact) -> str:
        """Build the display label for a contact in the results list."""
        return f"{protocol_emoji(contact.protocol)} {contact.display_name}"

    # ── Search ───────────────────────────────────────────────────────────

    def on_input_changed(self, event: Input.Changed) -> None:
        """Handle search input changes: re-filter the results list."""
        if event.input.id != "contact-search":
            return
        query = event.value
        self._render_results(search_contacts(self._all_contacts, query))

    # ── Selection ────────────────────────────────────────────────────────

    def action_select_contact(self) -> None:
        """Enter: select the highlighted contact and dismiss the picker."""
        results_list = self.query_one("#contact-results", ListView)
        index = results_list.index
        if index is not None and 0 <= index < len(self._results):
            self.dismiss(self._results[index])

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Click: select the clicked contact and dismiss the picker.

        Clicking a ``ListItem`` in the results list emits ``ListView.Selected``.
        The ``enter`` binding (priority) consumes the Enter key, so keyboard
        selection goes through ``action_select_contact`` while mouse clicks
        arrive here — the two paths never conflict.
        """
        results_list = self.query_one("#contact-results", ListView)
        index = results_list.index
        if index is not None and 0 <= index < len(self._results):
            self.dismiss(self._results[index])

