"""Reaction aggregation tests for the persisted web message payload."""

from __future__ import annotations

import pytest

from protocols.db import _add_message_to_cache, _apply_reaction_delta
from web.api import _messages


def _message(
    protocol: str,
    contact: str,
    timestamp: int,
    msg_id: str | None,
    text: str = "message",
) -> None:
    _add_message_to_cache(
        contact,
        text,
        False,
        "Alice",
        timestamp,
        protocol=protocol,
        msg_id=msg_id,
    )


def _reaction(
    protocol: str,
    contact: str,
    target_msg_id: str | None,
    target_timestamp: int | None,
    emoji: str,
    author_key: str,
    author: str,
    *,
    is_mine: bool = False,
    is_remove: bool = False,
    timestamp: int = 2_000,
) -> None:
    _apply_reaction_delta(
        protocol,
        contact,
        target_msg_id,
        target_timestamp,
        emoji,
        author_key,
        author,
        is_mine,
        is_remove,
        timestamp,
    )


def test_messages_aggregates_and_orders_reactions_and_omits_empty_messages():
    protocol = "signal"
    contact = "alice"
    _message(protocol, contact, 1_000, "signal-1", "reacted")
    _message(protocol, contact, 1_001, "signal-2", "plain")
    _reaction(
        protocol,
        contact,
        "signal-1",
        1_000,
        "❤️",
        "anna",
        "Anna",
        timestamp=2_000,
    )
    _reaction(
        protocol,
        contact,
        "signal-1",
        1_000,
        "👍",
        "you",
        "You",
        is_mine=True,
        timestamp=2_001,
    )
    _reaction(
        protocol,
        contact,
        "signal-1",
        1_000,
        "👍",
        "giovanni",
        "Giovanni",
        timestamp=2_002,
    )

    reacted, plain = _messages(protocol, contact)

    assert reacted["reactions"] == [
        {
            "emoji": "👍",
            "count": 2,
            "is_mine": True,
            "authors": ["Giovanni", "You"],
        },
        {
            "emoji": "❤️",
            "count": 1,
            "is_mine": False,
            "authors": ["Anna"],
        },
    ]
    assert "reactions" not in plain


def test_messages_deduplicates_and_sorts_non_empty_authors():
    _message("telegram", "group", 1_000, "42")
    _reaction("telegram", "group", "42", None, "🔥", "first", "Zoe")
    _reaction("telegram", "group", "42", None, "🔥", "second", "Ada", timestamp=2_001)
    _reaction("telegram", "group", "42", None, "🔥", "third", "Zoe", timestamp=2_002)
    _reaction("telegram", "group", "42", None, "🔥", "unknown", "", timestamp=2_003)

    assert _messages("telegram", "group")[0]["reactions"] == [
        {
            "emoji": "🔥",
            "count": 4,
            "is_mine": False,
            "authors": ["Ada", "Zoe"],
        }
    ]


@pytest.mark.parametrize(
    ("protocol", "contact", "message_id"),
    [
        ("whatsapp", "wa-contact", "true_wa-message"),
        ("telegram", "tg-contact", "77"),
    ],
)
def test_messages_matches_reactions_by_message_id(protocol, contact, message_id):
    _message(protocol, contact, 1_000, message_id)
    _reaction(protocol, contact, message_id, None, "👍", "alice", "Alice")

    assert _messages(protocol, contact)[0]["reactions"][0]["emoji"] == "👍"


def test_messages_matches_signal_legacy_reactions_by_timestamp():
    _message("signal", "legacy", 1_234, None)
    _reaction("signal", "legacy", "different-id", 1_234, "🎉", "alice", "Alice")

    assert _messages("signal", "legacy")[0]["reactions"] == [
        {
            "emoji": "🎉",
            "count": 1,
            "is_mine": False,
            "authors": ["Alice"],
        }
    ]


def test_messages_omits_reactions_after_last_reaction_is_removed():
    _message("signal", "alice", 1_000, "signal-1")
    _reaction("signal", "alice", "signal-1", 1_000, "👍", "alice", "Alice")
    assert "reactions" in _messages("signal", "alice")[0]

    _reaction(
        "signal",
        "alice",
        "signal-1",
        1_000,
        "",
        "alice",
        "Alice",
        is_remove=True,
        timestamp=2_001,
    )

    assert "reactions" not in _messages("signal", "alice")[0]
