"""Tests for the unread-only filter (Ctrl+U) and the clickable status bar.

Part A covers the intersection in ``_filtered_contacts``, the toggle action,
title suffix / border class, header visibility, ``_select_contact`` refresh and
the ghost-contact behaviour.  Part B covers the ``StatusBar`` widget API, the
click → filter state flow and the ``-active`` class synchronisation.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from textual.screen import ModalScreen
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
from ui_components import (
    MessageTextArea,
    StatusBar,
    StatusSegment,
)


def _contact(
    protocol: str,
    cid: str,
    name: str = "X",
    phone: str | None = None,
    ts: int = 0,
) -> ChatContact:
    extras: dict[str, object] = {}
    if phone:
        extras["phone"] = phone
    c = ChatContact(id=cid, display_name=name, protocol=protocol, extras=extras)
    c.last_message_ts = ts
    return c


def _make_app(*contacts: ChatContact) -> SignalTUI:
    app = SignalTUI()
    app.contacts = list(contacts)
    return app


class _FakeListView:
    """Minimal ListView double: append/clear/move_child + ``children``/``index``."""

    def __init__(self):
        self.items = []
        self.index = None

    @property
    def children(self):
        return self.items

    def clear(self):
        self.items = []

    def append(self, item):
        self.items.append(item)

    def move_child(self, child, *, before=None, after=None):
        self.items.remove(child)
        if before is not None:
            self.items.insert(self.items.index(before), child)
        else:
            self.items.insert(self.items.index(after) + 1, child)


def _render(app: SignalTUI) -> _FakeListView:
    """Render the app's contacts into a fake ListView and return it."""
    fake = _FakeListView()
    app.query_one = MagicMock(return_value=fake)
    app._render_contact_list(list(app.contacts))
    return fake


def _member_rows(fake: _FakeListView) -> list:
    return [it for it in fake.items if getattr(it, "_row_kind", None) == "member"]


def _group_rows(fake: _FakeListView) -> list:
    return [it for it in fake.items if getattr(it, "_row_kind", None) == "group"]


def _prepare_select(app: SignalTUI, fake_list) -> None:
    """Neutralize the heavy operations of ``_select_contact`` for unit tests."""
    app.query_one = MagicMock(return_value=fake_list)
    app._add_message = MagicMock()
    app._clear_chat = MagicMock()
    app._cancel_reply = MagicMock()
    app._cancel_edit = MagicMock()
    app._load_messages_worker = MagicMock()
    app.manager = MagicMock()
    app.manager.get.return_value = MagicMock()
    app.run_worker = MagicMock()
    app.run_worker.return_value = None


class _SelectListView(_FakeListView):
    """``_FakeListView`` tolerant of the extra widget calls in ``_select_contact``.

    Unlike a bare ``MagicMock``, ``append``/``clear``/``move_child`` mutate the
    backing list for real, so contacts created during ``_select_contact`` (ghost
    open-or-create) actually appear in ``children`` for the visibility pass.
    """

    def update(self, *args, **kwargs):
        pass

    def focus(self):
        pass

    def set_counts(self, *args, **kwargs):
        pass

    def show_default(self, *args, **kwargs):
        pass

    def sync_active(self, *args, **kwargs):
        pass


# ─── A. Intersection in `_filtered_contacts` ─────────────────────────────────


class TestFilteredContactsIntersection:
    def test_all_plus_unread(self):
        sig = _contact(PROTOCOL_SIGNAL, "+1", "Mario")
        wa = _contact(PROTOCOL_WHATSAPP, "wa1", "Anna")
        app = _make_app(sig, wa)
        app._unread_only = True
        app._unread_counts = {"signal:+1": 2}

        assert [c.id for c in app._filtered_contacts()] == ["+1"]

    def test_proto_plus_unread(self):
        sig = _contact(PROTOCOL_SIGNAL, "+1", "Mario")
        sig2 = _contact(PROTOCOL_SIGNAL, "+2", "Luigi")
        wa = _contact(PROTOCOL_WHATSAPP, "wa1", "Anna")
        app = _make_app(sig, sig2, wa)
        app._protocol_filter = PROTOCOL_SIGNAL
        app._unread_only = True
        app._unread_counts = {"signal:+1": 2, "whatsapp:wa1": 5}

        assert [c.id for c in app._filtered_contacts()] == ["+1"]

    def test_unread_only_with_no_counts_is_empty(self):
        app = _make_app(
            _contact(PROTOCOL_SIGNAL, "+1", "Mario"),
            _contact(PROTOCOL_WHATSAPP, "wa1", "Anna"),
        )
        app._unread_only = True
        app._unread_counts = {}

        assert app._filtered_contacts() == []

    def test_protocol_filter_unchanged_without_unread_only(self):
        sig = _contact(PROTOCOL_SIGNAL, "+1", "Mario")
        wa = _contact(PROTOCOL_WHATSAPP, "wa1", "Anna")
        app = _make_app(sig, wa)
        app._protocol_filter = PROTOCOL_SIGNAL
        app._unread_counts = {"signal:+1": 2, "whatsapp:wa1": 5}

        assert [c.id for c in app._filtered_contacts()] == ["+1"]


# ─── A. Toggle action, title suffix, border class ────────────────────────────


class TestUnreadToggle:
    def test_toggle_flips_state(self):
        app = _make_app()
        app._apply_contact_filter = MagicMock()
        app._sync_status_segments = MagicMock()

        app.action_toggle_unread_filter()
        assert app._unread_only is True
        app._apply_contact_filter.assert_called_once()
        app._sync_status_segments.assert_called_once()

        app.action_toggle_unread_filter()
        assert app._unread_only is False

    def test_title_suffix_unread(self):
        app = _make_app()
        assert app._filter_title_suffix() == " - All"
        app._unread_only = True
        assert app._filter_title_suffix() == " - All · Unread"
        app._protocol_filter = PROTOCOL_WHATSAPP
        assert app._filter_title_suffix() == " - WhatsApp · Unread"
        app._unread_only = False
        assert app._filter_title_suffix() == " - WhatsApp"
        app._protocol_filter = PROTOCOL_TELEGRAM
        assert app._filter_title_suffix() == " - Telegram"
        app._unread_only = True
        assert app._filter_title_suffix() == " - Telegram · Unread"
        app._protocol_filter = "unknown"
        app._unread_only = False
        assert app._filter_title_suffix() == ""

    def test_apply_contact_filter_adds_unread_class(self):
        app = _make_app(_contact(PROTOCOL_SIGNAL, "+1", "Mario"))
        app._apply_contact_visibility = MagicMock()
        app._refresh_header_labels = MagicMock()
        title = MagicMock()
        chat_title = MagicMock()
        chat_log = MagicMock()
        contact_list = MagicMock()
        app._chat_log = chat_log

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
        app._unread_only = True
        app._protocol_filter = "all"

        app._apply_contact_filter()

        chat_log.remove_class.assert_called_with(
            "chat-filter-signal",
            "chat-filter-whatsapp",
            "chat-filter-telegram",
            "chat-filter-unread",
        )
        chat_log.add_class.assert_called_with("chat-filter-unread")
        contact_list.add_class.assert_called_with("chat-filter-unread")
        title.add_class.assert_called_with("chat-filter-unread")

    def test_apply_contact_filter_unread_title_omits_contacts_word(self):
        app = _make_app(_contact(PROTOCOL_SIGNAL, "+1", "Mario"))
        app._apply_contact_visibility = MagicMock()
        app._refresh_header_labels = MagicMock()
        title = MagicMock()
        app._chat_log = MagicMock()

        def fake_q(selector, *_a, **_k):
            if selector == "#ContactsTitle":
                return title
            return MagicMock()

        app.query_one = fake_q
        app._unread_only = True
        app._protocol_filter = "all"

        app._apply_contact_filter()

        assert title.update.call_args.args[0] == "📇 All · Unread"

    def test_apply_contact_filter_title_keeps_contacts_word_when_not_unread(self):
        app = _make_app(_contact(PROTOCOL_SIGNAL, "+1", "Mario"))
        app._apply_contact_visibility = MagicMock()
        app._refresh_header_labels = MagicMock()
        title = MagicMock()
        app._chat_log = MagicMock()

        def fake_q(selector, *_a, **_k):
            if selector == "#ContactsTitle":
                return title
            return MagicMock()

        app.query_one = fake_q
        app._unread_only = False
        app._protocol_filter = "all"

        app._apply_contact_filter()

        assert title.update.call_args.args[0] == "📇 Contacts - All"

    def test_apply_contact_filter_protocol_class_kept_with_unread(self):
        app = _make_app(_contact(PROTOCOL_SIGNAL, "+1", "Mario"))
        app._apply_contact_visibility = MagicMock()
        app._refresh_header_labels = MagicMock()
        chat_log = MagicMock()
        contact_list = MagicMock()
        app._chat_log = chat_log

        def fake_q(selector, *_a, **_k):
            if selector == "#chat-log":
                return chat_log
            if selector == "#contact-list":
                return contact_list
            return MagicMock()

        app.query_one = fake_q
        app._unread_only = True
        app._protocol_filter = "whatsapp"

        app._apply_contact_filter()

        # The protocol colour survives the unread-only filter…
        chat_log.add_class.assert_called_once_with("chat-filter-whatsapp")
        # …and the unread class is reserved for "all" + unread (no extra call).
        contact_list.add_class.assert_called_once_with("chat-filter-whatsapp")


# ─── A. Header visibility with unread-only ───────────────────────────────────


class TestUnreadGroupHeaders:
    def test_group_without_unread_header_hidden(self):
        mario = _contact(PROTOCOL_SIGNAL, "+1", "Mario", phone="391")
        anna = _contact(PROTOCOL_WHATSAPP, "wa1", "Anna", phone="392")
        app = _make_app(mario, anna)
        app._unread_only = True
        app._unread_counts = {"signal:+1": 3}

        _render(app)

        mario_header = app._group_widgets[app._member_to_group[mario.cache_key]]
        anna_header = app._group_widgets[app._member_to_group[anna.cache_key]]
        assert mario_header.display is True
        assert anna_header.display is False

    def test_mixed_group_header_visible_when_one_member_unread(self):
        mario = _contact(PROTOCOL_SIGNAL, "+1", "Mario", phone="391")
        anna = _contact(PROTOCOL_WHATSAPP, "wa1", "Anna", phone="391")
        app = _make_app(mario, anna)
        app._unread_only = True
        app._unread_counts = {"whatsapp:wa1": 2}

        fake = _render(app)

        header = _group_rows(fake)[0]
        # Both members belong to the same group; WhatsApp has unread → visible.
        assert header.display is True
        # The signal member is masked (no unread) even if expanded.
        app._expanded_groups.add(app._member_to_group[mario.cache_key])
        app._apply_contact_visibility()
        sig_item = app._contact_widgets[mario.cache_key]
        assert sig_item.display is False


# ─── A. `_visible_keys` pin (selected contact stays visible) ─────────────────


class TestVisibleKeys:
    def test_pin_adds_selected_contact_in_all_scope(self):
        mario = _contact(PROTOCOL_SIGNAL, "+1", "Mario", phone="391")
        luigi = _contact(PROTOCOL_SIGNAL, "+2", "Luigi", phone="392")
        app = _make_app(mario, luigi)
        app._unread_only = True
        app._unread_counts = {"signal:+2": 3}
        app.selected_contact = mario  # read → would vanish without the pin

        assert app._visible_keys() == {"signal:+1", "signal:+2"}

    def test_pin_skipped_for_different_protocol_scope(self):
        mario = _contact(PROTOCOL_SIGNAL, "+1", "Mario", phone="391")
        anna = _contact(PROTOCOL_WHATSAPP, "wa1", "Anna", phone="392")
        app = _make_app(mario, anna)
        app._protocol_filter = PROTOCOL_SIGNAL
        app._unread_only = True
        app._unread_counts = {"signal:+1": 2}
        app.selected_contact = anna

        assert app._visible_keys() == {"signal:+1"}

    def test_pin_adds_selected_when_protocol_matches(self):
        mario = _contact(PROTOCOL_SIGNAL, "+1", "Mario", phone="391")
        anna = _contact(PROTOCOL_WHATSAPP, "wa1", "Anna", phone="392")
        app = _make_app(mario, anna)
        app._protocol_filter = PROTOCOL_WHATSAPP
        app._unread_only = True
        app._unread_counts = {"signal:+1": 2}
        app.selected_contact = anna  # read but inside the whatsapp scope → pinned

        assert app._visible_keys() == {"whatsapp:wa1"}


# ─── A. `_select_contact` refresh under unread-only ──────────────────────────


class TestSelectContactUnreadOnly:
    def test_select_contact_pins_selected_keeps_chat(self):
        mario = _contact(PROTOCOL_SIGNAL, "+1", "Mario", phone="391")
        luigi = _contact(PROTOCOL_SIGNAL, "+2", "Luigi", phone="392")
        app = _make_app(mario, luigi)
        app._unread_only = True
        app._unread_counts = {"signal:+1": 2, "signal:+2": 3}

        fake = _render(app)
        mario_header = app._group_widgets[app._member_to_group[mario.cache_key]]
        luigi_header = app._group_widgets[app._member_to_group[luigi.cache_key]]
        assert mario_header.display is True
        assert luigi_header.display is True

        fake_list = MagicMock()
        fake_list.children = list(fake.items)
        fake_list.index = None
        _prepare_select(app, fake_list)
        app._cache = {}

        app._select_contact(mario)

        # Chat stays open; the just-read selected contact is PINNED (stays
        # visible) and the still-unread contact remains visible too.
        assert app.selected_contact is mario
        assert mario_header.display is True
        assert luigi_header.display is True
        # Highlight lands on the selected contact's header (member collapsed).
        assert fake_list.index == fake_list.children.index(mario_header)

    def test_select_other_contact_unpins_previous(self):
        mario = _contact(PROTOCOL_SIGNAL, "+1", "Mario", phone="391")
        luigi = _contact(PROTOCOL_SIGNAL, "+2", "Luigi", phone="392")
        app = _make_app(mario, luigi)
        app._unread_only = True
        app._unread_counts = {"signal:+1": 2, "signal:+2": 3}

        fake = _render(app)
        mario_header = app._group_widgets[app._member_to_group[mario.cache_key]]
        luigi_header = app._group_widgets[app._member_to_group[luigi.cache_key]]

        fake_list = MagicMock()
        fake_list.children = list(fake.items)
        fake_list.index = None
        _prepare_select(app, fake_list)
        app._cache = {}

        app._select_contact(mario)
        assert mario_header.display is True
        assert luigi_header.display is True

        app._select_contact(luigi)

        # The previous contact (now unread=0 and no longer selected) vanishes;
        # the newly selected contact is pinned instead.
        assert app.selected_contact is luigi
        assert mario_header.display is False
        assert luigi_header.display is True

    def test_proto_unread_pin_only_when_protocol_matches(self):
        mario = _contact(PROTOCOL_SIGNAL, "+1", "Mario", phone="391")
        anna = _contact(PROTOCOL_WHATSAPP, "wa1", "Anna", phone="392")
        app = _make_app(mario, anna)
        app._protocol_filter = PROTOCOL_SIGNAL
        app._unread_only = True
        app._unread_counts = {"signal:+1": 2}

        fake = _render(app)
        mario_header = app._group_widgets[app._member_to_group[mario.cache_key]]
        anna_header = app._group_widgets[app._member_to_group[anna.cache_key]]
        assert mario_header.display is True
        assert anna_header.display is False

        fake_list = MagicMock()
        fake_list.children = list(fake.items)
        fake_list.index = None
        _prepare_select(app, fake_list)
        app._cache = {}

        app._select_contact(anna)

        # Chat stays open, but the selected WhatsApp contact is NOT pinned:
        # its protocol is outside the Signal filter's scope.
        assert app.selected_contact is anna
        assert anna_header.display is False
        assert mario_header.display is True

    def test_no_pin_when_unread_only_false(self):
        mario = _contact(PROTOCOL_SIGNAL, "+1", "Mario", phone="391")
        app = _make_app(mario)
        app._protocol_filter = PROTOCOL_WHATSAPP  # mario out of scope
        app._unread_only = False
        app.selected_contact = mario

        # No pin: the selected contact is NOT force-added when unread-only is off.
        assert app._visible_keys() == set()

    def test_pin_preserved_through_render_next_chunk(self):
        mario = _contact(PROTOCOL_SIGNAL, "+1", "Mario", phone="391")
        luigi = _contact(PROTOCOL_SIGNAL, "+2", "Luigi", phone="392")
        app = _make_app(mario, luigi)
        app._unread_only = True
        app._unread_counts = {"signal:+1": 0, "signal:+2": 3}
        app.selected_contact = mario  # read but selected → pinned

        fake = _FakeListView()
        app.query_one = MagicMock(return_value=fake)
        app._start_progressive_render(app._visible_rows())

        mario_header = app._group_widgets[app._member_to_group[mario.cache_key]]
        luigi_header = app._group_widgets[app._member_to_group[luigi.cache_key]]
        assert mario_header.display is True  # pin survives the chunk render
        assert luigi_header.display is True

    def test_ghost_contact_hidden_in_unread_only_but_chat_open(self):
        mario = _contact(PROTOCOL_SIGNAL, "+1", "Mario", phone="391")
        app = _make_app(mario)
        app._unread_only = True
        app._unread_counts = {"signal:+1": 1}

        lst = _SelectListView()
        app.query_one = MagicMock(return_value=lst)
        app._render_contact_list(list(app.contacts))

        _prepare_select(app, lst)
        app._cache = {}

        ghost = _contact(PROTOCOL_WHATSAPP, "wa-ghost", "Ghost")
        app._select_contact(ghost)

        assert app.selected_contact is ghost
        assert ghost.extras.get("ghost") is True
        ghost_item = app._contact_widgets.get(ghost.cache_key)
        assert ghost_item is not None
        assert ghost_item.display is False


# ─── A. Auto-selection of the first visible contact on filter change ──────────


class TestAutoSelectFirstVisible:
    @staticmethod
    def _query_for(fake):
        def fake_q(selector, *args, **_k):
            # The contact-list widget loop in ``_apply_contact_filter`` queries
            # ``#contact-list`` WITHOUT a type, so only return the real list for
            # the typed lookups (``_apply_contact_visibility`` /
            # ``_contact_for_first_visible`` / ``_select_contact``).
            if selector == "#contact-list" and args:
                return fake
            return MagicMock()

        return fake_q

    def _render_and_arm(self, app):
        """Render the app into a fake ListView and re-arm ``query_one``."""
        fake = _render(app)
        app.query_one = MagicMock(side_effect=self._query_for(fake))
        app._select_contact = MagicMock()
        app._sync_status_segments = MagicMock()
        return fake

    # ── `_contact_for_first_visible` resolution ────────────────────────────────

    def test_contact_for_first_visible_resolves_member(self):
        mario = _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391")
        app = _make_app(mario)
        item = MagicMock()
        item._row_kind = "member"
        item._contact_id = mario.cache_key
        item.display = True
        fake = _FakeListView()
        fake.items.append(item)
        app.query_one = MagicMock(return_value=fake)

        assert app._contact_for_first_visible() is mario

    def test_contact_for_first_visible_resolves_header_in_filter(self):
        sig = _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391")
        wa = _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391")
        app = _make_app(sig, wa)
        app._visible_rows()  # populate _group_members
        app._protocol_filter = "signal"
        item = MagicMock()
        item._row_kind = "group"
        item._group_key = "phone:391"
        item.display = True
        fake = _FakeListView()
        fake.items.append(item)
        app.query_one = MagicMock(return_value=fake)

        assert app._contact_for_first_visible() is sig

    def test_contact_for_first_visible_header_all_uses_default(self):
        sig = _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391", ts=100)
        wa = _contact(
            PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391", ts=200
        )
        app = _make_app(sig, wa)
        entry = app._visible_rows()[0].entry
        app._protocol_filter = "all"
        item = MagicMock()
        item._row_kind = "group"
        item._group_key = "phone:391"
        item._entry = entry
        item.display = True
        fake = _FakeListView()
        fake.items.append(item)
        app.query_one = MagicMock(return_value=fake)

        # Default is the most-recent member (wa, ts=200).
        assert app._contact_for_first_visible() is wa

    def test_contact_for_first_visible_none_when_no_match(self):
        mario = _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391")
        app = _make_app(mario)
        item = MagicMock()
        item._row_kind = "member"
        item._contact_id = "signal:missing"
        item.display = True
        fake = _FakeListView()
        fake.items.append(item)
        app.query_one = MagicMock(return_value=fake)

        assert app._contact_for_first_visible() is None

    def test_contact_for_first_visible_skips_member_without_id(self):
        app = _make_app(_contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"))
        item = MagicMock()
        item._row_kind = "member"
        item._contact_id = None
        item.display = True
        fake = _FakeListView()
        fake.items.append(item)
        app.query_one = MagicMock(return_value=fake)

        assert app._contact_for_first_visible() is None

    def test_contact_for_first_visible_skips_unresolved_filter_header(self):
        sig = _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391")
        app = _make_app(sig)
        app._visible_rows()  # populate _group_members
        app._protocol_filter = "telegram"  # no telegram member in the group
        header = MagicMock()
        header._row_kind = "group"
        header._group_key = "phone:391"
        header.display = True
        fake = _FakeListView()
        fake.items.append(header)
        app.query_one = MagicMock(return_value=fake)

        assert app._contact_for_first_visible() is None

    def test_contact_for_first_visible_skips_header_without_entry(self):
        app = _make_app()
        app._protocol_filter = "all"
        header = MagicMock()
        header._row_kind = "group"
        header._group_key = "phone:391"
        header._entry = None
        header.display = True
        fake = _FakeListView()
        fake.items.append(header)
        app.query_one = MagicMock(return_value=fake)

        assert app._contact_for_first_visible() is None

    # ── `_select_first_visible_contact` ───────────────────────────────────────

    def test_select_first_visible_calls_select_contact(self):
        mario = _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391")
        app = _make_app(mario)
        item = MagicMock()
        item._row_kind = "member"
        item._contact_id = mario.cache_key
        item.display = True
        fake = _FakeListView()
        fake.items.append(item)
        app.query_one = MagicMock(return_value=fake)
        app._select_contact = MagicMock()

        app._select_first_visible_contact()

        app._select_contact.assert_called_once_with(mario)

    def test_select_first_visible_noop_when_already_selected(self):
        mario = _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391")
        app = _make_app(mario)
        app.selected_contact = mario
        item = MagicMock()
        item._row_kind = "member"
        item._contact_id = mario.cache_key
        item.display = True
        fake = _FakeListView()
        fake.items.append(item)
        app.query_one = MagicMock(return_value=fake)
        app._select_contact = MagicMock()

        app._select_first_visible_contact()

        app._select_contact.assert_not_called()

    def test_select_first_visible_noop_on_empty_list(self):
        app = _make_app()
        fake = _FakeListView()
        app.query_one = MagicMock(return_value=fake)
        app._select_contact = MagicMock()

        app._select_first_visible_contact()

        app._select_contact.assert_not_called()

    # ── The 4 call sites select the first visible contact ─────────────────────

    def test_cycle_protocol_filter_selects_first_visible(self):
        mario = _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391")
        wa = _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391")
        app = _make_app(mario, wa)
        self._render_and_arm(app)

        app.action_cycle_protocol_filter()  # "all" -> "signal"

        app._select_contact.assert_called_once_with(mario)

    def test_toggle_unread_filter_selects_first_visible(self):
        mario = _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391")
        luigi = _contact(PROTOCOL_SIGNAL, "+392", "Luigi", phone="392")
        app = _make_app(mario, luigi)
        app._unread_counts = {"signal:+392": 3}
        self._render_and_arm(app)

        app.action_toggle_unread_filter()  # unread-only -> True

        # Only Luigi has unread: its header is the first visible row.
        app._select_contact.assert_called_once_with(luigi)

    def test_go_to_all_selects_first_visible(self):
        mario = _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391")
        wa = _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391")
        app = _make_app(mario, wa)
        app._protocol_filter = "signal"
        app._unread_only = True
        app._unread_counts = {"signal:+391": 2}
        self._render_and_arm(app)

        app.action_go_to_all()  # "all" + unread off

        # Single group phone:391 → default contact (tiebreak: signal first).
        app._select_contact.assert_called_once_with(mario)

    def test_activate_backend_unread_selects_first_visible(self):
        mario = _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391")
        wa = _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391")
        app = _make_app(mario, wa)
        app._unread_counts = {"whatsapp:wa:1@s.whatsapp.net": 3}
        self._render_and_arm(app)

        app._activate_backend_unread(PROTOCOL_WHATSAPP)

        app._select_contact.assert_called_once_with(wa)

    # ── Empty list deselects without selecting ────────────────────────────────

    def test_apply_contact_filter_empty_list_deselects_and_no_select(self):
        mario = _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391")
        app = _make_app(mario)
        fake = _render(app)
        app.selected_contact = mario
        app.query_one = MagicMock(side_effect=self._query_for(fake))
        app._select_contact = MagicMock()
        app._sync_status_segments = MagicMock()

        app._protocol_filter = "whatsapp"  # mario (signal) out of scope
        app._apply_contact_filter()

        assert app.selected_contact is None
        app._select_contact.assert_not_called()

    # ── No recursion: `_select_contact` → `_apply_contact_visibility` ─────────

    def test_select_first_visible_not_reentrant(self):
        mario = _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391", ts=100)
        luigi = _contact(PROTOCOL_SIGNAL, "+392", "Luigi", phone="392")
        app = _make_app(mario, luigi)
        app._protocol_filter = "signal"
        app._unread_only = True
        app._unread_counts = {"signal:+391": 2, "signal:+392": 3}
        fake = _render(app)

        fake_list = MagicMock()
        fake_list.children = list(fake.items)
        fake_list.index = None
        app.query_one = MagicMock(side_effect=self._query_for(fake_list))
        app._sync_status_segments = MagicMock()
        _prepare_select(app, fake_list)
        app._cache = {}

        calls = []
        real = app._select_contact

        def spy(contact):
            calls.append(contact)
            return real(contact)

        app._select_contact = spy

        app._apply_contact_filter()

        # The mark-read inside `_select_contact` re-applies visibility, but that
        # must NOT re-trigger the auto-selection hook: exactly one call.
        assert len(calls) == 1
        assert calls[0] is mario

    # ── Unread pin survives the auto-selection mark-read ──────────────────────

    def test_select_first_visible_keeps_pin_and_new_selection_visible(self):
        mario = _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391", ts=100)
        luigi = _contact(PROTOCOL_SIGNAL, "+392", "Luigi", phone="392", ts=200)
        app = _make_app(mario, luigi)
        app._unread_only = True
        app._unread_counts = {"signal:+391": 2, "signal:+392": 3}
        app.selected_contact = mario  # old selection (has unread)
        fake = _render(app)
        mario_header = app._group_widgets[app._member_to_group[mario.cache_key]]
        luigi_header = app._group_widgets[app._member_to_group[luigi.cache_key]]

        fake_list = MagicMock()
        fake_list.children = list(fake.items)
        fake_list.index = None
        app.query_one = MagicMock(side_effect=self._query_for(fake_list))
        _prepare_select(app, fake_list)
        app._cache = {}

        app._select_first_visible_contact()

        # New first visible (Luigi, most recent) is selected and, after
        # mark-read, stays pinned/visible; the old selection still has unread
        # and stays visible too.
        assert app.selected_contact is luigi
        assert app._unread_counts["signal:+392"] == 0
        assert luigi_header.display is True
        assert mario_header.display is True


# ─── A. ctrl+u binding + check_action gate ───────────────────────────────────


class TestUnreadBinding:
    def test_ctrl_u_binding_is_priority(self):
        app = _make_app()
        binding = next(
            (b for b in app.BINDINGS if b.action == "toggle_unread_filter"), None
        )
        assert binding is not None
        assert binding.key == "ctrl+u"
        assert binding.priority is True

    def test_message_text_area_drops_ctrl_u(self):
        def _key(binding):
            return binding.key if hasattr(binding, "key") else binding[0]

        keys = [_key(b) for b in MessageTextArea.BINDINGS]
        assert not any("ctrl+u" in k for k in keys)

    def test_check_action_gates_toggle_unread_filter(self):
        from contact_picker import ContactPickerScreen

        app = _make_app()
        app._screen_stacks["_default"].append(ContactPickerScreen())
        assert app.check_action("toggle_unread_filter", ()) is False
        assert app.check_action("cycle_protocol_filter", ()) is False


# ─── B. StatusSegment / StatusBar widget ─────────────────────────────────────


class TestStatusWidget:
    def test_status_segment_click_posts_pressed(self):
        seg = StatusSegment("whatsapp", id="status-whatsapp")
        assert seg.protocol == "whatsapp"
        posted = []
        seg.post_message = lambda msg: posted.append(msg)
        ev = MagicMock()
        seg.on_click(ev)
        ev.stop.assert_called_once()
        assert len(posted) == 1
        assert posted[0].protocol == "whatsapp"

    def test_status_bar_compose_structure(self):
        bar = StatusBar()
        widgets = list(bar.compose())
        assert [w.id for w in widgets] == [
            "status-signal",
            "status-whatsapp",
            "status-telegram",
            "status-text",
        ]
        assert all(isinstance(w, StatusSegment) for w in widgets[:3])
        assert isinstance(widgets[3], Static)


# ─── B. Click segment → filter state + toggle-off ────────────────────────────


class TestActivateBackendUnread:
    def test_click_backend_with_unread_sets_proto_and_unread(self):
        wa = _contact(PROTOCOL_WHATSAPP, "wa1", "Anna")
        app = _make_app(wa)
        app._unread_counts = {"whatsapp:wa1": 3}
        app._apply_contact_filter = MagicMock()
        app._sync_status_segments = MagicMock()

        app._activate_backend_unread(PROTOCOL_WHATSAPP)

        assert app._protocol_filter == PROTOCOL_WHATSAPP
        assert app._unread_only is True
        app._apply_contact_filter.assert_called_once()
        app._sync_status_segments.assert_called_once()

    def test_click_backend_without_unread_sets_proto_only(self):
        wa = _contact(PROTOCOL_WHATSAPP, "wa1", "Anna")
        app = _make_app(wa)
        app._unread_counts = {}
        app._apply_contact_filter = MagicMock()
        app._sync_status_segments = MagicMock()

        app._activate_backend_unread(PROTOCOL_WHATSAPP)

        assert app._protocol_filter == PROTOCOL_WHATSAPP
        assert app._unread_only is False

    def test_reclick_toggles_off_only_unread(self):
        wa = _contact(PROTOCOL_WHATSAPP, "wa1", "Anna")
        app = _make_app(wa)
        app._protocol_filter = PROTOCOL_WHATSAPP
        app._unread_only = True
        app._apply_contact_filter = MagicMock()
        app._sync_status_segments = MagicMock()

        app._activate_backend_unread(PROTOCOL_WHATSAPP)

        assert app._unread_only is False
        assert app._protocol_filter == PROTOCOL_WHATSAPP  # NOT cycled

    def test_click_switches_protocol_with_unread(self):
        tg = _contact(PROTOCOL_TELEGRAM, "42", "Tg")
        app = _make_app(tg)
        app._protocol_filter = PROTOCOL_SIGNAL
        app._unread_only = True
        app._unread_counts = {"telegram:42": 5}
        app._apply_contact_filter = MagicMock()
        app._sync_status_segments = MagicMock()

        app._activate_backend_unread(PROTOCOL_TELEGRAM)

        assert app._protocol_filter == PROTOCOL_TELEGRAM
        assert app._unread_only is True

    def test_click_switches_protocol_without_unread(self):
        tg = _contact(PROTOCOL_TELEGRAM, "42", "Tg")
        app = _make_app(tg)
        app._protocol_filter = PROTOCOL_SIGNAL
        app._unread_only = True
        app._unread_counts = {}
        app._apply_contact_filter = MagicMock()
        app._sync_status_segments = MagicMock()

        app._activate_backend_unread(PROTOCOL_TELEGRAM)

        assert app._protocol_filter == PROTOCOL_TELEGRAM
        assert app._unread_only is False

    def test_on_status_segment_pressed_ignored_when_modal(self):
        app = _make_app()
        app._screen_stacks["_default"].append(ModalScreen())
        app._activate_backend_unread = MagicMock()
        ev = MagicMock()
        ev.protocol = PROTOCOL_WHATSAPP

        app.on_status_segment_pressed(ev)

        app._activate_backend_unread.assert_not_called()

    def test_on_status_segment_pressed_delegates_when_not_modal(self):
        app = _make_app()
        app._screen_stacks["_default"].append(MagicMock())
        app._activate_backend_unread = MagicMock()
        ev = MagicMock()
        ev.protocol = PROTOCOL_SIGNAL

        app.on_status_segment_pressed(ev)

        app._activate_backend_unread.assert_called_once_with(PROTOCOL_SIGNAL)


# ─── B. ctrl+a → all view ────────────────────────────────────────────────────


class TestGoToAll:
    def test_action_go_to_all_resets_filter(self):
        app = _make_app()
        app._protocol_filter = PROTOCOL_WHATSAPP
        app._unread_only = True
        app._apply_contact_filter = MagicMock()
        app._sync_status_segments = MagicMock()

        app.action_go_to_all()

        assert app._protocol_filter == "all"
        assert app._unread_only is False
        app._apply_contact_filter.assert_called_once()
        app._sync_status_segments.assert_called_once()

    def test_ctrl_a_binding_is_priority_and_hidden(self):
        app = _make_app()
        binding = next(b for b in app.BINDINGS if b.action == "go_to_all")
        assert binding.key == "ctrl+a"
        assert binding.priority is True
        assert binding.show is False

    def test_check_action_gates_go_to_all(self):
        from contact_picker import ContactPickerScreen

        app = _make_app()
        app._screen_stacks["_default"].append(ContactPickerScreen())
        assert app.check_action("go_to_all", ()) is False


# ─── B. Status segment sync (unit) ───────────────────────────────────────────


class TestSyncStatusSegments:
    def test_sync_calls_bar_with_state(self):
        app = _make_app()
        bar = MagicMock()
        app.query_one = MagicMock(return_value=bar)
        app._protocol_filter = PROTOCOL_WHATSAPP
        app._unread_only = True

        app._sync_status_segments()

        bar.sync_active.assert_called_once_with(PROTOCOL_WHATSAPP, True)

    def test_sync_survives_query_one_failure(self):
        app = _make_app()
        app.query_one = MagicMock(side_effect=RuntimeError("no DOM"))

        app._sync_status_segments()  # must not raise

    def test_cycle_protocol_filter_syncs_segments(self):
        app = _make_app()
        app._apply_contact_filter = MagicMock()
        app._sync_status_segments = MagicMock()

        app.action_cycle_protocol_filter()

        app._sync_status_segments.assert_called_once()


# ─── B. Integration: ctrl+u, click, status display, -active ──────────────────


@pytest.mark.integration
async def test_ctrl_u_toggles_filter_keeps_input_text(app_for_test):
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one("#message-input", MessageTextArea)
        editor.focus()
        editor.text = "hello"
        editor.move_cursor((0, len(editor.text)))

        await pilot.press("ctrl+u")
        await pilot.pause()

        assert app._unread_only is True
        assert editor.text == "hello"
        assert editor.has_focus


@pytest.mark.integration
async def test_ctrl_u_noop_with_picker_open(app_for_test):
    from contact_picker import ContactPickerScreen

    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, ContactPickerScreen)

        await pilot.press("ctrl+u")
        await pilot.pause()

        assert app._unread_only is False


@pytest.mark.integration
async def test_segment_click_sets_filter_and_reclick_toggles_off(app_for_test):
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()
        app._unread_counts["signal:+391234567890"] = 2
        app._render_backend_unread_status()
        await pilot.pause()

        seg = app.query_one("#status-signal", StatusSegment)
        await pilot.click(seg)
        await pilot.pause()

        assert app._protocol_filter == PROTOCOL_SIGNAL
        assert app._unread_only is True
        assert seg.has_class("status-segment-active")

        await pilot.click(seg)
        await pilot.pause()

        assert app._unread_only is False
        assert app._protocol_filter == PROTOCOL_SIGNAL
        assert not seg.has_class("status-segment-active")


@pytest.mark.integration
async def test_segment_click_without_unread_sets_proto_only(app_for_test):
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()
        app._unread_counts["signal:+391234567890"] = 2
        app._render_backend_unread_status()
        await pilot.pause()

        # WhatsApp has no unread → clicking it only filters by protocol.
        seg = app.query_one("#status-whatsapp", StatusSegment)
        await pilot.click(seg)
        await pilot.pause()

        assert app._protocol_filter == PROTOCOL_WHATSAPP
        assert app._unread_only is False
        assert not seg.has_class("status-segment-active")


@pytest.mark.integration
async def test_ctrl_a_resets_to_all_keeps_input(app_for_test):
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one("#message-input", MessageTextArea)
        editor.focus()
        editor.text = "hello"
        editor.move_cursor((0, len(editor.text)))

        # Enter a protocol + unread state via the segment click.
        app._unread_counts["signal:+391234567890"] = 2
        app._activate_backend_unread(PROTOCOL_SIGNAL)
        await pilot.pause()
        assert app._protocol_filter == PROTOCOL_SIGNAL
        assert app._unread_only is True

        await pilot.press("ctrl+a")
        await pilot.pause()

        assert app._protocol_filter == "all"
        assert app._unread_only is False
        assert editor.text == "hello"
        assert editor.has_focus
        title = app.query_one("#ContactsTitle")
        assert title.content == "📇 Contacts - All"
        assert not app.query_one("#status-signal", StatusSegment).has_class(
            "status-segment-active"
        )


@pytest.mark.integration
async def test_ctrl_a_noop_with_picker_open(app_for_test):
    from contact_picker import ContactPickerScreen

    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()
        app._protocol_filter = PROTOCOL_WHATSAPP
        app._unread_only = True
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, ContactPickerScreen)

        await pilot.press("ctrl+a")
        await pilot.pause()

        assert app._protocol_filter == PROTOCOL_WHATSAPP
        assert app._unread_only is True


@pytest.mark.integration
async def test_active_class_syncs_after_ctrl_w_and_ctrl_u(app_for_test):
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()
        signal = app.query_one("#status-signal", StatusSegment)
        whatsapp = app.query_one("#status-whatsapp", StatusSegment)

        # Ctrl+W cycles to "signal" but unread_only is off → no active segment.
        await pilot.press("ctrl+w")
        await pilot.pause()
        assert app._protocol_filter == PROTOCOL_SIGNAL
        assert not signal.has_class("status-segment-active")
        assert not whatsapp.has_class("status-segment-active")

        # Ctrl+U turns unread-only on → the current protocol segment is active.
        await pilot.press("ctrl+u")
        await pilot.pause()
        assert app._unread_only is True
        assert signal.has_class("status-segment-active")
        assert not whatsapp.has_class("status-segment-active")

        # Ctrl+U off → no active segment.
        await pilot.press("ctrl+u")
        await pilot.pause()
        assert not signal.has_class("status-segment-active")


@pytest.mark.integration
async def test_status_hides_segments_and_clear_restores(app_for_test):
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()
        app._unread_counts["signal:+391234567890"] = 2
        app._render_backend_unread_status()
        signal = app.query_one("#status-signal", StatusSegment)
        status_text = app.query_one("#status-text", Static)
        assert signal.display is True

        app._status("Ciao", 0)

        assert status_text.content == "Ciao"
        assert signal.display is False
        assert app._status_active is True

        app._status_clear()

        assert signal.display is True
        assert app.query_one("#status-signal", Static).content == "📱 2"
        assert app.query_one("#status-whatsapp", Static).content == "💬 -"
        assert app.query_one("#status-telegram", Static).content == "📨 -"
        assert app._status_active is False


@pytest.mark.integration
async def test_status_segments_right_aligned(app_for_test):
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()
        app._unread_counts["signal:+391234567890"] = 2
        app._render_backend_unread_status()
        await pilot.pause()

        bar = app.query_one("#status-bar")
        signal = app.query_one("#status-signal", StatusSegment)
        telegram = app.query_one("#status-telegram", StatusSegment)

        # The rightmost segment is flush with the bar's right edge…
        assert telegram.region.right == bar.region.right
        # …and the segments are packed right (not left-aligned).
        assert signal.region.x > bar.region.x
