"""Telegram reaction snapshot translation and persistence tests."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from telethon.tl.types import (
    MessagePeerReaction,
    MessageReactions,
    PeerChannel,
    PeerChat,
    PeerUser,
    ReactionCount,
    ReactionCustomEmoji,
    ReactionEmoji,
    UpdateMessageReactions,
)

import backend as backend_mod
from backends.telegram import TelegramBackend
from models import PROTOCOL_TELEGRAM, ChatContact


def _backend() -> TelegramBackend:
    backend = TelegramBackend()
    backend._api_id = 123
    backend._api_hash = "hash"
    return backend


def _update(
    peer=None,
    *,
    can_see_list: bool = True,
    recent_reactions=None,
) -> UpdateMessageReactions:
    return UpdateMessageReactions(
        peer=peer or PeerUser(user_id=42),
        msg_id=99,
        reactions=MessageReactions(
            can_see_list=can_see_list,
            results=[
                ReactionCount(reaction=ReactionEmoji("👍"), count=3, chosen_order=0),
                ReactionCount(reaction=ReactionEmoji("❤️"), count=2),
                ReactionCount(reaction=ReactionCustomEmoji(document_id=1234), count=1),
            ],
            recent_reactions=recent_reactions,
        ),
    )


@pytest.fixture
def tmp_db(tmp_path):
    db_file = tmp_path / "messages.db"
    with (
        patch.object(backend_mod, "DB_FILE", db_file),
        patch.object(backend_mod, "CACHE_DIR", tmp_path),
    ):
        yield db_file


def test_handle_reactions_update_enqueues_renderable_snapshot_only():
    backend = _backend()
    ada = ChatContact(id="7", display_name="Ada", protocol=PROTOCOL_TELEGRAM)
    backend._contacts_by_id = {7: ada}
    recent = [
        MessagePeerReaction(
            peer_id=PeerUser(user_id=7),
            date=datetime.now(UTC),
            reaction=ReactionEmoji("👍"),
        ),
        MessagePeerReaction(
            peer_id=PeerUser(user_id=8),
            date=datetime.now(UTC),
            reaction=ReactionEmoji("❤️"),
            my=True,
        ),
    ]

    asyncio.run(backend._handle_reactions_update(_update(recent_reactions=recent)))

    event = backend.poll_once()[0]
    assert event.type == "reaction_update"
    assert event.protocol == PROTOCOL_TELEGRAM
    assert event.contact_id == "42"
    assert event.payload["mode"] == "snapshot"
    assert event.payload["target_message_id"] == "99"
    assert event.payload["target_timestamp"] is None
    assert isinstance(event.payload["timestamp"], int)
    assert event.payload["snapshot"] == [
        {"emoji": "👍", "count": 3, "is_mine": True, "authors": ["Ada"]},
        {"emoji": "❤️", "count": 2, "is_mine": True, "authors": ["8"]},
    ]


@pytest.mark.parametrize(
    ("peer", "contact_id"),
    [
        (PeerUser(user_id=42), "42"),
        (PeerChat(chat_id=123), "-123"),
        (PeerChannel(channel_id=456), str(-1000000000000 - 456)),
    ],
)
def test_handle_reactions_update_peer_contact_conventions(peer, contact_id):
    backend = _backend()

    asyncio.run(backend._handle_reactions_update(_update(peer)))

    assert backend.poll_once()[0].contact_id == contact_id


def test_handle_reactions_update_hides_authors_when_list_is_private():
    backend = _backend()
    recent = [
        MessagePeerReaction(
            peer_id=PeerUser(user_id=7),
            date=datetime.now(UTC),
            reaction=ReactionEmoji("👍"),
        )
    ]

    asyncio.run(
        backend._handle_reactions_update(
            _update(can_see_list=False, recent_reactions=recent)
        )
    )

    snapshot = backend.poll_once()[0].payload["snapshot"]
    assert all(item["authors"] == [] for item in snapshot)


def test_handle_reactions_update_enqueues_empty_snapshot():
    backend = _backend()
    update = UpdateMessageReactions(
        peer=PeerUser(user_id=42),
        msg_id=99,
        reactions=MessageReactions(
            results=[ReactionCount(ReactionCustomEmoji(document_id=1), 1)]
        ),
    )

    asyncio.run(backend._handle_reactions_update(update))

    assert backend.poll_once()[0].payload["snapshot"] == []


def test_apply_reaction_replaces_and_clears_aggregate_rows(tmp_db):
    backend_mod._add_message_to_cache(
        "42",
        "message",
        False,
        "Ada",
        1000,
        protocol=PROTOCOL_TELEGRAM,
        msg_id="99",
    )
    backend = _backend()
    backend.cache = {"42": [{"id": "99", "timestamp": 1000}]}
    payload = {
        "mode": "snapshot",
        "target_message_id": "99",
        "timestamp": 2000,
        "snapshot": [
            {"emoji": "👍", "count": 3, "is_mine": True, "authors": ["Ada"]},
            {"emoji": "❤️", "count": 2, "is_mine": False, "authors": []},
        ],
    }

    result = backend.apply_reaction("42", payload)

    assert result == {
        "message_id": "99",
        "timestamp": 1000,
        "reactions": [
            {"emoji": "👍", "count": 3, "is_mine": True, "authors": ["Ada"]},
            {"emoji": "❤️", "count": 2, "is_mine": False, "authors": []},
        ],
    }
    rows = backend_mod._reactions_for_contact(PROTOCOL_TELEGRAM, "42")
    assert {row["author_key"]: row["count"] for row in rows} == {
        "__agg__:👍": 3,
        "__agg__:❤️": 2,
    }

    cleared = backend.apply_reaction(
        "42", {**payload, "snapshot": [], "timestamp": 3000}
    )

    assert cleared == {"message_id": "99", "timestamp": 1000, "reactions": []}
    assert backend_mod._reactions_for_contact(PROTOCOL_TELEGRAM, "42") == []
    with sqlite3.connect(tmp_db) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM reactions WHERE author_key LIKE '__agg__%'"
            ).fetchone()[0]
            == 0
        )


def test_apply_reaction_resolves_target_from_db_when_cache_misses(tmp_db):
    backend_mod._add_message_to_cache(
        "42",
        "message",
        False,
        "Ada",
        1000,
        protocol=PROTOCOL_TELEGRAM,
        msg_id="99",
    )
    backend = _backend()

    result = backend.apply_reaction(
        "42",
        {
            "mode": "snapshot",
            "target_message_id": "99",
            "timestamp": 2000,
            "snapshot": [{"emoji": "👍", "count": 1, "is_mine": False}],
        },
    )

    assert result["timestamp"] == 1000


def test_apply_reaction_unknown_target_returns_none(tmp_db):
    backend = _backend()

    result = backend.apply_reaction(
        "42",
        {
            "mode": "snapshot",
            "target_message_id": "missing",
            "timestamp": 2000,
            "snapshot": [{"emoji": "👍", "count": 1, "is_mine": False}],
        },
    )

    assert result is None
    rows = backend_mod._reactions_for_contact(PROTOCOL_TELEGRAM, "42")
    assert len(rows) == 1
    assert rows[0]["target_msg_id"] == "missing"


def test_on_raw_dispatches_reaction_update(monkeypatch):
    clients = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.handlers = {}
            clients.append(self)

        async def connect(self):
            return None

        async def is_user_authorized(self):
            return True

        async def get_dialogs(self, limit):
            return []

        def on(self, _event):
            def register(handler):
                self.handlers[handler.__name__] = handler
                return handler

            return register

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def is_alive(self):
            return False

    backend = _backend()
    handle = AsyncMock()
    monkeypatch.setattr(backend, "_handle_reactions_update", handle)
    monkeypatch.setattr("telethon.TelegramClient", FakeClient)
    monkeypatch.setattr("backends.telegram.threading.Thread", FakeThread)

    backend._connect_sync()
    update = _update()
    try:
        asyncio.run(clients[0].handlers["_on_raw"](update))
    finally:
        backend._loop.close()
        backend._loop = None

    handle.assert_awaited_once_with(update)
