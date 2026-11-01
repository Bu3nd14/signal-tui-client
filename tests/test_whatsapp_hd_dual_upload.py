"""HD dual upload WhatsApp: il child HD nascosto non deve diventare un messaggio.

Payload ricavati da WAHA (WEBJS) per una foto HD reale: il parent SD è
visibile e porta la caption, il child HD arriva ~1 s dopo con
``associationType: HD_IMAGE_DUAL_UPLOAD`` e ``viewMode: HIDDEN``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from protocols import db
from protocols.whatsapp import WhatsAppBackend
from protocols.whatsapp_events import _event_from_message, _is_hd_child

CHAT = "111111111111111@lid"
PARENT_ID = f"false_{CHAT}_3A00000000000000000A"
CHILD_ID = f"false_{CHAT}_3A00000000000000000B"
CAPTION = "foto con caption"
TS_S = 1_793_531_968


def _msg_id(base: str, from_me: bool) -> str:
    return base.replace("false_", "true_", 1) if from_me else base


def _parent(from_me: bool = False) -> dict:
    return {
        "id": _msg_id(PARENT_ID, from_me),
        "from": CHAT,
        "to": CHAT,
        "fromMe": from_me,
        "timestamp": TS_S,
        "body": CAPTION,
        "caption": CAPTION,
        "hasMedia": True,
        "media": {"url": f"http://waha/{PARENT_ID}.jpeg", "mimetype": "image/jpeg"},
        "_data": {
            "type": "image",
            "mimetype": "image/jpeg",
            "pairedMediaType": "SD_IMAGE_PARENT",
            "viewMode": "VISIBLE",
        },
    }


def _child(from_me: bool = False) -> dict:
    return {
        "id": _msg_id(CHILD_ID, from_me),
        "from": CHAT,
        "to": CHAT,
        "fromMe": from_me,
        "timestamp": TS_S + 1,
        "body": CAPTION,
        "caption": CAPTION,
        "hasMedia": True,
        "media": {"url": f"http://waha/{CHILD_ID}.jpeg", "mimetype": "image/jpeg"},
        "_data": {
            "type": "image",
            "mimetype": "image/jpeg",
            "parentMsgKey": {"fromMe": from_me, "id": "3A00000000000000000A"},
            "associationType": "HD_IMAGE_DUAL_UPLOAD",
            "pairedMediaType": "HD_IMAGE_CHILD",
            "viewMode": "HIDDEN",
        },
    }


@pytest.fixture
def tmp_db(tmp_path: Path):
    db_file = tmp_path / "messages.db"
    with (
        patch.object(db, "DB_FILE", db_file),
        patch.object(db, "CACHE_DIR", tmp_path),
    ):
        yield db_file


def _message_events(backend: WhatsAppBackend) -> list:
    return [
        c.args[0]
        for c in backend._enqueue_event.call_args_list
        if getattr(c.args[0], "type", None) == "message"
    ]


def test_is_hd_child_detection():
    assert _is_hd_child(_child())
    assert _is_hd_child(_child()["_data"])
    assert _is_hd_child({"_data": {"pairedMediaType": "HD_VIDEO_CHILD"}})
    assert not _is_hd_child(_parent())
    assert not _is_hd_child({"_data": {}})
    assert not _is_hd_child(None)


@pytest.mark.parametrize("from_me", [False, True])
def test_event_from_message_drops_hd_child_keeps_parent(from_me):
    assert _event_from_message(_child(from_me)) == []
    events = _event_from_message(_parent(from_me))
    assert len(events) == 1
    assert events[0].type == "message"


def test_fetch_history_ingests_only_parent(tmp_db):
    backend = WhatsAppBackend(api_url="http://api.test")
    backend._rest = MagicMock()
    backend._presence_subscribe_lazy = MagicMock()
    backend._schedule_media_resolve = MagicMock()
    backend._rest.list_messages.return_value = [_child(), _parent()]

    backend.fetch_history(CHAT)

    assert [m["id"] for m in backend.cache.get(CHAT, [])] == [PARENT_ID]
    for call in backend._schedule_media_resolve.call_args_list:
        assert CHILD_ID not in call.args


@pytest.mark.parametrize("event", ["message", "message.any"])
def test_webhook_hd_child_is_not_enqueued(event):
    backend = WhatsAppBackend(api_url="http://api.test")
    backend._enqueue_event = MagicMock()
    backend.handle_webhook({"event": event, "payload": _child()})
    assert _message_events(backend) == []


def test_webhook_parent_is_enqueued():
    backend = WhatsAppBackend(api_url="http://api.test")
    backend._enqueue_event = MagicMock()
    backend.handle_webhook({"event": "message.any", "payload": _parent()})
    assert len(_message_events(backend)) == 1


def test_ack_of_outgoing_hd_child_creates_no_message():
    backend = WhatsAppBackend(api_url="http://api.test")
    backend._enqueue_event = MagicMock()
    backend._schedule_media_resolve = MagicMock()
    backend._presence_subscribe_lazy = MagicMock()
    payload = {**_child(from_me=True), "ack": 2}
    backend.handle_webhook({"event": "message.ack", "payload": payload})
    assert _message_events(backend) == []
    backend._schedule_media_resolve.assert_not_called()
