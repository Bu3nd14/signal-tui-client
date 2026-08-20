"""
Abstract ``ChatBackend`` interface — the protocol bridge layer.

Any chat protocol (Signal, WhatsApp, ...) is implemented as a ``ChatBackend``
subclass that converts its protocol-specific data into the neutral
``ChatContact`` / ``ChatMessage`` / ``ChatEvent`` models defined in
``models``.  The Textual UI and the ``BackendManager`` only ever interact with
this interface, so they are completely decoupled from the underlying protocol.

The receive loop is expected to run in a dedicated worker thread (the same
pattern already used by the Signal JSON-RPC polling); backends never block the
Textual reactive event loop.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

from models import ChatContact, ChatEvent


class ChatBackend(ABC):
    """Abstract interface implemented by every chat protocol backend.

    Subclasses must set the class attribute ``protocol``.
    """

    #: Protocol identifier (one of ``models.PROTOCOL_*``).
    protocol: str = ""

    # ─── Lifecycle ────────────────────────────────────────────────────

    @abstractmethod
    async def connect(self) -> None:
        """Start the backend (daemon, websocket, ...) and load initial data."""
        raise NotImplementedError

    @abstractmethod
    async def disconnect(self) -> None:
        """Stop the backend and release resources."""
        raise NotImplementedError

    # ─── Data access ───────────────────────────────────────────────────

    @abstractmethod
    async def list_contacts(self) -> list[ChatContact]:
        """Return all known contacts as normalized ``ChatContact`` objects."""
        raise NotImplementedError

    @abstractmethod
    async def send_message(
        self,
        contact_id: str,
        text: str,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_message: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> str:
        """Send *text* to *contact_id*; return the message id/timestamp.

        Quote parameters are optional reply data.
        """
        raise NotImplementedError

    @abstractmethod
    async def mark_read(self, contact_id: str) -> None:
        """Mark all messages for *contact_id* as read."""
        raise NotImplementedError

    @abstractmethod
    async def receive(self) -> AsyncIterator[ChatEvent]:
        """Yield normalized ``ChatEvent`` objects as they arrive.

        Implementations should run this in a worker thread and marshal events
        back into the Textual event loop via ``call_from_thread``.
        """
        raise NotImplementedError
        if False:  # pragma: no cover - makes this an async generator contract
            yield  # type: ignore

    # ─── Attachments ──────────────────────────────────────────────────

    def get_attachment_path(self, attachment_id: str) -> Path | None:
        """Resolve an attachment id to a local file path, or ``None``.

        Default returns ``None`` (no attachment support).
        """
        return None

    # ─── Editing ─────────────────────────────────────────────────────

    def edit_message_sync(
        self, contact_id: str, message_id: str, new_text: str
    ) -> bool:
        """Modifica un messaggio proprio già inviato.

        Bloccante: chiamare SOLO da worker thread (come ``send_message_sync``).
        Ritorna ``True`` se il backend ha accettato l'operazione.

        Semantica di ``message_id`` per protocollo:

        - signal:   timestamp (ms) del messaggio originale, come stringa;
        - telegram: id server del messaggio (int come stringa);
        - whatsapp: Baileys message id (es. ``true_39...@c.us_ABC``).

        Default: nessun supporto → ``False``.
        """
        return False

    async def edit_message(
        self, contact_id: str, message_id: str, new_text: str
    ) -> bool:
        """Wrapper async del contratto (symmetry con ``list_address_book``).

        Delega a ``edit_message_sync`` via ``asyncio.to_thread``.
        """
        return await asyncio.to_thread(
            self.edit_message_sync, contact_id, message_id, new_text
        )

    def apply_edit(
        self,
        contact_id: str,
        message_id: str,
        new_text: str,
        *,
        is_mine: bool | None = None,
        edit_timestamp: int | None = None,
    ) -> dict | None:
        """Applica un edit (ricevuto o echo) a cache in-memory + SQLite.

        Punto UNICO di mutazione lato backend per gli edit (specchio di
        ``ingest_message`` per i messaggi nuovi).  Idempotente: se il testo è
        già ``new_text`` ritorna ``None`` (niente da fare).  Ritorna un dict
        ``{"message_id", "timestamp", "old_text", "text", "is_mine"}`` quando
        ha davvero modificato qualcosa, ``None`` altrimenti (target ignoto,
        media, testo identico).  Non aggiorna mai ``timestamp`` né ``id``.

        Default: ``None`` (nessun supporto).
        """
        return None

    # ─── Address book (rubrica completa) ──────────────────────────────

    def list_address_book_sync(self, force: bool = False) -> list[ChatContact]:
        """Rubrica COMPLETA del backend (non solo chat attive).

        Bloccante: chiamare SOLO da worker thread (pattern esistente di
        ``send_message_sync`` / ``mark_read_sync``).  Non solleva mai
        eccezioni: in caso di errore remoto ritorna l'ultima copia cached o
        ``[]``.

        Default: i contatti già caricati (``self.contacts``) marcati come
        rubrica — sufficiente per backend la cui lista è già completa.
        """
        return [
            replace(
                contact,
                extras={**contact.extras, "address_book": True},
            )
            for contact in self.contacts
        ]

    async def list_address_book(self) -> list[ChatContact]:
        """Wrapper async del contratto (symmetry con ``list_contacts``).

        Delega a ``list_address_book_sync`` via ``asyncio.to_thread``.
        """
        return await asyncio.to_thread(self.list_address_book_sync)

    def register_contact(self, contact: ChatContact) -> None:
        """Rende il contatto noto al backend (lookup per eventi/invio)."""
        if contact not in self.contacts:
            self.contacts.append(contact)

    # ─── Pairing ──────────────────────────────────────────────────────

    @property
    def needs_pairing(self) -> bool:
        """Whether the backend requires interactive QR/device pairing."""
        return False

    async def get_pairing_qr(self) -> str | None:
        """Return the current QR pairing link, or ``None`` if not pairing."""
        return None
