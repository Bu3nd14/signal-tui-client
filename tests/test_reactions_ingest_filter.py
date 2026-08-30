"""Regression tests for reaction-shaped payload ingest filters."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from backends import SignalBackend
from backends.telegram import TelegramBackend
from backends.whatsapp_events import _event_from_message
from models import PROTOCOL_SIGNAL, ChatContact


def _signal_backend() -> SignalBackend:
    backend = SignalBackend()
    backend._set_contacts(
        [
            ChatContact(
                id="+391234567890",
                display_name="Mario",
                protocol=PROTOCOL_SIGNAL,
            )
        ]
    )
    return backend


def _telegram_message(**overrides: object) -> SimpleNamespace:
    fields = {
        "chat_id": 42,
        "text": "",
        "out": False,
        "sender": None,
        "date": datetime(2026, 8, 30, tzinfo=UTC),
        "photo": None,
        "document": None,
        "sticker": None,
        "video": None,
        "voice": None,
        "audio": None,
        "reply_to": None,
        "id": 99,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class TestSignalReactionIngestFilter:
    def test_data_message_reaction_is_filtered(self):
        envelope = {
            "source": "+391234567890",
            "timestamp": 1000,
            "dataMessage": {"reaction": {"emoji": "👍", "targetSentTimestamp": 900}},
        }

        events = _signal_backend().envelope_to_event(envelope)

        assert len(events) == 1
        assert events[0].type == "reaction_update"
        assert all(event.type != "message" for event in events)

    def test_sync_sent_message_reaction_is_filtered(self):
        envelope = {
            "timestamp": 1000,
            "syncMessage": {
                "sentMessage": {
                    "destination": "+391234567890",
                    "reaction": {"emoji": "👍", "targetSentTimestamp": 900},
                }
            },
        }

        events = _signal_backend().envelope_to_event(envelope)

        assert len(events) == 1
        assert events[0].type == "reaction_update"
        assert all(event.type != "message" for event in events)

    def test_malformed_reaction_is_filtered(self):
        envelope = {
            "source": "+391234567890",
            "timestamp": 1000,
            "dataMessage": {"reaction": {"emoji": "👍"}},
        }

        assert _signal_backend().envelope_to_event(envelope) == []

    def test_normal_message_is_unchanged(self):
        envelope = {
            "source": "+391234567890",
            "timestamp": 1000,
            "dataMessage": {"message": "Ciao", "timestamp": 1000},
        }

        events = _signal_backend().envelope_to_event(envelope)

        assert len(events) == 1
        assert events[0].type == "message"
        assert events[0].payload["text"] == "Ciao"

    def test_edit_message_is_unchanged(self):
        envelope = {
            "source": "+391234567890",
            "timestamp": 1000,
            "editMessage": {
                "targetSentTimestamp": 900,
                "dataMessage": {"message": "Testo corretto", "timestamp": 1000},
            },
        }

        events = _signal_backend().envelope_to_event(envelope)

        assert len(events) == 1
        assert events[0].type == "message_edit"


class TestWhatsAppReactionIngestFilter:
    def test_reaction_type_is_filtered(self):
        assert (
            _event_from_message(
                {"type": "reaction", "from": "391234567890@c.us", "body": "👍"}
            )
            == []
        )

    def test_nested_reaction_message_is_filtered(self):
        assert (
            _event_from_message(
                {
                    "from": "391234567890@c.us",
                    "_data": {"message": {"reactionMessage": {"text": "👍"}}},
                }
            )
            == []
        )

    def test_top_level_reaction_without_text_is_filtered(self):
        assert (
            _event_from_message(
                {"from": "391234567890@c.us", "reaction": {"text": "👍"}}
            )
            == []
        )

    def test_normal_message_is_unchanged(self):
        events = _event_from_message(
            {
                "id": "message-1",
                "from": "391234567890@c.us",
                "timestamp": 1700000000,
                "body": "Ciao",
            }
        )

        assert len(events) == 1
        assert events[0].type == "message"
        assert events[0].payload["text"] == "Ciao"

    def test_empty_text_without_media_is_filtered(self):
        # Media in download in corso su WAHA (hasMedia=true, media=null) o
        # ghost/servizio: mai bolle vuote (reperto live: 3ª foto ingerita come
        # text vuoto).  Il messaggio rientra con il media alla fetch_history.
        events = _event_from_message(
            {
                "id": "true_391234567890@c.us_ABC",
                "from": "391234567890@c.us",
                "fromMe": True,
                "hasMedia": True,
                "media": None,
                "timestamp": 1700000000,
                "body": "",
            }
        )

        assert events == []

    def test_empty_text_with_media_is_preserved(self):
        events = _event_from_message(
            {
                "id": "true_391234567890@c.us_ABC",
                "from": "391234567890@c.us",
                "fromMe": True,
                "hasMedia": True,
                "media": {
                    "url": "http://localhost:3000/api/files/default/true_391234567890@c.us_ABC.jpeg",
                    "mimetype": "image/jpeg",
                },
                "timestamp": 1700000000,
                "body": "",
            }
        )

        assert len(events) == 1
        assert events[0].payload["msg_type"] == "image"


class TestTelegramEmptyMessageIngestFilter:
    def test_empty_message_without_media_or_reply_is_filtered(self):
        backend = TelegramBackend()

        assert backend._message_to_chat_event(_telegram_message()) is None

    def test_photo_without_caption_is_preserved(self):
        backend = TelegramBackend()

        event = backend._message_to_chat_event(
            _telegram_message(photo=object(), text="")
        )

        assert event is not None
        assert event.payload["msg_type"] == "image"

    def test_non_empty_text_message_is_preserved(self):
        backend = TelegramBackend()

        event = backend._message_to_chat_event(_telegram_message(text="Ciao"))

        assert event is not None
        assert event.type == "message"
        assert event.payload["text"] == "Ciao"
