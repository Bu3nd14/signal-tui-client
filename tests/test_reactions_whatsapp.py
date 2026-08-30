from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import backend as backend_mod
import backends.whatsapp as whatsapp_mod
from backends.whatsapp import WhatsAppBackend
from backends.whatsapp_events import _event_from_raw, _event_from_reaction
from models import PROTOCOL_WHATSAPP

CONTACT_ID = "391234567890@c.us"
PARTICIPANT = "39000111222@lid"
SERIALIZED_ID = "true_391234567890@c.us_ABC123"
TIMESTAMP = 1710481111853


def _reaction_payload(**overrides) -> dict:
    payload = {
        "id": "false_391234567890@c.us_DEF456",
        "from": CONTACT_ID,
        "fromMe": False,
        "participant": PARTICIPANT,
        "timestamp": 1710481111.853,
        "reaction": {"text": "🙏", "messageId": SERIALIZED_ID},
    }
    payload.update(overrides)
    return payload


def _delta(**overrides) -> dict:
    payload = {
        "target_message_id": SERIALIZED_ID,
        "target_timestamp": None,
        "mode": "delta",
        "emoji": "🙏",
        "is_remove": False,
        "author": "Anna",
        "author_key": PARTICIPANT,
        "is_mine": False,
        "timestamp": TIMESTAMP,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def tmp_db(tmp_path: Path):
    db_file = tmp_path / "messages.db"
    with (
        patch.object(backend_mod, "DB_FILE", db_file),
        patch.object(backend_mod, "CACHE_DIR", tmp_path),
    ):
        yield db_file


@pytest.mark.parametrize("nested", [True, False])
def test_reaction_webhook_and_direct_payload_are_normalized(nested):
    payload = _reaction_payload()
    raw = (
        {"event": "message.reaction", "payload": payload}
        if nested
        else {"event": "message.reaction", **payload}
    )

    events = _event_from_raw(raw)

    assert len(events) == 1
    event = events[0]
    assert event is not None
    assert event.type == "reaction_update"
    assert event.protocol == PROTOCOL_WHATSAPP
    assert event.contact_id == CONTACT_ID
    assert event.payload == {
        "target_message_id": SERIALIZED_ID,
        "target_timestamp": None,
        "mode": "delta",
        "emoji": "🙏",
        "is_remove": False,
        "author": PARTICIPANT,
        "author_key": PARTICIPANT,
        "is_mine": False,
        "timestamp": TIMESTAMP,
        "contact": None,
    }


def test_from_me_reaction_uses_me_as_author():
    event = _event_from_reaction(
        _reaction_payload(fromMe=True, to=CONTACT_ID, participant=None)
    )

    assert event is not None
    assert event.contact_id == CONTACT_ID
    assert event.payload["author_key"] == "me"
    assert event.payload["is_mine"] is True


def test_empty_reaction_text_is_remove():
    event = _event_from_reaction(
        _reaction_payload(reaction={"text": "", "messageId": SERIALIZED_ID})
    )

    assert event is not None
    assert event.payload["emoji"] == ""
    assert event.payload["is_remove"] is True


def test_group_reaction_uses_participant_as_author_key():
    group_id = "120363000000000@g.us"
    event = _event_from_reaction(_reaction_payload(**{"from": group_id}))

    assert event is not None
    assert event.contact_id == group_id
    assert event.payload["author_key"] == PARTICIPANT


def test_configure_webhook_subscribes_to_reactions():
    backend = WhatsAppBackend(api_url="http://api.test")
    backend._rest = MagicMock()
    backend._rest.get_session_status.return_value = {"config": {}}
    with patch.object(
        whatsapp_mod,
        "get_whatsapp_webhook_url",
        return_value="http://host.docker.internal:8088/webhook",
    ):
        backend._configure_webhook()

    events = backend._rest.update_session_config.call_args.args[0]["config"][
        "webhooks"
    ][0]["events"]
    assert "message.reaction" in events


def test_compose_subscribes_both_event_variables_to_reactions():
    compose = (
        Path(__file__).resolve().parent.parent / "docker-compose.yml"
    ).read_text()

    lines = [line.strip() for line in compose.splitlines()]
    event_lines = [
        line
        for line in lines
        if line.startswith(("WAHA_WEBHOOK_EVENTS:", "WHATSAPP_HOOK_EVENTS:"))
    ]
    assert len(event_lines) == 2
    assert all("message.reaction" in line for line in event_lines)


def _backend_with_target() -> WhatsAppBackend:
    backend = WhatsAppBackend(api_url="http://api.test")
    backend.cache[CONTACT_ID] = [
        {"id": SERIALIZED_ID, "timestamp": TIMESTAMP, "text": "target"}
    ]
    return backend


def _persist_target():
    backend_mod._add_message_to_cache(
        CONTACT_ID,
        "target",
        True,
        "You",
        TIMESTAMP,
        protocol=PROTOCOL_WHATSAPP,
        msg_id=SERIALIZED_ID,
    )


def test_apply_reaction_add_change_and_remove(tmp_db):
    _persist_target()
    backend = _backend_with_target()

    added = backend.apply_reaction(CONTACT_ID, _delta())
    changed = backend.apply_reaction(CONTACT_ID, _delta(emoji="❤️"))

    assert added == {
        "message_id": SERIALIZED_ID,
        "timestamp": TIMESTAMP,
        "reactions": [
            {"emoji": "🙏", "count": 1, "is_mine": False, "authors": ["Anna"]}
        ],
    }
    assert changed["reactions"] == [
        {"emoji": "❤️", "count": 1, "is_mine": False, "authors": ["Anna"]}
    ]
    with sqlite3.connect(tmp_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM reactions").fetchone()[0] == 1

    removed = backend.apply_reaction(
        CONTACT_ID, _delta(emoji="", is_remove=True, timestamp=TIMESTAMP + 1)
    )

    assert removed == {
        "message_id": SERIALIZED_ID,
        "timestamp": TIMESTAMP,
        "reactions": [],
    }


def test_apply_reaction_matches_plain_id_canonically(tmp_db):
    _persist_target()
    backend = WhatsAppBackend(api_url="http://api.test")

    result = backend.apply_reaction(CONTACT_ID, _delta(target_message_id="abc123"))

    assert result is not None
    assert result["message_id"] == SERIALIZED_ID
    assert result["reactions"][0]["emoji"] == "🙏"


def test_apply_reaction_unknown_target_is_persisted_without_update(tmp_db):
    backend = WhatsAppBackend(api_url="http://api.test")

    assert backend.apply_reaction(CONTACT_ID, _delta()) is None
    rows = backend_mod._reactions_for_contact(PROTOCOL_WHATSAPP, CONTACT_ID)
    assert len(rows) == 1
    assert rows[0]["target_msg_id"] == SERIALIZED_ID
