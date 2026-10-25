"""Domain helpers shared by the core and the frontends (no UI imports)."""

from __future__ import annotations

from models import PROTOCOL_WHATSAPP, ChatContact
from protocols.whatsapp import _is_placeholder_display_name, _jid_digits


def resolve_placeholder_name(backend, contact_id: str) -> str:
    """Nome placeholder dallo snapshot in-memory della rubrica (zero rete)."""
    if getattr(backend, "protocol", None) != PROTOCOL_WHATSAPP:
        return contact_id
    phone = ""
    lid = ""
    if contact_id.endswith("@lid"):
        lid = contact_id
        lid_map = getattr(backend, "_lid_map", None)
        if lid_map and contact_id in lid_map:
            entry = lid_map[contact_id]
            if isinstance(entry, dict):
                phone = str(entry.get("phone") or "")
    elif "@" in contact_id:
        phone = _jid_digits(contact_id.split("@", 1)[0])
    from protocols.whatsapp import _cached_address_book_name

    name = _cached_address_book_name(backend, phone or None, lid or None)
    return name or contact_id


def looks_unresolved(contact: ChatContact) -> bool:
    """True when *contact* still shows a placeholder instead of a real name."""
    if contact.protocol == PROTOCOL_WHATSAPP:
        return _is_placeholder_display_name(
            contact.display_name, contact.id, contact.extras.get("phone")
        )
    return (
        not contact.display_name
        or contact.display_name == contact.id
        or contact.display_name.isdigit()
    )
