"""``to_legacy``: core bus events -> the exact legacy ``/ws`` JSON frames."""

from __future__ import annotations

import json
import time

import pytest

from models import PROTOCOL_SIGNAL, PROTOCOL_WHATSAPP, ChatContact
from server.bus import GAP, Event
from web.legacy_events import to_legacy


def _ev(etype: str, payload: dict) -> Event:
    return Event(1, etype, payload, time.time())


def _message(**over) -> Event:
    payload = {
        "protocol": PROTOCOL_SIGNAL,
        "contact_id": "+39",
        "contact": ChatContact(id="+39", display_name="A", protocol=PROTOCOL_SIGNAL),
        "was_placeholder": False,
        "message": {"id": "m1", "text": "hi", "is_mine": 1, "timestamp": 5},
        "timestamp": 5,
        "added": True,
        "changed": False,
    }
    payload.update(over)
    return _ev("message", payload)


class TestMessage:
    def test_added(self):
        assert to_legacy(_message()) == {
            "type": "message",
            "payload": {
                "id": "m1",
                "protocol": PROTOCOL_SIGNAL,
                "contact_id": "+39",
                "timestamp": 5,
                "is_mine": True,
            },
        }

    def test_changed_only(self):
        frame = to_legacy(_message(added=False, changed=True))
        assert frame is not None and frame["type"] == "message"

    def test_neither_added_nor_changed_is_not_sent(self):
        assert to_legacy(_message(added=False, changed=False)) is None

    def test_missing_id_and_is_mine(self):
        frame = to_legacy(_message(message={"text": "x"}))
        assert frame is not None
        assert frame["payload"]["id"] is None
        assert frame["payload"]["is_mine"] is False

    def test_contact_id_is_the_resolved_one(self):
        frame = to_legacy(_message(contact_id="393@c.us"))
        assert frame is not None and frame["payload"]["contact_id"] == "393@c.us"

    def test_frame_is_json_serializable(self):
        json.dumps(to_legacy(_message()))


class TestOtherEvents:
    def test_message_edit(self):
        event = _ev(
            "message_edit",
            {
                "protocol": "telegram",
                "contact_id": "42",
                "contact": object(),
                "info": {
                    "message_id": 77,
                    "timestamp": "1000",
                    "old_text": None,
                    "is_mine": 0,
                },
                "text": "Dopo",
            },
        )
        assert to_legacy(event) == {
            "type": "message_edit",
            "payload": {
                "protocol": "telegram",
                "contact_id": "42",
                "message_id": "77",
                "timestamp": 1000,
                "old_text": "",
                "text": "Dopo",
                "is_mine": False,
            },
        }

    def test_reaction_update(self):
        reactions = [{"emoji": "👍", "count": 2, "is_mine": True, "authors": ["A"]}]
        event = _ev(
            "reaction_update",
            {
                "protocol": "telegram",
                "contact_id": "42",
                "info": {"message_id": 77, "timestamp": "1000", "reactions": reactions},
            },
        )
        assert to_legacy(event) == {
            "type": "reaction_update",
            "payload": {
                "protocol": "telegram",
                "contact_id": "42",
                "message_id": "77",
                "timestamp": 1000,
                "reactions": reactions,
            },
        }

    def test_receipt_normalizes_updates(self):
        event = _ev(
            "receipt",
            {
                "protocol": "telegram",
                "contact_id": "42",
                "updated": [
                    {"id": 77, "timestamp": "1000", "status": "delivered", "text": "C"},
                    {"id": "", "timestamp": None, "status": None, "text": None},
                    {},
                ],
            },
        )
        assert to_legacy(event) == {
            "type": "receipt",
            "payload": {
                "protocol": "telegram",
                "contact_id": "42",
                "updates": [
                    {"id": "77", "timestamp": 1000, "status": "delivered", "text": "C"},
                    {"id": None, "timestamp": 0, "status": "sent", "text": ""},
                    {"id": None, "timestamp": 0, "status": "sent", "text": ""},
                ],
            },
        }

    @pytest.mark.parametrize("action", ["STARTED", "STOPPED"])
    def test_typing(self, action):
        event = _ev(
            "typing",
            {"protocol": PROTOCOL_SIGNAL, "contact_id": "+39", "action": action},
        )
        assert to_legacy(event) == {
            "type": "typing",
            "payload": {
                "protocol": PROTOCOL_SIGNAL,
                "contact_id": "+39",
                "action": action,
            },
        }

    def test_contact_update(self):
        event = _ev(
            "contact_update",
            {
                "protocol": PROTOCOL_WHATSAPP,
                "contact_id": "1@lid",
                "display_name": "Giulia",
                "phone": "391234567",
                "contact": object(),
            },
        )
        assert to_legacy(event) == {
            "type": "contact_update",
            "payload": {
                "protocol": PROTOCOL_WHATSAPP,
                "contact_id": "1@lid",
                "display_name": "Giulia",
                "phone": "391234567",
            },
        }

    def test_contact_update_missing_fields_are_none(self):
        event = _ev(
            "contact_update", {"protocol": PROTOCOL_SIGNAL, "contact_id": "+39"}
        )
        assert to_legacy(event) == {
            "type": "contact_update",
            "payload": {
                "protocol": PROTOCOL_SIGNAL,
                "contact_id": "+39",
                "display_name": None,
                "phone": None,
            },
        }

    def test_contact_merge_whatsapp(self):
        event = _ev(
            "contact_update",
            {
                "protocol": PROTOCOL_WHATSAPP,
                "contact_id": "1@lid",
                "merged_into": "39@c.us",
                "display_name": "G",
                "phone": "39",
            },
        )
        assert to_legacy(event) == {
            "type": "contact_update",
            "payload": {
                "protocol": PROTOCOL_WHATSAPP,
                "contact_id": "1@lid",
                "merged_into": "39@c.us",
                "display_name": "G",
                "phone": "39",
            },
        }

    def test_merged_into_ignored_outside_whatsapp(self):
        frame = to_legacy(
            _ev(
                "contact_update",
                {
                    "protocol": PROTOCOL_SIGNAL,
                    "contact_id": "+39",
                    "merged_into": "x",
                },
            )
        )
        assert frame is not None and "merged_into" not in frame["payload"]

    def test_lid_warmup_done(self):
        event = _ev(
            "lid_warmup_done", {"protocol": PROTOCOL_WHATSAPP, "contact_id": ""}
        )
        assert to_legacy(event) == {
            "type": "lid_warmup_done",
            "payload": {"protocol": PROTOCOL_WHATSAPP},
        }

    def test_web_push_is_passed_through_unchanged(self):
        frame = {"type": "message", "payload": {"id": "x"}}
        assert to_legacy(_ev("web_push", {"event": frame})) is frame

    def test_web_push_without_dict_is_dropped(self):
        assert to_legacy(_ev("web_push", {"event": None})) is None
        assert to_legacy(_ev("web_push", {})) is None

    @pytest.mark.parametrize(
        "etype",
        [
            "backend_connecting",
            "backend_status",
            "backend_ready",
            "backend_done",
            GAP,
            "sent_mirror",
            "unknown",
        ],
    )
    def test_no_legacy_frame(self, etype):
        assert to_legacy(_ev(etype, {"protocol": "x", "contact_id": "y"})) is None
