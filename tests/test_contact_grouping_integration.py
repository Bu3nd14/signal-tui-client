"""Integration coverage gaps for Sprint 2 — contact grouping (headless TUI).

The unit suite in ``test_contact_grouping.py`` covers the projection, the label,
the toggle, the dispatch (via mocks), the filter matrix and the highlight
fallback.  These headless ``App.run_test()`` tests close the remaining gaps that
only a real Textual widget tree can exercise:

- Enter on a header toggles the group and NEVER selects a contact.
- Enter on a (visible) member opens the chat (``_select_contact``).
- Toggling (space) does NOT move focus away from the contact list.
- Ctrl+W filter × groups: a group without the filtered protocol disappears;
  a mixed group keeps its header + only the matching member.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from textual.containers import Vertical
from textual.widgets import ListView

from models import PROTOCOL_SIGNAL, PROTOCOL_WHATSAPP, ChatContact
from tui.app import SignalTUI


def _contact(
    protocol: str, cid: str, name: str, phone: str, ts: int = 0
) -> ChatContact:
    c = ChatContact(
        id=cid, display_name=name, protocol=protocol, extras={"phone": phone}
    )
    c.last_message_ts = ts
    return c


@contextmanager
def _grouped_app(*contacts: ChatContact):
    """Build a headless-ready ``SignalTUI`` with real grouping across protocols.

    Mirrors ``conftest._make_test_app`` but with multi-protocol contacts that
    share a phone number, so we get real multi-member groups (header + members).
    """

    def _noop_on_mount(self: SignalTUI) -> None:
        self._chat_log = self.query_one("#chat-log", Vertical)
        self._render_contact_list(list(self.contacts))

    with (
        patch("tui.app.BackendManager"),
        patch("tui.app.SignalBackend"),
        patch("tui.app.whatsapp_enabled", return_value=False),
        patch("tui.app.telegram_enabled", return_value=False),
        patch.object(SignalTUI, "on_mount", _noop_on_mount),
    ):
        app = SignalTUI()
        app.run_worker = MagicMock()
        app.contacts = list(contacts)
        yield app


def _find_row(contact_list: ListView, label: str):
    return next(item for item in contact_list.children if item._label_text == label)


@pytest.mark.integration
async def test_enter_on_header_toggles_and_never_selects():
    """Enter on a header only toggles the group; ``_select_contact`` is not called."""
    with _grouped_app(
        _contact(PROTOCOL_SIGNAL, "+391", "Mario", "391"),
        _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", "391"),
    ) as app:
        async with app.run_test() as pilot:
            await pilot.pause()
            contact_list = app.query_one("#contact-list", ListView)
            contact_list.focus()
            header = _find_row(contact_list, "▸ Mario")
            contact_list.index = contact_list.children.index(header)
            await pilot.pause()

            app._select_contact = MagicMock(wraps=app._select_contact)
            await pilot.press("enter")
            await pilot.pause()

            assert "phone:391" in app._expanded_groups
            app._select_contact.assert_not_called()
            # Member is now visible (collapsed → expanded).
            member = app._contact_widgets["signal:+391"]
            assert member.display is True


@pytest.mark.integration
async def test_enter_on_member_opens_chat():
    """Enter on a visible member selects it (existing member flow preserved)."""
    with _grouped_app(
        _contact(PROTOCOL_SIGNAL, "+391", "Mario", "391"),
        _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", "391"),
    ) as app:
        async with app.run_test() as pilot:
            await pilot.pause()
            contact_list = app.query_one("#contact-list", ListView)
            contact_list.focus()
            # Expand the group so the member row is visible.
            app._toggle_group("phone:391")
            await pilot.pause()
            member = app._contact_widgets["signal:+391"]
            contact_list.index = contact_list.children.index(member)
            await pilot.pause()

            await pilot.press("enter")
            await pilot.pause()

            assert app.selected_contact is not None
            assert app.selected_contact.cache_key == "signal:+391"


@pytest.mark.integration
async def test_toggle_does_not_move_focus():
    """Space on a header toggles the group without stealing focus from the list."""
    with _grouped_app(
        _contact(PROTOCOL_SIGNAL, "+391", "Mario", "391"),
        _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", "391"),
    ) as app:
        async with app.run_test() as pilot:
            await pilot.pause()
            contact_list = app.query_one("#contact-list", ListView)
            contact_list.focus()
            header = _find_row(contact_list, "▸ Mario")
            contact_list.index = contact_list.children.index(header)
            await pilot.pause()

            await pilot.press("space")
            await pilot.pause()

            assert "phone:391" in app._expanded_groups
            assert app.focused is contact_list


@pytest.mark.integration
async def test_ctrl_w_filter_hides_group_without_protocol():
    """Ctrl+W: a group with no matching member disappears; mixed group survives."""
    with _grouped_app(
        _contact(PROTOCOL_SIGNAL, "+391", "Mario", "391"),
        _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", "391"),
        _contact(PROTOCOL_WHATSAPP, "wa:2@s.whatsapp.net", "Wonly", "777"),
    ) as app:
        async with app.run_test() as pilot:
            await pilot.pause()
            contact_list = app.query_one("#contact-list", ListView)

            await pilot.press("ctrl+w")  # all → signal
            await pilot.pause()
            assert app._protocol_filter == "signal"

            # WhatsApp-only group (phone:777) header hidden.
            wonly_header = app._group_widgets[
                app._member_to_group["whatsapp:wa:2@s.whatsapp.net"]
            ]
            assert wonly_header.display is False

            # Mixed group (phone:391) header still visible (Signal member exists).
            mixed_header = app._group_widgets[app._member_to_group["signal:+391"]]
            assert mixed_header.display is True
            assert contact_list.index == contact_list.children.index(mixed_header)


@pytest.mark.integration
async def test_enter_on_header_with_filter_opens_chat_directly():
    """With a single-protocol filter, Enter on a header opens that protocol's
    member chat directly (no toggle, no expansion)."""
    with _grouped_app(
        _contact(PROTOCOL_SIGNAL, "+391", "Mario", "391"),
        _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", "391"),
    ) as app:
        async with app.run_test() as pilot:
            await pilot.pause()
            contact_list = app.query_one("#contact-list", ListView)
            contact_list.focus()

            app._protocol_filter = "signal"
            app._apply_contact_filter()
            await pilot.pause()

            # The chevron is gone: the header row is now just "Mario".
            header = _find_row(contact_list, "Mario")
            contact_list.index = contact_list.children.index(header)
            await pilot.pause()

            await pilot.press("enter")
            await pilot.pause()

            assert app.selected_contact is not None
            assert app.selected_contact.cache_key == "signal:+391"
            assert app._expanded_groups == set()  # no expansion
