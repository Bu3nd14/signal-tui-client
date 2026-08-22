"""
Regression tests for Sprint 2 — contact grouping by person in the main list.

Covers the projection (``_visible_rows``), the group header label
(``_group_label``), the collapse/expand toggle (default COLLAPSED), the
header/member click dispatch, the filter×group interaction, the header
highlight fallback, the aggregate unread badge, and block reordering.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from models import (
    PROTOCOL_SIGNAL,
    PROTOCOL_TELEGRAM,
    PROTOCOL_WHATSAPP,
    ChatContact,
)
from signal_tui import SignalTUI
from tui.contacts import _Row


def _contact(
    protocol: str, cid: str, name: str, phone: str | None = None, ts: int = 0
) -> ChatContact:
    extras: dict[str, object] = {}
    if phone:
        extras["phone"] = phone
    c = ChatContact(id=cid, display_name=name, protocol=protocol, extras=extras)
    c.last_message_ts = ts
    return c


def _make_app(*contacts) -> SignalTUI:
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


# ─── Projection ──────────────────────────────────────────────────────────────


class TestProjection:
    def test_header_for_every_contact_including_single_member(self):
        app = _make_app(_contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"))
        rows = app._visible_rows()
        assert [r.kind for r in rows] == ["group", "member"]
        assert rows[0].key == "person:phone:391"
        assert rows[0].group_key == "phone:391"
        assert rows[1].key == "signal:+391"
        assert rows[1].group_key == "phone:391"

    def test_person_and_cache_keys_are_disjoint(self):
        app = _make_app(
            _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"),
            _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391"),
        )
        rows = app._visible_rows()
        group_keys = {r.key for r in rows if r.kind == "group"}
        member_keys = {r.key for r in rows if r.kind == "member"}
        assert group_keys == {"person:phone:391"}
        assert member_keys == {"signal:+391", "whatsapp:wa:1@s.whatsapp.net"}
        assert group_keys.isdisjoint(member_keys)

    def test_groups_ordered_by_default_recency(self):
        app = _make_app(
            _contact(PROTOCOL_SIGNAL, "+391", "Vecchio", phone="391", ts=100),
            _contact(PROTOCOL_SIGNAL, "+392", "Recente", phone="392", ts=200),
        )
        rows = app._visible_rows()
        group_keys = [r.key for r in rows if r.kind == "group"]
        assert group_keys == ["person:phone:392", "person:phone:391"]

    def test_member_order_fixed_protocol_priority_even_against_recency(self):
        app = _make_app(
            _contact(PROTOCOL_TELEGRAM, "42", "Tg", phone="391", ts=300),
            _contact(
                PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391", ts=5000
            ),
            _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391", ts=100),
        )
        rows = app._visible_rows()
        member_keys = [r.key for r in rows if r.kind == "member"]
        # Signal → WhatsApp → Telegram, regardless of last-message recency.
        assert member_keys == [
            "signal:+391",
            "whatsapp:wa:1@s.whatsapp.net",
            "telegram:42",
        ]

    def test_mamma_vod_single_member_group_has_header(self):
        app = _make_app(
            ChatContact(id="42", display_name="Mamma Vod", protocol=PROTOCOL_TELEGRAM)
        )
        rows = app._visible_rows()
        assert [r.kind for r in rows] == ["group", "member"]
        assert rows[0].key == "person:raw:telegram:42"
        assert rows[0].entry.display_name == "Mamma Vod"
        assert rows[1].key == "telegram:42"


# ─── Header label ────────────────────────────────────────────────────────────


class TestGroupLabel:
    def _entry(self, app):
        return app._visible_rows()[0].entry

    def test_no_emoji_in_header(self):
        app = _make_app(
            _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"),
            _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391"),
        )
        label = app._group_label(self._entry(app))
        assert "📱" not in label and "💬" not in label and "📨" not in label
        assert "Mario" in label

    def test_chevron_reflects_collapse_state(self):
        app = _make_app(_contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"))
        entry = self._entry(app)
        assert app._group_label(entry).startswith("▸")  # collapsed by default
        app._expanded_groups.add(entry.key)
        assert app._group_label(entry).startswith("▾")

    def test_badge_sums_multi_member_unread(self):
        app = _make_app(
            _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"),
            _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391"),
        )
        app._unread_counts = {"signal:+391": 2, "whatsapp:wa:1@s.whatsapp.net": 3}
        label = app._group_label(self._entry(app))
        assert " *5" in label

    def test_badge_suppressed_when_selected_in_group(self):
        app = _make_app(
            _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"),
            _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391"),
        )
        app._unread_counts = {"signal:+391": 2, "whatsapp:wa:1@s.whatsapp.net": 3}
        app.selected_contact = app.contacts[0]  # Mario (signal)
        label = app._group_label(self._entry(app))
        assert " *" not in label

    def test_no_chevron_when_filter_active(self):
        app = _make_app(
            _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"),
            _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391"),
        )
        app._protocol_filter = "signal"
        app._unread_counts = {"signal:+391": 2}
        label = app._group_label(self._entry(app))
        # No chevron in filter mode: name + bare unread badge (no emoji).
        assert label == "Mario *2"

    def test_filter_badge_single_backend_with_unread(self):
        app = _make_app(
            _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"),
            _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391"),
        )
        app._protocol_filter = "signal"
        app._unread_counts = {"signal:+391": 2}
        label = app._group_label(self._entry(app))
        assert label == "Mario *2"

    def test_filter_badge_only_filtered_protocol_unread(self):
        # Filter signal with unread on BOTH members: only signal's is shown.
        app = _make_app(
            _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"),
            _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391"),
        )
        app._protocol_filter = "signal"
        app._unread_counts = {"signal:+391": 2, "whatsapp:wa:1@s.whatsapp.net": 3}
        label = app._group_label(self._entry(app))
        assert label == "Mario *2"

    def test_filter_badge_whatsapp_filter_shows_only_whatsapp(self):
        app = _make_app(
            _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"),
            _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391"),
        )
        app._protocol_filter = "whatsapp"
        app._unread_counts = {"whatsapp:wa:1@s.whatsapp.net": 1}
        label = app._group_label(self._entry(app))
        assert label == "Mario *1"

    def test_filter_badge_ignores_other_backends(self):
        # Higher unread on other backends is not shown under a single filter.
        app = _make_app(
            _contact(PROTOCOL_TELEGRAM, "42", "Tg", phone="391"),
            _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391"),
            _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"),
        )
        app._protocol_filter = "signal"
        app._unread_counts = {
            "telegram:42": 9,
            "whatsapp:wa:1@s.whatsapp.net": 7,
            "signal:+391": 1,
        }
        label = app._group_label(self._entry(app))
        assert label == "Mario *1"

    def test_filter_badge_zero_unread_omitted(self):
        app = _make_app(
            _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"),
            _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391"),
            _contact(PROTOCOL_TELEGRAM, "42", "Tg", phone="391"),
        )
        app._protocol_filter = "signal"
        app._unread_counts = {"signal:+391": 2}
        label = app._group_label(self._entry(app))
        assert label == "Mario *2"
        assert "💬" not in label and "📨" not in label and "📱" not in label

    def test_filter_badge_own_backend_bare_no_emoji(self):
        app = _make_app(
            _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"),
            _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391"),
            _contact(PROTOCOL_TELEGRAM, "42", "Tg", phone="391"),
        )
        app._protocol_filter = "whatsapp"
        app._unread_counts = {"whatsapp:wa:1@s.whatsapp.net": 3}
        label = app._group_label(self._entry(app))
        assert label == "Mario *3"

    def test_filter_badge_none_when_all_zero(self):
        app = _make_app(
            _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"),
            _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391"),
        )
        app._protocol_filter = "signal"
        app._unread_counts = {}
        label = app._group_label(self._entry(app))
        assert label == "Mario"

    def test_filter_badge_selection_does_not_affect_filter(self):
        # No selected-member exclusion in filter mode: the badge is the filtered
        # member's unread regardless of which contact is selected.
        sig = _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391")
        wa = _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391")
        other = _contact(PROTOCOL_SIGNAL, "+392", "Bruno", phone="392")
        app = _make_app(sig, wa, other)
        app._protocol_filter = "signal"
        app._unread_counts = {"signal:+391": 2, "whatsapp:wa:1@s.whatsapp.net": 3}
        rows = app._visible_rows()
        entry = next(
            r.entry for r in rows if r.kind == "group" and r.group_key == "phone:391"
        )

        app.selected_contact = sig  # Mario belongs to phone:391
        assert app._group_label(entry) == "Mario *2"

        app.selected_contact = other  # Bruno belongs to a different group
        assert app._group_label(entry) == "Mario *2"


# ─── Member label ─────────────────────────────────────────────────────────────


class TestMemberLabel:
    def test_protocol_name_for_all_protocols(self):
        app = _make_app(
            _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"),
            _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391"),
            _contact(PROTOCOL_TELEGRAM, "42", "Tg", phone="391"),
        )
        names = ("Mario", "Anna", "Tg")
        labels = [app._member_label(c) for c in app.contacts]
        assert labels == ["📱 Signal", "💬 WhatsApp", "📨 Telegram"]
        # Il nome del contatto non deve essere ripetuto nella riga membro.
        assert all(name not in label for name, label in zip(names, labels))

    def test_unread_badge(self):
        app = _make_app(_contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"))
        contact = app.contacts[0]
        app._unread_counts[contact.cache_key] = 3
        assert " *3" in app._member_label(contact)

    def test_badge_suppressed_when_selected(self):
        app = _make_app(_contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"))
        contact = app.contacts[0]
        app.selected_contact = contact
        app._unread_counts[contact.cache_key] = 3
        assert " *" not in app._member_label(contact)

    def test_typing_icons(self):
        app = _make_app(_contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"))
        contact = app.contacts[0]
        app._typing_contacts[contact.cache_key] = 100.0
        assert "✍️" in app._member_label(contact)

        app._typing_contacts = {}
        app._typing_mumbling[contact.cache_key] = 100.0
        label = app._member_label(contact)
        assert "💭" in label
        assert "✍️" not in label


# ─── Default collapsed + toggle ──────────────────────────────────────────────


class TestToggleAndDefaultCollapsed:
    def _two_member_app(self):
        return _make_app(
            _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"),
            _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391"),
        )

    def test_default_collapsed_shows_only_headers(self):
        app = self._two_member_app()
        fake = _render(app)
        assert len(fake.items) == 3  # 1 header + 2 members, full DOM preserved
        assert all(it.display is False for it in _member_rows(fake))
        assert all(it.display is True for it in _group_rows(fake))

    def test_toggle_expands_then_collapses_without_clear(self):
        app = self._two_member_app()
        fake = _render(app)
        clears = []
        fake.clear = lambda: (clears.append(True), fake.items.clear())

        app._toggle_group("phone:391")
        assert "phone:391" in app._expanded_groups
        assert all(it.display is True for it in _member_rows(fake))
        assert clears == []  # toggle is display-only: no clear

        app._toggle_group("phone:391")
        assert "phone:391" not in app._expanded_groups
        assert all(it.display is False for it in _member_rows(fake))
        assert len(fake.items) == 3  # DOM never rebuilt

    def test_toggle_updates_header_chevron(self):
        app = self._two_member_app()
        fake = _render(app)
        header = _group_rows(fake)[0]
        assert header._label_text.startswith("▸")
        app._toggle_group("phone:391")
        assert header._label_text.startswith("▾")
        app._toggle_group("phone:391")
        assert header._label_text.startswith("▸")

    def test_toggle_does_not_call_render_contact_list(self):
        app = self._two_member_app()
        _render(app)
        app._render_contact_list = MagicMock()
        app._toggle_group("phone:391")
        app._render_contact_list.assert_not_called()


# ─── Click/Enter dispatch ────────────────────────────────────────────────────


class TestClickDispatch:
    def test_header_selected_only_toggles_never_selects(self):
        app = _make_app(_contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"))
        item = MagicMock()
        item._row_kind = "group"
        item._group_key = "phone:391"
        event = MagicMock(item=item)
        app._select_contact = MagicMock()
        app._toggle_group = MagicMock()

        app.on_list_view_selected(event)

        app._toggle_group.assert_called_once_with("phone:391")
        app._select_contact.assert_not_called()

    def test_member_selected_selects_contact(self):
        contact = _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391")
        app = _make_app(contact)
        item = MagicMock()
        item._row_kind = "member"
        item._contact_id = contact.cache_key
        event = MagicMock(item=item)
        app._select_contact = MagicMock()
        app._toggle_group = MagicMock()

        app.on_list_view_selected(event)

        app._select_contact.assert_called_once_with(contact)
        app._toggle_group.assert_not_called()

    def test_member_already_selected_is_skipped(self):
        contact = _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391")
        app = _make_app(contact)
        app.selected_contact = contact
        item = MagicMock()
        item._row_kind = "member"
        item._contact_id = contact.cache_key
        event = MagicMock(item=item)
        app._select_contact = MagicMock()

        app.on_list_view_selected(event)

        app._select_contact.assert_not_called()

    def test_header_selected_with_filter_selects_member(self):
        sig = _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391")
        wa = _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391")
        app = _make_app(sig, wa)
        app._visible_rows()  # populate _group_members
        app._protocol_filter = "signal"
        item = MagicMock()
        item._row_kind = "group"
        item._group_key = "phone:391"
        event = MagicMock(item=item)
        app._select_contact = MagicMock()
        app._toggle_group = MagicMock()

        app.on_list_view_selected(event)

        app._select_contact.assert_called_once_with(sig)
        app._toggle_group.assert_not_called()
        assert app._expanded_groups == set()  # no expansion

    def test_header_selected_with_whatsapp_filter_selects_wa_member(self):
        sig = _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391")
        wa = _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391")
        app = _make_app(sig, wa)
        app._visible_rows()
        app._protocol_filter = "whatsapp"
        item = MagicMock()
        item._row_kind = "group"
        item._group_key = "phone:391"
        event = MagicMock(item=item)
        app._select_contact = MagicMock()

        app.on_list_view_selected(event)

        app._select_contact.assert_called_once_with(wa)
        assert app._expanded_groups == set()

    def test_header_selected_with_filter_noop_when_member_missing(self):
        app = _make_app(_contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"))
        app._visible_rows()
        app._protocol_filter = "telegram"  # no telegram member in the group
        item = MagicMock()
        item._row_kind = "group"
        item._group_key = "phone:391"
        event = MagicMock(item=item)
        app._select_contact = MagicMock()
        app._toggle_group = MagicMock()

        app.on_list_view_selected(event)

        # Defensive no-op: never toggle nor expand while a single protocol is
        # filtered.
        app._select_contact.assert_not_called()
        app._toggle_group.assert_not_called()

    def test_header_selected_with_filter_skips_when_already_selected(self):
        sig = _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391")
        wa = _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391")
        app = _make_app(sig, wa)
        app._visible_rows()
        app._protocol_filter = "signal"
        app.selected_contact = sig
        item = MagicMock()
        item._row_kind = "group"
        item._group_key = "phone:391"
        event = MagicMock(item=item)
        app._select_contact = MagicMock()
        app._toggle_group = MagicMock()

        app.on_list_view_selected(event)

        app._select_contact.assert_not_called()
        app._toggle_group.assert_not_called()


# ─── Filter × group × collapse ───────────────────────────────────────────────


class TestFilterWithGroups:
    def test_group_without_filtered_protocol_header_hidden(self):
        app = _make_app(_contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"))
        app._protocol_filter = "whatsapp"
        fake = _render(app)
        header = _group_rows(fake)[0]
        member = _member_rows(fake)[0]
        assert header.display is False
        assert member.display is False

    def test_mixed_group_header_visible_members_masked(self):
        app = _make_app(
            _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"),
            _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391"),
        )
        app._protocol_filter = "signal"
        fake = _render(app)
        header = _group_rows(fake)[0]
        assert header.display is True  # signal member exists in group
        app._toggle_group("phone:391")  # expand (state untouched by filter)
        sig_member = next(
            it for it in _member_rows(fake) if it._contact_id == "signal:+391"
        )
        wa_member = next(
            it
            for it in _member_rows(fake)
            if it._contact_id == "whatsapp:wa:1@s.whatsapp.net"
        )
        # Filter mode (single protocol) masks EVERY member row, even the
        # matching one and even when the group is expanded.
        assert sig_member.display is False
        assert wa_member.display is False
        # The collapse state itself is never mutated by the filter.
        assert "phone:391" in app._expanded_groups

    def test_filter_times_collapse_hides_all_members(self):
        app = _make_app(
            _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"),
            _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391"),
        )
        app._protocol_filter = "signal"
        fake = _render(app)
        header = _group_rows(fake)[0]
        assert header.display is True
        # Still collapsed: even the matching member is hidden.
        assert all(it.display is False for it in _member_rows(fake))


# ─── Filter masking: _row_visible ────────────────────────────────────────────


class TestRowVisibleFilter:
    def _mixed_app(self):
        sig = _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391")
        wa = _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391")
        app = _make_app(sig, wa)
        app._group_members = {"phone:391": [sig.cache_key, wa.cache_key]}
        return app, sig, wa

    def test_filter_masks_member_even_if_expanded(self):
        app, sig, _ = self._mixed_app()
        app._protocol_filter = "signal"
        app._expanded_groups.add("phone:391")
        row = _Row("member", sig.cache_key, "phone:391", contact=sig)
        assert app._row_visible(row, {sig.cache_key}) is False

    def test_filter_keeps_header_when_member_matches(self):
        app, sig, _ = self._mixed_app()
        app._protocol_filter = "signal"
        row = _Row("group", "person:phone:391", "phone:391")
        assert app._row_visible(row, {sig.cache_key}) is True

    def test_filter_hides_header_without_matching_member(self):
        app, _, _ = self._mixed_app()
        app._protocol_filter = "telegram"
        row = _Row("group", "person:phone:391", "phone:391")
        assert app._row_visible(row, set()) is False

    def test_all_mode_member_visible_when_expanded(self):
        app, sig, wa = self._mixed_app()
        app._protocol_filter = "all"
        app._expanded_groups.add("phone:391")
        row = _Row("member", sig.cache_key, "phone:391", contact=sig)
        assert app._row_visible(row, {sig.cache_key, wa.cache_key}) is True

    def test_all_mode_member_hidden_when_collapsed(self):
        app, sig, wa = self._mixed_app()
        app._protocol_filter = "all"
        row = _Row("member", sig.cache_key, "phone:391", contact=sig)
        assert app._row_visible(row, {sig.cache_key, wa.cache_key}) is False


# ─── Filter member resolution ─────────────────────────────────────────────────


class TestGroupMemberForFilter:
    def test_none_when_filter_all(self):
        sig = _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391")
        wa = _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391")
        app = _make_app(sig, wa)
        app._visible_rows()
        assert app._group_member_for_filter("phone:391") is None

    def test_resolves_protocol_member(self):
        sig = _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391")
        wa = _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391")
        app = _make_app(sig, wa)
        app._visible_rows()
        app._protocol_filter = "whatsapp"
        assert app._group_member_for_filter("phone:391") is wa

    def test_none_when_group_missing(self):
        app = _make_app(_contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"))
        app._visible_rows()
        app._protocol_filter = "signal"
        assert app._group_member_for_filter("phone:999") is None


# ─── Header label refresh on filter change ────────────────────────────────────


class TestRefreshHeaderLabels:
    def _query_for(self, fake):
        def _q(selector, *_a, **_k):
            if selector == "#contact-list":
                return fake
            return MagicMock()

        return _q

    def _rendered_app(self):
        app = _make_app(
            _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"),
            _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391"),
        )
        fake = _FakeListView()
        app.query_one = MagicMock(side_effect=self._query_for(fake))
        app._render_contact_list(list(app.contacts))
        return app

    def test_refresh_header_labels_drops_and_restores_chevron(self):
        app = self._rendered_app()
        header = app._group_widgets["phone:391"]
        assert header._label_text.startswith("▸")

        app._protocol_filter = "signal"
        app._refresh_header_labels()
        assert header._label_text == "Mario"

        app._protocol_filter = "all"
        app._refresh_header_labels()
        assert header._label_text.startswith("▸")

    def test_apply_contact_filter_refreshes_header_labels(self):
        app = self._rendered_app()
        header = app._group_widgets["phone:391"]
        assert header._label_text.startswith("▸")

        # _apply_contact_filter also touches title/border widgets → all mocks.
        app.query_one = MagicMock(return_value=MagicMock())
        app._protocol_filter = "signal"
        app._apply_contact_filter()
        assert header._label_text == "Mario"

        app._protocol_filter = "all"
        app._apply_contact_filter()
        assert header._label_text.startswith("▸")

    def test_refresh_skips_item_without_entry(self):
        app = _make_app()

        class _BareItem:
            pass

        app._group_widgets = {"phone:391": _BareItem()}
        app._refresh_header_labels()  # must not raise

    def test_refresh_noop_when_label_unchanged(self):
        app = self._rendered_app()
        header = app._group_widgets["phone:391"]
        # Label already reflects the current "all" filter → no-op branch.
        app._refresh_header_labels()
        assert header._label_text.startswith("▸")

    def test_refresh_updates_child_label(self):
        app = self._rendered_app()
        entry = app._visible_rows()[0].entry

        class _HeaderItem:
            def __init__(self):
                self._entry = entry
                self._label_text = "stale"
                self.children = [MagicMock()]

        item = _HeaderItem()
        app._group_widgets = {"phone:391": item}
        app._refresh_header_labels()
        assert item._label_text.startswith("▸")
        item.children[0].update.assert_called_once()


# ─── Header highlight fallback ───────────────────────────────────────────────


class TestHeaderHighlight:
    def test_select_contact_with_collapsed_group_highlights_header(self):
        app = _make_app(
            _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"),
            _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391"),
        )
        fake = _render(app)
        fake_list = MagicMock()
        fake_list.children = list(fake.items)
        fake_list.index = None
        _prepare_select(app, fake_list)

        # Mario's group is collapsed: selecting Mario highlights the header.
        app._select_contact(app.contacts[0])

        header = app._group_widgets["phone:391"]
        assert fake_list.index == fake_list.children.index(header)
        # No auto-expansion on selection.
        assert app._expanded_groups == set()

    def test_picker_select_into_collapsed_group_no_auto_expand(self):
        app = _make_app(
            _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"),
            _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391"),
        )
        fake = _render(app)
        fake_list = MagicMock()
        fake_list.children = list(fake.items)
        fake_list.index = None
        _prepare_select(app, fake_list)

        app._select_contact(app.contacts[0])

        assert app._expanded_groups == set()
        header = app._group_widgets["phone:391"]
        assert fake_list.index == fake_list.children.index(header)

    def test_select_contact_with_visible_member_highlights_member(self):
        app = _make_app(
            _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"),
            _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391"),
        )
        fake = _render(app)
        app._toggle_group("phone:391")  # expand → members visible
        fake_list = MagicMock()
        fake_list.children = list(fake.items)
        fake_list.index = None
        _prepare_select(app, fake_list)

        app._select_contact(app.contacts[0])

        member = app._contact_widgets["signal:+391"]
        assert fake_list.index == fake_list.children.index(member)


# ─── Aggregate unread badge ──────────────────────────────────────────────────


class TestHeaderBadge:
    def test_header_badge_after_flush(self):
        app = _make_app(
            _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"),
            _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391"),
        )
        app._cache = {
            "signal:+391": [
                {"is_mine": False, "read": False, "timestamp": 1, "text": "a"}
            ],
            "whatsapp:wa:1@s.whatsapp.net": [
                {"is_mine": False, "read": False, "timestamp": 1, "text": "b"}
            ],
        }
        _render(app)
        header = app._group_widgets["phone:391"]
        assert " *" not in header._label_text

        # Poll flush: recompute unread data, then the single re-sort/render.
        app._recompute_unread()
        app._render_contact_list(list(app.contacts))

        assert " *2" in header._label_text

    def test_header_badge_filtered_protocol_only_after_flush(self):
        app = _make_app(
            _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"),
            _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391"),
        )
        app._protocol_filter = "signal"
        app._cache = {
            "signal:+391": [
                {"is_mine": False, "read": False, "timestamp": 1, "text": "a"}
            ],
            "whatsapp:wa:1@s.whatsapp.net": [
                {"is_mine": False, "read": False, "timestamp": 1, "text": "b"}
            ],
        }
        _render(app)
        header = app._group_widgets["phone:391"]
        assert " *" not in header._label_text

        # Poll flush: recompute unread data, then the single re-sort/render.
        app._recompute_unread()
        app._render_contact_list(list(app.contacts))

        # Filter mode shows only the filtered protocol's unread (no breakdown).
        assert header._label_text == "Mario *1"

    def test_header_badge_suppressed_after_select_contact(self):
        app = _make_app(
            _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391"),
            _contact(PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391"),
        )
        app._unread_counts = {"signal:+391": 2, "whatsapp:wa:1@s.whatsapp.net": 3}
        fake = _render(app)
        header = app._group_widgets["phone:391"]
        assert " *5" in header._label_text

        fake_list = MagicMock()
        fake_list.children = list(fake.items)
        fake_list.index = None
        _prepare_select(app, fake_list)
        app._cache = {}
        app._select_contact(app.contacts[0])

        # The header label is refreshed in-place with the badge suppressed.
        assert " *" not in header._label_text


# ─── Block reorder ───────────────────────────────────────────────────────────


class TestReorderBlock:
    def test_new_message_moves_group_block_together(self):
        a_sig = _contact(PROTOCOL_SIGNAL, "+391", "Mario", phone="391", ts=100)
        a_wa = _contact(
            PROTOCOL_WHATSAPP, "wa:1@s.whatsapp.net", "Anna", phone="391", ts=100
        )
        b_sig = _contact(PROTOCOL_SIGNAL, "+392", "Bruno", phone="392", ts=200)
        app = _make_app(a_sig, a_wa, b_sig)
        app._protocol_filter = "all"
        fake = _FakeListView()
        app.query_one = MagicMock(return_value=fake)

        app._render_contact_list(list(app.contacts))
        # B (more recent) first, then A: [B header, B member, A header, A sig, A wa].
        assert len(fake.items) == 5
        assert [it._contact_id for it in fake.items] == [
            "person:phone:392",
            "signal:+392",
            "person:phone:391",
            "signal:+391",
            "whatsapp:wa:1@s.whatsapp.net",
        ]
        objs_before = list(fake.items)

        # A new message on A's signal member bumps the WHOLE A block to the top.
        a_sig.last_message_ts = 300
        app._sort_contacts()
        app._render_contact_list(list(app.contacts))

        assert [it._contact_id for it in fake.items] == [
            "person:phone:391",
            "signal:+391",
            "whatsapp:wa:1@s.whatsapp.net",
            "person:phone:392",
            "signal:+392",
        ]
        assert set(fake.items) == set(objs_before)  # same objects, reordered
