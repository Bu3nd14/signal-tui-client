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
import inspect
import threading
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

from models import ChatContact, ChatEvent


def should_upgrade_outgoing_attachment(
    *,
    is_mine: bool,
    existing_path: str | Path | None,
    incoming_path: str | Path | None,
) -> bool:
    """Allow an outgoing attachment upgrade only toward a local file."""
    return bool(
        is_mine
        and (existing_path is None or not Path(existing_path).is_file())
        and incoming_path is not None
        and Path(incoming_path).is_file()
    )


class ChatBackend(ABC):
    """Abstract interface implemented by every chat protocol backend.

    Subclasses must set the class attribute ``protocol``.
    """

    #: Protocol identifier (one of ``models.PROTOCOL_*``).
    protocol: str = ""
    contacts: list[ChatContact]

    #: Class-level lock guarding ``register_contact`` (dedup check-then-act).
    #: Class-scoped so backends and test doubles need no ``__init__`` change.
    _register_lock = threading.Lock()

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

    def send_message_sync(
        self,
        contact_id: str,
        text: str,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_message: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> str:
        """Blocking message send for callers running in a worker thread."""
        raise NotImplementedError

    def send_attachment_sync(
        self,
        contact_id: str,
        file_path: Path,
        *,
        caption: str | None = None,
        mime_type: str,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_message: str | None = None,
        reply_to_message_id: str | None = None,
        filename: str | None = None,
    ) -> str:
        """Blocking image send for callers running in a worker thread."""
        raise NotImplementedError

    def send_attachments_sync(
        self,
        contact_id: str,
        file_paths: list[Path],
        *,
        captions: list[str | None],
        mime_types: list[str],
        media_kinds: list[str | None],
        filenames: list[str | None],
        batch_id: str | None = None,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_message: str | None = None,
        reply_to_message_id: str | None = None,
        quote_attachments: list[str] | None = None,
    ) -> list[str]:
        """Blocking multi-attachment send for callers in a worker thread.

        Default implementation: loop over ``send_attachment_sync`` one file
        at a time.  Backends with a native batch API (Signal attachment
        lists, Telegram albums) override this with a single optimized call.
        ``captions`` carries one caption per file (the web UI only fills the
        first one); quote/reply metadata applies to the first file only.
        Optional kwargs are forwarded only when the single-send signature
        accepts them (``quote_attachments`` exists on Signal only).
        """
        parameters = inspect.signature(self.send_attachment_sync).parameters
        var_keyword = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )

        def accepted(name: str) -> bool:
            return var_keyword or name in parameters

        message_ids: list[str] = []
        for index, file_path in enumerate(file_paths):
            optional: dict[str, Any] = {
                "media_kind": media_kinds[index],
                "filename": filenames[index],
            }
            if index == 0:
                optional.update(
                    quote_timestamp=quote_timestamp,
                    quote_author=quote_author,
                    quote_message=quote_message,
                    reply_to_message_id=reply_to_message_id,
                    quote_attachments=quote_attachments,
                )
            kwargs = {
                name: value
                for name, value in optional.items()
                if value is not None and accepted(name)
            }
            message_ids.append(
                self.send_attachment_sync(
                    contact_id,
                    file_path,
                    caption=captions[index],
                    mime_type=mime_types[index],
                    **kwargs,
                )
            )
        return message_ids

    def enqueue_sent_message(
        self,
        contact_id: str,
        message_id: str,
        text: str,
        *,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_message: str | None = None,
        reply_to_message_id: str | None = None,
        attachment_path: Path | None = None,
        mime_type: str | None = None,
        filename: str | None = None,
        batch_id: str | None = None,
        batch_index: int | None = None,
    ) -> None:
        """Publish a successful facade send through the normal receive queue."""

    def enqueue_sent_notification(
        self,
        contact_id: str,
        message_id: str,
        timestamp: int,
        batch_id: str | None = None,
    ) -> None:
        """Notify the UI that a batch send already mirrored its own rows.

        The Signal backend overrides this to publish the lightweight
        ``sent-mirror`` event after its send barrier materialized the mirror
        rows; the default is a no-op.
        """

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

    def send_reaction_sync(
        self,
        contact_id: str,
        message_id: str,
        emoji: str,
        *,
        target_author: str | None = None,
    ) -> bool:
        """Invia una reaction a un messaggio.

        Bloccante: chiamare SOLO da worker thread. ``message_id`` è il
        timestamp Signal, l'id server Telegram o il Baileys id WhatsApp.
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
        mark_edited: bool = True,
    ) -> dict[str, object] | None:
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

    def apply_reaction(
        self,
        contact_id: str,
        payload: dict[str, object],
    ) -> dict[str, object] | None:
        """Applica una reazione (ricevuta o snapshot) a cache + SQLite.

        Punto UNICO di mutazione lato backend per le reazioni (specchio di
        ``apply_edit``).  Risolve il target (§3.2), applica delta/snapshot (§3.3)
        e ritorna ``{"message_id", "timestamp", "reactions": [...]}`` per il push
        WS, ``None`` se target ignoto o no-op.

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

    def find_address_book_contact(self, contact_id: str) -> ChatContact | None:
        """Cerca un contatto nella cache rubrica in-memory (zero rete).

        Default: ``None`` (backend senza rubrica separata).  Implementato da
        WhatsApp/Telegram; per Signal la rubrica coincide con ``self.contacts``.
        Non bloccante: solo lookup in-memory (alcuni backend lazy-caricano la
        cache LID da disco, mai dalla rete).
        """
        return None

    def find_contact(self, contact_id: str) -> ChatContact | None:
        """Cerca un contatto: prima in ``self.contacts``, poi in rubrica.

        Zero rete.  Ritorna il contatto con l'id del client (nessuna riscrittura
        di ``@c.us`` in ``@lid``).
        """
        for contact in self.contacts:
            if str(contact.id) == contact_id:
                return contact
        return self.find_address_book_contact(contact_id)

    def register_contact(self, contact: ChatContact) -> bool:
        """Rende il contatto noto al backend (lookup per eventi/invio).

        Idempotente: dedup per ``cache_key`` (non per ``__eq__``, che include
        ``extras``).  Thread-safe: lock di classe.

        Returns:
            ``True`` se il contatto è stato aggiunto, ``False`` se già presente.
        """
        with ChatBackend._register_lock:
            for existing in self.contacts:
                if existing.cache_key == contact.cache_key:
                    return False
            self.contacts.append(contact)
            return True

    # ─── Status ───────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        """Whether the backend has an active session right now.

        Default ``False`` (a freshly constructed, un-connected backend).
        Subclasses override this with their own readiness flag so callers
        (e.g. the web UI's backend status badge) reflect the *real* session
        state instead of inferring it from whether any contacts happen to be
        loaded — a chat with zero conversations is still connected.
        """
        return False

    # ─── Pairing ──────────────────────────────────────────────────────

    @property
    def needs_pairing(self) -> bool:
        """Whether the backend requires interactive QR/device pairing."""
        return False

    async def get_pairing_qr(self) -> str | None:
        """Return the current QR pairing link, or ``None`` if not pairing."""
        return None
