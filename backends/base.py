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

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
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

    # ─── Pairing ──────────────────────────────────────────────────────

    @property
    def needs_pairing(self) -> bool:
        """Whether the backend requires interactive QR/device pairing."""
        return False

    async def get_pairing_qr(self) -> str | None:
        """Return the current QR pairing link, or ``None`` if not pairing."""
        return None
