"""Test TUI per l'evento ``contact_update`` (risoluzione dinamica ``@lid``).

Copre il dispatcher, il push verso la web UI e il "live path": un contatto
materializzato dal resolver e restituito da ``_identify_contact`` deve essere
comunque aggiunto alla lista della TUI e registrato nel backend.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from models import PROTOCOL_WHATSAPP, ChatContact, ChatEvent, contact_cache_key
from protocols.whatsapp import WhatsAppBackend
from tui.events import EventHandlingMixin


class _App(EventHandlingMixin):
    """Harness minimale: espone solo gli attributi toccati dai handler."""

    def __init__(self, backend, contacts=None, *, web_enabled=False):
        self.manager = SimpleNamespace(get=lambda _protocol: backend)
        self.contacts = list(contacts or [])
        self.selected_contact = None
        self._contact_list_dirty = False
        self._dirty_contact_keys: set[str] = set()
        self._cache: dict[str, list[dict]] = {}
        self._typing_contacts: dict[str, float] = {}
        self._typing_mumbling: dict[str, float] = {}
        self._seen_message_ids: set[tuple] = set()
        self._seen_timestamps: set[tuple] = set()
        self._TYPING_MUMBLING_DURATION = 5.0
        self._web_enabled = web_enabled

    def call_from_thread(self, *_args, **_kwargs):  # pragma: no cover - no-op
        return None


def _contact_update_event(contact: ChatContact, phone: str = "39123") -> ChatEvent:
    return ChatEvent(
        type="contact_update",
        protocol=PROTOCOL_WHATSAPP,
        contact_id=contact.id,
        payload={
            "phone": phone,
            "display_name": contact.display_name,
            "contact": contact,
        },
    )


def test_dispatcher_routes_contact_update():
    backend = WhatsAppBackend(api_url="")
    app = _App(backend)
    contact = ChatContact(id="1@lid", display_name="Giulia", protocol=PROTOCOL_WHATSAPP)
    event = _contact_update_event(contact)

    with patch.object(
        app, "_handle_contact_update_event", return_value=True
    ) as handler:
        assert app._handle_event(event) is True

    handler.assert_called_once_with(event)


def test_contact_update_marks_dirty_and_pushes_web():
    backend = WhatsAppBackend(api_url="")
    app = _App(backend, web_enabled=True)
    contact = ChatContact(id="1@lid", display_name="Giulia", protocol=PROTOCOL_WHATSAPP)
    event = _contact_update_event(contact, phone="391234567")

    with patch("web.bridge.push_event") as push:
        assert app._handle_contact_update_event(event) is True

    assert app._contact_list_dirty is True
    assert contact_cache_key(PROTOCOL_WHATSAPP, "1@lid") in app._dirty_contact_keys
    push.assert_called_once()
    pushed = push.call_args[0][0]
    assert pushed["type"] == "contact_update"
    assert pushed["payload"] == {
        "protocol": PROTOCOL_WHATSAPP,
        "contact_id": "1@lid",
        "display_name": "Giulia",
        "phone": "391234567",
    }


def test_contact_update_without_web_does_not_push():
    backend = WhatsAppBackend(api_url="")
    app = _App(backend, web_enabled=False)
    contact = ChatContact(id="1@lid", display_name="Giulia", protocol=PROTOCOL_WHATSAPP)

    with patch("web.bridge.push_event") as push:
        assert app._handle_contact_update_event(_contact_update_event(contact)) is True

    push.assert_not_called()


def test_materialized_contact_is_added_to_tui_list_on_message():
    backend = WhatsAppBackend(api_url="")
    materialized = ChatContact(
        id="777@lid",
        display_name="Giulia",
        protocol=PROTOCOL_WHATSAPP,
        extras={"phone": "393330000000"},
    )
    backend.register_contact(materialized)
    backend.ingest_message = MagicMock(return_value=True)

    app = _App(backend, contacts=[])
    event = ChatEvent(
        type="message",
        protocol=PROTOCOL_WHATSAPP,
        contact_id="777@lid",
        payload={
            "id": "m1",
            "text": "hi",
            "is_mine": False,
            "sender": "Giulia",
            "timestamp": 1000,
            "msg_type": "text",
        },
    )

    assert app._handle_message_event(event) is True
    assert materialized in app.contacts
    assert app._contact_list_dirty is True


def test_materialized_contact_is_added_to_tui_list_on_sent_mirror():
    backend = WhatsAppBackend(api_url="")
    materialized = ChatContact(
        id="888@lid", display_name="Luca", protocol=PROTOCOL_WHATSAPP
    )
    backend.register_contact(materialized)

    app = _App(backend, contacts=[])
    event = ChatEvent(
        type="sent-mirror",
        protocol=PROTOCOL_WHATSAPP,
        contact_id="888@lid",
        payload={"id": "x", "timestamp": 5000},
    )

    assert app._handle_sent_mirror_event(event) is True
    assert materialized in app.contacts


def test_placeholder_is_registered_with_backend():
    backend = WhatsAppBackend(api_url="")
    backend.ingest_message = MagicMock(return_value=True)
    backend.register_contact = MagicMock(return_value=True)
    backend._identify_contact = MagicMock(return_value=None)

    app = _App(backend, contacts=[])
    event = ChatEvent(
        type="message",
        protocol=PROTOCOL_WHATSAPP,
        contact_id="999@lid",
        payload={
            "id": "m2",
            "text": "hi",
            "is_mine": False,
            "sender": "999@lid",
            "timestamp": 2000,
            "msg_type": "text",
        },
    )

    assert app._handle_message_event(event) is True
    backend.register_contact.assert_called_once()
    placeholder = backend.register_contact.call_args[0][0]
    assert placeholder.cache_key == contact_cache_key(PROTOCOL_WHATSAPP, "999@lid")
    assert placeholder in app.contacts
