"""
Phase 2 (cross-protocol contracts) tests for message editing.

Covers the optional ``ChatBackend`` editing methods and the
``BackendManager`` router introduced in DESIGN_EDIT_MESSAGES.md §2:

- ``edit_message_sync`` default returns ``False`` (no edit support);
- ``edit_message`` (async wrapper) default returns ``False``;
- ``apply_edit`` default returns ``None``;
- ``BackendManager.edit_message_sync`` returns ``False`` when the protocol
  is unknown, and routes to the backend with the right arguments otherwise;
- the async wrapper delegates to ``edit_message_sync`` via ``asyncio.to_thread``.

No real I/O: a minimal ``ChatBackend`` subclass (the ``_MinimalBackend``
pattern from ``test_address_book.py``) and plain mocks are used throughout.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backends.base import ChatBackend
from backends.manager import BackendManager
from models import ChatContact


class _MinimalBackend(ChatBackend):
    """Concrete ChatBackend implementing every abstract method trivially.

    It deliberately does NOT override the optional editing methods, so the
    base-class defaults (``False`` / ``None``) are exercised.
    """

    protocol = "test"

    def __init__(self, contacts: list[ChatContact] | None = None):
        self.contacts: list[ChatContact] = list(contacts or [])

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def list_contacts(self) -> list[ChatContact]:
        return []

    async def send_message(self, *args, **kwargs) -> str:
        return ""

    async def mark_read(self, contact_id: str) -> None:
        pass

    async def receive(self):
        if False:
            yield


# ─── ChatBackend default contracts ────────────────────────────────────────────


class TestEditDefaults:
    """🧱 Default senza supporto: ``False``/``None`` e mai eccezioni."""

    def test_edit_message_sync_default_false(self):
        backend = _MinimalBackend()

        assert backend.edit_message_sync("+391234567890", "1000", "nuovo") is False

    def test_edit_message_async_default_false(self):
        backend = _MinimalBackend()

        result = asyncio.run(backend.edit_message("+391234567890", "1000", "nuovo"))

        assert result is False

    def test_apply_edit_default_none(self):
        backend = _MinimalBackend()

        assert backend.apply_edit("+391234567890", "1000", "nuovo") is None

    def test_apply_edit_accepts_keyword_only_arguments(self):
        """La firma accetta ``is_mine``/``edit_timestamp`` keyword-only."""
        backend = _MinimalBackend()

        result = backend.apply_edit(
            "+391234567890",
            "1000",
            "nuovo",
            is_mine=True,
            edit_timestamp=1234567890,
        )

        assert result is None


# ─── BackendManager router ────────────────────────────────────────────────────


class TestManagerEditRouting:
    """🗂️ Il router ``BackendManager.edit_message_sync`` delega (o nega)."""

    def test_unknown_protocol_returns_false(self):
        manager = BackendManager()

        assert manager.edit_message_sync("nope", "c", "m", "t") is False

    def test_registered_backend_without_support_returns_false(self):
        """Un backend registrato ma senza override → router ritorna ``False``."""
        manager = BackendManager()
        manager.register(_MinimalBackend())

        assert manager.edit_message_sync("test", "c", "m", "t") is False

    def test_router_delegates_with_correct_arguments(self):
        """Il router inoltra (contact_id, message_id, new_text) al backend."""
        manager = BackendManager()
        backend = _MinimalBackend()
        backend.edit_message_sync = MagicMock(return_value=True)
        manager.register(backend)

        result = manager.edit_message_sync("test", "c", "m", "new-text")

        assert result is True
        backend.edit_message_sync.assert_called_once_with("c", "m", "new-text")

    def test_router_returns_backend_result(self):
        """Il valore di ritorno del backend viene propagato tal quale."""
        manager = BackendManager()
        backend = _MinimalBackend()
        backend.edit_message_sync = MagicMock(return_value=False)
        manager.register(backend)

        assert manager.edit_message_sync("test", "c", "m", "t") is False


# ─── Async wrapper delegation ─────────────────────────────────────────────────


class TestAsyncWrapperDelegation:
    """🔄 ``edit_message`` delega a ``edit_message_sync`` con gli stessi argomenti."""

    def test_delegates_with_same_arguments(self):
        backend = _MinimalBackend()
        backend.edit_message_sync = MagicMock(return_value=True)
        seen: dict = {}

        async def fake_to_thread(func, *args):
            seen["args"] = args
            return func(*args)

        with patch("backends.base.asyncio.to_thread", side_effect=fake_to_thread):
            result = asyncio.run(backend.edit_message("c", "m", "new-text"))

        assert result is True
        assert seen["args"] == ("c", "m", "new-text")
        backend.edit_message_sync.assert_called_once_with("c", "m", "new-text")

    def test_delegates_result_false_when_sync_returns_false(self):
        backend = _MinimalBackend()

        async def fake_to_thread(func, *args):
            return func(*args)

        with patch("backends.base.asyncio.to_thread", side_effect=fake_to_thread):
            result = asyncio.run(backend.edit_message("c", "m", "t"))

        assert result is False
