"""Contact listing and address-book search operations."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from models import ChatContact, contact_cache_key, contact_storage_keys
from server.api import ApiError, ChatAPI, operation
from server.domain import bump_recency, search_contacts
from server.events_api import ContactWire, contact_to_wire
from server.models_api import (
    ContactOut,
    ContactsAddressBookOut,
    ContactsAddressBookRequest,
    ContactsBookRequest,
    ContactsCheckNumberOut,
    ContactsCheckNumberRequest,
    ContactsForProtocolRequest,
    ContactsListRequest,
    ContactsRegisterRequest,
    ContactsTouchRequest,
    OkOut,
)
from server.ops.common import PROTOCOLS

logger = logging.getLogger(__name__)


def _unread_counts() -> dict[tuple[str, str], int]:
    import protocols.db as backend
    from protocols.db import _DB_LOCK

    with _DB_LOCK:
        try:
            connection = sqlite3.connect(backend.DB_FILE)
            try:
                rows = connection.execute(
                    "SELECT protocol, contact_number, COUNT(*) FROM messages "
                    "WHERE is_mine = 0 AND read = 0 "
                    "GROUP BY protocol, contact_number"
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.Error:
            return {}
    return {(row[0], row[1]): row[2] for row in rows}


def _contact_payload(contact: Any, unread: dict[tuple[str, str], int]) -> ContactOut:
    """Serializza un contatto nel formato usato dalla web UI."""
    extras = dict(contact.extras)
    return ContactOut(
        id=str(contact.id),
        display_name=str(contact.display_name),
        protocol=str(contact.protocol),
        extras=extras,
        last_message_ts=int(extras.get("last_message_ts", 0) or 0),
        # Read-union: somma gli unread di tutte le chiavi alias dello stesso
        # contatto WhatsApp (storico DB diviso fra @c.us e @lid).
        unread=sum(
            unread.get((contact.protocol, key), 0)
            for key in contact_storage_keys(contact)
        ),
    )


@operation("contacts.list", request=ContactsListRequest, response=list[ContactOut])
def contacts_list(api: ChatAPI, req: ContactsListRequest) -> list[ContactOut]:
    """Active chats with unread counts, optionally filtered by *q*.  Blocking."""
    manager = api.manager
    # Riallinea il badge "non letti" WhatsApp allo stato reale (telefono
    # incluso) prima di calcolare i conteggi: leggere sul telefono non
    # tocca il DB locale finché non lo facciamo qui. Best-effort/throttled
    # (vedi WhatsAppBackend.reconcile_unread), nessun impatto per gli
    # altri protocolli o se il metodo non esiste.
    reconcile = getattr(manager.get("whatsapp"), "reconcile_unread", None)
    if reconcile is not None:
        try:
            reconcile()
        except Exception:
            logger.debug("WhatsApp unread reconcile failed", exc_info=True)
    unread = _unread_counts()
    contacts = manager.list_contacts()
    query = (req.q or "").strip()
    if query:
        # Substring case-insensitive su nome/id/telefono (stessa ricerca del
        # picker TUI) sulle chat attive (risposta rapida).
        contacts = search_contacts(contacts, query)
    return [_contact_payload(contact, unread) for contact in contacts]


@operation("contacts.book", request=ContactsBookRequest, response=list[ContactOut])
def contacts_book(api: ChatAPI, req: ContactsBookRequest) -> list[ContactOut]:
    """Search the aggregated address book.  Slow and blocking."""
    unread = _unread_counts()
    query = (req.q or "").strip()
    if not query:
        return []
    # Rubrica completa aggregata (come il picker TUI, in background):
    # lenta, il client la chiama dopo aver già mostrato i risultati delle chat.
    # ``force`` è disponibile ma non passato dal frontend di default per non
    # martellare WAHA a ogni keystroke.
    contacts = search_contacts(
        api.manager.list_address_book_sync(force=req.force), query
    )
    return [_contact_payload(contact, unread) for contact in contacts]


@operation(
    "contacts.for_protocol",
    request=ContactsForProtocolRequest,
    response=list[ContactWire],
)
def contacts_for_protocol(api: ChatAPI, req: ContactsForProtocolRequest) -> list[dict]:
    """Snapshot of the contacts one backend holds, in wire form.

    Unlike ``contacts.list`` (web shape: unread counts, WhatsApp reconcile,
    search) this is the raw contact list a frontend seeds its own list from.
    Copies: the caller never shares identity with the backend's objects.
    """
    backend = api.manager.get(req.protocol)
    if backend is None:
        raise ApiError("backend_down", "Backend unavailable")
    return [contact_to_wire(contact) for contact in list(backend.contacts)]


@operation("contacts.register", request=ContactsRegisterRequest, response=ContactWire)
def contacts_register(api: ChatAPI, req: ContactsRegisterRequest) -> dict:
    """Open-or-create a contact (ghost) the backend does not list yet.

    The contact is marked ``extras["ghost"]`` and registered in its backend
    (WhatsApp folds ``@c.us``/``@lid`` twins and may merge metadata into the
    object).  Returns the wire form of the contact as registered.  Blocking
    (WhatsApp loads the LID cache from disk).
    """
    if req.protocol not in PROTOCOLS or not req.id:
        raise ApiError("invalid_request", "Invalid request")
    backend = api.manager.get(req.protocol)
    if backend is None:
        raise ApiError("backend_down", "Backend unavailable")
    contact = ChatContact(
        id=req.id,
        display_name=req.display_name,
        protocol=req.protocol,
        extras={**req.extras, "ghost": True},
    )
    backend.register_contact(contact)
    return contact_to_wire(contact)


@operation(
    "contacts.address_book",
    request=ContactsAddressBookRequest,
    response=ContactsAddressBookOut,
)
def contacts_address_book(
    api: ChatAPI, req: ContactsAddressBookRequest
) -> ContactsAddressBookOut:
    """Whole address book of *protocols* (all when omitted) with failures.

    Unlike ``contacts.book`` no search is applied and no unread counts are
    computed.  Starts the WhatsApp LID resolver first (opportunistic) and
    never raises for a failing backend: ``errors`` maps protocol -> message.
    Slow and blocking.
    """
    manager = api.manager
    whatsapp = manager.get("whatsapp")
    start_resolver = getattr(whatsapp, "start_lid_resolver", None)
    if start_resolver is not None:
        try:
            start_resolver()
        except Exception:
            logger.debug("WhatsApp LID resolver start failed", exc_info=True)
    scope = set(req.protocols) if req.protocols is not None else None
    contacts = manager.list_address_book_sync(protocols=scope, force=req.force)
    errors = dict(manager.address_book_errors)
    return ContactsAddressBookOut(
        contacts=[ContactWire(**contact_to_wire(c)) for c in contacts],
        errors=errors,
    )


@operation("contacts.touch", request=ContactsTouchRequest, response=OkOut)
def contacts_touch(api: ChatAPI, req: ContactsTouchRequest) -> OkOut:
    """Advance the backend contact's ``last_message_ts`` (never lowers it).

    For frontends that show a message before the backend sees it (optimistic
    send); incoming traffic is bumped by the core itself.
    """
    backend = api.manager.get(req.protocol)
    if backend is not None:
        bump_recency(
            backend, contact_cache_key(req.protocol, req.contact_id), req.timestamp
        )
    return OkOut(ok=True)


@operation(
    "contacts.check_number",
    request=ContactsCheckNumberRequest,
    response=ContactsCheckNumberOut,
)
def contacts_check_number(
    api: ChatAPI, req: ContactsCheckNumberRequest
) -> ContactsCheckNumberOut:
    """Does *phone* exist on WhatsApp?  ``exists`` is ``None`` when unknown
    (no backend/endpoint, or the lookup failed).  Blocking (remote call)."""
    backend = api.manager.get("whatsapp")
    rest = getattr(backend, "_rest", None)
    check = getattr(rest, "check_number_exists", None)
    if check is None or not req.phone:
        return ContactsCheckNumberOut(exists=None)
    try:
        exists = check(req.phone)
    except Exception:
        logger.debug("WhatsApp number-exists check failed", exc_info=True)
        return ContactsCheckNumberOut(exists=None)
    return ContactsCheckNumberOut(exists=exists if isinstance(exists, bool) else None)
