"""
``BackendManager`` — owns all active ``ChatBackend`` instances and exposes a
unified, protocol-agnostic surface to the Textual UI.

The UI registers backends via ``register()`` and then calls the manager for
every operation.  The manager merges contact lists across protocols and routes
send/mark-read calls to the correct backend.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from models import ChatContact

from .base import ChatBackend

logger = logging.getLogger(__name__)


class BackendManager:
    """Registry and facade over one or more ``ChatBackend`` implementations."""

    def __init__(self) -> None:
        self._backends: dict[str, ChatBackend] = {}
        #: protocol → error message for the last ``list_address_book_sync`` run.
        self.address_book_errors: dict[str, str] = {}

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

    def list_address_book_sync(
        self, protocols: set[str] | None = None, force: bool = False
    ) -> list[ChatContact]:
        """Rubrica aggregata dei backend registrati (filtrata per *protocols*).

        Fan-out parallelo (``ThreadPoolExecutor``, max 3 worker) con
        ``future.result(timeout=25)`` per backend.  Un backend che fallisce o
        va in timeout → log + ``address_book_errors`` + si continua con gli
        altri (risultato parziale ammesso).  Concatena e ritorna.
        """
        self.address_book_errors = {}
        backends = [
            backend
            for backend in self._backends.values()
            if protocols is None or backend.protocol in protocols
        ]
        if not backends:
            return []

        contacts: list[ChatContact] = []
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {
                pool.submit(backend.list_address_book_sync, force=force): backend
                for backend in backends
            }
            for future, backend in futures.items():
                try:
                    contacts.extend(future.result(timeout=25))
                except Exception as exc:
                    self.address_book_errors[backend.protocol] = str(exc)
                    logger.warning(
                        "Address book failed for %s: %s",
                        backend.protocol,
                        exc,
                        exc_info=True,
                    )
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

    def send_message_sync(
        self,
        protocol: str,
        contact_id: str,
        text: str,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_message: str | None = None,
        reply_to_message_id: str | None = None,
        quote_attachments: list[str] | None = None,
    ) -> str:
        """Send through *protocol* from a worker thread."""
        backend = self._get_or_raise(protocol)
        kwargs = {
            "quote_timestamp": quote_timestamp,
            "quote_author": quote_author,
            "quote_message": quote_message,
        }
        if reply_to_message_id is not None:
            kwargs["reply_to_message_id"] = reply_to_message_id
        if quote_attachments is not None:
            kwargs["quote_attachments"] = quote_attachments
        message_id = backend.send_message_sync(contact_id, text, **kwargs)
        self._enqueue_sent_message(
            backend,
            contact_id,
            message_id,
            text,
            quote_timestamp=quote_timestamp,
            quote_author=quote_author,
            quote_message=quote_message,
            reply_to_message_id=reply_to_message_id,
        )
        return message_id

    def send_attachment_sync(
        self,
        protocol: str,
        contact_id: str,
        file_path: Path,
        *,
        caption: str | None = None,
        mime_type: str,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_message: str | None = None,
        reply_to_message_id: str | None = None,
        quote_attachments: list[str] | None = None,
        media_kind: str | None = None,
        filename: str | None = None,
    ) -> str:
        """Send an attachment through *protocol* from a worker thread."""
        backend = self._get_or_raise(protocol)
        kwargs = {
            "caption": caption,
            "mime_type": mime_type,
            "quote_timestamp": quote_timestamp,
            "quote_author": quote_author,
            "quote_message": quote_message,
        }
        if reply_to_message_id is not None:
            kwargs["reply_to_message_id"] = reply_to_message_id
        if quote_attachments is not None:
            kwargs["quote_attachments"] = quote_attachments
        if media_kind is not None:
            kwargs["media_kind"] = media_kind
        if filename is not None:
            kwargs["filename"] = filename
        message_id = backend.send_attachment_sync(contact_id, file_path, **kwargs)
        self._enqueue_sent_message(
            backend,
            contact_id,
            message_id,
            caption or "",
            quote_timestamp=quote_timestamp,
            quote_author=quote_author,
            quote_message=quote_message,
            reply_to_message_id=reply_to_message_id,
            attachment_path=file_path,
            mime_type=mime_type,
            media_kind=media_kind,
            filename=filename,
        )
        return message_id

    @staticmethod
    def _enqueue_sent_message(
        backend: ChatBackend,
        contact_id: str,
        message_id: str,
        text: str,
        **kwargs,
    ) -> None:
        try:
            backend.enqueue_sent_message(contact_id, message_id, text, **kwargs)
        except OSError:
            logger.warning(
                "Unable to copy sent attachment while mirroring: protocol=%s contact=%s",
                backend.protocol,
                contact_id,
                exc_info=True,
            )
            fallback = {**kwargs, "attachment_path": None}
            try:
                backend.enqueue_sent_message(contact_id, message_id, text, **fallback)
            except Exception:
                logger.exception(
                    "Unable to mirror sent message without attachment: protocol=%s contact=%s",
                    backend.protocol,
                    contact_id,
                )
        except Exception:
            logger.exception(
                "Unable to mirror sent message: protocol=%s contact=%s",
                backend.protocol,
                contact_id,
            )

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

    def edit_message_sync(
        self, protocol: str, contact_id: str, message_id: str, new_text: str
    ) -> bool:
        """Route an edit to the backend for *protocol*.

        Returns ``False`` if the backend is absent or does not support edits.
        """
        backend = self._backends.get(protocol)
        if backend is None:
            return False
        return backend.edit_message_sync(contact_id, message_id, new_text)

    # ─── Helpers ──────────────────────────────────────────────────────

    def _get_or_raise(self, protocol: str) -> ChatBackend:
        backend = self._backends.get(protocol)
        if backend is None:
            raise KeyError(f"No backend registered for protocol {protocol!r}")
        return backend
