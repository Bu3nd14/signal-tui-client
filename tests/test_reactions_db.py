"""Database persistence tests for reaction deltas and snapshots."""

from __future__ import annotations

import sqlite3

import protocols.db as backend


def _add_message(protocol: str, contact: str, timestamp: int, msg_id: str | None):
    backend._add_message_to_cache(
        contact,
        "message",
        False,
        "Alice",
        timestamp,
        protocol=protocol,
        msg_id=msg_id,
    )
    with sqlite3.connect(backend.DB_FILE) as conn:
        return conn.execute(
            "SELECT id FROM messages WHERE protocol = ? AND contact_number = ? "
            "AND timestamp = ?",
            (protocol, contact, timestamp),
        ).fetchone()[0]


def _delta(
    protocol="signal",
    contact="contact",
    target_msg_id="1000",
    target_ts=1000,
    emoji="👍",
    author_key="alice",
    author="Alice",
    is_mine=False,
    is_remove=False,
    ts=2000,
):
    return backend._apply_reaction_delta(
        protocol,
        contact,
        target_msg_id,
        target_ts,
        emoji,
        author_key,
        author,
        is_mine,
        is_remove,
        ts,
    )


def test_init_db_creates_reactions_schema_idempotently():
    backend._init_db()
    backend._init_db()

    with sqlite3.connect(backend.DB_FILE) as conn:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='reactions'"
        ).fetchone()
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='reactions'"
            )
        }

    assert table == ("reactions",)
    assert {"idx_reactions_identity", "idx_reactions_contact"} <= indexes


def test_delta_add_change_remove_and_unique_identity():
    assert _delta() is True
    rows = backend._reactions_for_contact("signal", "contact")
    assert len(rows) == 1
    assert rows[0]["emoji"] == "👍"
    assert rows[0]["count"] == 1

    assert _delta(emoji="❤️", ts=2001) is True
    rows = backend._reactions_for_contact("signal", "contact")
    assert len(rows) == 1
    assert rows[0]["emoji"] == "❤️"

    assert _delta(emoji="ignored", is_remove=True, ts=2002) is True
    assert backend._reactions_for_contact("signal", "contact") == []
    assert _delta(is_remove=True, ts=2003) is False


def test_snapshot_replace_preserves_delta_rows():
    _delta(protocol="telegram", target_msg_id="42", target_ts=None)
    entries = [
        {
            "emoji": "👍",
            "count": 3,
            "is_mine": True,
            "author": "",
            "author_key": "__agg__:👍",
        },
        {
            "emoji": "❤️",
            "count": 2,
            "is_mine": False,
            "author": "",
            "author_key": "__agg__:❤️",
        },
    ]

    assert backend._replace_reactions_snapshot(
        "telegram", "contact", "42", None, entries, 3000
    )
    rows = backend._reactions_for_contact("telegram", "contact")
    aggregate = {
        row["author_key"]: row["count"]
        for row in rows
        if row["author_key"].startswith("__agg__:")
    }
    assert aggregate == {"__agg__:👍": 3, "__agg__:❤️": 2}
    assert any(row["author_key"] == "alice" for row in rows)

    assert backend._replace_reactions_snapshot(
        "telegram", "contact", "42", None, [entries[1]], 3001
    )
    rows = backend._reactions_for_contact("telegram", "contact")
    assert [
        row["author_key"] for row in rows if row["author_key"].startswith("__agg__:")
    ] == ["__agg__:❤️"]
    assert any(row["author_key"] == "alice" for row in rows)


def test_resolve_reaction_target_for_each_protocol():
    signal_id = _add_message("signal", "+39", 1000, "1000")
    signal_legacy_id = _add_message("signal", "+39", 2000, None)
    wa_id = _add_message("whatsapp", "wa", 3000, "wa-id")
    tg_id = _add_message("telegram", "tg", 4000, "44")

    assert backend._resolve_reaction_target_row("signal", "+39", "1000", 999) == {
        "id": signal_id,
        "msg_id": "1000",
        "timestamp": 1000,
    }
    assert backend._resolve_reaction_target_row("signal", "+39", None, 2000) == {
        "id": signal_legacy_id,
        "msg_id": None,
        "timestamp": 2000,
    }
    assert backend._resolve_reaction_target_row("whatsapp", "wa", "wa-id", 0) == {
        "id": wa_id,
        "msg_id": "wa-id",
        "timestamp": 3000,
    }
    assert backend._resolve_reaction_target_row("telegram", "tg", "44", None) == {
        "id": tg_id,
        "msg_id": "44",
        "timestamp": 4000,
    }
    assert (
        backend._resolve_reaction_target_row("signal", "+39", "missing", 9999) is None
    )
    assert backend._resolve_reaction_target_row("telegram", "tg", None, 4000) is None


def test_prune_orphan_reactions_keeps_existing_targets():
    _add_message("signal", "contact", 1000, "1000")
    _add_message("signal", "contact", 1001, "1001")
    _add_message("signal", "contact", 1002, None)
    _add_message("signal", "contact", 1003, None)
    _delta(target_msg_id="1000", target_ts=1000, author_key="first")
    _delta(target_msg_id="1001", target_ts=1001, author_key="second")
    _delta(target_msg_id="1002", target_ts=1002, author_key="idless")
    _delta(target_msg_id="1003", target_ts=1003, author_key="deleted-idless")

    with sqlite3.connect(backend.DB_FILE) as conn:
        conn.execute("DELETE FROM messages WHERE msg_id = '1000'")
        conn.execute("DELETE FROM messages WHERE timestamp = 1003")

    assert backend._prune_orphan_reactions() == 2
    rows = backend._reactions_for_contact("signal", "contact")
    assert [row["author_key"] for row in rows] == ["second", "idless"]


def test_cache_prune_removes_reactions_for_pruned_messages():
    for index in range(101):
        _add_message("signal", "contact", index, str(index))
    _delta(target_msg_id="0", target_ts=0)

    assert backend._prune_cache(limit=100, now_ms=1_000_000) == 1
    assert backend._reactions_for_contact("signal", "contact") == []
