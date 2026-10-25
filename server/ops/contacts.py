"""Contact listing and address-book search operations."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from models import contact_storage_keys
from server.api import ChatAPI, operation
from server.domain import search_contacts
from server.models_api import ContactOut, ContactsBookRequest, ContactsListRequest

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
