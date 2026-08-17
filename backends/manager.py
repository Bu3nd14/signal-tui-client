"""
``BackendManager`` — owns all active ``ChatBackend`` instances and exposes a
unified, protocol-agnostic surface to the Textual UI.

The UI registers backends via ``register()`` and then calls the manager for
every operation.  The manager merges contact lists across protocols and routes
send/mark-read calls to the correct backend.
"""

from __future__ import annotations

import logging

from models import ChatContact

from .base import ChatBackend

logger = logging.getLogger(__name__)


class BackendManager:
    """Registry and facade over one or more ``ChatBackend`` implementations."""

    def __init__(self) -> None:
        self._backends: dict[str, ChatBackend] = {}

    # ─── Registry ─────────────────────────────────────────────────────

    def register(self, backend: ChatBackend) -> None:
        """Register a backend, keyed by its ``protocol`` id."""
        if not backend.protocol:
            raise ValueError("ChatBackend must define a non-empty 'protocol'")
        self._backends[backend.protocol] = backend

    def get(self, protocol: str) -> ChatBackend | None:
        """Return the backend for *protocol*, or ``None`` if not registered."""
        return self._backends.get(protocol)

    def all(self) -> list[ChatBackend]:
        """Return all registered backends."""
        return list(self._backends.values())

    def protocols(self) -> list[str]:
        """Return the protocol ids of all registered backends."""
        return list(self._backends.keys())

    # ─── Unified operations ───────────────────────────────────────────

    async def connect_all(self) -> None:
        """Connect every registered backend."""
        for backend in self._backends.values():
            await backend.connect()

    async def disconnect_all(self) -> None:
        """Disconnect every registered backend (best-effort, failures are logged)."""
        for backend in self._backends.values():
            try:
                await backend.disconnect()
            except Exception as _e:
                logger.debug("Backend disconnect failed", exc_info=True)

    def list_contacts(self) -> list[ChatContact]:
        """Return the merged contact list across all backends."""
        contacts: list[ChatContact] = []
        for backend in self._backends.values():
            # list_contacts is async; run synchronously here since backends
            # already cache their contact lists on connect.
            try:
                contacts.extend(backend.contacts)
            except AttributeError:
                pass
        return contacts

    async def send_message(
        self,
        protocol: str,
        contact_id: str,
        text: str,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_message: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> str:
        """Send a message via the backend for *protocol*."""
        backend = self._get_or_raise(protocol)
        kwargs = {
            "quote_timestamp": quote_timestamp,
            "quote_author": quote_author,
            "quote_message": quote_message,
        }
        if reply_to_message_id is not None:
            kwargs["reply_to_message_id"] = reply_to_message_id
        return await backend.send_message(contact_id, text, **kwargs)

    async def mark_read(self, protocol: str, contact_id: str) -> None:
        """Mark messages read via the backend for *protocol*."""
        backend = self._get_or_raise(protocol)
        await backend.mark_read(contact_id)

    def get_attachment_path(self, protocol: str, attachment_id: str):
        """Resolve an attachment id to a local path via *protocol*'s backend."""
        backend = self._backends.get(protocol)
        if backend is None:
            return None
        return backend.get_attachment_path(attachment_id)

    # ─── Helpers ──────────────────────────────────────────────────────

    def _get_or_raise(self, protocol: str) -> ChatBackend:
        backend = self._backends.get(protocol)
        if backend is None:
            raise KeyError(f"No backend registered for protocol {protocol!r}")
        return backend
