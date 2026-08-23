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
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backends import BackendManager, SignalBackend
from backends.base import ChatBackend
from models import (
    PROTOCOL_SIGNAL,
    ChatContact,
    contact_cache_key,
)

# ─── ChatBackend ABC ─────────────────────────────────────────────────────────


class _MinimalBackend(ChatBackend):
    """Concrete ChatBackend implementing every abstract method trivially."""

    protocol = "test"

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
            async def list_contacts(self):
                return []

            async def send_message(self, *a, **k):
                return ""

            async def mark_read(self, *a): ...
            async def receive(self): ...
            def get_attachment_path(self, *a):
                return None

        with pytest.raises(ValueError):
            manager.register(_NoProtocol())

    def test_default_get_attachment_path_none(self):
        """Default get_attachment_path returns None."""
        assert _MinimalBackend().get_attachment_path("x") is None

    def test_default_needs_pairing_false(self):
        """Default needs_pairing is False."""
        assert _MinimalBackend().needs_pairing is False

    def test_default_get_pairing_qr_none(self):
        """Default get_pairing_qr returns None."""
        assert asyncio.run(_MinimalBackend().get_pairing_qr()) is None

    @pytest.mark.parametrize(
        "method_name,args",
        [
            ("connect", ()),
            ("disconnect", ()),
            ("list_contacts", ()),
            ("send_message", ("+391234567890", "ciao")),
            ("mark_read", ("+391234567890",)),
        ],
    )
    def test_abstract_method_raises_not_implemented(self, method_name, args):
        """Calling an abstract method on the base class raises NotImplementedError."""
        inst = _MinimalBackend()
        coro = getattr(ChatBackend, method_name)(inst, *args)
        with pytest.raises(NotImplementedError):
            asyncio.run(coro)

    def test_abstract_receive_raises_not_implemented(self):
        """The abstract async-generator receive raises NotImplementedError on iterate."""
        agen = ChatBackend.receive(_MinimalBackend())
        with pytest.raises(NotImplementedError):
            asyncio.run(agen.__anext__())


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
        from unittest.mock import patch

        import backend as backend_mod

        db_file = tmp_path / "messages.db"
        with (
            patch.object(backend_mod, "DB_FILE", db_file),
            patch.object(backend_mod, "CACHE_DIR", tmp_path),
        ):
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

    def test_parse_contacts_from_output_name_with_spaces(self):
        """Nomi con spazi (es. 'Mario Rossi') non vengono troncati."""
        backend = SignalBackend()
        output = (
            "Number:+391234567890 Name:Mario Rossi ACI:uuid-123\n"
            "Number:+391111111111 Name:Anna Maria Bianchi\n"
        )
        contacts = backend._parse_contacts_from_output(output)
        assert len(contacts) == 2
        assert contacts[0].display_name == "Mario Rossi"
        assert contacts[1].display_name == "Anna Maria Bianchi"
        # Second contact has no ACI → aci should be empty
        assert contacts[1].extras.get("aci", "") == ""

    def test_set_contacts_recovers_last_message_ts_from_cache(self):
        """_set_contacts calcola last_message_ts dal MAX timestamp della cache."""
        backend = SignalBackend()
        backend.cache = {
            "+391234567890": [
                {"timestamp": 1111},
                {"timestamp": 7777},
            ],
            "+391111111111": [],  # nessun messaggio -> 0
        }
        contacts = [
            ChatContact(
                id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
            ),
            ChatContact(
                id="+391111111111", display_name="Luigi", protocol=PROTOCOL_SIGNAL
            ),
        ]
        backend._set_contacts(contacts)
        assert contacts[0].last_message_ts == 7777
        assert contacts[1].last_message_ts == 0

    def test_set_contacts_filters_status_broadcast(self):
        """_set_contacts filtra il contatto di sistema 'status@broadcast'."""
        backend = SignalBackend()
        contacts = [
            ChatContact(
                id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
            ),
            ChatContact(
                id="status@broadcast",
                display_name="status@broadcast",
                protocol=PROTOCOL_SIGNAL,
            ),
            ChatContact(
                id="+391111111111", display_name="Luigi", protocol=PROTOCOL_SIGNAL
            ),
        ]
        backend._set_contacts(contacts)
        assert len(backend.contacts) == 2
        assert backend.contacts[0].id == "+391234567890"
        assert backend.contacts[1].id == "+391111111111"

    def test_envelope_to_event_message(self):
        """Un envelope di messaggio produce un ChatEvent type='message'."""
        backend = SignalBackend()
        contact = ChatContact(
            id="+391234567890",
            display_name="Mario",
            protocol=PROTOCOL_SIGNAL,
            extras={"aci": "uuid-123"},
        )
        backend._set_contacts([contact])
        envelope = {
            "source": "+391234567890",
            "sourceNumber": "+391234567890",
            "sourceName": "Mario",
            "timestamp": 2000,
            "dataMessage": {"message": "Ciao!", "timestamp": 2000},
        }
        events = backend.envelope_to_event(envelope)
        assert len(events) == 1
        event = events[0]
        assert event.type == "message"
        assert event.protocol == PROTOCOL_SIGNAL
        assert event.contact_id == "+391234567890"
        assert event.payload["text"] == "Ciao!"
        assert event.payload["timestamp"] == 2000

    def test_envelope_to_event_unknown_contact(self):
        """Un envelope che non identifica un contatto → lista vuota."""
        backend = SignalBackend()  # no contacts
        envelope = {
            "sourceNumber": "+39",
            "dataMessage": {"message": "x"},
        }
        assert backend.envelope_to_event(envelope) == []

    def test_envelope_to_event_sent_unknown_dest_returns_none(self):
        """Un envelope sentMessage con destinatario sconosciuto → lista vuota.

        NON deve cadere nella ricerca per source perché il source di un
        sentMessage è l'utente locale, non un contatto reale.
        """
        backend = SignalBackend()
        contact = ChatContact(
            id="+391234567890",
            display_name="Mario",
            protocol=PROTOCOL_SIGNAL,
            extras={"aci": "uuid-123"},
        )
        backend._set_contacts([contact])
        # Envelope con syncMessage.sentMessage dove NESSUN contatto matcha
        # il destination, ma sourceNumber MATCHA un contatto (Mario).
        # Non deve restituire Mario perché è il source, non il destinatario.
        envelope = {
            "source": "+391234567890",
            "sourceNumber": "+391234567890",
            "syncMessage": {
                "sentMessage": {
                    "destination": "+399999999999",  # sconosciuto
                    "destinationNumber": "+399999999999",
                }
            },
        }
        assert backend.envelope_to_event(envelope) == []

    def test_envelope_to_event_typing(self):
        """Un envelope di typing produce un ChatEvent type='typing'."""
        backend = SignalBackend()
        envelope = {
            "sourceNumber": "+391234567890",
            "timestamp": 1000,
            "typingMessage": {"action": "STARTED", "timestamp": 1000},
        }
        events = backend.envelope_to_event(envelope)
        assert len(events) == 1
        event = events[0]
        assert event.type == "typing"
        assert event.payload["action"] == "STARTED"

    def test_envelope_to_event_receipt(self):
        """Un envelope receipt produce un ChatEvent type='receipt'."""
        backend = SignalBackend()
        envelope = {
            "sourceNumber": "+391234567890",
            "receiptMessage": {"isRead": True, "timestamps": [111]},
        }
        events = backend.envelope_to_event(envelope)
        assert len(events) == 1
        event = events[0]
        assert event.type == "receipt"
        assert event.payload["receipt"]["timestamps"] == [111]

    # ─── Multiple attachments (bug #1) ───────────────────────────────────

    def test_single_image_attachment_backward_compat(self):
        """Un envelope con una foto → 1 evento (backward compat)."""
        backend = SignalBackend()
        contact = ChatContact(
            id="+391234567890",
            display_name="Mario",
            protocol=PROTOCOL_SIGNAL,
        )
        backend._set_contacts([contact])
        envelope = {
            "source": "+391234567890",
            "sourceNumber": "+391234567890",
            "sourceName": "Mario",
            "timestamp": 2000,
            "dataMessage": {
                "message": "guarda!",
                "timestamp": 2000,
                "attachments": [
                    {
                        "contentType": "image/jpeg",
                        "filename": "photo.jpg",
                        "id": "att-001",
                    }
                ],
            },
        }
        events = backend.envelope_to_event(envelope)
        assert len(events) == 1
        ev = events[0]
        assert ev.type == "message"
        assert ev.payload["msg_type"] == "image"
        assert ev.payload["attachment_id"] == "att-001"
        assert ev.payload["text"] == "guarda!"

    def test_multiple_image_attachments(self):
        """3 foto → 3 ChatEvent, ognuno msg_type='image'."""
        backend = SignalBackend()
        contact = ChatContact(
            id="+391234567890",
            display_name="Mario",
            protocol=PROTOCOL_SIGNAL,
        )
        backend._set_contacts([contact])
        envelope = {
            "source": "+391234567890",
            "sourceNumber": "+391234567890",
            "sourceName": "Mario",
            "timestamp": 3000,
            "dataMessage": {
                "timestamp": 3000,
                "attachments": [
                    {"contentType": "image/jpeg", "id": "img-1"},
                    {"contentType": "image/png", "id": "img-2"},
                    {"contentType": "image/webp", "id": "img-3"},
                ],
            },
        }
        events = backend.envelope_to_event(envelope)
        assert len(events) == 3
        ids = [ev.payload["attachment_id"] for ev in events]
        assert ids == ["img-1", "img-2", "img-3"]
        for ev in events:
            assert ev.payload["msg_type"] == "image"

    def test_mixed_attachments(self):
        """1 image + 1 video + 1 audio → 3 eventi con tipi corretti."""
        backend = SignalBackend()
        contact = ChatContact(
            id="+391234567890",
            display_name="Mario",
            protocol=PROTOCOL_SIGNAL,
        )
        backend._set_contacts([contact])
        envelope = {
            "source": "+391234567890",
            "sourceNumber": "+391234567890",
            "timestamp": 4000,
            "dataMessage": {
                "timestamp": 4000,
                "attachments": [
                    {"contentType": "image/jpeg", "id": "img"},
                    {"contentType": "video/mp4", "id": "vid"},
                    {"contentType": "audio/aac", "id": "aud"},
                ],
            },
        }
        events = backend.envelope_to_event(envelope)
        assert len(events) == 3
        assert events[0].payload["msg_type"] == "image"
        assert events[1].payload["msg_type"] == "attachment"
        assert events[2].payload["msg_type"] == "attachment"
        assert events[1].payload["attachment_id"] == "vid"
        assert events[2].payload["attachment_id"] == "aud"

    def test_text_with_multiple_attachments_only_first_has_text(self):
        """Testo + 2 foto → primo evento ha il testo, secondo no."""
        backend = SignalBackend()
        contact = ChatContact(
            id="+391234567890",
            display_name="Mario",
            protocol=PROTOCOL_SIGNAL,
        )
        backend._set_contacts([contact])
        envelope = {
            "source": "+391234567890",
            "sourceNumber": "+391234567890",
            "timestamp": 5000,
            "dataMessage": {
                "message": "Guarda queste!",
                "timestamp": 5000,
                "attachments": [
                    {"contentType": "image/jpeg", "id": "img-1"},
                    {"contentType": "image/png", "id": "img-2"},
                ],
            },
        }
        events = backend.envelope_to_event(envelope)
        assert len(events) == 2
        assert events[0].payload["text"] == "Guarda queste!"
        # Second attachment gets attachment_info as text
        assert events[1].payload["text"] != "Guarda queste!"

    def test_no_attachments_pure_text(self):
        """Solo testo, nessun attachment → 1 evento."""
        backend = SignalBackend()
        contact = ChatContact(
            id="+391234567890",
            display_name="Mario",
            protocol=PROTOCOL_SIGNAL,
        )
        backend._set_contacts([contact])
        envelope = {
            "source": "+391234567890",
            "sourceNumber": "+391234567890",
            "timestamp": 6000,
            "dataMessage": {"message": "Ciao!", "timestamp": 6000},
        }
        events = backend.envelope_to_event(envelope)
        assert len(events) == 1
        assert events[0].payload["msg_type"] == "text"
        assert events[0].payload["attachment_id"] is None

    def test_sent_multiple_attachments(self):
        """syncMessage.sentMessage con 2 foto → 2 eventi is_mine=True."""
        backend = SignalBackend()
        contact = ChatContact(
            id="+391234567890",
            display_name="Mario",
            protocol=PROTOCOL_SIGNAL,
        )
        backend._set_contacts([contact])
        envelope = {
            "source": "+391234567890",
            "sourceNumber": "+391234567890",
            "timestamp": 7000,
            "syncMessage": {
                "sentMessage": {
                    "destination": "+391234567890",
                    "timestamp": 7000,
                    "attachments": [
                        {"contentType": "image/jpeg", "id": "sent-1"},
                        {"contentType": "image/png", "id": "sent-2"},
                    ],
                }
            },
        }
        events = backend.envelope_to_event(envelope)
        assert len(events) == 2
        for ev in events:
            assert ev.payload["is_mine"] is True
            assert ev.payload["msg_type"] == "image"
        assert events[0].payload["attachment_id"] == "sent-1"
        assert events[1].payload["attachment_id"] == "sent-2"

    def test_envelope_empty_data_returns_empty_list(self):
        """Envelope senza dataMessage né syncMessage → lista vuota."""
        backend = SignalBackend()
        contact = ChatContact(
            id="+391234567890",
            display_name="Mario",
            protocol=PROTOCOL_SIGNAL,
        )
        backend._set_contacts([contact])
        envelope = {
            "source": "+391234567890",
            "sourceNumber": "+391234567890",
            "timestamp": 8000,
        }
        assert backend.envelope_to_event(envelope) == []


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
            "+391234567890",
            "ciao",
            quote_timestamp=None,
            quote_author=None,
            quote_message=None,
        )

    def test_send_message_forwards_reply_to_message_id(self):
        manager = BackendManager()
        backend = SignalBackend()
        manager.register(backend)

        from unittest.mock import AsyncMock

        backend.send_message = AsyncMock(return_value="1234")

        result = asyncio.run(
            manager.send_message(
                PROTOCOL_SIGNAL,
                "+391234567890",
                "ciao",
                reply_to_message_id="42",
            )
        )

        assert result == "1234"
        backend.send_message.assert_awaited_once_with(
            "+391234567890",
            "ciao",
            quote_timestamp=None,
            quote_author=None,
            quote_message=None,
            reply_to_message_id="42",
        )

    def test_send_message_unknown_protocol_raises(self):
        manager = BackendManager()
        with pytest.raises(KeyError):
            asyncio.run(manager.send_message("nope", "x", "ciao"))

    def test_connect_all(self):
        """connect_all awaits every backend's connect()."""
        from unittest.mock import AsyncMock

        manager = BackendManager()
        backend = SignalBackend()
        manager.register(backend)
        backend.connect = AsyncMock()

        asyncio.run(manager.connect_all())

        backend.connect.assert_awaited_once()

    def test_list_contacts_ignores_backend_without_contacts(self):
        """A backend without a .contacts attribute is skipped without crashing."""

        class _NoContacts(ChatBackend):
            protocol = "test"

            async def connect(self): ...
            async def disconnect(self): ...
            async def list_contacts(self):
                return []

            async def send_message(self, *a, **k):
                return ""

            async def mark_read(self, *a): ...
            async def receive(self): ...

        manager = BackendManager()
        manager.register(_NoContacts())
        assert manager.list_contacts() == []


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
            "text": "Ciao!",
            "is_mine": True,
            "sender": "You",
            "timestamp": 1000,
            "quote_text": None,
            "msg_type": "text",
            "attachment_info": None,
            "attachment_id": None,
        }
        assert backend.ingest_message("+391234567890", data, 1000) is True
        assert backend.ingest_message("+391234567890", data, 1000) is False
        assert len(backend.cache["+391234567890"]) == 1

    def test_distinct_messages_both_kept(self):
        """Messaggi diversi (text o ts diversi) vengono entrambi conservati."""
        backend = SignalBackend()

        def d(text, ts):
            return {
                "text": text,
                "is_mine": True,
                "sender": "You",
                "timestamp": ts,
                "quote_text": None,
                "msg_type": "text",
                "attachment_info": None,
                "attachment_id": None,
            }

        backend.ingest_message("+391234567890", d("a", 1000), 1000)
        backend.ingest_message("+391234567890", d("b", 1001), 1001)
        assert len(backend.cache["+391234567890"]) == 2

    def test_dedup_survives_across_callers(self):
        """Deterministiche: l'ingest ottimistico + sync-envelope non duplicano."""
        backend = SignalBackend()
        data = {
            "text": "Ciao!",
            "is_mine": True,
            "sender": "You",
            "timestamp": 2000,
            "quote_text": None,
            "msg_type": "text",
            "attachment_info": None,
            "attachment_id": None,
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
            {
                "text": "ciao",
                "is_mine": True,
                "sender": "You",
                "timestamp": 5000000,
                "quote_text": None,
                "msg_type": "text",
                "attachment_info": None,
                "attachment_id": None,
            },
            5000000,
        )
        # Echo with a *different* (later) timestamp within the window.
        assert (
            backend.ingest_message(
                "+391234567890",
                {
                    "text": "ciao",
                    "is_mine": True,
                    "sender": "You",
                    "timestamp": 5002000,
                    "quote_text": None,
                    "msg_type": "text",
                    "attachment_info": None,
                    "attachment_id": None,
                },
                5002000,
            )
            is False
        )
        assert len(backend.cache["+391234567890"]) == 1

    def test_full_pipeline_multi_attachment_to_cache_and_db(self, tmp_path):
        """End-to-end: envelope con 6 foto → 6 eventi → ingest → cache + DB.

        Questo test simula l'esatto flusso che avviene quando signal-cli
        consegna un messaggio con N attachment.  Usa un DB temporaneo
        (tmp_path) per non toccare il DB reale.
        """
        from unittest.mock import patch

        import backend as backend_mod

        TEST_CONTACT = "+399999999999"
        db_file = tmp_path / "messages.db"

        with (
            patch.object(backend_mod, "DB_FILE", db_file),
            patch.object(backend_mod, "CACHE_DIR", tmp_path),
        ):
            # Inizializza DB vergine
            backend_mod._init_db()

            backend = SignalBackend()
            contact = ChatContact(
                id=TEST_CONTACT,
                display_name="Test",
                protocol=PROTOCOL_SIGNAL,
            )
            backend._set_contacts([contact])

            # Envelope realistico: 6 foto (come il caso reale)
            envelope = {
                "source": TEST_CONTACT,
                "sourceNumber": TEST_CONTACT,
                "sourceName": "Test",
                "timestamp": 9000000,
                "dataMessage": {
                    "timestamp": 9000000,
                    "message": "Ecco le foto!",
                    "attachments": [
                        {"contentType": "image/jpeg", "id": "photo-1.jpg"},
                        {"contentType": "image/jpeg", "id": "photo-2.jpg"},
                        {"contentType": "image/png", "id": "photo-3.png"},
                        {"contentType": "image/webp", "id": "photo-4.webp"},
                        {"contentType": "image/jpeg", "id": "photo-5.jpg"},
                        {"contentType": "image/jpeg", "id": "photo-6.jpg"},
                    ],
                },
            }

            # Step 1: envelope → eventi
            events = backend.envelope_to_event(envelope)
            assert len(events) == 6, f"Expected 6 events, got {len(events)}"

            # Step 2: ogni evento → ingest_message
            for ev in events:
                assert ev.type == "message"
                added = backend.ingest_message(
                    TEST_CONTACT,
                    ev.payload,
                    ev.payload["timestamp"],
                )
                assert added is True, (
                    f"ingest_message returned False for {ev.payload.get('attachment_id')}"
                )

            # Step 3: verifica backend.cache
            assert TEST_CONTACT in backend.cache
            cached = backend.cache[TEST_CONTACT]
            assert len(cached) == 6, (
                f"Expected 6 in cache, got {len(cached)}: "
                f"{[m.get('attachment_id') for m in cached]}"
            )
            cached_ids = {m["attachment_id"] for m in cached}
            expected_ids = {
                "photo-1.jpg",
                "photo-2.jpg",
                "photo-3.png",
                "photo-4.webp",
                "photo-5.jpg",
                "photo-6.jpg",
            }
            assert cached_ids == expected_ids, (
                f"Attachment ID mismatch: {cached_ids} != {expected_ids}"
            )

            # Step 4: verifica DB
            loaded = backend_mod._load_cache()
            assert TEST_CONTACT in loaded, f"Contact {TEST_CONTACT} not found in DB"
            db_msgs = loaded[TEST_CONTACT]
            assert len(db_msgs) == 6, f"Expected 6 in DB, got {len(db_msgs)}"
            db_ids = {m["attachment_id"] for m in db_msgs}
            assert db_ids == expected_ids, (
                f"DB attachment IDs mismatch: {db_ids} != {expected_ids}"
            )

            # Step 5: verifica che il testo sia solo sul primo messaggio
            texts = [m["text"] for m in cached]
            assert texts[0] == "Ecco le foto!", (
                f"First message should have the text, got {texts[0]!r}"
            )
            for i in range(1, 6):
                assert "Ecco le foto!" not in texts[i], (
                    f"Message {i} should NOT contain the original text"
                )

        # Il tmp_path viene pulito automaticamente da pytest

    def test_outgoing_same_text_far_apart_is_distinct(self):
        """Due invii con lo stesso testo molto distanti NON vengono fusi."""
        backend = SignalBackend()

        def out(ts):
            return {
                "text": "ok",
                "is_mine": True,
                "sender": "You",
                "timestamp": ts,
                "quote_text": None,
                "msg_type": "text",
                "attachment_info": None,
                "attachment_id": None,
            }

        backend.ingest_message("+391234567890", out(10_000), 10_000)
        # Far outside the dedup window → a genuinely different message.
        assert (
            backend.ingest_message("+391234567890", out(1_000_000), 1_000_000) is True
        )
        assert len(backend.cache["+391234567890"]) == 2


class TestSignalQuoteMedia:
    """🖼️ Bug #37 — quote di un media Signal → segnaposto tipizzato (Decisione A)."""

    @staticmethod
    def _backend() -> SignalBackend:
        backend = SignalBackend()
        contact = ChatContact(
            id="+391234567890",
            display_name="Mario",
            protocol=PROTOCOL_SIGNAL,
        )
        backend._set_contacts([contact])
        return backend

    def _envelope(self, quote: dict, sent: bool = False) -> dict:
        if sent:
            return {
                "source": "+391234567890",
                "sourceNumber": "+391234567890",
                "timestamp": 2000,
                "syncMessage": {
                    "sentMessage": {
                        "destination": "+391234567890",
                        "destinationNumber": "+391234567890",
                        "message": "Guarda!",
                        "timestamp": 2000,
                        "quote": quote,
                    }
                },
            }
        return {
            "source": "+391234567890",
            "sourceNumber": "+391234567890",
            "sourceName": "Mario",
            "timestamp": 2000,
            "dataMessage": {
                "message": "Guarda!",
                "timestamp": 2000,
                "quote": quote,
            },
        }

    @pytest.mark.parametrize(
        ("content_type", "expected"),
        [
            ("image/jpeg", "🖼️ Immagine"),
            ("video/mp4", "🎬 Video"),
            ("audio/ogg", "🎵 Audio"),
            ("application/pdf", "📎 File"),
            # Uno sticker quotato arriva come image/webp: degrada a "Immagine".
            ("image/webp", "🖼️ Immagine"),
        ],
    )
    def test_signal_quote_image_placeholder(self, content_type, expected):
        """dataMessage con quote media senza testo → segnaposto tipizzato."""
        backend = self._backend()
        envelope = self._envelope(
            {
                "id": 1000,
                "author": "+391234567890",
                "attachments": [{"contentType": content_type}],
            }
        )
        events = backend.envelope_to_event(envelope)
        assert len(events) == 1
        assert events[0].payload["quote_text"] == expected

    def test_signal_quote_image_with_filename_prepended(self):
        """Il filename dell'allegato quotato viene anteposto al segnaposto."""
        backend = self._backend()
        envelope = self._envelope(
            {
                "id": 1000,
                "author": "+391234567890",
                "attachments": [{"contentType": "image/jpeg", "filename": "photo.jpg"}],
            }
        )
        events = backend.envelope_to_event(envelope)
        assert events[0].payload["quote_text"] == "photo.jpg — 🖼️ Immagine"

    def test_signal_sent_message_quote_media(self):
        """sync sentMessage con quote media → stesso fallback, is_mine=True."""
        backend = self._backend()
        envelope = self._envelope(
            {
                "id": 1000,
                "author": "+391234567890",
                "attachments": [{"contentType": "video/mp4"}],
            },
            sent=True,
        )
        events = backend.envelope_to_event(envelope)
        assert len(events) == 1
        assert events[0].payload["quote_text"] == "🎬 Video"
        assert events[0].payload["is_mine"] is True

    def test_signal_quote_caption_preferred(self):
        """Il testo reale della quote vince sul segnaposto (invariato)."""
        backend = self._backend()
        envelope = self._envelope(
            {
                "id": 1000,
                "author": "+391234567890",
                "text": "Che bella foto!",
                "attachments": [{"contentType": "image/jpeg"}],
            }
        )
        events = backend.envelope_to_event(envelope)
        assert events[0].payload["quote_text"] == "Che bella foto!"

    def test_signal_quote_empty_remains_none(self):
        """Quote vuota / senza allegati → nessun segnaposto (nessuna bolla)."""
        backend = self._backend()
        envelope = self._envelope({"id": 1000, "author": "+391234567890"})
        events = backend.envelope_to_event(envelope)
        assert events[0].payload["quote_text"] is None

    def test_media_message_with_quote_placeholder_not_duplicated(self):
        """Regressione dedup: quote segnaposto su media non causa duplicati."""
        backend = SignalBackend()
        data = {
            "text": "Media: att-1",
            "is_mine": False,
            "sender": "Mario",
            "timestamp": 1000,
            "quote_text": "🖼️ Immagine",
            "msg_type": "image",
            "attachment_info": "photo.jpg",
            "attachment_id": "att-1",
        }
        assert (
            backend.ingest_message("+391234567890", data, 1000, persist=False) is True
        )
        assert (
            backend.ingest_message("+391234567890", data, 1000, persist=False) is False
        )
        assert len(backend.cache["+391234567890"]) == 1
