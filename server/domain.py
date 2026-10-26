"""Domain helpers shared by the core and the frontends (no UI imports)."""

from __future__ import annotations

from typing import Any

from models import PROTOCOL_WHATSAPP, ChatContact, contact_storage_keys
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


def search_contacts(
    contacts: list[ChatContact], query: str, max_results: int = 50
) -> list[ChatContact]:
    """Filter *contacts* by *query* (case-insensitive) on name, id or phone.

    A contact matches if the query is a substring of its display name, its
    contact id (e.g. phone number) or its normalized ``extras["phone"]``.
    The query is stripped; an empty query returns all contacts (up to
    ``max_results``).

    Parameters
    ----------
    contacts:
        The full list of contacts to search.
    query:
        The search string typed by the user.
    max_results:
        Maximum number of results to return (default 50).

    Returns
    -------
    list[ChatContact]
        The matching contacts, in their original order.
    """
    q = query.strip().lower()
    if not q:
        return contacts[:max_results]

    results: list[ChatContact] = []
    for contact in contacts:
        name = contact.display_name.lower()
        cid = contact.id.lower()
        phone = contact.phone.lower()
        if q in name or q in cid or q in phone:
            results.append(contact)
            if len(results) >= max_results:
                break
    return results


def bump_recency(backend, cache_key: str, ts: Any) -> bool:
    """Raise ``last_message_ts`` of the backend's own contact for *cache_key*.

    The backend owns its contact objects; frontends hold wire copies, so the
    recency of the shared (backend/web) view is advanced here.  Returns True
    when a contact with that key exists.
    """
    if not isinstance(ts, int) or isinstance(ts, bool):
        return False
    for known in getattr(backend, "contacts", None) or []:
        if known.cache_key == cache_key:
            known.last_message_ts = max(known.last_message_ts, ts)
            return True
    return False


def sync_recency(backend) -> None:
    """Recover ``last_message_ts`` of *backend*'s contacts from its message cache.

    Best-effort fallback for backends that did not populate it (e.g. WhatsApp
    whose ``/chats`` payload may lack ``t``): keeps the highest cached message
    timestamp across the contact's storage keys, never lowering the value.
    """
    cache = getattr(backend, "cache", None)
    if not isinstance(cache, dict):
        return
    for contact in list(getattr(backend, "contacts", None) or []):
        best = contact.last_message_ts
        for key in contact_storage_keys(contact):
            for message in list(cache.get(key) or []):
                try:
                    best = max(best, int(message.get("timestamp") or 0))
                except (AttributeError, TypeError, ValueError):
                    continue
        if best > contact.last_message_ts:  # noqa: PLR1730 (no write when equal)
            contact.last_message_ts = best
