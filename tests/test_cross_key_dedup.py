"""Regression tests for the WhatsApp cross-key dedup fix (@lid vs @c.us).

A double JID of the same phone (active ``@c.us`` + ``@lid``, or the address-book
ghost ``@c.us``) splits the SQLite history across two ``contact_number`` values
that can hold the SAME physical message (same ``msg_id``) with a different
``attachment_id`` (WAHA URL on one side, local ``sent-*`` file on the other).

These tests cover:

* the pure helpers in ``models`` (``_cross_key_message_equivalent`` and
  ``dedup_cross_key``);
* the web read-union (``web.api._cross_key_dedup_rows`` / ``_messages``);
* the TUI cache merge (``ChatViewMixin._merge_backend_cache`` and
  ``BackendConnectMixin._on_backend_ready``).

They also try to falsify the fix, documenting the two residual boundaries:
over-collapse of two genuinely distinct messages sharing the whole tuple but a
different ``msg_id``, and the residual duplicate when the twin has NO ``msg_id``
on one side and a different ``attachment_id`` (both marked ``xfail``).
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest

from models import (
    PROTOCOL_SIGNAL,
    PROTOCOL_WHATSAPP,
    ChatContact,
    _cross_key_message_equivalent,
    contact_storage_keys,
    dedup_cross_key,
)
from tui.backend_connect import BackendConnectMixin
from tui.chat_view import ChatViewMixin
from web.api import _cross_key_dedup_rows, _messages

# ─── Helpers ──────────────────────────────────────────────────────────────────

_ROW_COLUMNS = (
    "contact_number",
    "msg_id",
    "text",
    "is_mine",
    "timestamp",
    "msg_type",
    "attachment_id",
)


def _msg(
    contact_number: str,
    msg_id: str | None,
    *,
    text: str = "",
    is_mine: bool = False,
    timestamp: int = 1000,
    msg_type: str = "text",
    attachment_id: str | None = None,
) -> dict[str, Any]:
    """Build one ``messages`` row spec (insertion order == rowid order)."""
    return {
        "contact_number": contact_number,
        "msg_id": msg_id,
        "text": text,
        "is_mine": is_mine,
        "timestamp": timestamp,
        "msg_type": msg_type,
        "attachment_id": attachment_id,
    }


def _make_rows(*specs: dict[str, Any]) -> list[sqlite3.Row]:
    """Materialize row specs as ``sqlite3.Row`` objects (id 1..N in order)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "CREATE TABLE messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, contact_number TEXT, "
            "msg_id TEXT, text TEXT, is_mine INTEGER, timestamp INTEGER, "
            "msg_type TEXT, attachment_id TEXT)"
        )
        for spec in specs:
            values = {column: spec.get(column) for column in _ROW_COLUMNS}
            conn.execute(
                "INSERT INTO messages (contact_number, msg_id, text, is_mine, "
                "timestamp, msg_type, attachment_id) VALUES "
                "(:contact_number, :msg_id, :text, :is_mine, :timestamp, "
                ":msg_type, :attachment_id)",
                values,
            )
        return list(conn.execute("SELECT * FROM messages ORDER BY id"))
    finally:
        conn.close()


def _d(
    msg_id: str | None,
    *,
    text: str = "",
    is_mine: bool = False,
    timestamp: int = 1000,
    msg_type: str = "text",
    attachment_id: str | None = None,
) -> dict[str, Any]:
    """Build a TUI message dict (identity fields only)."""
    return {
        "id": msg_id,
        "text": text,
        "is_mine": is_mine,
        "timestamp": timestamp,
        "msg_type": msg_type,
        "attachment_id": attachment_id,
    }


def _wa_contact(
    contact_id: str, *, phone: str | None = None, lid: str | None = None
) -> ChatContact:
    extras: dict[str, Any] = {}
    if phone is not None:
        extras["phone"] = phone
    if lid is not None:
        extras["lid"] = lid
    return ChatContact(
        id=contact_id,
        display_name="Contact",
        protocol=PROTOCOL_WHATSAPP,
        extras=extras,
    )


class _ChatViewStub:
    """Minimal duck-type for ``ChatViewMixin._merge_backend_cache``."""

    def __init__(self) -> None:
        self._cache: dict[str, list[dict]] = {}
        self.selected_contact = None


class _BackendConnectStub:
    """Minimal duck-type for ``BackendConnectMixin._on_backend_ready``."""

    def __init__(self) -> None:
        self._cache: dict[str, list[dict]] = {}
        self.contacts: list[ChatContact] = []
        self._pending_backends: set[str] = set()
        self.selected_contact = None
        self.selection = None
        self.rendered: list[ChatContact] | None = None

    def _select_contact(self, contact: ChatContact) -> None:
        self.selection = contact

    def _mark_backend_done(self, proto: str) -> None:
        self._pending_backends.discard(proto)

    def _sync_last_ts(self) -> None:
        pass

    def _sort_contacts(self) -> None:
        pass

    def _render_contact_list(self, contacts: list[ChatContact]) -> None:
        self.rendered = list(contacts)

    def _update_unread_badges(self) -> None:
        pass

    def _refresh_backend_status_if_idle(self) -> None:
        pass

    def _status(self, message: str) -> None:
        pass


# ─── Pure helper: contact_storage_keys ───────────────────────────────────────


class TestContactStorageKeys:
    def test_lid_canonical_includes_cus_alias(self):
        contact = _wa_contact("39123@lid", phone="39123")
        assert contact_storage_keys(contact) == ["39123@lid", "39123@c.us"]

    def test_cus_canonical_includes_lid_alias(self):
        contact = _wa_contact("39123@c.us", lid="39123@lid")
        assert contact_storage_keys(contact) == ["39123@c.us", "39123@lid"]

    def test_non_whatsapp_single_key(self):
        contact = ChatContact(
            id="+39123", display_name="X", protocol=PROTOCOL_SIGNAL, extras={}
        )
        assert contact_storage_keys(contact) == ["+39123"]

    def test_group_is_never_expanded(self):
        contact = _wa_contact("12345@g.us", phone="12345")
        assert contact_storage_keys(contact) == ["12345@g.us"]


# ─── Pure helper: _cross_key_message_equivalent / dedup_cross_key ─────────────


class TestCrossKeyEquivalent:
    def test_same_id_ignores_attachment_id(self):
        assert _cross_key_message_equivalent(
            _d("M1", msg_type="image", attachment_id="sent-a.png"),
            _d("M1", msg_type="image", attachment_id="http://waha/a"),
        )

    def test_different_id_identical_tuple_is_equivalent(self):
        # Intended: cross-key id divergence (webhook vs REST) falls back to the
        # exact tuple (same attachment included).
        assert _cross_key_message_equivalent(
            _d("A", attachment_id="same.png"),
            _d("B", attachment_id="same.png"),
        )

    def test_different_id_different_attachment_not_equivalent(self):
        assert not _cross_key_message_equivalent(
            _d("A", msg_type="image", attachment_id="a.png"),
            _d("B", msg_type="image", attachment_id="b.png"),
        )


class TestDedupCrossKeyPure:
    def _dedup(self, records):
        return dedup_cross_key(
            records,
            canonical_key="lid",
            key_of=lambda pair: pair[0],
            normalize=lambda pair: pair[1],
        )

    def test_twins_same_id_different_attachment_collapse(self):
        records = [
            ("lid", _d("M1", msg_type="image", attachment_id="http://waha/a")),
            ("cus", _d("M1", msg_type="image", attachment_id="sent-a.png")),
        ]
        kept = self._dedup(records)
        assert len(kept) == 1
        assert kept[0][0] == "lid"

    def test_different_id_identical_tuple_collapse(self):
        records = [
            ("lid", _d("webhook-id", msg_type="image", attachment_id="same.png")),
            ("cus", _d("rest-id", msg_type="image", attachment_id="same.png")),
        ]
        kept = self._dedup(records)
        assert len(kept) == 1

    def test_two_distinct_images_stay(self):
        records = [
            (
                "lid",
                _d("M1", msg_type="image", attachment_id="a.png", timestamp=1000),
            ),
            (
                "cus",
                _d("M2", msg_type="image", attachment_id="b.png", timestamp=2000),
            ),
        ]
        kept = self._dedup(records)
        assert len(kept) == 2

    def test_single_key_is_no_op(self):
        records = [("lid", _d("M1")), ("lid", _d("M2"))]
        kept = self._dedup(records)
        assert kept is records

    def test_empty_records_is_no_op(self):
        records: list[tuple[str, dict]] = []
        assert self._dedup(records) is records


# ─── API: _cross_key_dedup_rows ───────────────────────────────────────────────


class TestCrossKeyDedupRows:
    def test_alias_rowid_before_canonical_same_msg_id_collapses(self):
        rows = _make_rows(
            # Alias inserted FIRST -> smaller rowid (id=1).
            _msg(
                "39123@c.us",
                "M1",
                is_mine=True,
                msg_type="image",
                attachment_id="sent-a.png",
            ),
            _msg(
                "39123@lid",
                "M1",
                is_mine=True,
                msg_type="image",
                attachment_id="http://waha/a",
            ),
        )
        kept = _cross_key_dedup_rows(rows, "39123@lid")
        assert len(kept) == 1
        assert kept[0]["contact_number"] == "39123@lid"
        assert kept[0]["attachment_id"] == "http://waha/a"

    def test_canonical_first_same_msg_id_collapses(self):
        rows = _make_rows(
            _msg(
                "39123@lid",
                "M1",
                is_mine=True,
                msg_type="image",
                attachment_id="http://waha/a",
            ),
            _msg(
                "39123@c.us",
                "M1",
                is_mine=True,
                msg_type="image",
                attachment_id="sent-a.png",
            ),
        )
        kept = _cross_key_dedup_rows(rows, "39123@lid")
        assert len(kept) == 1
        assert kept[0]["contact_number"] == "39123@lid"

    def test_two_distinct_images_same_key_stay_two(self):
        rows = _make_rows(
            _msg(
                "39123@lid",
                "M1",
                is_mine=True,
                timestamp=1000,
                msg_type="image",
                attachment_id="img-a.png",
            ),
            _msg(
                "39123@lid",
                "M2",
                is_mine=True,
                timestamp=2000,
                msg_type="image",
                attachment_id="img-b.png",
            ),
        )
        kept = _cross_key_dedup_rows(rows, "39123@lid")
        assert len(kept) == 2
        assert {row["msg_id"] for row in kept} == {"M1", "M2"}

    def test_two_incoming_ok_same_key_stay_two(self):
        rows = _make_rows(
            _msg("39123@lid", "ok-1", text="OK", is_mine=False, timestamp=1000),
            _msg("39123@lid", "ok-2", text="OK", is_mine=False, timestamp=2000),
        )
        kept = _cross_key_dedup_rows(rows, "39123@lid")
        assert len(kept) == 2

    def test_multi_attachment_same_key_untouched(self):
        rows = _make_rows(
            *[
                _msg(
                    "39123@lid",
                    "M1",
                    is_mine=True,
                    timestamp=1000,
                    msg_type="image",
                    attachment_id=f"file-{index}.png",
                )
                for index in range(3)
            ]
        )
        kept = _cross_key_dedup_rows(rows, "39123@lid")
        assert len(kept) == 3
        assert [row["attachment_id"] for row in kept] == [
            "file-0.png",
            "file-1.png",
            "file-2.png",
        ]

    def test_single_key_is_no_op(self):
        rows = _make_rows(
            _msg(
                "39123@lid", "M1", is_mine=True, msg_type="image", attachment_id="a.png"
            ),
            _msg(
                "39123@lid", "M1", is_mine=True, msg_type="image", attachment_id="b.png"
            ),
        )
        kept = _cross_key_dedup_rows(rows, "39123@lid")
        assert kept is rows


# ─── API integration: _messages(read union) ───────────────────────────────────


class TestMessagesReadUnion:
    def _seed(self) -> tuple[str, str]:
        from protocols.db import _add_message_to_cache

        canonical = "39123@lid"
        alias = "39123@c.us"
        # Twin: same msg_id, different attachment_id; alias inserted first so
        # that the alias rowid is SMALLER than the canonical one.
        _add_message_to_cache(
            alias,
            "",
            True,
            "You",
            1000,
            msg_type="image",
            attachment_id="sent-a.png",
            protocol="whatsapp",
            msg_id="M1",
        )
        _add_message_to_cache(
            canonical,
            "",
            True,
            "You",
            1000,
            msg_type="image",
            attachment_id="http://waha/a",
            protocol="whatsapp",
            msg_id="M1",
        )
        # Distinct pair: different msg_id -> must both survive the union.
        _add_message_to_cache(
            alias,
            "",
            True,
            "You",
            2000,
            msg_type="image",
            attachment_id="sent-b.png",
            protocol="whatsapp",
            msg_id="M2",
        )
        _add_message_to_cache(
            canonical,
            "",
            True,
            "You",
            2001,
            msg_type="image",
            attachment_id="sent-c.png",
            protocol="whatsapp",
            msg_id="M3",
        )
        return canonical, alias

    def test_read_union_dedups_twin_and_keeps_distinct(self):
        canonical, alias = self._seed()

        messages = _messages("whatsapp", canonical, extra_keys=(alias,))

        assert [message["id"] for message in messages] == ["M1", "M2", "M3"]

    def test_single_key_control_does_not_apply_union(self):
        canonical, _alias = self._seed()

        messages = _messages("whatsapp", canonical)

        assert [message["id"] for message in messages] == ["M1", "M3"]

    def test_union_is_symmetric_when_canonical_is_the_alias_key(self):
        canonical, alias = self._seed()

        # Requested id is the ghost @c.us while the requested "canonical" for
        # the union is that same id: the @lid rows become aliases.  The union
        # must yield the SAME number of distinct messages (the twin M1 collapses
        # exactly once, M2 and M3 stay distinct).
        messages = _messages("whatsapp", alias, extra_keys=(canonical,))

        assert [message["id"] for message in messages] == ["M1", "M2", "M3"]


# ─── TUI: _merge_backend_cache ────────────────────────────────────────────────


class TestMergeBackendCache:
    def test_cross_key_same_id_different_attachment_collapses(self):
        contact = _wa_contact("39123@lid", phone="39123")
        backend = SimpleNamespace(
            cache={
                "39123@lid": [
                    _d(
                        "M1",
                        is_mine=True,
                        msg_type="image",
                        attachment_id="http://waha/a",
                    )
                ],
                "39123@c.us": [
                    _d(
                        "M1",
                        is_mine=True,
                        msg_type="image",
                        attachment_id="sent-a.png",
                    )
                ],
            }
        )
        stub = _ChatViewStub()

        changed = ChatViewMixin._merge_backend_cache(stub, contact, backend)

        assert changed is True
        merged = stub._cache[contact.cache_key]
        assert len(merged) == 1
        assert merged[0]["attachment_id"] == "http://waha/a"

    def test_cross_key_id_divergence_identical_tuple_collapses(self):
        contact = _wa_contact("39123@lid", phone="39123")
        backend = SimpleNamespace(
            cache={
                "39123@lid": [
                    _d(
                        "webhook-id",
                        is_mine=True,
                        msg_type="image",
                        attachment_id="same.png",
                    )
                ],
                "39123@c.us": [
                    _d(
                        "rest-id",
                        is_mine=True,
                        msg_type="image",
                        attachment_id="same.png",
                    )
                ],
            }
        )
        stub = _ChatViewStub()

        ChatViewMixin._merge_backend_cache(stub, contact, backend)

        merged = stub._cache[contact.cache_key]
        assert len(merged) == 1

    def test_same_key_multi_attachment_not_collapsed(self):
        contact = _wa_contact("39123@lid", phone="39123")
        backend = SimpleNamespace(
            cache={
                "39123@lid": [
                    _d(
                        "M1",
                        is_mine=True,
                        msg_type="image",
                        attachment_id="f0.png",
                    ),
                    _d(
                        "M1",
                        is_mine=True,
                        msg_type="image",
                        attachment_id="f1.png",
                    ),
                ]
            }
        )
        stub = _ChatViewStub()

        ChatViewMixin._merge_backend_cache(stub, contact, backend)

        merged = stub._cache[contact.cache_key]
        assert len(merged) == 2
        assert {message["attachment_id"] for message in merged} == {"f0.png", "f1.png"}

    def test_cross_key_distinct_messages_both_kept(self):
        contact = _wa_contact("39123@lid", phone="39123")
        backend = SimpleNamespace(
            cache={
                "39123@lid": [
                    _d(
                        "M1",
                        is_mine=True,
                        msg_type="image",
                        attachment_id="a.png",
                        timestamp=1000,
                    )
                ],
                "39123@c.us": [
                    _d(
                        "M2",
                        is_mine=True,
                        msg_type="image",
                        attachment_id="b.png",
                        timestamp=2000,
                    )
                ],
            }
        )
        stub = _ChatViewStub()

        ChatViewMixin._merge_backend_cache(stub, contact, backend)

        merged = stub._cache[contact.cache_key]
        assert len(merged) == 2


# ─── TUI: _on_backend_ready ───────────────────────────────────────────────────


class TestOnBackendReady:
    def test_cross_key_union_merges_once_under_canonical(self):
        contact = _wa_contact("39123@lid", phone="39123")
        backend = SimpleNamespace(
            protocol=PROTOCOL_WHATSAPP,
            contacts=[contact],
            cache={
                "39123@lid": [
                    _d(
                        "M1",
                        is_mine=True,
                        msg_type="image",
                        attachment_id="http://waha/a",
                        timestamp=1000,
                    )
                ],
                "39123@c.us": [
                    _d(
                        "M1",
                        is_mine=True,
                        msg_type="image",
                        attachment_id="sent-a.png",
                        timestamp=1000,
                    )
                ],
            },
        )
        stub = _BackendConnectStub()

        BackendConnectMixin._on_backend_ready(stub, backend)

        merged = stub._cache["whatsapp:39123@lid"]
        assert len(merged) == 1
        assert merged[0]["attachment_id"] == "http://waha/a"
        # The alias key must NOT produce a separate UI bucket.
        assert "whatsapp:39123@c.us" not in stub._cache

    def test_on_backend_ready_cross_key_distinct_messages_both_kept(self):
        contact = _wa_contact("39123@lid", phone="39123")
        backend = SimpleNamespace(
            protocol=PROTOCOL_WHATSAPP,
            contacts=[contact],
            cache={
                "39123@lid": [
                    _d(
                        "M1",
                        is_mine=True,
                        msg_type="image",
                        attachment_id="a.png",
                        timestamp=1000,
                    )
                ],
                "39123@c.us": [
                    _d(
                        "M2",
                        is_mine=True,
                        msg_type="image",
                        attachment_id="b.png",
                        timestamp=2000,
                    )
                ],
            },
        )
        stub = _BackendConnectStub()

        BackendConnectMixin._on_backend_ready(stub, backend)

        merged = stub._cache["whatsapp:39123@lid"]
        assert len(merged) == 2

    @pytest.mark.xfail(
        strict=False,
        reason=(
            "PRE-EXISTING (not the cross-key fix): tui/backend_connect.py "
            "_merge_messages collapses two same-key attachments of one "
            "multi-media message when they share (is_mine,text,timestamp): the "
            "message text is forced to '' for images by _load_cache "
            "(protocols/db.py:463), so the by_identity fallback ignores the "
            "distinct attachment_id and only one bubble survives. "
            "test_backend_connect.py:test_two_attachments_same_id_both_kept only "
            "passes because it feeds DIFFERENT texts."
        ),
    )
    def test_on_backend_ready_same_key_multi_attachment_intact(self):
        contact = _wa_contact("39123@lid", phone="39123")
        backend = SimpleNamespace(
            protocol=PROTOCOL_WHATSAPP,
            contacts=[contact],
            cache={
                "39123@lid": [
                    _d(
                        "M1",
                        is_mine=True,
                        msg_type="image",
                        attachment_id="f0.png",
                        timestamp=1000,
                    ),
                    _d(
                        "M1",
                        is_mine=True,
                        msg_type="image",
                        attachment_id="f1.png",
                        timestamp=1000,
                    ),
                ]
            },
        )
        stub = _BackendConnectStub()

        BackendConnectMixin._on_backend_ready(stub, backend)

        merged = stub._cache["whatsapp:39123@lid"]
        assert len(merged) == 2


# ─── Explicit falsification ───────────────────────────────────────────────────


@pytest.mark.xfail(
    strict=False,
    reason=(
        "OVER-COLLAPSE: two genuinely distinct messages sharing the whole tuple "
        "(is_mine,text,timestamp,msg_type,attachment_id) but with a different "
        "msg_id are treated as equivalent by the tuple fallback of "
        "models._cross_key_message_equivalent. Cross-key only: they would be "
        "data-loss if such a coincidence happens between @lid and @c.us."
    ),
)
def test_falsification_distinct_ids_identical_tuple_should_not_collapse():
    rows = _make_rows(
        _msg("39123@lid", "id-canonical", text="OK", is_mine=False, timestamp=1000),
        _msg("39123@c.us", "id-alias", text="OK", is_mine=False, timestamp=1000),
    )
    kept = _cross_key_dedup_rows(rows, "39123@lid")
    assert len(kept) == 2


@pytest.mark.xfail(
    strict=False,
    reason=(
        "RESIDUAL DUPLICATE: a twin whose alias row has NO msg_id (legacy row) "
        "and a different attachment_id is NOT deduplicated, because the id "
        "branch needs an id on both sides and the tuple fallback compares "
        "attachment_id exactly."
    ),
)
def test_falsification_missing_id_different_attachment_should_dedup():
    rows = _make_rows(
        _msg(
            "39123@lid",
            "real-id",
            is_mine=True,
            msg_type="image",
            attachment_id="http://waha/a",
        ),
        _msg(
            "39123@c.us",
            None,
            is_mine=True,
            msg_type="image",
            attachment_id="sent-a.png",
        ),
    )
    kept = _cross_key_dedup_rows(rows, "39123@lid")
    assert len(kept) == 1


def test_falsification_same_key_multi_attachment_survives_api():
    """Same-key multi-attachment must NEVER be collapsed by the cross-key rule."""
    rows = _make_rows(
        _msg(
            "39123@lid",
            "M1",
            is_mine=True,
            timestamp=1000,
            msg_type="image",
            attachment_id="f0.png",
        ),
        _msg(
            "39123@lid",
            "M1",
            is_mine=True,
            timestamp=1000,
            msg_type="image",
            attachment_id="f1.png",
        ),
        _msg(
            "39123@lid",
            "M1",
            is_mine=True,
            timestamp=1000,
            msg_type="image",
            attachment_id="f2.png",
        ),
    )
    kept = _cross_key_dedup_rows(rows, "39123@lid")
    assert len(kept) == 3
