from __future__ import annotations

import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from models import PROTOCOL_SIGNAL, ChatContact, ChatEvent
from protocols.base import ChatBackend


@pytest.mark.parametrize(
    "payload",
    [
        {
            "target_message_id": "1700000000000",
            "target_timestamp": 1700000000000,
            "mode": "delta",
            "emoji": "👍",
            "is_remove": False,
            "author": "Ada",
            "author_key": "+391234567890",
            "is_mine": False,
            "snapshot": None,
            "timestamp": 1700000000100,
            "contact": None,
        },
        {
            "target_message_id": "42",
            "target_timestamp": None,
            "mode": "snapshot",
            "snapshot": [
                {
                    "emoji": "❤️",
                    "count": 2,
                    "is_mine": True,
                    "authors": ["Ada", "You"],
                }
            ],
            "timestamp": 1700000000200,
            "contact": None,
        },
    ],
)
def test_reaction_event_constructs_and_serializes(payload):
    event = ChatEvent(
        type="reaction_update",
        protocol=PROTOCOL_SIGNAL,
        contact_id="+391234567890",
        payload=payload,
    )

    serialized = asdict(event)
    assert serialized == {
        "type": "reaction_update",
        "protocol": PROTOCOL_SIGNAL,
        "contact_id": "+391234567890",
        "payload": payload,
    }
    assert json.loads(json.dumps(serialized, ensure_ascii=False)) == serialized


def _reaction_event() -> ChatEvent:
    return ChatEvent(
        type="reaction_update",
        protocol=PROTOCOL_SIGNAL,
        contact_id="+391234567890",
        payload={"mode": "delta", "emoji": "👍"},
    )


def test_reaction_dispatch_without_backend_support_returns_false(app_for_test):
    app_for_test.manager.get.return_value = object()
    app_for_test._contact_list_dirty = False

    assert app_for_test._handle_event(_reaction_event()) is False
    assert app_for_test._contact_list_dirty is False


def test_reaction_dispatch_applies_without_marking_contact_dirty(
    app_for_test_with_mocks,
):
    app, backend = app_for_test_with_mocks
    backend.apply_reaction = MagicMock(
        return_value={"message_id": "1", "timestamp": 1, "reactions": []}
    )
    app._contact_list_dirty = False
    event = _reaction_event()

    assert app._handle_event(event) is True
    backend.apply_reaction.assert_called_once_with(event.contact_id, event.payload)
    assert app._contact_list_dirty is False


def test_reaction_dispatch_unknown_backend_returns_false(app_for_test):
    app_for_test.manager.get.return_value = None
    app_for_test._contact_list_dirty = False

    assert app_for_test._handle_event(_reaction_event()) is False
    assert app_for_test._contact_list_dirty is False


def _reaction_app(web_enabled: bool):
    info = {
        "message_id": 77,
        "timestamp": "1000",
        "reactions": [
            {
                "emoji": "👍",
                "count": 2,
                "is_mine": True,
                "authors": ["Ada", "You"],
            }
        ],
    }
    backend = SimpleNamespace(apply_reaction=MagicMock(return_value=info))
    return SimpleNamespace(
        manager=SimpleNamespace(get=lambda _protocol: backend),
        _web_enabled=web_enabled,
    )


def test_reaction_event_pushes_complete_web_update():
    app = _reaction_app(web_enabled=True)
    event = ChatEvent(
        type="reaction_update",
        protocol="telegram",
        contact_id="42",
        payload={"mode": "snapshot"},
    )

    with patch("web.bridge.push_event") as push_event:
        assert app_for_mixin(app, event)

    push_event.assert_called_once_with(
        {
            "type": "reaction_update",
            "payload": {
                "protocol": "telegram",
                "contact_id": "42",
                "message_id": "77",
                "timestamp": 1000,
                "reactions": [
                    {
                        "emoji": "👍",
                        "count": 2,
                        "is_mine": True,
                        "authors": ["Ada", "You"],
                    }
                ],
            },
        }
    )


def test_reaction_event_does_not_push_when_web_is_disabled():
    app = _reaction_app(web_enabled=False)

    with patch("web.bridge.push_event") as push_event:
        assert app_for_mixin(app, _reaction_event())

    push_event.assert_not_called()


def app_for_mixin(app, event):
    from tui.events import EventHandlingMixin

    return EventHandlingMixin._handle_reaction_event(app, event)


class _MinimalBackend(ChatBackend):
    protocol = "test"

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def list_contacts(self) -> list[ChatContact]:
        return []

    async def send_message(self, *args, **kwargs) -> str:
        return ""

    async def mark_read(self, contact_id: str) -> None:
        pass

    async def receive(self):
        if False:
            yield


def test_apply_reaction_default_returns_none():
    assert _MinimalBackend().apply_reaction("contact", {"mode": "delta"}) is None
