"""
Tests for the multi-protocol abstraction layer.

Covers:
- ``ChatBackend`` abstract interface (base).
- ``SignalBackend`` contact conversion / envelope-to-event normalization.
- ``BackendManager`` registry and unified operations.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from models import (
    ChatContact,
    ChatEvent,
    PROTOCOL_SIGNAL,
    contact_cache_key,
)
from backends import SignalBackend, BackendManager
from backends.base import ChatBackend


# ─── ChatBackend ABC ─────────────────────────────────────────────────────────

class TestChatBackendABC:
    """🧱 ChatBackend deve essere astratto e con protocollo obbligatorio."""

    def test_cannot_instantiate_base(self):
        """ChatBackend è una ABC: non istanziabile senza implementazione."""
        with pytest.raises(TypeError):
            ChatBackend()  # type: ignore

    def test_protocol_empty_by_default(self):
        """Il protocollo di default è vuoto."""
        assert ChatBackend.protocol == ""

    def test_register_rejects_empty_protocol(self):
        """BackendManager rifiuta backend senza protocollo."""
        manager = BackendManager()

        class _NoProtocol(ChatBackend):
            protocol = ""
            def __init__(self):
                self.contacts = []
            async def connect(self): ...
            async def disconnect(self): ...
            async def list_contacts(self): return []
            async def send_message(self, *a, **k): return ""
            async def mark_read(self, *a): ...
            async def receive(self): ...
            def get_attachment_path(self, *a): return None

        with pytest.raises(ValueError):
            manager.register(_NoProtocol())


# ─── SignalBackend ───────────────────────────────────────────────────────────

class TestSignalBackend:
    """📱 Conversione contatti ed eventi nel backend Signal."""

    def test_protocol_signal(self):
        assert SignalBackend.protocol == PROTOCOL_SIGNAL

    def test_to_chat_contact(self):
        """Contact legacy → ChatContact con protocol='signal'."""
        from backend import Contact
        backend = SignalBackend()
        cc = backend._to_chat_contact(
            Contact(number="+391234567890", name="Mario", aci="uuid-123")
        )
        assert cc.id == "+391234567890"
        assert cc.display_name == "Mario"
        assert cc.protocol == PROTOCOL_SIGNAL
        assert cc.extras["aci"] == "uuid-123"
        assert cc.cache_key == contact_cache_key(PROTOCOL_SIGNAL, "+391234567890")

    def test_mark_read_sync_persists(self, tmp_path):
        """mark_read_sync persiste lo stato letto su SQLite."""
        import backend as backend_mod
        from unittest.mock import patch
        db_file = tmp_path / "messages.db"
        with patch.object(backend_mod, "DB_FILE", db_file), \
             patch.object(backend_mod, "CACHE_DIR", tmp_path):
            backend_mod._add_message_to_cache(
                "+391234567890", "Ciao!", False, "Mario", 1000
            )
            backend = SignalBackend()
            backend.mark_read_sync("+391234567890")
            loaded = backend_mod._load_cache()
        assert loaded["+391234567890"][0]["read"] is True

    def test_parse_contacts_from_output(self):
        """Parsing dell'output di 'signal-cli listContacts'."""
        backend = SignalBackend()
        output = (
            "Number:+391234567890 Name:Mario ACI:uuid-123\n"
            "Number:+391111111111 Name:Luigi ACI:uuid-456\n"
        )
        contacts = backend._parse_contacts_from_output(output)
        assert len(contacts) == 2
        assert contacts[0].id == "+391234567890"
        assert contacts[0].extras["aci"] == "uuid-123"

    def test_envelope_to_event_message(self):
        """Un envelope di messaggio produce un ChatEvent type='message'."""
        backend = SignalBackend()
        contact = ChatContact(
            id="+391234567890", display_name="Mario",
            protocol=PROTOCOL_SIGNAL, extras={"aci": "uuid-123"},
        )
        backend._set_contacts([contact])
        envelope = {
            "source": "+391234567890",
            "sourceNumber": "+391234567890",
            "sourceName": "Mario",
            "timestamp": 2000,
            "dataMessage": {"message": "Ciao!", "timestamp": 2000},
        }
        event = backend.envelope_to_event(envelope)
        assert event is not None
        assert event.type == "message"
        assert event.protocol == PROTOCOL_SIGNAL
        assert event.contact_id == "+391234567890"
        assert event.payload["text"] == "Ciao!"
        assert event.payload["timestamp"] == 2000

    def test_envelope_to_event_unknown_contact(self):
        """Un envelope che non identifica un contatto → None."""
        backend = SignalBackend()  # no contacts
        envelope = {
            "sourceNumber": "+39",
            "dataMessage": {"message": "x"},
        }
        assert backend.envelope_to_event(envelope) is None

    def test_envelope_to_event_typing(self):
        """Un envelope di typing produce un ChatEvent type='typing'."""
        backend = SignalBackend()
        envelope = {
            "sourceNumber": "+391234567890",
            "timestamp": 1000,
            "typingMessage": {"action": "STARTED", "timestamp": 1000},
        }
        event = backend.envelope_to_event(envelope)
        assert event is not None
        assert event.type == "typing"
        assert event.payload["action"] == "STARTED"

    def test_envelope_to_event_receipt(self):
        """Un envelope receipt produce un ChatEvent type='receipt'."""
        backend = SignalBackend()
        envelope = {
            "sourceNumber": "+391234567890",
            "receiptMessage": {"isRead": True, "timestamps": [111]},
        }
        event = backend.envelope_to_event(envelope)
        assert event is not None
        assert event.type == "receipt"
        assert event.payload["receipt"]["timestamps"] == [111]

# ─── BackendManager ──────────────────────────────────────────────────────────

class TestBackendManager:
    """🗂️ Registro e operazioni unificate del manager."""

    def test_register_and_get(self):
        manager = BackendManager()
        backend = SignalBackend()
        manager.register(backend)
        assert manager.get(PROTOCOL_SIGNAL) is backend
        assert manager.protocols() == [PROTOCOL_SIGNAL]

    def test_get_missing_returns_none(self):
        manager = BackendManager()
        assert manager.get("whatsapp") is None

    def test_list_contacts_merges(self):
        """list_contacts unisce le liste di tutti i backend registrati."""
        manager = BackendManager()
        backend = SignalBackend()
        contact = ChatContact(
            id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        backend.contacts = [contact]
        manager.register(backend)
        assert manager.list_contacts() == [contact]

    def test_send_message_routes_to_backend(self):
        """send_message inoltra al backend del protocollo corretto."""
        manager = BackendManager()
        backend = SignalBackend()
        manager.register(backend)

        from unittest.mock import AsyncMock
        backend.send_message = AsyncMock(return_value="1234")

        result = asyncio.run(
            manager.send_message(PROTOCOL_SIGNAL, "+391234567890", "ciao")
        )
        assert result == "1234"
        backend.send_message.assert_called_once_with(
            "+391234567890", "ciao",
            quote_timestamp=None, quote_author=None, quote_message=None,
        )

    def test_send_message_unknown_protocol_raises(self):
        manager = BackendManager()
        with pytest.raises(KeyError):
            asyncio.run(manager.send_message("nope", "x", "ciao"))



# ─── Send path regression (message actually sent) ─────────────────────────────

class TestSendMsgSync:
    """📤 send_message_sync esegue davvero l'invio (regression P1)."""

    def _backend(self):
        backend = SignalBackend()
        backend._use_daemon = True
        return backend

    def test_send_message_sync_calls_rpc(self):
        """send_message_sync invoca SignalRPCClient.send_message e ritorna ts."""
        backend = SignalBackend()
        backend._use_daemon = True
        with patch.object(backend._rpc, "send_message") as mock_send:
            mock_send.return_value = {"result": {}}
            ts = backend.send_message_sync("+391234567890", "Ciao!")
        mock_send.assert_called_once()
        args = mock_send.call_args[0]
        assert args[0] == "Ciao!"
        assert args[1] == "+391234567890"
        assert isinstance(ts, int) and ts > 0

    def test_send_message_sync_raises_on_rpc_error(self):
        """Un errore RPC fa sollevare RuntimeError (non viene ignorato)."""
        backend = SignalBackend()
        backend._use_daemon = True
        with patch.object(backend._rpc, "send_message") as mock_send:
            mock_send.return_value = {"error": "boom"}
            with pytest.raises(RuntimeError):
                backend.send_message_sync("+391234567890", "x")

    def test_send_message_sync_fallback_subprocess(self):
        """Senza daemon, invia via subprocess."""
        from backend import _send_subprocess
        backend = SignalBackend()
        backend._use_daemon = False
        with patch("backends.signal._send_subprocess") as mock_sub:
            backend.send_message_sync("+391234567890", "Ciao!")
        mock_sub.assert_called_once()


# ─── Ingest dedup (no doubled messages) ───────────────────────────────────────

class TestIngestDedup:
    """📚 ingest_message non duplica messaggi con la stessa identità."""

    def test_ingest_identical_message_not_added_twice(self):
        """La stessa identità (contact, ts, is_mine, text) non viene raddoppiata.

        `ingest_message` ritorna True solo al primo ingest e False per i
        duplicati — così il chiamante sa se deve riflettere il messaggio nel
        proprio cache UI (fix del "messaggio doppio rientrando in chat").
        """
        backend = SignalBackend()
        data = {
            "text": "Ciao!", "is_mine": True, "sender": "You",
            "timestamp": 1000, "quote_text": None, "msg_type": "text",
            "attachment_info": None, "attachment_id": None,
        }
        assert backend.ingest_message("+391234567890", data, 1000) is True
        assert backend.ingest_message("+391234567890", data, 1000) is False
        assert len(backend.cache["+391234567890"]) == 1

    def test_distinct_messages_both_kept(self):
        """Messaggi diversi (text o ts diversi) vengono entrambi conservati."""
        backend = SignalBackend()
        def d(text, ts):
            return {"text": text, "is_mine": True, "sender": "You", "timestamp": ts,
                    "quote_text": None, "msg_type": "text",
                    "attachment_info": None, "attachment_id": None}
        backend.ingest_message("+391234567890", d("a", 1000), 1000)
        backend.ingest_message("+391234567890", d("b", 1001), 1001)
        assert len(backend.cache["+391234567890"]) == 2

    def test_dedup_survives_across_callers(self):
        """Deterministiche: l'ingest ottimistico + sync-envelope non duplicano."""
        backend = SignalBackend()
        data = {
            "text": "Ciao!", "is_mine": True, "sender": "You",
            "timestamp": 2000, "quote_text": None, "msg_type": "text",
            "attachment_info": None, "attachment_id": None,
        }
        backend.ingest_message("+391234567890", data, 2000)
        # Same identity arrives again (e.g. sync sent-envelope).
        backend.ingest_message("+391234567890", data, 2000)
        # Incoming counterpart (same text, different is_mine) is NOT a duplicate.
        backend.ingest_message("+391234567890", dict(data, is_mine=False), 2000)
        assert len(backend.cache["+391234567890"]) == 2  # sent + received



    def test_outgoing_echo_with_different_ts_not_duplicated(self):
        """L'echo di un messaggio inviato con timestamp diverso NON raddoppia.

        Lo sync sent-envelope può riportare un timestamp diverso da quello
        ottimistico; entro la finestra di dedup va trattato come lo stesso
        messaggio (fix "sent messages doppi rientrando in chat").
        """
        backend = SignalBackend()
        backend.ingest_message(
            "+391234567890",
            {"text": "ciao", "is_mine": True, "sender": "You", "timestamp": 5000000,
             "quote_text": None, "msg_type": "text",
             "attachment_info": None, "attachment_id": None},
            5000000,
        )
        # Echo with a *different* (later) timestamp within the window.
        assert backend.ingest_message(
            "+391234567890",
            {"text": "ciao", "is_mine": True, "sender": "You", "timestamp": 5002000,
             "quote_text": None, "msg_type": "text",
             "attachment_info": None, "attachment_id": None},
            5002000,
        ) is False
        assert len(backend.cache["+391234567890"]) == 1

    def test_outgoing_same_text_far_apart_is_distinct(self):
        """Due invii con lo stesso testo molto distanti NON vengono fusi."""
        backend = SignalBackend()
        def out(ts):
            return {"text": "ok", "is_mine": True, "sender": "You", "timestamp": ts,
                    "quote_text": None, "msg_type": "text",
                    "attachment_info": None, "attachment_id": None}
        backend.ingest_message("+391234567890", out(10_000), 10_000)
        # Far outside the dedup window → a genuinely different message.
        assert backend.ingest_message("+391234567890", out(1_000_000), 1_000_000) is True
        assert len(backend.cache["+391234567890"]) == 2

