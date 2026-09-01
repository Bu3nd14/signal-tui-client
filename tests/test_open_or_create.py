"""
Regression tests for open-or-create (Ctrl+S rubrica — milestone 5).

Covers:
- ``ContactListMixin._ensure_contact_selectable`` (canonical contact, ghost
  append + ``register_contact`` + in-place re-render, missing backend → None).
- WhatsApp ghost number-exists check (``_check_ghost_whatsapp_number``).
- Telegram ghost end-to-end send via ``InputPeerUser`` fallback.
- ``_select_contact`` regression: a missing backend never opens a chat.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from models import (
    PROTOCOL_TELEGRAM,
    PROTOCOL_WHATSAPP,
    ChatContact,
)
from protocols.telegram import TelegramBackend
from tui.app import SignalTUI


def _make_app(contacts: list[ChatContact] | None = None) -> SignalTUI:
    """Build a ``SignalTUI`` with neutralized workers and a controllable manager."""
    with (
        patch("tui.app.BackendManager"),
        patch("tui.app.SignalBackend"),
        patch("tui.app.whatsapp_enabled", return_value=False),
        patch("tui.app.telegram_enabled", return_value=False),
    ):
        app = SignalTUI()
    app.manager = MagicMock()
    app.contacts = list(contacts or [])
    app._render_contact_list = MagicMock()
    app._status = MagicMock()
    app.run_worker = MagicMock()
    app.call_from_thread = MagicMock(side_effect=lambda fn, *a, **k: fn(*a, **k))
    return app


def _ghost_whatsapp() -> ChatContact:
    """A WhatsApp ghost contact with ``id == "{phone}@c.us"``."""
    return ChatContact(
        id="393331234567@c.us",
        display_name="+393331234567",
        protocol=PROTOCOL_WHATSAPP,
        extras={"phone": "393331234567", "ghost": True},
    )


# ─── _ensure_contact_selectable ─────────────────────────────────────────────


class TestEnsureContactSelectable:
    """🪄 Open-or-create: contatto canonico vs ghost vs backend assente."""

    def test_known_contact_returns_canonical_object(self):
        known = ChatContact(id="+391", display_name="Mario", protocol="whatsapp")
        app = _make_app([known])
        duplicate = ChatContact(
            id="+391",
            display_name="Mario",
            protocol="whatsapp",
            extras={"address_book": True, "phone": "391"},
        )

        result = app._ensure_contact_selectable(duplicate)

        assert result is known  # stesso oggetto già in lista (non il duplicato)
        app.manager.get.assert_not_called()
        app._render_contact_list.assert_not_called()

    def test_new_contact_ghost_append_register_and_render(self):
        backend = MagicMock()
        app = _make_app([])
        app.manager.get.return_value = backend
        new = ChatContact(
            id="393331234567@c.us",
            display_name="Mario",
            protocol=PROTOCOL_WHATSAPP,
            extras={"phone": "393331234567"},
        )

        result = app._ensure_contact_selectable(new)

        assert result is new
        assert new.extras["ghost"] is True
        assert app.contacts == [new]
        backend.register_contact.assert_called_once_with(new)
        app._render_contact_list.assert_called_once_with([new])

    def test_missing_backend_returns_none_without_mutation(self):
        app = _make_app([])
        app.manager.get.return_value = None
        new = ChatContact(id="x", display_name="x", protocol="nosuch")

        assert app._ensure_contact_selectable(new) is None
        assert app.contacts == []
        assert "ghost" not in new.extras
        app._render_contact_list.assert_not_called()


# ─── WhatsApp ghost: check_number_exists ────────────────────────────────────


class TestGhostWhatsAppNumberCheck:
    """💬 Check-esiste numero WhatsApp ghost (worker, non bloccante)."""

    def test_ghost_id_is_phone_at_c_us(self):
        ghost = _ghost_whatsapp()
        assert ghost.id == "393331234567@c.us"
        assert ghost.phone == "393331234567"

    def test_false_warns(self):
        backend = MagicMock()
        backend._rest.check_number_exists.return_value = False
        app = _make_app([])
        app.manager.get.return_value = backend

        app._check_ghost_whatsapp_number(_ghost_whatsapp())

        app._status.assert_called_once_with("⚠️ 393331234567 non risulta su WhatsApp", 0)

    def test_true_no_warning(self):
        backend = MagicMock()
        backend._rest.check_number_exists.return_value = True
        app = _make_app([])
        app.manager.get.return_value = backend

        app._check_ghost_whatsapp_number(_ghost_whatsapp())

        app.call_from_thread.assert_not_called()

    def test_none_no_warning(self):
        backend = MagicMock()
        backend._rest.check_number_exists.return_value = None
        app = _make_app([])
        app.manager.get.return_value = backend

        app._check_ghost_whatsapp_number(_ghost_whatsapp())

        app.call_from_thread.assert_not_called()

    def test_check_error_no_warning(self):
        backend = MagicMock()
        backend._rest.check_number_exists.side_effect = RuntimeError("boom")
        app = _make_app([])
        app.manager.get.return_value = backend

        app._check_ghost_whatsapp_number(_ghost_whatsapp())

        app.call_from_thread.assert_not_called()


# ─── Telegram ghost send ────────────────────────────────────────────────────


class TestSendGhostTelegram:
    """📨 Invio verso un ghost Telegram senza dialogo via ``InputPeerUser``."""

    def test_send_ghost_uses_input_peer_user(self, monkeypatch):
        from telethon.tl.types import InputPeerUser

        backend = TelegramBackend()
        backend._loop = MagicMock()
        backend._client = MagicMock()
        backend._client.get_input_entity = AsyncMock(
            side_effect=ValueError("entity not in session")
        )

        sent: dict = {}

        async def _fake_send_message(entity, text, reply_to=None):
            sent["entity"] = entity
            sent["text"] = text
            return SimpleNamespace(id=777)

        backend._client.send_message = _fake_send_message

        ghost = ChatContact(
            id="42",
            display_name="Mamma Vod",
            protocol=PROTOCOL_TELEGRAM,
            extras={"access_hash": "123456789", "ghost": True, "phone": ""},
        )
        backend.register_contact(ghost)

        monkeypatch.setattr(
            "protocols.telegram.asyncio.run_coroutine_threadsafe",
            lambda coro, loop: SimpleNamespace(
                result=lambda timeout: asyncio.run(coro)
            ),
        )

        result = backend.send_message_sync("42", "ciao")

        assert result == "777"
        assert isinstance(sent["entity"], InputPeerUser)
        assert sent["entity"].user_id == 42
        assert sent["entity"].access_hash == 123456789
        assert sent["text"] == "ciao"

    def test_send_ghost_without_access_hash_raises(self, monkeypatch):
        backend = TelegramBackend()
        backend._loop = MagicMock()
        backend._client = MagicMock()
        backend._client.get_input_entity = AsyncMock(
            side_effect=ValueError("entity not in session")
        )
        ghost = ChatContact(id="42", display_name="NoHash", protocol=PROTOCOL_TELEGRAM)
        backend.register_contact(ghost)

        monkeypatch.setattr(
            "protocols.telegram.asyncio.run_coroutine_threadsafe",
            lambda coro, loop: SimpleNamespace(
                result=lambda timeout: asyncio.run(coro)
            ),
        )

        with pytest.raises(RuntimeError, match="access_hash mancante per 42"):
            backend.send_message_sync("42", "ciao")


# ─── _select_contact regression ─────────────────────────────────────────────


class TestSelectContactRegression:
    """🛡️ Regressione guard: backend assente → nessuna chat aperta."""

    def test_select_contact_missing_backend_does_not_open(self):
        app = _make_app([])
        app.manager.get.return_value = None

        app._select_contact(ChatContact(id="x", display_name="x", protocol="nosuch"))

        assert app.selected_contact is None
        app._status.assert_called_once_with("❌ backend non disponibile")
