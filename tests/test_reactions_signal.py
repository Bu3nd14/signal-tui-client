"""Signal reaction envelope parsing and persistence tests."""

from __future__ import annotations

import protocols.db as backend
from models import PROTOCOL_SIGNAL, ChatContact
from protocols import SignalBackend

CONTACT_ID = "+391234567890"
TARGET_TS = 1_700_000_000_000


def _signal_backend() -> SignalBackend:
    signal = SignalBackend()
    signal._set_contacts(
        [
            ChatContact(
                id=CONTACT_ID,
                display_name="Mario",
                protocol=PROTOCOL_SIGNAL,
                extras={"aci": "uuid-123"},
            )
        ]
    )
    return signal


def _incoming_envelope(**reaction_overrides: object) -> dict:
    reaction = {
        "emoji": "👍",
        "targetSentTimestamp": TARGET_TS,
        "isRemove": False,
    }
    reaction.update(reaction_overrides)
    return {
        "source": CONTACT_ID,
        "sourceNumber": CONTACT_ID,
        "sourceName": "Mario",
        "timestamp": TARGET_TS + 100,
        "dataMessage": {
            "timestamp": TARGET_TS + 100,
            "reaction": reaction,
        },
    }


def _delta(**overrides: object) -> dict:
    payload = {
        "target_message_id": str(TARGET_TS),
        "target_timestamp": TARGET_TS,
        "mode": "delta",
        "emoji": "👍",
        "is_remove": False,
        "author": "Mario",
        "author_key": CONTACT_ID,
        "is_mine": False,
        "timestamp": TARGET_TS + 100,
    }
    payload.update(overrides)
    return payload


def _cache_target(signal: SignalBackend, *, msg_id: str | None, timestamp: int) -> None:
    signal.cache[CONTACT_ID] = [
        {
            "id": msg_id,
            "text": "target",
            "is_mine": True,
            "sender": "You",
            "timestamp": timestamp,
            "msg_type": "text",
        }
    ]


class TestReactionEnvelopeToEvent:
    def test_incoming_reaction_is_normalized_as_delta(self):
        events = _signal_backend().envelope_to_event(_incoming_envelope())

        assert len(events) == 1
        event = events[0]
        assert event.type == "reaction_update"
        assert event.protocol == PROTOCOL_SIGNAL
        assert event.contact_id == CONTACT_ID
        assert event.payload["target_message_id"] == str(TARGET_TS)
        assert event.payload["target_timestamp"] == TARGET_TS
        assert event.payload["mode"] == "delta"
        assert event.payload["emoji"] == "👍"
        assert event.payload["is_remove"] is False
        assert event.payload["author"] == "Mario"
        assert event.payload["author_key"] == CONTACT_ID
        assert event.payload["is_mine"] is False
        assert event.payload["timestamp"] == TARGET_TS + 100
        assert event.payload["contact"].id == CONTACT_ID

    def test_sync_sent_reaction_is_mine(self):
        envelope = {
            "source": "+390000000000",
            "timestamp": TARGET_TS + 200,
            "syncMessage": {
                "sentMessage": {
                    "destination": CONTACT_ID,
                    "timestamp": TARGET_TS + 200,
                    "reaction": {
                        "emoji": "❤️",
                        "targetSentTimestamp": TARGET_TS,
                    },
                }
            },
        }

        event = _signal_backend().envelope_to_event(envelope)[0]

        assert event.type == "reaction_update"
        assert event.payload["is_mine"] is True
        assert event.payload["author"] == "You"
        assert event.payload["author_key"] == "me"

    def test_remove_reaction_sets_remove_flag(self):
        event = _signal_backend().envelope_to_event(
            _incoming_envelope(emoji="", isRemove=True)
        )[0]

        assert event.payload["emoji"] == ""
        assert event.payload["is_remove"] is True

    def test_missing_target_is_dropped_without_message_fallback(self):
        envelope = _incoming_envelope()
        del envelope["dataMessage"]["reaction"]["targetSentTimestamp"]

        assert _signal_backend().envelope_to_event(envelope) == []


class TestApplyReaction:
    def test_add_change_remove_and_unknown_target(self):
        backend._add_message_to_cache(
            CONTACT_ID,
            "target",
            True,
            "You",
            TARGET_TS - 5,
            protocol=PROTOCOL_SIGNAL,
            msg_id=str(TARGET_TS),
        )
        signal = _signal_backend()
        _cache_target(signal, msg_id=str(TARGET_TS), timestamp=TARGET_TS - 5)

        added = signal.apply_reaction(CONTACT_ID, _delta())
        assert added == {
            "message_id": str(TARGET_TS),
            "timestamp": TARGET_TS - 5,
            "reactions": [
                {
                    "emoji": "👍",
                    "count": 1,
                    "is_mine": False,
                    "authors": ["Mario"],
                }
            ],
        }

        changed = signal.apply_reaction(
            CONTACT_ID, _delta(emoji="❤️", timestamp=TARGET_TS + 200)
        )
        assert changed["reactions"] == [
            {
                "emoji": "❤️",
                "count": 1,
                "is_mine": False,
                "authors": ["Mario"],
            }
        ]
        assert len(backend._reactions_for_contact(PROTOCOL_SIGNAL, CONTACT_ID)) == 1

        removed = signal.apply_reaction(
            CONTACT_ID,
            _delta(emoji="", is_remove=True, timestamp=TARGET_TS + 300),
        )
        assert removed["reactions"] == []
        assert (
            signal.apply_reaction(
                CONTACT_ID,
                _delta(
                    target_message_id="999",
                    target_timestamp=999,
                    timestamp=TARGET_TS + 400,
                ),
            )
            is None
        )
        rows = backend._reactions_for_contact(PROTOCOL_SIGNAL, CONTACT_ID)
        assert len(rows) == 1
        assert rows[0]["target_msg_id"] == "999"
        assert rows[0]["target_timestamp"] == 999

    def test_matches_idless_target_by_timestamp(self):
        signal = _signal_backend()
        _cache_target(signal, msg_id=None, timestamp=TARGET_TS)

        result = signal.apply_reaction(
            CONTACT_ID,
            _delta(target_message_id=None),
        )

        assert result["message_id"] == str(TARGET_TS)
        assert result["timestamp"] == TARGET_TS
        assert result["reactions"][0]["count"] == 1

    def test_aggregates_two_authors_and_deduplicates_names(self):
        signal = _signal_backend()
        _cache_target(signal, msg_id=str(TARGET_TS), timestamp=TARGET_TS)

        signal.apply_reaction(CONTACT_ID, _delta(author_key="author-1", author="Mario"))
        result = signal.apply_reaction(
            CONTACT_ID,
            _delta(author_key="author-2", author="Mario", timestamp=TARGET_TS + 200),
        )

        assert result["reactions"] == [
            {
                "emoji": "👍",
                "count": 2,
                "is_mine": False,
                "authors": ["Mario"],
            }
        ]

    def test_non_delta_mode_is_ignored(self):
        signal = _signal_backend()
        _cache_target(signal, msg_id=str(TARGET_TS), timestamp=TARGET_TS)

        assert signal.apply_reaction(CONTACT_ID, _delta(mode="snapshot")) is None
