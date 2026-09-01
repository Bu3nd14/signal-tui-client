"""
Contact Picker for Signal TUI Client.

Provides:
- ``ContactPickerScreen`` — a modal screen with a search bar and a live-updating
  list of contacts.  The user types to filter, navigates with ↑/↓, and presses
  Enter to select a contact (dismissing the screen with the chosen ``Contact``).
- ``BackendChoiceScreen`` — a small modal shown when a picker row aggregates the
  same person across multiple backends, letting the user pick the backend.
- ``search_contacts`` / ``search_entries`` — pure helpers that filter contacts
  or grouped entries by name, id or phone (case-insensitive).
- ``group_by_person`` / ``PickerEntry`` / ``entry_default_contact`` — the
  "same person across backends" grouping model used only inside the picker.
- ``contact_sort_key`` — the shared contact ordering key (extracted from
  ``tui/contacts.py`` so the picker reuses the exact same semantics).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, Static

from models import (
    PROTOCOL_SIGNAL,
    PROTOCOL_TELEGRAM,
    PROTOCOL_WHATSAPP,
    ChatContact,
    protocol_emoji,
)
from protocols.config import get_picker_max_results, get_picker_preferred_backend

logger = logging.getLogger(__name__)


# ─── Phone / grouping model ──────────────────────────────────────────────────


def normalize_phone(s: str) -> str:
    """Return only the digits of *s*, or ``""`` when there are none."""
    return "".join(ch for ch in s if ch.isdigit())


#: Deterministic tiebreak order for cross-backend grouping / default selection.
_PROTOCOL_PRIORITY: dict[str, int] = {
    PROTOCOL_SIGNAL: 0,
    PROTOCOL_WHATSAPP: 1,
    PROTOCOL_TELEGRAM: 2,
}


def _protocol_priority(protocol: str) -> int:
    return _PROTOCOL_PRIORITY.get(protocol, 3)


@dataclass
class PickerEntry:
    """One picker row: the same person across one or more backends."""

    key: str  # "phone:393331234567" | "raw:<protocol>:<id>"
    display_name: str  # best name among the members
    members: dict[str, ChatContact]  # protocol → contact


def _group_key(contact: ChatContact) -> str:
    """Compute the cross-backend grouping key for a single contact.

    Groups/channels and unresolved ``@lid`` contacts are never grouped by
    phone (they always get a unique ``raw:`` key).  Telegram numeric user ids
    are *not* phones and therefore never used as a grouping key.
    """
    extras = contact.extras
    if (
        extras.get("is_group")
        or extras.get("is_channel")
        or extras.get("lid_unresolved")
    ):
        return f"raw:{contact.protocol}:{contact.id}"

    phone = normalize_phone(extras.get("phone") or "")
    if not phone and (
        contact.protocol == PROTOCOL_SIGNAL
        or (contact.protocol == PROTOCOL_WHATSAPP and contact.id.endswith("@c.us"))
    ):
        phone = normalize_phone(contact.id)

    if phone:
        return f"phone:{phone}"
    return f"raw:{contact.protocol}:{contact.id}"


def _name_quality(contact: ChatContact) -> int:
    """Score a contact's display name: real name (2) > number-only (1) > empty (0)."""
    name = (contact.display_name or "").strip()
    if not name:
        return 0
    if name == contact.id:
        return 1
    return 2


def _best_display_name(members: list[ChatContact]) -> str:
    """Return the best display name among *members* (falls back to the id)."""
    ranked = sorted(
        members,
        key=lambda c: (-_name_quality(c), _protocol_priority(c.protocol)),
    )
    best = ranked[0]
    return best.display_name or best.id


def group_by_person(contacts: list[ChatContact]) -> list[PickerEntry]:
    """Group *contacts* into picker entries keyed by normalized phone.

    Two contacts of different backends sharing the same normalized phone
    number collapse into a single ``PickerEntry`` with one member per
    protocol.  Everything else (Telegram user ids, unresolved ``@lid``,
    groups, contacts without a phone) becomes a standalone entry.
    """
    groups: dict[str, PickerEntry] = {}
    order: list[str] = []
    for contact in contacts:
        key = _group_key(contact)
        if key not in groups:
            groups[key] = PickerEntry(key=key, display_name="", members={})
            order.append(key)
        groups[key].members[contact.protocol] = contact

    entries: list[PickerEntry] = []
    for key in order:
        entry = groups[key]
        entry.display_name = _best_display_name(list(entry.members.values()))
        entries.append(entry)
    return entries


def entry_default_contact(entry: PickerEntry) -> ChatContact:
    """Return the default contact for *entry*.

    Default is the member with the highest ``last_message_ts`` (most recent),
    tiebroken deterministically by ``signal > whatsapp > telegram``.  When the
    ``picker_preferred_backend`` config is set and present among the members,
    that backend wins regardless of recency.
    """
    preferred = get_picker_preferred_backend()
    if preferred and preferred in entry.members:
        return entry.members[preferred]
    return max(
        entry.members.values(),
        key=lambda c: ((c.last_message_ts or 0), -_protocol_priority(c.protocol)),
    )


# ─── Shared contact ordering ─────────────────────────────────────────────────


def contact_sort_key(c: ChatContact) -> tuple:
    """Order contacts: most recent messages first, then alphabetical, numbers last.

    Extracted from ``ContactListMixin._contact_sort_key`` so the picker reuses
    the exact same ordering semantics (groups: (1) with messages by
    ``-last_message_ts``; (2) without messages but named, alphabetical;
    (3) number-only contacts, last).
    """
    ts = c.last_message_ts or 0
    name = (c.display_name or "").lower()
    unnamed = True
    if c.display_name and c.display_name != c.id:
        unnamed = False
    has_messages = ts > 0
    return (
        not has_messages,
        -ts,
        1 if (not has_messages and unnamed) else 0,
        name,
        c.id,
    )


# ─── Search helpers ──────────────────────────────────────────────────────────


def search_contacts(
    contacts: list[ChatContact], query: str, max_results: int = 50
) -> list[ChatContact]:
    """Filter *contacts* by *query* (case-insensitive) on name, id or phone.

    A contact matches if the query is a substring of its display name, its
    contact id (e.g. phone number) or its normalized ``extras["phone"]``.
    The query is stripped; an empty query returns all contacts (up to
    ``max_results``).

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
        phone = contact.phone.lower()
        if q in name or q in cid or q in phone:
            results.append(contact)
            if len(results) >= max_results:
                break
    return results


def search_entries(
    entries: list[PickerEntry], query: str, max_results: int = 50
) -> list[PickerEntry]:
    """Filter *entries* by *query* (case-insensitive) on any member's fields.

    An entry matches if the query is a substring of the entry display name, or
    of the id or phone of **any** of its members.  Empty query returns all
    entries (capped at ``max_results``).
    """
    q = query.strip().lower()
    if not q:
        return entries[:max_results]

    results: list[PickerEntry] = []
    for entry in entries:
        haystacks = [entry.display_name.lower()]
        for member in entry.members.values():
            haystacks.append(member.id.lower())
            haystacks.append(member.phone.lower())
        if any(q in h for h in haystacks):
            results.append(entry)
            if len(results) >= max_results:
                break
    return results


def _relative_time(timestamp_ms: int) -> str:
    """Return a compact relative time for a millisecond timestamp, or ``"mai"``."""
    if not timestamp_ms:
        return "mai"
    delta_s = max(0, int(time.time() * 1000) - int(timestamp_ms)) // 1000
    if delta_s < 60:
        return f"{delta_s}s"
    minutes = delta_s // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}g"


# ─── Contact Picker Screen ───────────────────────────────────────────────────


class ContactPickerScreen(ModalScreen[ChatContact]):
    """Modal screen to search and select a contact.

    When the user selects a contact, the screen dismisses and returns the
    selected ``Contact`` via ``self.dismiss(contact)``.  Pressing Escape
    dismisses with ``None``.

    Contacts may arrive in two phases: a fast path (active chats, passed to
    the constructor) renders immediately while ``loading`` is ``True``; then
    ``set_contacts`` replaces the list with the full address book.
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
        Binding("ctrl+w", "cycle_filter", "Filter", priority=True),
    ]

    def __init__(
        self,
        contacts: list[ChatContact] | None = None,
        *,
        protocol_filter: str = "all",
        loading: bool = False,
    ) -> None:
        """Initialise the picker.

        Parameters
        ----------
        contacts:
            Optional fast-path contacts (active chats) shown while loading.
        protocol_filter:
            Active protocol filter ("all" | "signal" | "whatsapp" | "telegram").
        loading:
            Whether the full address book is still being fetched.
        """
        super().__init__()
        self._all_contacts: list[ChatContact] = list(contacts or [])
        self._protocol_filter = protocol_filter
        self._loading = loading
        self._query = ""
        self._max_results = get_picker_max_results()
        self._entries: list[PickerEntry] = []
        self._filtered_entries: list[PickerEntry] = []
        self._results: list[ChatContact] = []
        self._rebuild_entries()

    def compose(self) -> ComposeResult:
        with Vertical(id="contact-picker-container"):
            yield Static("🔍 Search Contacts", id="contact-picker-title")
            yield Input(
                placeholder="Type to search contacts...",
                id="contact-search",
            )
            yield ListView(id="contact-results")
            yield Static(
                "↑/↓ navigate · Enter/click select · Ctrl+W filter · Esc close",
                id="contact-picker-footer",
            )

    def on_mount(self) -> None:
        """Render the initial list and focus the search input."""
        self._rebuild_entries()
        self._rerender()
        self.query_one("#contact-search", Input).focus()

    # ── Data model ──────────────────────────────────────────────────────

    def set_contacts(self, contacts: list[ChatContact]) -> None:
        """Replace the picker list with the full *contacts* (address book).

        Called from a worker via ``call_from_thread``.  Guarded by
        ``is_mounted`` so a worker that resolves after the screen was
        dismissed is a no-op.  Re-applies the current query and protocol
        filter and re-renders.
        """
        if not self.is_mounted:
            return
        self._all_contacts = list(contacts)
        self._loading = False
        self._rebuild_entries()
        self._rerender()

    def _rebuild_entries(self) -> None:
        """Re-group and sort the source contacts into picker entries."""
        self._entries = group_by_person(self._all_contacts)
        self._entries.sort(key=lambda e: contact_sort_key(entry_default_contact(e)))

    def _visible_entries(self) -> list[PickerEntry]:
        """Apply the protocol filter and search query to the full entry list."""
        entries = self._entries
        if self._protocol_filter != "all":
            entries = [e for e in entries if self._protocol_filter in e.members]
        return search_entries(entries, self._query, max_results=self._max_results)

    # ── Rendering ────────────────────────────────────────────────────────

    def _rerender(self) -> None:
        """Fill the results ListView with the currently visible entries."""
        self._filtered_entries = self._visible_entries()
        self._results = [entry_default_contact(e) for e in self._filtered_entries]
        results_list = self.query_one("#contact-results", ListView)
        results_list.clear()
        for entry in self._filtered_entries:
            results_list.append(ListItem(Label(self._entry_label(entry))))
        self._update_footer()

    @staticmethod
    def _entry_label(entry: PickerEntry) -> str:
        """Build the display label for a picker entry.

        Single-member entries render like today (``protocol_emoji`` + name);
        multi-member entries concatenate the emojis of their members ordered
        by (most-recent first, then protocol priority), e.g. ``💬📱 Mario Rossi``.
        """
        members = sorted(
            entry.members.values(),
            key=lambda c: (-(c.last_message_ts or 0), _protocol_priority(c.protocol)),
        )
        if len(members) == 1:
            return f"{protocol_emoji(members[0].protocol)} {entry.display_name}"
        emojis = "".join(protocol_emoji(m.protocol) for m in members)
        return f"{emojis} {entry.display_name}"

    def _footer_text(self) -> str:
        if self._loading:
            return "⏳ Caricamento rubrica completa…"
        base = "↑/↓ navigate · Enter/click select · Ctrl+W filter · Esc close"
        if self._protocol_filter != "all":
            return f"Filtro: {self._protocol_filter.title()} · {base}"
        return base

    def _update_footer(self) -> None:
        try:
            self.query_one("#contact-picker-footer", Static).update(self._footer_text())
        except Exception as _e:  # pragma: no cover - widget not yet mounted
            logger.debug("Footer not found", exc_info=True)

    # ── Search ───────────────────────────────────────────────────────────

    def on_input_changed(self, event: Input.Changed) -> None:
        """Handle search input changes: re-filter the results list."""
        if event.input.id != "contact-search":
            return
        self._query = event.value
        self._rerender()

    # ── Selection ────────────────────────────────────────────────────────

    def action_select_contact(self) -> None:
        """Enter: select the highlighted entry and dismiss the picker."""
        results_list = self.query_one("#contact-results", ListView)
        index = results_list.index
        if index is not None and 0 <= index < len(self._filtered_entries):
            self._select_entry(self._filtered_entries[index])

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Click: select the clicked entry and dismiss the picker."""
        results_list = self.query_one("#contact-results", ListView)
        index = results_list.index
        if index is not None and 0 <= index < len(self._filtered_entries):
            self._select_entry(self._filtered_entries[index])

    def _select_entry(self, entry: PickerEntry) -> None:
        """Dismiss with the chosen member of *entry*.

        - Under a specific protocol filter (or a single-member entry), the
          member of that protocol is chosen directly.
        - Otherwise (multi-member, "all" filter) a ``BackendChoiceScreen`` is
          pushed; its callback dismisses this picker only when the user made a
          choice (``None`` keeps the picker open).
        """
        if self._protocol_filter in entry.members:
            self.dismiss(entry.members[self._protocol_filter])
            return
        if len(entry.members) == 1:
            self.dismiss(next(iter(entry.members.values())))
            return

        def _on_backend_chosen(contact: ChatContact | None) -> None:
            if contact is not None:
                self.dismiss(contact)

        self.app.push_screen(BackendChoiceScreen(entry), _on_backend_chosen)

    # ── Internal protocol filter ─────────────────────────────────────────

    def action_cycle_filter(self) -> None:
        """Ctrl+W: cycle the picker filter ALL → SIGNAL → WHATSAPP → TELEGRAM.

        Purely client-side on the already-loaded entries (no refetch).
        """
        order = ["all", "signal", "whatsapp", "telegram"]
        idx = (
            order.index(self._protocol_filter) if self._protocol_filter in order else 0
        )
        self._protocol_filter = order[(idx + 1) % len(order)]
        self._rerender()


# ─── Backend choice screen ───────────────────────────────────────────────────


class BackendChoiceScreen(ModalScreen[ChatContact]):
    """Small modal to choose which backend to open for a multi-backend entry."""

    DEFAULT_CSS = """
    BackendChoiceScreen {
        align: center middle;
        background: $surface 80%;
    }

    #backend-choice-container {
        width: 60;
        border: thick $accent;
        background: $surface;
        padding: 0 1;
    }

    #backend-choice-title {
        text-style: bold;
        text-align: center;
        padding: 0 0 1 0;
        color: $text;
    }

    #backend-choice-list {
        height: auto;
        max-height: 12;
    }

    #backend-choice-list ListItem {
        padding: 0 1;
    }

    #backend-choice-list ListItem:hover {
        background: $accent 20%;
    }

    #backend-choice-list ListItem:focus {
        background: $accent 40%;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss(None)", "Back", priority=True),
        Binding("enter", "select", "Select", priority=True),
    ]

    def __init__(self, entry: PickerEntry) -> None:
        super().__init__()
        self._entry = entry
        self._members = sorted(
            entry.members.values(),
            key=lambda c: (-(c.last_message_ts or 0), _protocol_priority(c.protocol)),
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="backend-choice-container"):
            yield Static("Scegli backend", id="backend-choice-title")
            yield ListView(id="backend-choice-list")

    def on_mount(self) -> None:
        """Render one row per member and pre-select the default (most recent)."""
        results_list = self.query_one("#backend-choice-list", ListView)
        default = entry_default_contact(self._entry)
        default_index = 0
        for i, member in enumerate(self._members):
            results_list.append(ListItem(Label(self._member_label(member))))
            if member is default:
                default_index = i
        results_list.index = default_index
        results_list.focus()

    @staticmethod
    def _member_label(member: ChatContact) -> str:
        rel = _relative_time(member.last_message_ts)
        return f"{protocol_emoji(member.protocol)} {member.protocol.title()} — ultimo msg {rel}"

    def _selected_member(self) -> ChatContact | None:
        results_list = self.query_one("#backend-choice-list", ListView)
        index = results_list.index
        if index is not None and 0 <= index < len(self._members):
            return self._members[index]
        return None

    def action_select(self) -> None:
        """Enter: dismiss with the highlighted member."""
        member = self._selected_member()
        if member is not None:
            self.dismiss(member)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Click: dismiss with the clicked member."""
        member = self._selected_member()
        if member is not None:
            self.dismiss(member)
