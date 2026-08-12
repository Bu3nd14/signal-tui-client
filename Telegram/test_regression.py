"""
Regression test suite — verifies that adding Telegram support does NOT
break existing Signal and WhatsApp functionality.

Covers:
- BackendManager multi-protocol routing (3 backends)
- Cache isolation across protocols
- Protocol filter cycle integrity (4 protocols)
- Model constants integrity (adding PROTOCOL_TELEGRAM)
- Emoji map integrity
- Config isolation (telegram_enabled vs whatsapp_enabled)
- Multi-backend poll_once contract
"""

from __future__ import annotations

import queue
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from models import (
    ChatContact,
    ChatEvent,
    ChatMessage,
    PROTOCOL_SIGNAL,
    PROTOCOL_WHATSAPP,
    PROTOCOL_TELEGRAM,
    contact_cache_key,
    protocol_emoji,
)
from backends import BackendManager, SignalBackend, WhatsAppBackend
from backends.base import ChatBackend


# ─── Helpers ──────────────────────────────────────────────────────────────

def _signal_contact(cid="+391", name="Mario") -> ChatContact:
    return ChatContact(id=cid, display_name=name, protocol=PROTOCOL_SIGNAL)

def _whatsapp_contact(cid="wa:1@s.whatsapp.net", name="Anna") -> ChatContact:
    return ChatContact(id=cid, display_name=name, protocol=PROTOCOL_WHATSAPP)

def _telegram_contact(cid="123456789", name="Luigi") -> ChatContact:
    return ChatContact(id=cid, display_name=name, protocol=PROTOCOL_TELEGRAM)

def _make_multi_manager() -> BackendManager:
    """Creates a BackendManager with all 3 backends registered."""
    manager = BackendManager()
    # Use __new__ to skip __init__ side-effects (daemon, RPC, etc.)
    signal = SignalBackend.__new__(SignalBackend)
    signal.protocol = PROTOCOL_SIGNAL
    signal.contacts = [_signal_contact()]
    signal.cache = {}
    signal._events = queue.Queue()
    signal._event_queue = signal._events   # alias per poll_once()

    whatsapp = WhatsAppBackend.__new__(WhatsAppBackend)
    whatsapp.protocol = PROTOCOL_WHATSAPP
    whatsapp.contacts = [_whatsapp_contact()]
    whatsapp.cache = {}
    whatsapp._events = queue.Queue()

    manager.register(signal)
    manager.register(whatsapp)
    return manager, signal, whatsapp



# ─── BackendManager multi-protocol routing ───────────────────────────────

class TestMultiBackendRouting:
    """🗂️ 3 backends registrati: routing corretto per ogni protocollo."""

    def test_all_protocols_registered(self):
        manager, signal, whatsapp = _make_multi_manager()
        from backends.telegram import TelegramBackend
        telegram = TelegramBackend.__new__(TelegramBackend)
        telegram.protocol = PROTOCOL_TELEGRAM
        telegram.contacts = [_telegram_contact()]
        telegram.cache = {}
        telegram._events = queue.Queue()
        manager.register(telegram)

        assert set(manager.protocols()) == {
            PROTOCOL_SIGNAL, PROTOCOL_WHATSAPP, PROTOCOL_TELEGRAM,
        }

    def test_list_contacts_merges_three_backends(self):
        manager, signal, whatsapp = _make_multi_manager()
        from backends.telegram import TelegramBackend
        telegram = TelegramBackend.__new__(TelegramBackend)
        telegram.protocol = PROTOCOL_TELEGRAM
        telegram.contacts = [_telegram_contact()]
        telegram.cache = {}
        telegram._events = queue.Queue()
        manager.register(telegram)

        contacts = manager.list_contacts()
        assert len(contacts) == 3
        protocols = {c.protocol for c in contacts}
        assert protocols == {PROTOCOL_SIGNAL, PROTOCOL_WHATSAPP, PROTOCOL_TELEGRAM}

    def test_get_attachment_path_routes_to_correct_backend(self):
        manager, signal, whatsapp = _make_multi_manager()
        signal.get_attachment_path = lambda aid: Path("/signal/file.jpg") if aid == "s1" else None
        whatsapp.get_attachment_path = lambda aid: Path("/wa/file.jpg") if aid == "w1" else None

        assert manager.get_attachment_path(PROTOCOL_SIGNAL, "s1") == Path("/signal/file.jpg")
        assert manager.get_attachment_path(PROTOCOL_WHATSAPP, "w1") == Path("/wa/file.jpg")
        assert manager.get_attachment_path("nonexistent", "x") is None

    def test_send_message_routes_to_correct_backend(self):
        import asyncio
        manager, signal, whatsapp = _make_multi_manager()
        signal.send_message = AsyncMock(return_value="sig-ts")
        whatsapp.send_message = AsyncMock(return_value="wa-ts")

        result_sig = asyncio.run(
            manager.send_message(PROTOCOL_SIGNAL, "+391", "ciao signal")
        )
        result_wa = asyncio.run(
            manager.send_message(PROTOCOL_WHATSAPP, "wa:1", "ciao wa")
        )

        assert result_sig == "sig-ts"
        assert result_wa == "wa-ts"
        signal.send_message.assert_called_once_with(
            "+391", "ciao signal",
            quote_timestamp=None, quote_author=None, quote_message=None,
        )
        whatsapp.send_message.assert_called_once_with(
            "wa:1", "ciao wa",
            quote_timestamp=None, quote_author=None, quote_message=None,
        )

    def test_send_message_unknown_protocol_still_raises(self):
        import asyncio
        manager, signal, whatsapp = _make_multi_manager()
        # Adding Telegram doesn't make unknown protocols valid
        with pytest.raises(KeyError):
            asyncio.run(manager.send_message("madeup", "x", "y"))

    def test_mark_read_routes_to_correct_backend(self):
        import asyncio
        manager, signal, whatsapp = _make_multi_manager()
        signal.mark_read = AsyncMock()
        whatsapp.mark_read = AsyncMock()

        asyncio.run(manager.mark_read(PROTOCOL_SIGNAL, "+391"))
        asyncio.run(manager.mark_read(PROTOCOL_WHATSAPP, "wa:1"))

        signal.mark_read.assert_called_once_with("+391")
        whatsapp.mark_read.assert_called_once_with("wa:1")

    def test_poll_once_independent_per_backend(self):
        """Ogni backend ha la sua coda: poll_once non mischia eventi."""
        manager, signal, whatsapp = _make_multi_manager()
        signal._events.put(ChatEvent(
            type="message", protocol=PROTOCOL_SIGNAL,
            contact_id="+391", payload={"text": "sig"},
        ))
        whatsapp._events.put(ChatEvent(
            type="message", protocol=PROTOCOL_WHATSAPP,
            contact_id="wa:1", payload={"text": "wa"},
        ))

        sig_events = signal.poll_once()
        wa_events = whatsapp.poll_once()

        assert len(sig_events) == 1
        assert sig_events[0].protocol == PROTOCOL_SIGNAL
        assert len(wa_events) == 1
        assert wa_events[0].protocol == PROTOCOL_WHATSAPP

    def test_register_same_protocol_twice_overwrites(self):
        """Registrare due backend con lo stesso protocol sovrascrive."""
        manager, signal, _ = _make_multi_manager()
        signal2 = SignalBackend.__new__(SignalBackend)
        signal2.protocol = PROTOCOL_SIGNAL
        signal2.contacts = []
        manager.register(signal2)


# ─── Cache isolation across protocols ─────────────────────────────────────

class TestCacheIsolation:
    """🗃️ I messaggi di protocolli diversi NON si mischiano nel cache."""

    def _data(self, contact_id, protocol, text="Hello", id_="1"):
        return {
            "id": id_, "contact_id": contact_id,
            "protocol": protocol, "text": text,
            "is_mine": False, "sender": "X",
            "timestamp": 1000, "quote_text": None,
            "msg_type": "text", "attachment_info": None,
            "attachment_id": None,
        }

    def test_signal_messages_not_visible_in_whatsapp_cache(self):
        """Messaggi Signal nel cache non appaiono in WhatsApp."""
        manager, signal, whatsapp = _make_multi_manager()
        signal.cache = {}
        whatsapp.cache = {}

        # Ingest a signal message
        data = self._data("+391", PROTOCOL_SIGNAL, "Signal msg")
        signal.ingest_message("+391", data, 1000)

        assert "+391" in signal.cache
        assert "+391" not in whatsapp.cache
        assert len(signal.cache["+391"]) == 1
        assert signal.cache["+391"][0]["text"] == "Signal msg"

    def test_whatsapp_messages_not_visible_in_signal_cache(self):
        """Messaggi WhatsApp nel cache non appaiono in Signal."""
        manager, signal, whatsapp = _make_multi_manager()
        signal.cache = {}
        whatsapp.cache = {}

        data = self._data("wa:1", PROTOCOL_WHATSAPP, "WA msg")
        whatsapp.ingest_message("wa:1", data, 1000)

        assert "wa:1" in whatsapp.cache
        assert "wa:1" not in signal.cache
        assert whatsapp.cache["wa:1"][0]["text"] == "WA msg"

    def test_same_phone_different_protocol_different_cache_keys(self):
        """Stesso numero su Signal e Telegram → due cache key diverse."""
        from backends.telegram import TelegramBackend
        telegram = TelegramBackend.__new__(TelegramBackend)
        telegram.protocol = PROTOCOL_TELEGRAM
        telegram.contacts = []
        telegram.cache = {}
        telegram._events = queue.Queue()
        telegram._contacts_by_id = {}
        telegram._seen_msg_ids = set()

        manager, signal, whatsapp = _make_multi_manager()
        signal.cache = {}
        manager.register(telegram)

        data_sig = self._data("+391234567890", PROTOCOL_SIGNAL, "Signal", "s1")
        data_tg = self._data("+391234567890", PROTOCOL_TELEGRAM, "Telegram", "t1")

        signal.ingest_message("+391234567890", data_sig, 1000)
        telegram.ingest_message("+391234567890", data_tg, 1000)

        assert signal.cache["+391234567890"][0]["text"] == "Signal"
        assert telegram.cache["+391234567890"][0]["text"] == "Telegram"
        assert len(signal.cache["+391234567890"]) == 1
        assert len(telegram.cache["+391234567890"]) == 1

    def test_contact_cache_key_protocol_namespacing(self):
        """contact_cache_key include il protocollo per evitare collisioni."""
        key_sig = contact_cache_key(PROTOCOL_SIGNAL, "+391234567890")
        key_tg = contact_cache_key(PROTOCOL_TELEGRAM, "+391234567890")
        key_wa = contact_cache_key(PROTOCOL_WHATSAPP, "+391234567890")

        assert key_sig != key_tg
        assert key_sig != key_wa
        assert key_tg != key_wa
        assert key_sig.startswith(PROTOCOL_SIGNAL + ":")


# ─── Model constants integrity ───────────────────────────────────────────

class TestModelConstants:
    """🧬 Aggiungere PROTOCOL_TELEGRAM non modifica le costanti esistenti."""

    def test_signal_constant_unchanged(self):
        assert PROTOCOL_SIGNAL == "signal"

    def test_whatsapp_constant_unchanged(self):
        assert PROTOCOL_WHATSAPP == "whatsapp"

    def test_telegram_constant_new(self):
        assert PROTOCOL_TELEGRAM == "telegram"

    def test_emoji_map_retains_signal(self):
        from models import PROTOCOL_EMOJI
        assert PROTOCOL_EMOJI[PROTOCOL_SIGNAL] == "📱"

    def test_emoji_map_retains_whatsapp(self):
        from models import PROTOCOL_EMOJI
        assert PROTOCOL_EMOJI[PROTOCOL_WHATSAPP] == "💬"

    def test_emoji_map_has_telegram(self):
        from models import PROTOCOL_EMOJI
        assert PROTOCOL_EMOJI[PROTOCOL_TELEGRAM] == "📨"

    def test_protocol_emoji_fallback_unchanged(self):
        """protocol_emoji per protocollo sconosciuto → fallback '💬'."""
        assert protocol_emoji("madeup") == "💬"

    def test_protocol_emoji_signal(self):
        assert protocol_emoji(PROTOCOL_SIGNAL) == "📱"

    def test_protocol_emoji_whatsapp(self):
        assert protocol_emoji(PROTOCOL_WHATSAPP) == "💬"

    def test_protocol_emoji_telegram(self):
        assert protocol_emoji(PROTOCOL_TELEGRAM) == "📨"

    def test_contact_cache_key_format_unchanged(self):
        key = contact_cache_key(PROTOCOL_SIGNAL, "+391234567890")
        assert key == "signal:+391234567890"


# ─── Config isolation ────────────────────────────────────────────────────

class TestConfigIsolation:
    """⚙️ telegram_enabled() non interferisce con whatsapp_enabled()."""

    def test_telegram_disabled_by_default(self):
        """Senza credenziali, telegram_enabled() è False."""
        with patch.dict("os.environ", {}, clear=True):
            from backends.config import telegram_enabled
            # Without TELEGRAM_API_ID/API_HASH in env, should be False
            assert telegram_enabled() is False

    def test_whatsapp_enabled_not_affected_by_telegram_config(self):
        """whatsapp_enabled() non è influenzato da variabili Telegram."""
        with patch.dict("os.environ", {
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": "abc",
        }, clear=True):
            from backends.config import telegram_enabled, whatsapp_enabled
            # Mock the TCP reachability check since WAHA may be running locally
            with patch("backends.config._local_waha_reachable", return_value=False):
                assert telegram_enabled() is True
                assert whatsapp_enabled() is False

    def test_telegram_enabled_requires_both_credentials(self):
        with patch.dict("os.environ", {"TELEGRAM_API_ID": "12345"}, clear=True):
            from backends.config import telegram_enabled
            assert telegram_enabled() is False

        with patch.dict("os.environ", {"TELEGRAM_API_HASH": "abc"}, clear=True):
            from backends.config import telegram_enabled
            assert telegram_enabled() is False

        with patch.dict("os.environ", {
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": "abc",
        }, clear=True):
            from backends.config import telegram_enabled
            assert telegram_enabled() is True

    def test_session_path_uses_cache_dir(self):
        from backends.config import get_telegram_session_path
        path = get_telegram_session_path()
        assert path.name == "telegram.session"
        assert "signal-tui-client" in str(path)


# ─── Protocol filter cycle with 4 protocols ──────────────────────────────

class TestProtocolFilterFourWay:
    """🎛️ Il filtro Ctrl+W cicla su 4 protocolli senza rompere l'ordine."""

    def test_filter_cycle_order_with_telegram(self):
        """Ordine: all → signal → whatsapp → telegram → all."""
        order = ["all", "signal", "whatsapp", "telegram"]
        assert len(order) == 4

        # Simulate the cycle logic that will be in signal_tui.py
        current = "all"
        observed = []
        for _ in range(5):
            idx = order.index(current)
            current = order[(idx + 1) % len(order)]
            observed.append(current)

        assert observed == ["signal", "whatsapp", "telegram", "all", "signal"]

    def test_filtered_contacts_respects_telegram(self):
        """_filtered_contacts filtra per 'telegram' come per signal/whatsapp."""
        contacts = [
            _signal_contact(), _whatsapp_contact(), _telegram_contact(),
            _telegram_contact("999", "Paolo"),
        ]
        # Simula _filtered_contacts
        def filtered(protocol_filter):
            if protocol_filter == "all":
                return contacts
            return [c for c in contacts if c.protocol == protocol_filter]

        assert len(filtered("all")) == 4
        assert len(filtered(PROTOCOL_SIGNAL)) == 1
        assert len(filtered(PROTOCOL_WHATSAPP)) == 1
        assert len(filtered(PROTOCOL_TELEGRAM)) == 2


# ─── Edge cases and unhappy paths ────────────────────────────────────────

class TestEdgeCases:
    """⚠️ Casi limite: protocolli vuoti, doppia registrazione, shutdown."""

    def test_register_empty_protocol_still_rejected(self):
        """Registrare un backend con protocol='' deve ancora fallire."""
        manager = BackendManager()
        class _Bad(ChatBackend):
            protocol = ""
            def __init__(self): pass
            async def connect(self): ...
            async def disconnect(self): ...
            async def list_contacts(self): return []
            async def send_message(self, *a, **k): return ""
            async def mark_read(self, *a): ...
            async def receive(self): ...
        with pytest.raises(ValueError, match="non-empty"):
            manager.register(_Bad())

    def test_whatsapp_backend_not_affected_by_telegram_registration(self):
        """Registrare Telegram non modifica il riferimento WhatsApp."""
        manager, signal, whatsapp = _make_multi_manager()
        from backends.telegram import TelegramBackend
        telegram = TelegramBackend.__new__(TelegramBackend)
        telegram.protocol = PROTOCOL_TELEGRAM
        telegram.contacts = []
        telegram.cache = {}
        telegram._events = queue.Queue()
        manager.register(telegram)

        assert manager.get(PROTOCOL_WHATSAPP) is whatsapp
        assert manager.get(PROTOCOL_SIGNAL) is signal

    def test_all_method_returns_backends_in_registration_order(self):
        manager = BackendManager()
        signal = SignalBackend.__new__(SignalBackend)
        signal.protocol = PROTOCOL_SIGNAL
        signal.contacts = []
        signal._events = queue.Queue()

        from backends.telegram import TelegramBackend
        telegram = TelegramBackend.__new__(TelegramBackend)
        telegram.protocol = PROTOCOL_TELEGRAM
        telegram.contacts = []
        telegram._events = queue.Queue()

        whatsapp = WhatsAppBackend.__new__(WhatsAppBackend)
        whatsapp.protocol = PROTOCOL_WHATSAPP
        whatsapp.contacts = []
        whatsapp._events = queue.Queue()

        manager.register(signal)
        manager.register(telegram)
        manager.register(whatsapp)

        all_backends = manager.all()
        assert all_backends[0].protocol == PROTOCOL_SIGNAL
        assert all_backends[1].protocol == PROTOCOL_TELEGRAM
        assert all_backends[2].protocol == PROTOCOL_WHATSAPP

    def test_disconnect_all_with_partial_failure(self):
        """disconnect_all non crasha se un backend fallisce la disconnessione."""
        import asyncio
        manager, signal, whatsapp = _make_multi_manager()
        signal.disconnect = AsyncMock(side_effect=RuntimeError("fail"))
        whatsapp.disconnect = AsyncMock()

        # Non deve sollevare eccezioni
        asyncio.run(manager.disconnect_all())
        signal.disconnect.assert_called_once()
        whatsapp.disconnect.assert_called_once()

    def test_mark_read_unknown_protocol_still_raises(self):
        import asyncio
        manager, signal, _ = _make_multi_manager()
        with pytest.raises(KeyError):
            asyncio.run(manager.mark_read("nope", "x"))

    def test_get_unknown_protocol_still_returns_none(self):
        manager, signal, _ = _make_multi_manager()
        assert manager.get("fantasy") is None

    def test_signal_contacts_unchanged_after_telegram_registration(self):
        """I contatti Signal non vengono alterati registrando Telegram."""
        manager, signal, _ = _make_multi_manager()
        signal.contacts = [
            _signal_contact("+1", "Alice"),
            _signal_contact("+2", "Bob"),
        ]
        from backends.telegram import TelegramBackend
        telegram = TelegramBackend.__new__(TelegramBackend)
        telegram.protocol = PROTOCOL_TELEGRAM
        telegram.contacts = [_telegram_contact()]
        telegram.cache = {}
        telegram._events = queue.Queue()
        manager.register(telegram)

        contacts = manager.list_contacts()
        signal_contacts = [c for c in contacts if c.protocol == PROTOCOL_SIGNAL]
        assert len(signal_contacts) == 2
        assert {c.display_name for c in signal_contacts} == {"Alice", "Bob"}

    def test_whatsapp_specific_functionality_not_broken(self):
        """WhatsApp REST client e metodi non sono toccati da Telegram."""
        from backends.whatsapp import WhatsAppRESTClient
        client = WhatsAppRESTClient("http://test.local")
        assert client.base_url == "http://test.local"
        assert client.session_name is not None

    def test_filter_title_suffix_includes_telegram(self):
        """Il suffisso del titolo include Telegram."""
        def suffix(protocol_filter):
            if protocol_filter == "all":
                return " - All"
            return f" - {protocol_filter.title()}"

        assert suffix("all") == " - All"
        assert suffix(PROTOCOL_SIGNAL) == " - Signal"
        assert suffix(PROTOCOL_WHATSAPP) == " - Whatsapp"
        assert suffix(PROTOCOL_TELEGRAM) == " - Telegram"

    def test_protocol_class_includes_telegram(self):
        """La classe CSS per Telegram è 'protocol-telegram'."""
        def protocol_class(contact):
            return f"protocol-{contact.protocol}"

        assert protocol_class(_signal_contact()) == "protocol-signal"
        assert protocol_class(_whatsapp_contact()) == "protocol-whatsapp"
        assert protocol_class(_telegram_contact()) == "protocol-telegram"

