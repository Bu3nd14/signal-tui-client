"""Regression tests for the web UI group-sender name resolver.

WhatsApp hides group participants behind ``@lid`` "linked identifiers"; the web
``/messages`` endpoint resolves them to a display name so a bubble never shows
a bare numeric id.  The resolver used to call a non-existent backend method
(``_jid_to_phone``) and fall back to the raw lid digits.  These tests pin the
resolution order (exact id → lid alias → lid→phone cache → book number).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from models import PROTOCOL_WHATSAPP, ChatContact
from protocols.whatsapp import WhatsAppBackend
from web.api import _group_sender_resolver


class _Manager:
    def __init__(self, book, backend):
        self._book = book
        self._backend = backend

    def list_address_book_sync(self, protocols=None, force=False):
        if self._book is None:
            return self._backend.list_address_book_sync(force=force)
        return list(self._book)

    def get(self, protocol):
        return self._backend


def _book_contact(contact_id, name, phone="", lid="") -> ChatContact:
    extras = {"address_book": True}
    if phone:
        extras["phone"] = phone
    if lid:
        extras["lid"] = lid
    return ChatContact(
        id=contact_id,
        display_name=name,
        protocol=PROTOCOL_WHATSAPP,
        extras=extras,
    )


def _resolver(book, backend=None):
    return _group_sender_resolver(_Manager(book, backend), "whatsapp")


def test_lid_row_without_number_resolves_by_digits():
    book = [
        _book_contact(
            "220988985864200@c.us",
            "Mario Rossi",
            phone="220988985864200",
            lid="220988985864200@lid",
        )
    ]
    assert _resolver(book)("220988985864200@lid") == "Mario Rossi"


def test_lid_row_with_number_resolves_via_alias():
    book = [
        _book_contact(
            "393331234567@c.us",
            "Mario Rossi",
            phone="393331234567",
            lid="220988985864200@lid",
        )
    ]
    assert _resolver(book)("220988985864200@lid") == "Mario Rossi"


def test_lid_resolved_through_backend_cache_then_phone():
    backend = MagicMock()
    backend._jid_to_phone.return_value = "393331234567"
    book = [_book_contact("393331234567@c.us", "Luigi", phone="393331234567")]
    assert _resolver(book, backend)("278255131766315@lid") == "Luigi"


def test_c_us_sender_resolves_by_local_phone():
    book = [_book_contact("393331234567@c.us", "Luigi", phone="393331234567")]
    assert _resolver(book)("393331234567@c.us") == "Luigi"


def test_unknown_lid_falls_back_to_readable_number_not_jid():
    resolved = _resolver([])("999999999@lid")
    assert resolved == "999999999"
    assert "@" not in resolved


def test_backend_without_jid_to_phone_does_not_crash():
    # Backend "vecchio"/mock senza il metodo: nessun AttributeError propagato,
    # il fallback numerico leggibile resta.
    backend = object()
    assert _resolver([], backend)("999999999@lid") == "999999999"


def test_address_book_error_is_swallowed():
    class _Boom:
        def list_address_book_sync(self, protocols=None, force=False):
            raise RuntimeError("waha down")

        def get(self, protocol):
            return None

    resolve = _group_sender_resolver(_Boom(), "whatsapp")
    assert resolve("999999999@lid") == "999999999"


def test_whatsapp_backend_end_to_end_lid_alias(monkeypatch, tmp_path):
    """Rubrica WAHA reale (@lid con number) → nome, senza rete al render."""
    import protocols.db as backend_mod

    monkeypatch.setattr(backend_mod, "CACHE_DIR", tmp_path)
    backend = WhatsAppBackend(api_url="http://api.test", media_dir="")
    backend._rest = MagicMock()
    backend.start_lid_resolver = MagicMock()
    backend._rest.list_all_contacts.return_value = [
        {
            "id": "220988985864200@lid",
            "number": "393331234567",
            "name": "Mario Rossi",
            "pushname": None,
        }
    ]
    backend.contacts = [
        ChatContact(
            id="123456789@g.us",
            display_name="Gruppo",
            protocol=PROTOCOL_WHATSAPP,
            extras={"is_group": True},
        )
    ]

    resolve = _group_sender_resolver(_Manager(None, backend), "whatsapp")

    assert resolve("220988985864200@lid") == "Mario Rossi"
