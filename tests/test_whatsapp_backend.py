"""
Regression tests for the WhatsApp backend (backends/whatsapp.py) and the
optional configuration gating (backends/config.py).

REST endpoints are mocked via ``urllib.request.urlopen`` (same style as the
Signal RPC tests); the WebSocket stream is exercised through the internal
event-queue + ``poll_once`` surface.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backends.whatsapp import (
    WhatsAppBackend,
    WhatsAppRESTClient,
    _event_from_ack,
    _event_from_message,
    _event_from_raw,
    _event_from_receipt,
    _event_from_typing,
)
from models import PROTOCOL_WHATSAPP, contact_cache_key


def _msg(raw, contacts=None):
    """Wrapper: returns first event from _event_from_message (now returns list)."""
    events = _event_from_message(raw, contacts)
    return events[0] if events else None


def _raw(raw, contacts=None):
    """Wrapper: returns first event from _event_from_raw (now returns list)."""
    events = _event_from_raw(raw, contacts)
    return events[0] if events else None


def _json_response(payload):
    """Return a mock urlopen context manager that yields a JSON body."""
    data = json.dumps(payload).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = data
    mock_urlopen = MagicMock()
    mock_urlopen.return_value.__enter__.return_value = mock_resp
    return mock_urlopen


def _make_backend(
    api_url: str = "http://api.test", media_dir: str = ""
) -> WhatsAppBackend:
    return WhatsAppBackend(api_url=api_url, media_dir=media_dir)


# ─── WhatsAppRESTClient ───────────────────────────────────────────────────────


class TestWhatsAppRESTClient:
    """🔌 Client REST verso l'API Baileys."""

    def test_protocol(self):
        assert WhatsAppBackend.protocol == PROTOCOL_WHATSAPP

    def test_list_contacts(self):
        client = WhatsAppRESTClient("http://api.test")
        seen = []

        def fake_urlopen(req, timeout=30):
            seen.append((req.method, req.full_url))
            resp = MagicMock()
            resp.read.return_value = json.dumps(
                [
                    {"id": "wa:39123@s.whatsapp.net", "name": "Mario"},
                    {"id": "wa:45678@s.whatsapp.net", "name": "Luigi"},
                ]
            ).encode("utf-8")
            resp.status = 200
            ctx = MagicMock()
            ctx.__enter__.return_value = resp
            return ctx

        with patch("urllib.request.urlopen", fake_urlopen):
            contacts = client.list_contacts()

        assert len(contacts) == 2
        assert contacts[0]["id"] == "wa:39123@s.whatsapp.net"
        # list_contacts ora usa solo /api/{session}/chats (refactoring opt/wa-link-profile).
        assert any("/api/default/chats" in u for m, u in seen)

    def test_list_contacts_nested_data(self):
        client = WhatsAppRESTClient("http://api.test")
        # list_contacts now uses /api/{session}/chats which returns a flat list
        # (opt/wa-link-profile refactoring removed /api/contacts fallback).
        with patch(
            "urllib.request.urlopen",
            _json_response([{"id": "wa:x@s.whatsapp.net", "name": "A"}]),
        ):
            contacts = client.list_contacts()
        assert len(contacts) == 1

    def test_list_contacts_uses_chats_directly(self):
        """list_contacts chiama direttamente /api/{session}/chats (refactoring opt/wa-link-profile)."""
        client = WhatsAppRESTClient("http://api.test")
        seen = []

        def fake_urlopen(req, timeout=30):
            seen.append((req.method, req.full_url))
            resp = MagicMock()
            resp.status = 200
            # chats endpoint: lista di chat/contatti
            resp.read.return_value = json.dumps(
                [
                    {
                        "id": {"_serialized": "3112@c.us"},
                        "name": "Anna",
                        "isGroup": False,
                    },
                    {
                        "id": {"_serialized": "139153@lid"},
                        "pushName": "Bob",
                        "isGroup": False,
                    },
                ]
            ).encode("utf-8")
            ctx = MagicMock()
            ctx.__enter__.return_value = resp
            return ctx

        with patch("urllib.request.urlopen", fake_urlopen):
            contacts = client.list_contacts()

        assert any("/api/default/chats" in u for m, u in seen)
        assert len(contacts) == 2
        assert contacts[0]["id"] == "3112@c.us"
        assert contacts[1]["name"] == "Bob"

    def test_list_contacts_chats_parses_last_ts(self):
        """Il fallback /chats estrae t / last_message.timestamp come last_ts (ms)."""
        client = WhatsAppRESTClient("http://api.test")

        def fake_urlopen(req, timeout=30):
            resp = MagicMock()
            resp.status = 200
            if req.full_url.endswith("/api/contacts?session=default"):
                # contatti endpoint rotto -> scatena il fallback su /chats
                resp.read.return_value = b'{"statusCode":500}'
            else:
                # t in secondi (epoch) -> converto in ms; last_message.timestamp in ms.
                resp.read.return_value = json.dumps(
                    [
                        {
                            "id": {"_serialized": "3112@c.us"},
                            "name": "Anna",
                            "t": 1700000000,
                        },
                        {
                            "id": {"_serialized": "139153@lid"},
                            "pushName": "Bob",
                            "last_message": {"timestamp": 1750000000000},
                        },
                        {
                            "id": {"_serialized": "399@lid"},
                            "name": "Carla",
                            "isGroup": False,
                        },  # nessun timestamp -> last_ts 0
                    ]
                ).encode("utf-8")
            ctx = MagicMock()
            ctx.__enter__.return_value = resp
            return ctx

        with patch("urllib.request.urlopen", fake_urlopen):
            contacts = client.list_contacts()

        assert len(contacts) == 3
        assert contacts[0]["last_ts"] == 1700000000000  # t in secondi * 1000
        assert contacts[1]["last_ts"] == 1750000000000  # last_message.timestamp (ms)
        assert contacts[2]["last_ts"] == 0  # assente -> 0

    def test_get_pairing_qr_text_fallback(self):
        """QA testuale (WAHA vecchio): get_session_qr cade sul ramo JSON."""
        client = WhatsAppRESTClient("http://api.test")
        # _request_raw (PNG binario) non disponibile -> fallisce sul path JSON.
        with (
            patch.object(client, "_request_raw", return_value=None),
            patch(
                "urllib.request.urlopen",
                _json_response({"status": "pending", "qr": "2@ABC123"}),
            ),
        ):
            qr = client.get_pairing_qr()
        assert qr == "2@ABC123"

    def test_get_pairing_qr_binary_png(self):
        """WAHA corrente restituisce il QR come PNG binario (bytes)."""
        client = WhatsAppRESTClient("http://api.test")
        png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rfake-image"
        with patch.object(client, "_request_raw", return_value=png):
            qr = client.get_pairing_qr()
        assert qr == png

    def test_get_pairing_qr_starts_session_when_stopped(self):
        """Se non c'è QR, get_pairing_qr avvia la sessione e riprova."""
        client = WhatsAppRESTClient("http://api.test")
        calls = []
        with (
            patch.object(client, "_request_raw", side_effect=[None, b"\x89PNG-img"]),
            patch.object(
                client, "start_session", side_effect=lambda: calls.append("start")
            ),
            patch("urllib.request.urlopen", _json_response({})),
        ):
            qr = client.get_pairing_qr()
        assert calls == ["start"]
        assert qr == b"\x89PNG-img"

    def test_get_fresh_pairing_qr_resets_then_starts(self):
        """get_fresh_pairing_qr abbatte la sessione (QR fresco) prima di ripartire."""
        client = WhatsAppRESTClient("http://api.test")
        calls = []
        with (
            patch.object(
                client,
                "reset_session",
                side_effect=lambda logout=True: calls.append("reset"),
            ),
            patch.object(
                client, "start_session", side_effect=lambda: calls.append("start")
            ),
            patch.object(client, "get_session_qr", return_value=b"\x89PNG-fresh"),
        ):
            qr = client.get_fresh_pairing_qr(reset=True)
        assert calls == ["reset", "start"]
        assert qr == b"\x89PNG-fresh"

    def test_get_fresh_pairing_qr_no_reset(self):
        """Con reset=False get_fresh_pairing_qr riparte senza abbatte la sessione."""
        client = WhatsAppRESTClient("http://api.test")
        calls = []
        with (
            patch.object(
                client,
                "reset_session",
                side_effect=lambda logout=True: calls.append("reset"),
            ),
            patch.object(
                client, "start_session", side_effect=lambda: calls.append("start")
            ),
            patch.object(client, "get_session_qr", return_value="2@fresh"),
        ):
            qr = client.get_fresh_pairing_qr(reset=False)
        assert calls == ["start"]
        assert qr == "2@fresh"

    def test_reset_session_uses_logout_then_returns(self):
        """reset_session invoca /api/sessions/logout e ne ritorna il risultato."""

        client = WhatsAppRESTClient("http://api.test")
        seen = []

        def fake_urlopen(req, timeout=30):
            seen.append((req.method, req.full_url))
            resp = MagicMock()
            resp.read.return_value = b'{"status": "not_authenticated"}'
            resp.status = 200
            ctx = MagicMock()
            ctx.__enter__.return_value = resp
            return ctx

        with patch("urllib.request.urlopen", fake_urlopen):
            result = client.reset_session(logout=True)

        assert result == {"status": "not_authenticated"}
        assert any(m == "POST" and "/api/sessions/logout" in u for m, u in seen)

    def test_send_message(self):
        client = WhatsAppRESTClient("http://api.test")
        with patch("urllib.request.urlopen", _json_response({"id": "msg-1"})):
            result = client.send_message("wa:x@s.whatsapp.net", "Ciao!")
        assert result == {"id": "msg-1"}

    def test_send_message_uses_waha_reply_to(self):
        client = WhatsAppRESTClient("http://api.test")
        captured = {}

        def fake_urlopen(req, timeout=30):
            captured["payload"] = json.loads(req.data)
            resp = MagicMock()
            resp.read.return_value = b'{"id": "msg-1"}'
            ctx = MagicMock()
            ctx.__enter__.return_value = resp
            return ctx

        with patch("urllib.request.urlopen", fake_urlopen):
            client.send_message(
                "wa:x@s.whatsapp.net",
                "Ciao!",
                quote_message="legacy quote text",
                reply_to_message_id="message-id-1",
            )

        assert captured["payload"]["reply_to"] == "message-id-1"
        assert "quotedMessage" not in captured["payload"]
        assert "quote_message" not in captured["payload"]

    def test_send_message_error_returns_none(self):
        client = WhatsAppRESTClient("http://api.test")

        def boom(*a, **k):
            import urllib.error

            raise urllib.error.URLError("refused")

        with patch("urllib.request.urlopen", boom):
            result = client.send_message("wa:x@s.whatsapp.net", "Ciao!")
        assert result is None

    def test_request_sends_api_key_header(self):
        """Quando una key è configurata, _request la invia come X-Api-Key."""
        import backends.whatsapp as wh

        captured = {}

        def fake_urlopen(req, timeout=30):
            captured["headers"] = dict(req.headers)
            resp = MagicMock()
            resp.read.return_value = b"{}"
            ctx = MagicMock()
            ctx.__enter__.return_value = resp
            return ctx

        # whatsapp.py importa get_whatsapp_api_key nel proprio namespace.
        with (
            patch.object(wh, "get_whatsapp_api_key", return_value="secret-key-123"),
            patch("urllib.request.urlopen", fake_urlopen),
        ):
            client = WhatsAppRESTClient("http://api.test")
            client.get_session_qr()

        # urllib normalizza i nomi header in lowercase, quindi controlliamo in
        # modo case-insensitive (X-Api-Key -> X-api-key).
        got = {k: v for k, v in captured["headers"].items() if k.lower() == "x-api-key"}
        assert got and list(got.values()) == ["secret-key-123"]

    def test_request_skips_api_key_header_when_unset(self):
        """Senza key, _request NON invia l'header X-Api-Key."""
        import backends.whatsapp as wh

        captured = {}

        def fake_urlopen(req, timeout=30):
            captured["headers"] = dict(req.headers)
            resp = MagicMock()
            resp.read.return_value = b"{}"
            ctx = MagicMock()
            ctx.__enter__.return_value = resp
            return ctx

        with (
            patch.object(wh, "get_whatsapp_api_key", return_value=""),
            patch("urllib.request.urlopen", fake_urlopen),
        ):
            client = WhatsAppRESTClient("http://api.test")
            client.get_session_qr()

        assert not any(k.lower() == "x-api-key" for k in captured["headers"])

    def test_http_401_returns_none_and_sets_last_status(self):
        """Un 401 (key assente/errata) torna None e registra last_status=401."""

        def fake_urlopen(*a, **k):
            import urllib.error

            raise urllib.error.HTTPError(
                "http://api.test", 401, "Unauthorized", {}, None
            )

        with patch("urllib.request.urlopen", fake_urlopen):
            client = WhatsAppRESTClient("http://api.test")
            result = client.get_session_qr()

        assert result is None
        assert client.last_status == 401


# ─── Event normalization ──────────────────────────────────────────────────────


class TestWhatsAppEvents:
    """📨 Normalizzazione dei WebSocket frame in ChatEvent."""

    def test_message_event(self):
        ev = _msg(
            {
                "id": "m1",
                "from": "wa:39123@s.whatsapp.net",
                "timestamp": 1700000000,
                "text": "ciao",
                "pushName": "Mario",
                "fromMe": False,
            }
        )
        assert ev is not None
        assert ev.type == "message"
        assert ev.protocol == PROTOCOL_WHATSAPP
        assert ev.contact_id == "wa:39123@s.whatsapp.net"
        assert ev.payload["text"] == "ciao"
        assert ev.payload["timestamp"] == 1700000000000  # seconds → ms
        assert ev.payload["is_mine"] is False

    def test_status_broadcast_is_ignored(self):
        """Gli status (storie) con JID status@broadcast non sono messaggi di
        chat: devono produrre ZERO eventi (niente ingestione in cache/DB)."""
        events = _event_from_message(
            {
                "id": "false_status@broadcast_ABC",
                "chatId": "status@broadcast",
                "from": "191169011163167@lid",
                "timestamp": 1700000000,
                "body": "una storia",
                "fromMe": False,
            }
        )
        assert events == []

    def test_typing_event(self):
        ev = _event_from_typing(
            {"from": "wa:39123@s.whatsapp.net", "presence": "composing"}
        )
        assert ev is not None
        assert ev.type == "typing"
        assert ev.payload["action"] == "STARTED"

    def test_receipt_event(self):
        ev = _event_from_receipt(
            {
                "from": "wa:39123@s.whatsapp.net",
                "receipt": {"messageIds": ["m1"], "type": "read"},
            }
        )
        assert ev is not None
        assert ev.type == "receipt"
        assert ev.payload["message_ids"] == ["m1"]
        assert ev.payload["is_read"] is True

    # ── message.ack (delivery / read receipts via WAHA push) ──────────

    def test_ack_delivery_event(self):
        """message.ack with status=2 (DEVICE) → receipt with is_read=False."""
        ev = _event_from_ack(
            {
                "event": "message.ack",
                "payload": {
                    "id": "msg_abc",
                    "from": "me@lid",
                    "to": "39123@s.whatsapp.net",
                    "fromMe": True,
                    "status": 2,
                },
            }
        )
        assert ev is not None
        assert ev.type == "receipt"
        assert ev.protocol == PROTOCOL_WHATSAPP
        assert ev.contact_id == "39123@s.whatsapp.net"  # recipient, not sender
        assert ev.payload["message_ids"] == ["msg_abc"]
        assert ev.payload["is_read"] is False

    def test_ack_read_event(self):
        """message.ack with status=3 (READ) → receipt with is_read=True."""
        ev = _event_from_ack(
            {
                "event": "message.ack",
                "payload": {
                    "id": "msg_xyz",
                    "from": "me@lid",
                    "to": "39123@s.whatsapp.net",
                    "fromMe": True,
                    "status": 3,
                },
            }
        )
        assert ev is not None
        assert ev.type == "receipt"
        assert ev.payload["is_read"] is True
        assert ev.contact_id == "39123@s.whatsapp.net"

    def test_ack_server_ack_ignored(self):
        """message.ack with status=1 (SERVER) is ignored."""
        ev = _event_from_ack(
            {
                "event": "message.ack",
                "payload": {
                    "id": "msg_abc",
                    "from": "me@lid",
                    "to": "39123@s.whatsapp.net",
                    "fromMe": True,
                    "status": 1,
                },
            }
        )
        assert ev is None

    def test_ack_not_mine_ignored(self):
        """message.ack from someone else (fromMe=False) is ignored."""
        ev = _event_from_ack(
            {
                "event": "message.ack",
                "payload": {
                    "id": "msg_abc",
                    "from": "39123@s.whatsapp.net",
                    "fromMe": False,
                    "status": 4,
                },
            }
        )
        assert ev is None

    def test_ack_no_chat_id_returns_none(self):
        """message.ack without chatId/from returns None."""
        assert _event_from_ack({"id": "m1", "fromMe": True, "status": 3}) is None

    def test_ack_no_msg_id_returns_none(self):
        """message.ack without id returns None."""
        assert (
            _event_from_ack(
                {
                    "chatId": "39123@s.whatsapp.net",
                    "fromMe": True,
                    "status": 4,
                }
            )
            is None
        )

    def test_ack_dispatch_via_raw(self):
        """_event_from_raw dispatches 'message.ack' to _event_from_ack."""
        ev = _raw(
            {
                "event": "message.ack",
                "payload": {
                    "id": "msg_1",
                    "from": "me@lid",
                    "to": "39123@s.whatsapp.net",
                    "fromMe": True,
                    "status": 3,
                },
            }
        )
        assert ev is not None
        assert ev.type == "receipt"
        assert ev.payload["is_read"] is True
        assert ev.contact_id == "39123@s.whatsapp.net"

    def test_ack_slash_variant_dispatch(self):
        """_event_from_raw dispatches 'message/ack' variant too."""
        ev = _raw(
            {
                "event": "message/ack",
                "payload": {
                    "id": "msg_1",
                    "from": "me@lid",
                    "to": "39123@s.whatsapp.net",
                    "fromMe": True,
                    "status": 2,
                },
            }
        )
        assert ev is not None
        assert ev.type == "receipt"
        assert ev.payload["is_read"] is False
        assert ev.contact_id == "39123@s.whatsapp.net"

    def test_dispatch_by_event_type(self):
        raw = {
            "event": "messages.upsert",
            "from": "wa:1@s.whatsapp.net",
            "text": "hi",
            "timestamp": 1,
        }
        ev = _raw(raw)
        assert ev is not None and ev.type == "message"

    def test_group_message_extracts_sender_from_participant(self):
        """Un messaggio di gruppo (@g.us) estrae il mittente dal campo participant."""
        ev = _msg(
            {
                "id": "g1",
                "from": "123456789@g.us",
                "timestamp": 1700000000,
                "text": "ciao gruppo",
                "fromMe": False,
                "participant": "3912345678@c.us",
                "pushName": "Mario",
            }
        )
        assert ev is not None
        assert ev.contact_id == "123456789@g.us"
        assert ev.payload["is_group"] is True
        assert ev.payload["sender"] == "Mario"  # pushName ha priorità

    def test_group_message_sender_falls_back_to_jid(self):
        """Senza pushName, il mittente del gruppo cade sul JID del participant."""
        ev = _msg(
            {
                "id": "g2",
                "from": "123456789@g.us",
                "timestamp": 1700000000,
                "text": "ciao",
                "fromMe": False,
                "participant": "3912345678@c.us",
            }
        )
        assert ev is not None
        assert ev.payload["is_group"] is True
        assert ev.payload["sender"] == "3912345678@c.us"

    def test_group_message_sender_from_sender_field(self):
        """Il mittente del gruppo può essere nel campo 'sender'."""
        ev = _msg(
            {
                "id": "g3",
                "from": "123456789@g.us",
                "timestamp": 1700000000,
                "text": "ciao",
                "fromMe": False,
                "sender": "3912345678@c.us",
                "notifyName": "Luigi",
            }
        )
        assert ev is not None
        assert ev.payload["is_group"] is True
        assert ev.payload["sender"] == "Luigi"  # notifyName come fallback

    def test_group_message_jid_resolved_to_contact_name(self):
        """Il JID del mittente viene risolto al nome del contatto tramite la rubrica."""
        from models import ChatContact

        contacts = {
            "220988985864200@c.us": ChatContact(
                id="220988985864200@c.us",
                display_name="Mario Rossi",
                protocol=PROTOCOL_WHATSAPP,
            ),
        }
        ev = _msg(
            {
                "id": "g4",
                "from": "123456789@g.us",
                "timestamp": 1700000000,
                "text": "ciao",
                "fromMe": False,
                "participant": "220988985864200@lid",
            },
            contacts,
        )
        assert ev is not None
        assert ev.payload["is_group"] is True
        # Il JID @lid viene risolto al nome del contatto @c.us con lo stesso numero.
        assert ev.payload["sender"] == "Mario Rossi"

    def test_group_message_jid_exact_match(self):
        """Match esatto del JID nella rubrica."""
        from models import ChatContact

        contacts = {
            "220988985864200@lid": ChatContact(
                id="220988985864200@lid",
                display_name="Mario",
                protocol=PROTOCOL_WHATSAPP,
            ),
        }
        ev = _msg(
            {
                "id": "g5",
                "from": "123456789@g.us",
                "timestamp": 1700000000,
                "text": "ciao",
                "fromMe": False,
                "participant": "220988985864200@lid",
            },
            contacts,
        )
        assert ev is not None
        assert ev.payload["sender"] == "Mario"

    def test_group_message_jid_not_in_contacts_keeps_jid(self):
        """Se il JID non è in rubrica, resta il JID come fallback."""
        ev = _msg(
            {
                "id": "g6",
                "from": "123456789@g.us",
                "timestamp": 1700000000,
                "text": "ciao",
                "fromMe": False,
                "participant": "999999999@lid",
            },
            {},
        )
        assert ev is not None
        assert ev.payload["sender"] == "999999999@lid"

    def test_direct_message_not_group(self):
        """Un messaggio diretto (@c.us) non è un gruppo."""
        ev = _msg(
            {
                "id": "d1",
                "from": "3912345678@c.us",
                "timestamp": 1700000000,
                "text": "ciao",
                "fromMe": False,
                "pushName": "Mario",
            }
        )
        assert ev is not None
        assert ev.payload["is_group"] is False
        assert ev.payload["sender"] == "Mario"

    def test_hasMedia_image(self):
        """WAHA image message via hasMedia/media fields."""
        ev = _msg(
            {
                "id": "img1",
                "from": "3912345678@c.us",
                "timestamp": 1700000000,
                "text": "",
                "fromMe": False,
                "pushName": "Mario",
                "hasMedia": True,
                "media": {
                    "mimetype": "image/jpeg",
                    "url": "https://wa.to/img/abc123.jpg",
                    "filename": "photo.jpg",
                    "caption": "Guarda!",
                },
            }
        )
        assert ev is not None
        assert ev.payload["msg_type"] == "image"
        assert ev.payload["attachment_id"] == "https://wa.to/img/abc123.jpg"
        assert ev.payload["attachment_info"] == "Guarda!"  # caption

    def test_hasMedia_caption_in_body(self):
        """WAHA reale: la caption dei media arriva nel campo `body` (non `caption`)."""
        ev = _msg(
            {
                "id": "false_12345@lid_ABC",
                "from": "3912345678@c.us",
                "timestamp": 1700000000,
                "fromMe": False,
                "pushName": "Mario",
                "hasMedia": True,
                "body": "Nice, or?",
                "media": {
                    "mimetype": "image/jpeg",
                    "url": "https://wa.to/img/x.jpg",
                },
            }
        )
        assert ev is not None
        assert ev.payload["msg_type"] == "image"
        assert ev.payload["attachment_info"] == "Nice, or?"

    def test_hasMedia_no_caption_keeps_mime(self):
        """Media senza caption (`body` vuoto) → attachment_info cade sul mime."""
        ev = _msg(
            {
                "id": "img_nocap",
                "from": "3912345678@c.us",
                "timestamp": 1700000000,
                "fromMe": False,
                "hasMedia": True,
                "body": "",
                "media": {
                    "mimetype": "image/jpeg",
                    "url": "https://wa.to/img/x.jpg",
                },
            }
        )
        assert ev is not None
        assert ev.payload["msg_type"] == "image"
        assert ev.payload["attachment_info"] == "image/jpeg"

    def test_hasMedia_video(self):
        """WAHA video message via hasMedia/media fields."""
        ev = _msg(
            {
                "id": "vid1",
                "from": "3912345678@c.us",
                "timestamp": 1700000000,
                "text": "",
                "fromMe": False,
                "pushName": "Mario",
                "hasMedia": True,
                "media": {
                    "mimetype": "video/mp4",
                    "url": "https://wa.to/vid/vid.mp4",
                },
            }
        )
        assert ev is not None
        assert ev.payload["msg_type"] == "attachment"
        assert ev.payload["attachment_id"] == "https://wa.to/vid/vid.mp4"
        assert ev.payload["attachment_info"] == "video/mp4"

    def test_hasMedia_audio(self):
        """WAHA audio message via hasMedia/media fields."""
        ev = _msg(
            {
                "id": "aud1",
                "from": "3912345678@c.us",
                "timestamp": 1700000000,
                "fromMe": False,
                "hasMedia": True,
                "media": {
                    "mimetype": "audio/ogg",
                    "url": "https://wa.to/aud/audio.ogg",
                },
            }
        )
        assert ev is not None
        assert ev.payload["msg_type"] == "attachment"

    def test_media_synthetic_text_uses_url_then_parent_part_identity(self):
        events = _event_from_message(
            {
                "id": "parent-media",
                "from": "3912345678@c.us",
                "timestamp": 1700000000,
                "attachments": [
                    {"url": "https://wa.to/media/one", "filename": "one.jpg"},
                    {"filename": "two.jpg", "mimetype": "image/jpeg"},
                ],
            }
        )

        assert [event.payload["text"] for event in events] == ["", ""]
        assert [event.payload["attachment_info"] for event in events] == [
            "one.jpg",
            "two.jpg",
        ]

    def test_single_media_without_id_or_url_uses_parent_part_identity_in_all_forms(
        self,
    ):
        base = {
            "id": "parent-media",
            "from": "3912345678@c.us",
            "timestamp": 1700000000,
        }
        payloads = [
            {
                **base,
                "attachments": [{"filename": "photo.jpg", "mimetype": "image/jpeg"}],
            },
            {
                **base,
                "message": {
                    "id": "message-container-id",
                    "imageMessage": {
                        "filename": "photo.jpg",
                        "mimetype": "image/jpeg",
                    },
                },
            },
            {
                **base,
                "hasMedia": True,
                "media": {"filename": "photo.jpg", "mimetype": "image/jpeg"},
            },
        ]

        events = [_msg(payload) for payload in payloads]

        assert [event.payload["text"] for event in events] == [""] * len(payloads)
        assert [event.payload["attachment_id"] for event in events] == [None] * len(
            payloads
        )

    def test_hasMedia_no_media_dict(self):
        """hasMedia=true but media is not a dict → still text, no attachment."""
        ev = _msg(
            {
                "id": "bad1",
                "from": "3912345678@c.us",
                "timestamp": 1700000000,
                "text": "hello",
                "fromMe": False,
                "hasMedia": True,
                "media": None,
            }
        )
        assert ev is not None
        assert ev.payload["msg_type"] == "text"
        assert ev.payload["attachment_id"] is None

    def test_hasMedia_no_text_key_still_recognized_by_msg(self):
        """Immagine con hasMedia SENZA la chiave 'text' deve essere riconosciuta.

        Se WAHA non include la chiave 'text' per messaggi immagine (payload
        flat hasMedia/media), _event_from_message deve comunque estrarre
        msg_type=image e attachment_id.
        """
        ev = _msg(
            {
                "id": "img_no_text",
                "from": "3912345678@c.us",
                "timestamp": 1700000000,
                "fromMe": False,
                "pushName": "Mario",
                # NOTA: nessuna chiave 'text'!
                "hasMedia": True,
                "media": {
                    "mimetype": "image/jpeg",
                    "url": "https://wa.to/img/no-text-photo.jpg",
                    "caption": "Senza testo!",
                },
            }
        )
        assert ev is not None
        assert ev.payload["msg_type"] == "image", (
            "hasMedia image senza text key → msg_type deve essere 'image', "
            f"got {ev.payload.get('msg_type')!r}"
        )
        assert ev.payload["attachment_id"] == "https://wa.to/img/no-text-photo.jpg"
        assert ev.payload["attachment_info"] == "Senza testo!"
        assert ev.payload["text"] == ""

    def test_event_from_raw_fallback_recognizes_hasMedia_without_text_key(self):
        """_event_from_raw fallback riconosce hasMedia anche senza key 'text'.

        Il fallback (righe 788-790) controlla 'text' in content, 'body',
        content.get('message'), content.get('attachments').  Un payload
        hasMedia/media PUO' non avere nessuna di queste chiavi.  Il fallback
        deve riconoscere anche hasMedia/media.
        """
        ev = _raw(
            {
                # Simula un evento con nome non-standard (non 'message'):
                "event": "unknown_event_type",
                "payload": {
                    "id": "img_fallback",
                    "from": "3912345678@c.us",
                    "timestamp": 1700000000,
                    "fromMe": False,
                    "hasMedia": True,
                    "media": {
                        "mimetype": "image/jpeg",
                        "url": "https://wa.to/img/fallback.jpg",
                        "caption": "Fallback test",
                    },
                    # NOTA: nessuna chiave 'text', 'body', 'message', o 'attachments'
                },
            }
        )
        # Dovrebbe essere riconosciuto come messaggio immagine, non None.
        assert ev is not None, (
            "_event_from_raw returned None for hasMedia payload without text key"
        )
        assert ev.type == "message"
        assert ev.payload["msg_type"] == "image"
        assert ev.payload["attachment_id"] == "https://wa.to/img/fallback.jpg"

    def test_event_from_raw_fallback_accepts_hasMedia_video_too(self):
        """Fallback con hasMedia video (senza text key) deve funzionare."""
        ev = _raw(
            {
                "event": "strange_event",
                "payload": {
                    "id": "vid_fallback",
                    "from": "3912345678@c.us",
                    "timestamp": 1700000000,
                    "fromMe": False,
                    "hasMedia": True,
                    "media": {
                        "mimetype": "video/mp4",
                        "url": "https://wa.to/vid/fallback.mp4",
                    },
                },
            }
        )
        assert ev is not None
        assert ev.payload["msg_type"] == "attachment"
        assert ev.payload["attachment_id"] == "https://wa.to/vid/fallback.mp4"


# ─── WhatsAppBackend behaviour ────────────────────────────────────────────────


class TestWhatsAppBackend:
    """📱 Comportamento del backend (contatti, invio, pairing, media)."""

    def test_list_contacts_from_rest(self):
        backend = _make_backend()
        with patch.object(
            backend._rest,
            "list_contacts",
            return_value=[
                {"id": "wa:39123@s.whatsapp.net", "name": "Mario"},
            ],
        ):
            backend._load_contacts()
        assert len(backend.contacts) == 1
        assert backend.contacts[0].id == "wa:39123@s.whatsapp.net"
        assert backend.contacts[0].cache_key == contact_cache_key(
            PROTOCOL_WHATSAPP, "wa:39123@s.whatsapp.net"
        )

    def test_load_contacts_sets_phone_for_c_us(self):
        """I contatti @c.us derivano extras["phone"] dalla parte locale del JID."""
        backend = _make_backend()
        with patch.object(
            backend._rest,
            "list_contacts",
            return_value=[{"id": "393331234567@c.us", "name": "Mario"}],
        ):
            backend._load_contacts()
        assert backend.contacts[0].extras["phone"] == "393331234567"
        assert backend.contacts[0].extras["jid"] == "393331234567@c.us"
        assert backend.contacts[0].extras["last_message_ts"] == 0

    def test_load_contacts_sets_phone_for_resolved_lid(self):
        """Un @lid risolto in cache eredita il telefono dalla lid map."""
        backend = _make_backend()
        backend._lid_map = {"139153@lid": {"phone": "393331234567"}}
        with patch.object(
            backend._rest,
            "list_contacts",
            return_value=[{"id": "139153@lid", "name": "Bob"}],
        ):
            backend._load_contacts()
        assert backend.contacts[0].extras["phone"] == "393331234567"
        assert backend.contacts[0].extras["jid"] == "139153@lid"

    def test_load_contacts_no_phone_for_unresolved_lid(self):
        """Un @lid non risolto NON ha extras["phone"] (resta single-member)."""
        backend = _make_backend()
        backend._lid_map = {}
        with patch.object(
            backend._rest,
            "list_contacts",
            return_value=[{"id": "139153@lid", "name": "Bob"}],
        ):
            backend._load_contacts()
        assert "phone" not in backend.contacts[0].extras
        assert backend.contacts[0].extras["jid"] == "139153@lid"
        assert backend.contacts[0].extras["last_message_ts"] == 0

    def test_load_contacts_preserves_jid_and_last_ts_with_phone(self):
        """L'aggiunta di extras["phone"] non rimuove jid/last_message_ts."""
        backend = _make_backend()
        backend._lid_map = {}
        with patch.object(
            backend._rest,
            "list_contacts",
            return_value=[
                {"id": "393331234567@c.us", "name": "Mario", "last_ts": 1700000000000}
            ],
        ):
            backend._load_contacts()
        extras = backend.contacts[0].extras
        assert extras == {
            "jid": "393331234567@c.us",
            "last_message_ts": 1700000000000,
            "phone": "393331234567",
        }

    def test_send_message_sync_calls_rest(self):
        backend = _make_backend()
        with patch.object(
            backend._rest, "send_message", return_value={"id": "m"}
        ) as mock_send:
            msg_id = backend.send_message_sync(
                "wa:1@s.whatsapp.net", "Ciao!", reply_to_message_id="message-id-1"
            )
        mock_send.assert_called_once_with(
            "wa:1@s.whatsapp.net",
            "Ciao!",
            quote_timestamp=None,
            quote_author=None,
            quote_message=None,
            reply_to_message_id="message-id-1",
        )
        assert msg_id == "m"

    def test_resolve_send_chat_id_uses_cached_lid_phone(self):
        backend = _make_backend()
        backend._lid_map = {
            "139153@lid": {"phone": "393331234567", "resolved_at": 9999999999}
        }
        with patch.object(backend, "_lid_resolve_remote") as resolve_remote:
            assert backend._resolve_send_chat_id("139153@lid") == "393331234567@c.us"
        resolve_remote.assert_not_called()

    def test_unresolved_lid_never_reaches_send_request(self, tmp_path):
        backend = _make_backend()
        image = tmp_path / "photo.png"
        image.write_bytes(b"png-data")
        with (
            patch.object(backend, "_lid_lookup", return_value=None),
            patch.object(backend, "_lid_resolve_remote", return_value=None),
            patch.object(backend._rest, "_request") as request,
            pytest.raises(RuntimeError, match="non risolvibile a numero"),
        ):
            backend.send_attachment_sync("139153@lid", image, mime_type="image/png")
        request.assert_not_called()

    def test_send_message_forwards_reply_to_message_id(self):
        backend = _make_backend()
        with patch.object(backend, "send_message_sync", return_value="1") as mock_send:
            result = asyncio.run(
                backend.send_message(
                    "wa:1@s.whatsapp.net", "Ciao!", reply_to_message_id="message-id-1"
                )
            )

        assert result == "1"
        mock_send.assert_called_once_with(
            "wa:1@s.whatsapp.net",
            "Ciao!",
            quote_timestamp=None,
            quote_author=None,
            quote_message=None,
            reply_to_message_id="message-id-1",
        )

    def test_send_message_sync_raises_when_unreachable(self):
        backend = _make_backend()
        backend._rest.send_message = MagicMock(return_value=None)
        with pytest.raises(RuntimeError):
            backend.send_message_sync("wa:1@s.whatsapp.net", "x")

    def test_send_attachment_sync_sends_waha_file_object(self, tmp_path):
        backend = _make_backend()
        backend._lid_map = {
            "139153@lid": {"phone": "393331234567", "resolved_at": 9999999999}
        }
        image = tmp_path / "photo.png"
        image.write_bytes(b"png-data")
        with patch.object(
            backend._rest, "_request", return_value={"id": "image-id"}
        ) as request:
            message_id = backend.send_attachment_sync(
                "139153@lid", image, mime_type="image/png"
            )

        assert message_id == "image-id"
        request.assert_called_once_with(
            "POST",
            "/api/sendImage",
            {
                "session": backend._rest.session_name,
                "chatId": "393331234567@c.us",
                "file": {
                    "mimetype": "image/png",
                    "filename": "photo.png",
                    "data": "cG5nLWRhdGE=",
                },
                "caption": "",
            },
        )

    def test_send_attachment_sync_reports_waha_error(self, tmp_path, caplog):
        import urllib.error

        backend = _make_backend()
        image = tmp_path / "photo.png"
        image.write_bytes(b"png-data")
        error = urllib.error.HTTPError(
            "http://api.test/api/sendImage",
            500,
            "Internal Server Error",
            {},
            MagicMock(read=MagicMock(return_value=b'{"message":"WEBJS error t"}')),
        )
        with (
            patch("urllib.request.urlopen", side_effect=error),
            pytest.raises(RuntimeError, match="status=500.*WEBJS error t"),
        ):
            backend.send_attachment_sync("1@c.us", image, mime_type="image/png")

        assert "path=/api/sendImage status=500 detail=WEBJS error t" in caplog.text

    def test_waha_error_redacts_data_url(self, caplog):
        import urllib.error

        client = WhatsAppRESTClient("http://api.test")
        error = urllib.error.HTTPError(
            "http://api.test/api/sendImage",
            500,
            "Internal Server Error",
            {},
            MagicMock(
                read=MagicMock(
                    return_value=(
                        b'{"exception":{"message":"bad data:image/png;base64,'
                        b'c2VjcmV0"}}'
                    )
                )
            ),
        )

        with patch("urllib.request.urlopen", side_effect=error):
            assert client._request("POST", "/api/sendImage", {"file": {}}) is None

        assert client.last_error == "bad [redacted data URL]"
        assert "c2VjcmV0" not in caplog.text

    def test_needs_pairing(self):
        backend = _make_backend()
        with patch.object(
            backend._rest, "get_session_status", return_value={"status": "pending"}
        ):
            assert backend.needs_pairing is True
        with patch.object(
            backend._rest, "get_session_status", return_value={"status": "connected"}
        ):
            assert backend.needs_pairing is False

    def test_get_attachment_path(self, tmp_path):
        """Fast path: file already on disk returns it immediately."""
        media = tmp_path / "media"
        media.mkdir()
        (media / "att-1.jpg").write_bytes(b"data")
        backend = _make_backend(media_dir=str(media))
        p = backend.get_attachment_path("att-1.jpg")
        assert p is not None and p.exists()

    def test_get_attachment_path_missing_downloads(self, tmp_path):
        """Missing file triggers a lazy download from WAHA REST."""
        media = tmp_path / "media"
        media.mkdir()
        backend = _make_backend(media_dir=str(media))
        with patch.object(
            backend._rest, "download_media", return_value=b"downloaded"
        ) as mock_dl:
            p = backend.get_attachment_path("remote-media-1.jpg")
        mock_dl.assert_called_once_with("remote-media-1.jpg")
        assert p is not None and p.exists()
        assert p.read_bytes() == b"downloaded"

    def test_get_attachment_path_download_fails_returns_none(self, tmp_path):
        """When WAHA returns None (404 / error), the method returns None."""
        media = tmp_path / "media"
        media.mkdir()
        backend = _make_backend(media_dir=str(media))
        with patch.object(backend._rest, "download_media", return_value=None):
            p = backend.get_attachment_path("missing-media.jpg")
        assert p is None

    def test_get_attachment_path_no_rest_returns_none(self):
        """Without a REST client (no api_url), return None immediately."""
        backend = WhatsAppBackend(api_url="", media_dir="/tmp")
        assert backend.get_attachment_path("anything") is None

    def test_download_media_direct_binary(self):
        """WAHA Core direct binary endpoint returns bytes."""
        client = WhatsAppRESTClient("http://api.test")
        fake_bytes = b"\x89PNG\r\n\x1a\n"
        with patch.object(client, "_request_raw", return_value=fake_bytes) as mock_raw:
            result = client.download_media("msg-abc")
        mock_raw.assert_called_once_with(
            "GET", "/api/default/msg-abc/download", timeout=60
        )
        assert result == fake_bytes

    def test_download_media_falls_back_to_legacy_url(self):
        """When direct binary fails, fall back to get_download_url + fetch."""
        client = WhatsAppRESTClient("http://api.test")
        fake_bytes = b"media-data"
        with (
            patch.object(client, "_request_raw", return_value=None),
            patch.object(
                client, "get_download_url", return_value="https://s3.example.com/file"
            ),
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_resp = MagicMock()
            mock_resp.read.return_value = fake_bytes
            mock_resp.status = 200
            mock_urlopen.return_value.__enter__.return_value = mock_resp
            result = client.download_media("msg-abc")
        assert result == fake_bytes

    def test_download_media_returns_none_when_all_fail(self):
        """Both direct binary and legacy URL fail → None."""
        client = WhatsAppRESTClient("http://api.test")
        with (
            patch.object(client, "_request_raw", return_value=None),
            patch.object(client, "get_download_url", return_value=None),
        ):
            assert client.download_media("msg-abc") is None

    def test_download_media_direct_url(self):
        """When media_id looks like a URL, fetch it directly."""
        client = WhatsAppRESTClient("http://api.test")
        fake_bytes = b"image-data"
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = fake_bytes
            mock_resp.status = 200
            mock_urlopen.return_value.__enter__.return_value = mock_resp
            result = client.download_media("https://wa.to/media/img.jpg")
        assert result == fake_bytes

    def test_download_media_url_fetch_failure_falls_back_to_id_endpoint(self):
        """Quando il fetch dell'URL fallisce (media.url non più servito da
        WAHA), estrae l'id dal path e prova l'endpoint binario per id."""
        client = WhatsAppRESTClient("http://api.test")
        fake_bytes = b"\x89PNG\r\n\x1a\nimage"
        url = "http://localhost:3000/api/files/default/true_123@lid_ABC.jpeg"
        with (
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")),
            patch.object(client, "_request_raw", return_value=fake_bytes) as mock_raw,
        ):
            result = client.download_media(url, timeout=30)
        assert result == fake_bytes
        # L'id estratto dallo stem del path (estensione rimossa) viene
        # percent-encodato per il download endpoint.
        mock_raw.assert_any_call(
            "GET", "/api/default/true_123%40lid_ABC/download", timeout=30
        )

    def test_download_media_encodes_at_sign(self):
        """Message IDs with @lid are percent-encoded in the URL path."""
        client = WhatsAppRESTClient("http://api.test")
        fake_bytes = b"binary-data"
        with patch.object(client, "_request_raw", return_value=fake_bytes) as mock_raw:
            result = client.download_media("false_12345@lid_ABC")
        mock_raw.assert_called_once()
        call_path = mock_raw.call_args[0][1]
        # The @ must become %40 in the path.
        assert "@" not in call_path
        assert "%40" in call_path
        assert result == fake_bytes

    def test_poll_once_drains_queue(self):
        backend = _make_backend()
        ev = _msg(
            {"id": "m1", "from": "wa:1@s.whatsapp.net", "text": "hi", "timestamp": 1}
        )
        backend._enqueue_event(ev)
        events = backend.poll_once()
        assert len(events) == 1
        assert events[0].type == "message"
        # Second poll is empty.
        assert backend.poll_once() == []

    def test_ingest_message_dedup(self):
        import backend as backend_mod

        backend = _make_backend()
        data = {
            "text": "ciao",
            "is_mine": True,
            "sender": "You",
            "timestamp": 1000,
            "quote_text": None,
            "msg_type": "text",
            "attachment_info": None,
            "attachment_id": None,
        }
        with patch.object(backend_mod, "_add_message_to_cache") as mock_add:
            assert backend.ingest_message("wa:1@s.whatsapp.net", data, 1000) is True
            assert backend.ingest_message("wa:1@s.whatsapp.net", data, 1050) is False
            mock_add.assert_called_once()
        assert mock_add.call_args.kwargs["protocol"] == PROTOCOL_WHATSAPP

    def test_ingest_message_keeps_distinct_same_second_with_ids(self):
        """🛡️ Regressione: due messaggi DISTINTI con stesso testo E stesso
        secondo (stesso timestamp) ma id diversi NON devono essere deduplicati
        se la differenza di timestamp supera i 5 secondi (fuzzy dedup window).

        Il fuzzy dedup con tolleranza ±5s serve a gestire ID mismatch tra
        webhook e REST API di WAHA.  Messaggi oltre la finestra sono distinti.
        """
        import backend as backend_mod

        backend = _make_backend()
        cid = "wa:1@s.whatsapp.net"
        data1 = {
            "id": "m1",
            "text": "ok",
            "is_mine": False,
            "sender": "M",
            "timestamp": 1000,
            "quote_text": None,
            "msg_type": "text",
            "attachment_info": None,
            "attachment_id": None,
        }
        data2 = {
            "id": "m2",
            "text": "ok",
            "is_mine": False,
            "sender": "M",
            "timestamp": 10000,
            "quote_text": None,
            "msg_type": "text",
            "attachment_info": None,
            "attachment_id": None,
        }
        with patch.object(backend_mod, "_add_message_to_cache") as mock_add:
            assert backend.ingest_message(cid, data1, 1000) is True
            assert (
                backend.ingest_message(cid, data2, 10000) is True
            )  # fuori finestra fuzzy
            assert mock_add.call_count == 2
        # Lo stesso id (stesso messaggio) resta deduplicato.
        with patch.object(backend_mod, "_add_message_to_cache") as mock_add:
            assert backend.ingest_message(cid, data1, 1000) is False
            mock_add.assert_not_called()

    def test_ingest_message_dedup_falls_back_without_id(self):
        """Senza id, il dedup ricade su (is_mine, testo, timestamp) come prima."""
        import backend as backend_mod

        backend = _make_backend()
        cid = "wa:1@s.whatsapp.net"
        data = {
            "text": "ciao",
            "is_mine": True,
            "sender": "You",
            "timestamp": 1000,
            "quote_text": None,
            "msg_type": "text",
            "attachment_info": None,
            "attachment_id": None,
        }
        with patch.object(backend_mod, "_add_message_to_cache") as mock_add:
            assert backend.ingest_message(cid, data, 1000) is True
            assert (
                backend.ingest_message(cid, data, 1050) is False
            )  # echo entro finestra
            mock_add.assert_called_once()

    def test_two_identical_texts_confirmed_not_merged(self):
        """Outgoing rows with different real ids remain distinct within 10 min."""
        import backend as backend_mod

        backend = _make_backend()
        cid = "wa:1@s.whatsapp.net"
        t0 = 1_700_000_000_000
        original = {
            "id": "A",
            "text": "OK",
            "is_mine": True,
            "sender": "You",
            "timestamp": t0,
            "msg_type": "text",
            "attachment_id": None,
        }
        backend.cache[cid] = [original]
        echo = {
            "id": "B",
            "text": "OK",
            "is_mine": True,
            "sender": "You",
            "msg_type": "text",
            "attachment_id": None,
        }

        with (
            patch.object(backend_mod, "_add_message_to_cache") as mock_add,
            patch.object(backend_mod, "_update_message_id") as mock_update,
            patch.object(backend, "_reuse_failed_db_row", return_value=False),
        ):
            added = backend.ingest_message(cid, echo, t0 + 120_000)

        assert added is True
        assert len(backend.cache[cid]) == 2
        assert original["id"] == "A"
        assert original["timestamp"] == t0
        assert {msg["id"] for msg in backend.cache[cid]} == {"A", "B"}
        mock_add.assert_called_once()
        mock_update.assert_not_called()

    def test_three_images_rapid_send_not_merged(self):
        """Three rapid images with distinct ids and attachments produce three rows."""
        import backend as backend_mod

        backend = _make_backend()
        cid = "wa:1@s.whatsapp.net"
        t0 = 1_700_000_000_000

        with (
            patch.object(backend_mod, "_add_message_to_cache") as mock_add,
            patch.object(backend, "_reuse_failed_db_row", return_value=False),
        ):
            results = [
                backend.ingest_message(
                    cid,
                    {
                        "id": f"image-{index}",
                        "text": "",
                        "is_mine": True,
                        "sender": "You",
                        "msg_type": "image",
                        "attachment_id": f"attachment-{index}",
                        "attachment_info": f"photo-{index}.jpg",
                    },
                    t0 + index * 1000,
                )
                for index in range(3)
            ]

        assert results == [True, True, True]
        assert len(backend.cache[cid]) == 3
        assert {msg["id"] for msg in backend.cache[cid]} == {
            "image-0",
            "image-1",
            "image-2",
        }
        assert {msg["attachment_id"] for msg in backend.cache[cid]} == {
            "attachment-0",
            "attachment-1",
            "attachment-2",
        }
        assert mock_add.call_count == 3

    def test_echo_upgrades_only_idless_row(self):
        """An echo cannot fallback-match a confirmed row with a different id."""
        import backend as backend_mod

        backend = _make_backend()
        cid = "wa:1@s.whatsapp.net"
        t0 = 1_700_000_000_000
        confirmed = {
            "id": "A",
            "text": "ciao",
            "is_mine": True,
            "timestamp": t0,
            "attachment_id": None,
        }
        optimistic = {
            "id": None,
            "text": "ciao",
            "is_mine": True,
            "timestamp": t0 + 1000,
            "attachment_id": None,
        }
        backend.cache[cid] = [confirmed, optimistic]

        with patch.object(backend_mod, "_update_message_id") as mock_update:
            added = backend.ingest_message(
                cid,
                {
                    "id": "B",
                    "text": "ciao",
                    "is_mine": True,
                    "sender": "You",
                    "msg_type": "text",
                    "attachment_id": None,
                },
                t0 + 2000,
            )

        assert added is False
        assert len(backend.cache[cid]) == 2
        assert confirmed["id"] == "A"
        assert confirmed["timestamp"] == t0
        assert optimistic["id"] == "B"
        assert optimistic["timestamp"] == t0 + 2000
        mock_update.assert_called_once()

    def test_media_race_row_is_upgraded_by_later_echo(self):
        """Riga text vuota creata da media race (webhook senza media) viene
        ripristinata in place a immagine quando l'echo/fetch porta l'URL media
        reale di WAHA (reperto live: la 3ª foto a Giovanni non compariva
        nemmeno dopo aver riaperto la chat)."""
        import backend as backend_mod

        backend = _make_backend()
        cid = "15771304468671@lid"
        t0 = 1_788_114_863_000
        mid = "true_15771304468671@lid_4AB02B4889D1AFFF775C"
        url = (
            "http://localhost:3000/api/files/default/"
            "true_15771304468671@lid_4AB02B4889D1AFFF775C.jpeg"
        )

        # 1) Media race: il webhook arriva con hasMedia=true ma media=null →
        #    riga text vuota (prima della fix veniva creata una bolla vuota).
        assert backend.ingest_message(
            cid,
            {
                "id": mid,
                "text": "",
                "is_mine": True,
                "sender": "You",
                "msg_type": "text",
                "attachment_id": None,
            },
            t0,
        )

        # 2) Echo/fetch successivo con il media reale → upgrade in place.
        added = backend.ingest_message(
            cid,
            {
                "id": mid,
                "text": "",
                "is_mine": True,
                "sender": "You",
                "msg_type": "image",
                "attachment_id": url,
            },
            t0,
        )

        assert added is False  # dedup: nessuna riga duplicata
        assert len(backend.cache[cid]) == 1
        assert backend.cache[cid][0]["attachment_id"] == url
        assert backend.cache[cid][0]["msg_type"] == "image"
        with sqlite3.connect(backend_mod.DB_FILE) as conn:
            row = conn.execute(
                "SELECT attachment_id, msg_type FROM messages"
            ).fetchone()
            assert row == (url, "image")

    def test_echo_out_of_order_picks_closest_idless(self):
        """Among id-less fallback candidates, the closest timestamp is upgraded."""
        import backend as backend_mod

        backend = _make_backend()
        cid = "wa:1@s.whatsapp.net"
        t0 = 1_700_000_000_000
        older = {
            "id": None,
            "text": "OK",
            "is_mine": True,
            "timestamp": t0,
            "attachment_id": None,
        }
        closest = {
            "id": None,
            "text": "OK",
            "is_mine": True,
            "timestamp": t0 + 6000,
            "attachment_id": None,
        }
        backend.cache[cid] = [older, closest]

        with patch.object(backend_mod, "_update_message_id") as mock_update:
            added = backend.ingest_message(
                cid,
                {
                    "id": "B",
                    "text": "OK",
                    "is_mine": True,
                    "sender": "You",
                    "msg_type": "text",
                    "attachment_id": None,
                },
                t0 + 6000,
            )

        assert added is False
        assert len(backend.cache[cid]) == 2
        assert older["id"] is None
        assert older["timestamp"] == t0
        assert closest["id"] == "B"
        assert closest["timestamp"] == t0 + 6000
        mock_update.assert_called_once()

    def test_ack_echo_media_does_not_duplicate(self):
        """🛡️ Regressione: il message.ack di un'immagine uscente con caption in
        `body` (stesso msg_id del media reale ma text=caption) NON deve essere
        ingerito come nuovo messaggio di testo.

        Il match per id (outgoing) deve precedere il confronto sul testo:
        altrimenti "Media: <url>" ≠ caption e l'evento ack diventa una bolla
        di testo duplicata accanto alla caption reale.
        """
        import backend as backend_mod

        backend = _make_backend()
        cid = "wa:1@s.whatsapp.net"
        msg_id = "true_189025889575055@lid_3A268CF00E4ECCEA4474"
        image = {
            "id": msg_id,
            "text": "Media: https://wa.to/img/abc123.jpg",
            "is_mine": True,
            "sender": "You",
            "msg_type": "image",
            "attachment_info": "Yes, nice",
            "attachment_id": "https://wa.to/img/abc123.jpg",
        }
        ack = {
            "id": msg_id,
            "text": "Yes, nice",
            "is_mine": True,
            "sender": "You",
            "msg_type": "text",
            "attachment_info": None,
            "attachment_id": None,
        }
        with patch.object(backend_mod, "_add_message_to_cache") as mock_add:
            assert backend.ingest_message(cid, image, 1700000000) is True
            assert backend.ingest_message(cid, ack, 1700000001) is False
            mock_add.assert_called_once()
        assert len(backend.cache[cid]) == 1
        assert backend.cache[cid][0]["msg_type"] == "image"

    def test_ack_echo_text_still_dedups_optimistic(self):
        """🛡️ Regressione: il dedup ottimistico (TUI-send senza id) + echo con
        id resta funzionante dopo il reorder id-su-testo per gli outgoing."""
        import backend as backend_mod

        backend = _make_backend()
        cid = "wa:1@s.whatsapp.net"
        optimistic = {
            "text": "hello",
            "is_mine": True,
            "sender": "You",
            "msg_type": "text",
            "attachment_info": None,
            "attachment_id": None,
        }
        echo = {
            "id": "echo-id-1",
            "text": "hello",
            "is_mine": True,
            "sender": "You",
            "msg_type": "text",
            "attachment_info": None,
            "attachment_id": None,
        }
        with (
            patch.object(backend_mod, "_add_message_to_cache") as mock_add,
            patch.object(backend_mod, "_update_message_id") as mock_upd,
        ):
            assert backend.ingest_message(cid, optimistic, 1700000000) is True
            assert backend.ingest_message(cid, echo, 1700000002) is False
            mock_add.assert_called_once()
            mock_upd.assert_called_once()
        assert len(backend.cache[cid]) == 1
        assert backend.cache[cid][0]["id"] == "echo-id-1"

    def test_ack_echo_media_reverse_order(self):
        """🛡️ Regressione: ack sintetico (text=caption, id=msg_id) ingerito
        PRIMA del messaggio immagine (stesso id) non crea due righe."""
        import backend as backend_mod

        backend = _make_backend()
        cid = "wa:1@s.whatsapp.net"
        msg_id = "true_reverse_189025889575055@lid"
        ack = {
            "id": msg_id,
            "text": "Yes, nice",
            "is_mine": True,
            "sender": "You",
            "msg_type": "text",
            "attachment_info": None,
            "attachment_id": None,
        }
        image = {
            "id": msg_id,
            "text": "Media: https://wa.to/img/abc123.jpg",
            "is_mine": True,
            "sender": "You",
            "msg_type": "image",
            "attachment_info": "Yes, nice",
            "attachment_id": "https://wa.to/img/abc123.jpg",
        }
        with patch.object(backend_mod, "_add_message_to_cache") as mock_add:
            assert backend.ingest_message(cid, ack, 1700000000) is True
            assert backend.ingest_message(cid, image, 1700000001) is False
            mock_add.assert_called_once()
        assert len(backend.cache[cid]) == 1


class TestWhatsAppWebhook:
    """📥 Ricezione PUSH via webhook di WAHA (niente più polling).

    La ricezione live è interamente event-driven: WAHA Core fa POST a
    ``/webhook`` con ``{"event": "message", "session": ..., "payload": {...}}``
    e ``handle_webhook`` lo normalizza in un ``ChatEvent`` accodato alla TUI
    (via ``poll_once``).  Nessun GET periodico ``/api/messages``.
    """

    def _backend(self) -> WhatsAppBackend:
        backend = _make_backend("http://api.test")
        backend._rest = MagicMock()
        return backend

    def test_handle_webhook_enqueues_message_event(self):
        """Un envelope message webhook viene normalizzato e accodato."""
        import time

        now = int(time.time())
        backend = self._backend()
        envelope = {
            "session": "default",
            "event": "message",
            "payload": {
                "id": "m1",
                "from": "393400716440@c.us",
                "fromMe": False,
                "body": "Ciao via webhook",
                "timestamp": now,
            },
        }
        ok = backend.handle_webhook(envelope)
        assert ok is True
        events = backend.poll_once()
        assert len(events) == 1
        ev = events[0]
        assert ev.type == "message"
        assert ev.payload["text"] == "Ciao via webhook"
        assert ev.payload["is_mine"] is False
        assert ev.contact_id == "393400716440@c.us"

    def test_handle_webhook_deduplicates_retried_id(self):
        """Un retry di WAHA (stesso id) non viene accodato due volte."""
        import time

        now = int(time.time())
        backend = self._backend()
        envelope = {
            "session": "default",
            "event": "message",
            "payload": {
                "id": "dup1",
                "from": "1@c.us",
                "fromMe": False,
                "body": "stesso msg",
                "timestamp": now,
            },
        }
        assert backend.handle_webhook(envelope) is True
        assert backend.handle_webhook(envelope) is True  # retry
        events = backend.poll_once()
        assert len(events) == 1  # dedup per id

    def test_attachment_parts_have_stable_text_and_live_dedup_is_per_part(self):
        """A multipart message keeps every attachment while retries stay idempotent."""
        backend = self._backend()
        payload = {
            "id": "parent-1",
            "from": "1@c.us",
            "fromMe": False,
            "timestamp": 1700000000,
            "attachments": [
                {"id": "media-a", "filename": "first.jpg", "mimetype": "image/jpeg"},
                {"id": "media-b", "filename": "second.jpg", "mimetype": "image/jpeg"},
            ],
        }
        envelope = {"event": "message", "payload": payload}

        assert backend.handle_webhook(envelope) is True
        assert backend.handle_webhook(envelope) is True
        events = backend.poll_once()
        assert [event.payload["text"] for event in events] == ["", ""]

        payload["attachments"].reverse()
        assert backend.handle_webhook(envelope) is True
        assert backend.poll_once() == []

    def test_single_media_has_synthetic_text_while_plain_text_is_unchanged(self):
        plain = _msg({"id": "plain", "from": "1@c.us", "text": "hello", "timestamp": 1})
        media = _msg(
            {
                "id": "parent-media",
                "from": "1@c.us",
                "timestamp": 1,
                "hasMedia": True,
                "media": {"url": "https://wa.test/photo.jpg", "filename": "photo.jpg"},
            }
        )
        assert plain.payload["text"] == "hello"
        assert media.payload["text"] == "Media: https://wa.test/photo.jpg"

    def test_outgoing_echo_and_ack_read_keep_the_parent_message_id(self):
        import backend as backend_mod

        backend = self._backend()
        payload = {
            "id": "outgoing-parent",
            "to": "1@c.us",
            "fromMe": True,
            "timestamp": 1700000000,
            "body": "sent from another device",
            "status": 2,
        }
        with (
            patch.object(backend, "_persist_message"),
            patch.object(backend_mod, "_update_message_status_by_id"),
        ):
            # status 2 (DEVICE) → synthetic message event + delivered receipt;
            # handle_webhook must NOT mutate the cache (single mutation point).
            assert backend.handle_webhook({"event": "message.ack", "payload": payload})
            echo = backend.poll_once()
            assert [event.type for event in echo] == ["message", "receipt"]
            # The consumer performs the ingestion.
            backend.ingest_message(
                echo[0].contact_id, echo[0].payload, echo[0].payload["timestamp"]
            )
            assert backend.cache["1@c.us"][0]["id"] == "outgoing-parent"

            payload["status"] = 4
            assert backend.handle_webhook({"event": "message.ack", "payload": payload})
            events = backend.poll_once()
            receipt = next(event for event in events if event.type == "receipt")
            assert receipt.payload["message_ids"] == ["outgoing-parent"]
            assert backend.process_receipt(receipt.payload)[0]["status"] == "read"
            assert len(backend.cache["1@c.us"]) == 1

    def test_handle_webhook_ignores_non_message_events(self):
        """Eventi non-message (es. presence/typing fuori dalla registrazione)
        non generano errori; quelli message con envelope valido sì."""
        backend = self._backend()
        assert (
            backend.handle_webhook({"session": "default", "event": "message.ack"})
            is False
        )
        assert backend.handle_webhook("not-a-dict") is False
        assert backend.handle_webhook({}) is False
        assert backend.poll_once() == []

    def test_handle_webhook_normalizes_from_message_nested_key(self):
        """L'id può essere annidato sotto key.id (come /api/messages)."""
        import time

        now = int(time.time())
        backend = self._backend()
        env = {
            "event": "message",
            "payload": {
                "key": {"id": "KEY123", "remoteJid": "15771304468671@lid"},
                "from": "1@c.us",
                "fromMe": True,
                "body": "hello",
                "timestamp": now,
            },
        }
        assert backend.handle_webhook(env) is True
        events = backend.poll_once()
        assert len(events) == 1
        assert events[0].payload["id"] == "KEY123"
        assert events[0].payload["is_mine"] is True

    def test_handle_webhook_image_message_ack_retains_image_fields(self):
        """message.ack per un'immagine uscente deve preservare msg_type/image.

        WAHA invia message.ack (non message) per gli echo di messaggi in
        uscita.  Il synthetic event costruito da handle_webhook DEVE includere
        msg_type, attachment_id e attachment_info estratti dai campi
        hasMedia/media del payload, altrimenti l'immagine appare come testo
        vuoto senza banner [🖼️].
        """
        import time

        now = int(time.time())
        backend = self._backend()
        envelope = {
            "session": "default",
            "event": "message.ack",
            "payload": {
                "id": "IMG_ECHO_1",
                "to": "3912345678@c.us",
                "fromMe": True,
                "timestamp": now,
                "status": 2,  # DEVICE (2 → delivered receipt)
                "body": "",
                "hasMedia": True,
                "media": {
                    "mimetype": "image/jpeg",
                    "url": "https://wa.to/img/echo-photo.jpg",
                    "caption": "Guarda qua!",
                },
            },
        }
        ok = backend.handle_webhook(envelope)
        assert ok is True
        events = backend.poll_once()
        assert len(events) == 2, (
            f"Expected message + delivered receipt for image ack, got {len(events)}"
        )
        ev = events[0]
        assert ev.type == "message"
        assert ev.payload["msg_type"] == "image", (
            "Synthetic ack event msg_type should be 'image', "
            f"got {ev.payload.get('msg_type')!r}"
        )
        assert ev.payload["attachment_id"] == "https://wa.to/img/echo-photo.jpg"
        assert ev.payload["attachment_info"] == "Guarda qua!"

    def test_handle_webhook_image_ack_caption_in_body(self):
        """message.ack per un'immagine uscente con caption in `body` (WAHA reale).

        WAHA consegna la caption dei media inviati in `body`/`text`, non in
        `caption`/`media.caption`.  Il synthetic event accodato deve portare
        msg_type=image e attachment_info con la caption reale.
        """
        import time

        now = int(time.time())
        backend = self._backend()
        envelope = {
            "session": "default",
            "event": "message.ack",
            "payload": {
                "id": "ACK_IMG_BODY",
                "to": "3912345678@c.us",
                "fromMe": True,
                "timestamp": now,
                "status": 2,  # DEVICE (2 → delivered receipt)
                "body": "Nice, or?",
                "hasMedia": True,
                "media": {
                    "mimetype": "image/jpeg",
                    "url": "https://wa.to/img/x.jpg",
                },
            },
        }
        assert backend.handle_webhook(envelope) is True
        events = backend.poll_once()
        assert len(events) == 2
        ev = events[0]
        assert ev.type == "message"
        assert ev.payload["msg_type"] == "image"
        assert ev.payload["attachment_info"] == "Nice, or?"

    def test_handle_webhook_image_message_ack_without_hasMedia_is_plain(self):
        """message.ack senza hasMedia → synthetic event resta text (nessun crash)."""
        import time

        now = int(time.time())
        backend = self._backend()
        envelope = {
            "session": "default",
            "event": "message.ack",
            "payload": {
                "id": "TXT_ECHO_1",
                "to": "3912345678@c.us",
                "fromMe": True,
                "timestamp": now,
                "status": 2,
                "body": "hello",
            },
        }
        ok = backend.handle_webhook(envelope)
        assert ok is True
        events = backend.poll_once()
        assert len(events) == 2
        ev = events[0]
        assert ev.payload["msg_type"] == "text"  # default
        assert ev.payload.get("attachment_id") is None

    def test_list_messages_uses_worker_timeout(self):
        """list_messages usa un timeout generoso (30s): gira in un worker
        thread e WAHA può impiegare >10s a rispondere a /api/messages."""
        client = WhatsAppRESTClient("http://api.test")
        seen_timeout = []

        def fake_urlopen(req, timeout=30):
            seen_timeout.append(timeout)
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = b"[]"
            ctx = MagicMock()
            ctx.__enter__.return_value = resp
            return ctx

        with patch("urllib.request.urlopen", fake_urlopen):
            client.list_messages("X@lid", limit=1)
        assert seen_timeout and seen_timeout[0] == 30

    def test_list_messages_rest(self):
        """RESTClient.list_messages costruisce la GET corretta."""
        client = WhatsAppRESTClient("http://api.test")
        seen = []

        def fake_urlopen(req, timeout=30):
            seen.append((req.method, req.full_url))
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = json.dumps(
                [
                    {
                        "id": "m1",
                        "from": "1@c.us",
                        "body": "hi",
                        "timestamp": 1700000000,
                    },
                ]
            ).encode("utf-8")
            ctx = MagicMock()
            ctx.__enter__.return_value = resp
            return ctx

        with patch("urllib.request.urlopen", fake_urlopen):
            msgs = client.list_messages("X@lid", limit=3)
        assert len(msgs) == 1
        assert seen[0][0] == "GET"
        assert "/api/messages?session=default&chatId=X@lid&limit=3" in seen[0][1]

    def test_fetch_history_normalizes_and_ingests(self):
        """fetch_history normalizza lo storico remoto e lo ingerisce nel cache."""
        import time

        now = int(time.time())
        backend = self._backend()  # _rest = MagicMock
        # WAHA ritorna i messaggi dal più recente in giù; li riordina e li ingerisce.
        backend._rest.list_messages.return_value = [
            {
                "id": "m_new",
                "from": "393400716440@c.us",
                "fromMe": False,
                "body": "più recente",
                "timestamp": now,
            },
            {
                "id": "m_my",
                "from": "19645297868955@lid",
                "fromMe": True,
                "body": "il mio inviato",
                "timestamp": now - 50,
            },
            {
                "id": "m_old",
                "from": "393400716440@c.us",
                "fromMe": False,
                "body": "più vecchio",
                "timestamp": now - 100,
            },
        ]
        # isola ingest_message (toccherebbe SQLite)
        ingested = []
        backend.ingest_message = lambda cid, data, ts: (
            ingested.append((cid, data.get("text"), data.get("is_mine"), ts)) or True
        )
        result = backend.fetch_history("15771304468671@lid", limit=20)
        # la versione REST è stata chiamata col jid e limit
        backend._rest.list_messages.assert_called_once_with(
            "15771304468671@lid", limit=20
        )
        # i 3 messaggi sono stati ingeriti, inclusi i miei (is_mine=True)
        assert len(ingested) == 3
        assert ingested[0][1] == "più vecchio"  # ordinato cronologico
        assert ingested[1][1] == "il mio inviato"
        assert ingested[2][1] == "più recente"
        assert ingested[1][2] is True  # il mio -> is_mine=True
        assert ingested[0][2] is False and ingested[2][2] is False
        # risultato ritornato non vuoto
        assert len(result) == 3


# ─── Optional configuration gating ────────────────────────────────────────────


class TestWhatsAppConfigGating:
    """⚙️ WhatsApp backend è opzionale (non rompe la modalità solo-Signal)."""

    def test_disabled_when_no_api_url_and_no_local(self):
        from backends import config

        with (
            patch.object(config, "get_whatsapp_api_url", return_value=""),
            patch.object(config, "_local_waha_reachable", return_value=False),
        ):
            assert config.whatsapp_enabled() is False

    def test_enabled_with_api_url(self):
        from backends import config

        with patch.object(
            config, "get_whatsapp_api_url", return_value="http://127.0.0.1:3000"
        ):
            assert config.whatsapp_enabled() is True

    def test_enabled_when_local_waha_detected(self):
        """Auto-detect: WAHA locale raggiungibile abilita il backend anche senza URL."""
        from backends import config

        with (
            patch.object(config, "get_whatsapp_api_url", return_value=""),
            patch.object(config, "_local_waha_reachable", return_value=True),
        ):
            assert config.whatsapp_enabled() is True

    def test_resolve_whatsapp_api_url_prefers_configured(self):
        from backends import config

        with patch.object(
            config, "get_whatsapp_api_url", return_value="http://waha:9999"
        ):
            assert config.resolve_whatsapp_api_url() == "http://waha:9999"

    def test_resolve_whatsapp_api_url_falls_back_to_local_port(self):
        from backends import config

        with (
            patch.object(config, "get_whatsapp_api_url", return_value=""),
            patch.object(config, "get_whatsapp_api_port", return_value=3005),
        ):
            assert config.resolve_whatsapp_api_url() == "http://127.0.0.1:3005"

    def test_api_key_prefers_env_over_config_and_dotenv(self):
        from backends import config

        with (
            patch.dict(os.environ, {"WHATSAPP_API_KEY": "from-env"}),
            patch.object(
                config, "_load_config", return_value={"whatsapp_api_key": "from-cfg"}
            ),
            patch.object(
                config, "_load_dotenv", return_value={"WAHA_API_KEY": "from-dotenv"}
            ),
        ):
            assert config.get_whatsapp_api_key() == "from-env"

    def test_api_key_falls_back_to_config(self):
        from backends import config

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                config, "_load_config", return_value={"whatsapp_api_key": "from-cfg"}
            ),
            patch.object(
                config, "_load_dotenv", return_value={"WAHA_API_KEY": "from-dotenv"}
            ),
        ):
            assert config.get_whatsapp_api_key() == "from-cfg"

    def test_api_key_falls_back_to_dotenv_waha(self):
        from backends import config

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(config, "_load_config", return_value={}),
            patch.object(
                config, "_load_dotenv", return_value={"WAHA_API_KEY": "from-dotenv"}
            ),
        ):
            assert config.get_whatsapp_api_key() == "from-dotenv"

    def test_api_key_empty_when_nowhere(self):
        from backends import config

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(config, "_load_config", return_value={}),
            patch.object(config, "_load_dotenv", return_value={}),
        ):
            assert config.get_whatsapp_api_key() == ""


# ─── WAHA (WhatsApp HTTP API) contract ────────────────────────────────────────


class TestWAHAContract:
    """📨 Mapping degli endpoint reali di WAHA (devlikeapro/waha)."""

    def test_webhook_path_is_delivery_contract(self):
        """WAHA consegna gli eventi via POST a ``/webhook`` (nessun WS)."""
        # Il backend accetta un envelope WAHA {event:message, payload} via
        # handle_webhook (chiamato dall'HTTP server avviato da ensure_webhook_server).
        backend = _make_backend("http://api.test")
        backend._contacts_by_jid = {}
        frame = {
            "event": "message",
            "session": "default",
            "payload": {
                "id": "w0",
                "from": "39123@s.whatsapp.net",
                "body": "push delivery",
                "fromMe": False,
                "timestamp": 1700000000,
            },
        }
        assert backend.handle_webhook(frame) is True
        ev = backend.poll_once()[0]
        assert ev.type == "message"
        assert ev.contact_id == "39123@s.whatsapp.net"

    def test_rest_paths_are_api_prefixed(self):
        """I path REST usano il prefisso /api come da contratto WAHA."""

        client = WhatsAppRESTClient("http://api.test")
        seen = []

        def fake_urlopen(req, timeout=30):
            seen.append((req.method, req.full_url))
            resp = MagicMock()
            resp.read.return_value = b"{}"
            ctx = MagicMock()
            ctx.__enter__.return_value = resp
            return ctx

        with patch("urllib.request.urlopen", fake_urlopen):
            client.list_contacts()
            client.send_message("wa:1@s.whatsapp.net", "ciao")
            client.get_session_status()  # /api/sessions/{name}
            client.get_session_qr()  # /api/{name}/auth/qr (PNG binario)

        assert any(m == "GET" and "/api/default/chats" in u for m, u in seen)
        assert any(m == "POST" and "/api/sendText" in u for m, u in seen)
        assert any(m == "GET" and "/api/sessions/default" in u for m, u in seen)
        assert any(m == "GET" and "/api/default/auth/qr" in u for m, u in seen)

    def test_waha_ws_frame_unwraps_payload(self):
        """Un frame WAHA {event, payload} viene normalizzato come messaggio."""
        frame = {
            "event": "message",
            "session": "default",
            "payload": {
                "id": "w1",
                "chatId": "39123@s.whatsapp.net",
                "fromMe": False,
                "text": "ciao da WAHA",
                "timestamp": 1700000000,
                "pushName": "Mario",
            },
        }
        ev = _raw(frame)
        assert ev is not None
        assert ev.type == "message"
        assert ev.contact_id == "39123@s.whatsapp.net"
        assert ev.payload["text"] == "ciao da WAHA"
        assert ev.payload["is_mine"] is False

    def test_waha_ws_frame_chat_is_object(self):
        """Il payload WAHA può avere chat come OGGETTO {id._serialized}: il
        contact_id va comunque normalizzato a stringa."""
        frame = {
            "event": "message",
            "session": "default",
            "payload": {
                "id": "w2",
                "from": "39124@s.whatsapp.net",
                "body": "testo con body",
                "fromMe": False,
                "timestamp": 1700000001,
                "chat": {"id": {"_serialized": "39124@s.whatsapp.net"}, "name": "Anna"},
            },
        }
        ev = _raw(frame)
        assert ev is not None
        assert ev.contact_id == "39124@s.whatsapp.net"
        assert isinstance(ev.contact_id, str)
        assert ev.payload["text"] == "testo con body"
        assert ev.payload["timestamp"] == 1700000001000

    def test_waha_message_any_recognized(self):
        """WAHA può emettere l'evento 'message.any'; va trattato come messaggio."""
        frame = {
            "event": "message.any",
            "session": "default",
            "payload": {
                "id": "w3",
                "from": "39125@s.whatsapp.net",
                "body": "hello",
                "fromMe": True,
                "timestamp": 1700000002,
            },
        }
        ev = _raw(frame)
        assert ev is not None
        assert ev.type == "message"
        assert ev.contact_id == "39125@s.whatsapp.net"
        assert ev.payload["is_mine"] is True

    # ── Image webhook integration tests ──────────────────────────────────

    def test_webhook_image_via_hasMedia_end_to_end(self):
        """Catena completa: webhook immagine → handle_webhook → poll_once →
        ChatEvent → ingest_message → cache con metadati immagine.

        Verifica che un'immagine ricevuta via webhook WAHA (formato
        hasMedia/media) venga:
        1. Normalizzata in un ChatEvent con msg_type=image
        2. Accodata via poll_once
        3. Salvata in cache con attachment_id e attachment_info
        """
        backend = _make_backend("http://api.test")
        backend._contacts_by_jid = {
            "3912345678@c.us": MagicMock(
                display_name="Mario", id="3912345678@c.us", protocol=PROTOCOL_WHATSAPP
            ),
        }

        # Simula un webhook WAHA per un'immagine in arrivo.
        envelope = {
            "event": "message",
            "session": "default",
            "payload": {
                "id": "IMG_WEBHOOK_1",
                "from": "3912345678@c.us",
                "fromMe": False,
                "pushName": "Mario",
                "timestamp": 1700000000,
                "hasMedia": True,
                "media": {
                    "mimetype": "image/jpeg",
                    "url": "https://wa.to/media/abc123.jpg",
                    "filename": "photo.jpg",
                    "caption": "Guarda questa foto!",
                },
            },
        }

        # Step 1: handle_webhook processa l'envelope.
        ok = backend.handle_webhook(envelope)
        assert ok is True, "handle_webhook should return True for image message"

        # Step 2: poll_once deve produrre un evento.
        events = backend.poll_once()
        assert len(events) == 1, f"Expected 1 event, got {len(events)}"
        ev = events[0]
        assert ev.type == "message"
        assert ev.contact_id == "3912345678@c.us"
        assert ev.payload["msg_type"] == "image", (
            f"msg_type should be 'image', got {ev.payload.get('msg_type')!r}"
        )
        assert ev.payload["attachment_id"] == "https://wa.to/media/abc123.jpg"
        assert ev.payload["attachment_info"] == "Guarda questa foto!"

        # Step 3: ingest_message salva i metadati immagine nella cache.
        added = backend.ingest_message(
            "3912345678@c.us",
            ev.payload,
            ev.payload["timestamp"],
        )
        assert added is True, "ingest_message should return True for new image"

        # Step 4: verifica che la cache contenga i metadati immagine.
        cached = backend.cache.get("3912345678@c.us", [])
        assert len(cached) == 1
        msg = cached[0]
        assert msg["msg_type"] == "image"
        assert msg["attachment_id"] == "https://wa.to/media/abc123.jpg"
        assert msg["attachment_info"] == "Guarda questa foto!"
        assert msg["text"] == ""

    def test_webhook_image_via_message_any_end_to_end(self):
        """Stessa catena ma con event=message.any (WAHA Core può usarlo)."""
        backend = _make_backend("http://api.test")
        backend._contacts_by_jid = {}

        envelope = {
            "event": "message.any",
            "session": "default",
            "payload": {
                "id": "IMG_ANY_1",
                "from": "3912345678@c.us",
                "fromMe": False,
                "timestamp": 1700000001,
                "hasMedia": True,
                "media": {
                    "mimetype": "image/png",
                    "url": "https://wa.to/media/img.png",
                    "caption": "Screenshot",
                },
            },
        }

        ok = backend.handle_webhook(envelope)
        assert ok is True

        events = backend.poll_once()
        assert len(events) == 1
        ev = events[0]
        assert ev.payload["msg_type"] == "image"
        assert ev.payload["attachment_id"] == "https://wa.to/media/img.png"
        assert ev.payload["attachment_info"] == "Screenshot"

        # Verifica che ingest_message salvi correttamente.
        added = backend.ingest_message(
            "3912345678@c.us",
            ev.payload,
            ev.payload["timestamp"],
        )
        assert added is True
        cached = backend.cache["3912345678@c.us"]
        assert cached[0]["msg_type"] == "image"
        assert cached[0]["attachment_id"] == "https://wa.to/media/img.png"

    def test_webhook_image_via_nested_message_imageMessage(self):
        """Immagine WAHA nel formato nested message.imageMessage (senza hasMedia).

        Alcune versioni WAHA Core mandano l'immagine dentro
        payload.message.imageMessage invece che nei campi flat hasMedia/media.
        """
        backend = _make_backend("http://api.test")
        backend._contacts_by_jid = {}

        envelope = {
            "event": "message",
            "session": "default",
            "payload": {
                "id": "IMG_NESTED_1",
                "from": "3912345678@c.us",
                "fromMe": False,
                "pushName": "Mario",
                "timestamp": 1700000003,
                "message": {
                    "imageMessage": {
                        "url": "https://wa.to/media/nested.jpg",
                        "mimetype": "image/jpeg",
                        "caption": "Foto dal nested!",
                    },
                },
            },
        }

        ok = backend.handle_webhook(envelope)
        assert ok is True

        events = backend.poll_once()
        assert len(events) == 1
        ev = events[0]
        assert ev.payload["msg_type"] == "image", (
            f"Nested imageMessage → msg_type should be 'image', got {ev.payload.get('msg_type')!r}"
        )
        assert ev.payload["attachment_id"] == "https://wa.to/media/nested.jpg"
        assert ev.payload["attachment_info"] == "Foto dal nested!"

    def test_webhook_image_via_nested_message_imageMessage_no_text_key(self):
        """Nested imageMessage SENZA chiave 'text' nel payload (caso reale WAHA)."""
        backend = _make_backend("http://api.test")
        backend._contacts_by_jid = {}

        envelope = {
            "event": "message",
            "session": "default",
            "payload": {
                "id": "IMG_NESTED_NOTEXT_1",
                "from": "3912345678@c.us",
                "fromMe": False,
                "timestamp": 1700000004,
                # NOTA: nessuna chiave 'text' o 'body'!
                "message": {
                    "imageMessage": {
                        "url": "https://wa.to/media/no-text.jpg",
                        "mimetype": "image/jpeg",
                    },
                },
            },
        }

        ok = backend.handle_webhook(envelope)
        assert ok is True

        events = backend.poll_once()
        assert len(events) == 1
        ev = events[0]
        assert ev.payload["msg_type"] == "image"
        assert ev.payload["attachment_id"] == "https://wa.to/media/no-text.jpg"

        envelope = {
            "event": "message.any",
            "session": "default",
            "payload": {
                "id": "IMG_ANY_1",
                "from": "3912345678@c.us",
                "fromMe": False,
                "timestamp": 1700000001,
                "hasMedia": True,
                "media": {
                    "mimetype": "image/png",
                    "url": "https://wa.to/media/img.png",
                    "caption": "Screenshot",
                },
            },
        }

        ok = backend.handle_webhook(envelope)
        assert ok is True

        events = backend.poll_once()
        assert len(events) == 1
        ev = events[0]
        assert ev.payload["msg_type"] == "image"
        assert ev.payload["attachment_id"] == "https://wa.to/media/img.png"
        assert ev.payload["attachment_info"] == "Screenshot"

        # Verifica che ingest_message salvi correttamente.
        added = backend.ingest_message(
            "3912345678@c.us",
            ev.payload,
            ev.payload["timestamp"],
        )
        assert added is True
        cached = backend.cache["3912345678@c.us"]
        assert cached[0]["msg_type"] == "image"
        assert cached[0]["attachment_id"] == "https://wa.to/media/img.png"


# ─── Regression: seed cache from DB at startup ───────────────────────────────


class TestSeedCacheFromDB:
    """Il backend WhatsApp deve caricare i messaggi persistiti dal DB all'avvio.

    Prima del fix, ``connect_sync`` partiva con ``self.cache = {}`` (a differenza
    del backend Signal): la cache dell'UI restava vuota per i contatti WhatsApp e
    i messaggi già nel DB non comparivano finché ``fetch_history`` non li
    riscaricava (e li re-inseriva, duplicandoli).  Questo test verifica che
    ``connect_sync`` semini la cache dal DB, filtrando SOLO il protocollo
    WhatsApp.
    """

    def test_multipart_attachments_persist_and_seed_after_restart_without_schema_change(
        self, tmp_path, monkeypatch
    ):
        import backend as backend_mod

        monkeypatch.setattr(backend_mod, "DB_FILE", tmp_path / "messages.db")
        backend_mod._init_db()
        with sqlite3.connect(backend_mod.DB_FILE) as conn:
            before = (
                conn.execute("PRAGMA table_info(messages)").fetchall(),
                conn.execute("PRAGMA index_list(messages)").fetchall(),
            )

        backend = _make_backend()
        envelope = {
            "event": "message",
            "payload": {
                "id": "parent-persisted",
                "from": "1@c.us",
                "timestamp": 1700000000,
                "attachments": [
                    {"id": "media-1", "filename": "one.jpg", "mimetype": "image/jpeg"},
                    {"id": "media-2", "filename": "two.jpg", "mimetype": "image/jpeg"},
                ],
            },
        }
        backend.handle_webhook(envelope)
        for event in backend.poll_once():
            assert backend.ingest_message(
                event.contact_id, event.payload, event.payload["timestamp"]
            )

        restarted = _make_backend()
        restarted.cache = restarted._load_protocol_cache()
        assert [message["text"] for message in restarted.cache["1@c.us"]] == ["", ""]
        assert [message["id"] for message in restarted.cache["1@c.us"]] == [
            "parent-persisted",
            "parent-persisted",
        ]

        assert restarted.handle_webhook(envelope) is True
        for event in restarted.poll_once():
            assert not restarted.ingest_message(
                event.contact_id, event.payload, event.payload["timestamp"]
            )
        assert len(restarted.cache["1@c.us"]) == 2

        with sqlite3.connect(backend_mod.DB_FILE) as conn:
            after = (
                conn.execute("PRAGMA table_info(messages)").fetchall(),
                conn.execute("PRAGMA index_list(messages)").fetchall(),
            )
        assert after == before

    def test_single_media_variants_share_a_canonical_identity(
        self, tmp_path, monkeypatch
    ):
        import backend as backend_mod

        monkeypatch.setattr(backend_mod, "DB_FILE", tmp_path / "messages.db")
        backend_mod._init_db()
        backend = _make_backend()
        attachment_form = {
            "event": "message",
            "payload": {
                "id": "parent-media",
                "from": "1@c.us",
                "timestamp": 1700000000,
                "attachments": [
                    {
                        "id": "stable-media-id",
                        "filename": "original.jpg",
                        "caption": "First caption",
                        "mimetype": "image/jpeg",
                    }
                ],
            },
        }
        nested_form = {
            "event": "message",
            "payload": {
                "id": "parent-media",
                "from": "1@c.us",
                "timestamp": 1700000000,
                "message": {
                    "imageMessage": {
                        "id": "stable-media-id",
                        "filename": "renamed.png",
                        "caption": "Different caption",
                        "mimetype": "image/png",
                    }
                },
            },
        }

        assert backend.handle_webhook(attachment_form)
        for event in backend.poll_once():
            assert backend.ingest_message(
                event.contact_id, event.payload, event.payload["timestamp"]
            )
            assert event.payload["text"] == ""

        assert backend.handle_webhook(nested_form)
        assert backend.poll_once() == []
        assert len(backend.cache["1@c.us"]) == 1

        with sqlite3.connect(backend_mod.DB_FILE) as conn:
            assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1

        restarted = _make_backend()
        restarted.cache = restarted._load_protocol_cache()
        assert len(restarted.cache["1@c.us"]) == 1
        assert restarted.handle_webhook(nested_form)
        for event in restarted.poll_once():
            assert not restarted.ingest_message(
                event.contact_id, event.payload, event.payload["timestamp"]
            )
        assert len(restarted.cache["1@c.us"]) == 1

    def test_connect_sync_seeds_cache_from_db(self, tmp_path, monkeypatch):
        import time

        import backend as backend_mod

        # Isola il DB su un file temporaneo.
        monkeypatch.setattr(backend_mod, "DB_FILE", tmp_path / "messages.db")

        cid = "391234567890@s.whatsapp.net"
        ts = int(time.time() * 1000)
        backend_mod._add_message_to_cache(
            cid,
            "Ok  ci sentiamo",
            False,
            "Giovanni",
            ts,
            protocol=PROTOCOL_WHATSAPP,
        )
        # Un messaggio di un ALTRO protocollo non deve finire nella cache WhatsApp.
        backend_mod._add_message_to_cache(
            "+391234567890",
            "msg signal",
            False,
            "Mario",
            ts + 1,
            protocol="signal",
        )

        backend = _make_backend("http://api.test")
        # connect_sync tenta _load_contacts (REST): lo neutralizziamo per non
        # toccare la rete.  La ricezione è via webhook, quindi non c'è alcun
        # thread di polling/push da neutralizzare.
        with (
            patch.object(backend, "_load_contacts"),
            patch.object(backend, "_wait_session_ready", return_value=True),
            patch.object(backend, "_configure_webhook"),
        ):
            backend.connect_sync()

        msgs = backend.cache.get(cid, [])
        assert len(msgs) == 1
        assert msgs[0]["text"] == "Ok  ci sentiamo"
        assert msgs[0]["is_mine"] is False
        assert msgs[0]["timestamp"] == ts
        # Il messaggio Signal non deve essere presente nella cache WhatsApp.
        assert "+391234567890" not in backend.cache

    def test_connect_sync_dedup_prevents_db_duplicates(self, tmp_path, monkeypatch):
        """Con la cache seminata dal DB, fetch_history non re-inserisce i
        messaggi già persistiti (il dedup di ingest_message ora funziona anche
        tra sessioni)."""
        import time

        import backend as backend_mod

        monkeypatch.setattr(backend_mod, "DB_FILE", tmp_path / "messages.db")

        cid = "391234567890@s.whatsapp.net"
        ts = int(time.time() * 1000)
        backend_mod._add_message_to_cache(
            cid,
            "Ok  ci sentiamo",
            False,
            "Giovanni",
            ts,
            protocol=PROTOCOL_WHATSAPP,
        )

        backend = _make_backend("http://api.test")
        with (
            patch.object(backend, "_load_contacts"),
            patch.object(backend, "_wait_session_ready", return_value=True),
            patch.object(backend, "_configure_webhook"),
        ):
            backend.connect_sync()

        # Simula fetch_history che riscarica lo stesso messaggio remoto.
        added = backend.ingest_message(
            cid,
            {
                "id": "wa-msg-1",
                "text": "Ok  ci sentiamo",
                "is_mine": False,
                "sender": "Giovanni",
                "quote_text": None,
                "msg_type": "text",
                "attachment_info": None,
                "attachment_id": None,
            },
            ts,
        )
        assert added is False  # già in cache (dal DB) -> non duplicato

        # Il DB deve contenere ancora UNA sola copia.
        loaded = backend_mod._load_cache(protocol=PROTOCOL_WHATSAPP)
        assert len(loaded.get(cid, [])) == 1

    def test_db_seeded_cache_keeps_distinct_same_second_with_ids(
        self, tmp_path, monkeypatch
    ):
        """🛡️ Regressione "chat indietro": due messaggi DISTINTI con stesso testo
        E stesso secondo, uno persistito nel DB (con id), non devono essere fusi
        quando fetch_history li riscarica.

        Prima del fix, ``_load_cache`` non ricaricava l'id WhatsApp: le entry
        seminate dal DB avevano ``id=None``, quindi il dedup di ingest_message
        ricadeva su (testo, timestamp) e il secondo messaggio (stesso secondo +
        stesso testo) veniva scartato -> la chat appariva "indietro" quando
        veniva aperta.
        """
        import time

        import backend as backend_mod

        monkeypatch.setattr(backend_mod, "DB_FILE", tmp_path / "messages.db")

        cid = "391234567890@s.whatsapp.net"
        ts = int(time.time() * 1000)
        # Due messaggi distinti, stesso testo e stesso secondo, con id diversi.
        backend_mod._add_message_to_cache(
            cid,
            "ok",
            False,
            "Giovanni",
            ts,
            protocol=PROTOCOL_WHATSAPP,
            msg_id="wa-1",
        )
        backend_mod._add_message_to_cache(
            cid,
            "ok",
            False,
            "Giovanni",
            ts,
            protocol=PROTOCOL_WHATSAPP,
            msg_id="wa-2",
        )

        backend = _make_backend("http://api.test")
        with (
            patch.object(backend, "_load_contacts"),
            patch.object(backend, "_wait_session_ready", return_value=True),
            patch.object(backend, "_configure_webhook"),
        ):
            backend.connect_sync()

        # La cache seminata dal DB deve contenere ENTRAMBI i messaggi (con id).
        seeded = backend.cache.get(cid, [])
        assert len(seeded) == 2
        assert {m.get("id") for m in seeded} == {"wa-1", "wa-2"}

        # fetch_history riscarica gli stessi due messaggi: nessuno deve essere
        # scartato come falso duplicato.
        added1 = backend.ingest_message(
            cid,
            {
                "id": "wa-1",
                "text": "ok",
                "is_mine": False,
                "sender": "Giovanni",
                "quote_text": None,
                "msg_type": "text",
                "attachment_info": None,
                "attachment_id": None,
            },
            ts,
        )
        added2 = backend.ingest_message(
            cid,
            {
                "id": "wa-2",
                "text": "ok",
                "is_mine": False,
                "sender": "Giovanni",
                "quote_text": None,
                "msg_type": "text",
                "attachment_info": None,
                "attachment_id": None,
            },
            ts,
        )
        assert added1 is False  # già in cache (stesso id) -> dedup corretto
        assert added2 is False  # già in cache (stesso id) -> dedup corretto
        # Nessun duplicato aggiunto: il DB resta con 2 sole copie.
        loaded = backend_mod._load_cache(protocol=PROTOCOL_WHATSAPP)
        assert len(loaded.get(cid, [])) == 2

    def test_optimistic_send_echo_upgrades_id_no_duplicate(self, tmp_path, monkeypatch):
        """🛡️ Regressione "messaggi inviati duplicati": l'echo di un invio
        ottimistico (id=None) che arriva con l'id reale e timestamp distante
        (> finestra 5s) NON deve creare un duplicato, e deve aggiornare l'entry
        ottimistica con l'id reale.
        """
        import backend as backend_mod

        monkeypatch.setattr(backend_mod, "DB_FILE", tmp_path / "messages.db")

        cid = "391234567890@s.whatsapp.net"
        backend = _make_backend("http://api.test")
        with (
            patch.object(backend, "_load_contacts"),
            patch.object(backend, "_wait_session_ready", return_value=True),
            patch.object(backend, "_configure_webhook"),
        ):
            backend.connect_sync()

        # 1) Invio ottimistico dalla TUI: id sconosciuto (None), ts client.
        ts_opt = 1700000000000
        added = backend.ingest_message(
            cid,
            {
                "id": None,
                "text": "ciao",
                "is_mine": True,
                "sender": "You",
                "quote_text": None,
                "msg_type": "text",
                "attachment_info": None,
                "attachment_id": None,
            },
            ts_opt,
        )
        assert added is True

        # 2) Echo di WAHA: id reale, timestamp molto più tardi (> 5s).
        ts_echo = ts_opt + 60000  # 60s dopo -> fuori dalla finestra di dedup
        added_echo = backend.ingest_message(
            cid,
            {
                "id": "wa-echo-1",
                "text": "ciao",
                "is_mine": True,
                "sender": "You",
                "quote_text": None,
                "msg_type": "text",
                "attachment_info": None,
                "attachment_id": None,
            },
            ts_echo,
        )
        # Non deve essere aggiunto come nuovo messaggio.
        assert added_echo is False

        # L'entry ottimistica è stata aggiornata con l'id reale.
        cached = backend.cache.get(cid, [])
        assert len(cached) == 1
        assert cached[0]["id"] == "wa-echo-1"
        assert cached[0]["timestamp"] == ts_echo

        # Il DB contiene una sola copia, con l'id reale.
        loaded = backend_mod._load_cache(protocol=PROTOCOL_WHATSAPP)
        assert len(loaded.get(cid, [])) == 1
        assert loaded[cid][0]["id"] == "wa-echo-1"

    def test_legacy_idless_sent_does_not_swallow_new_message(self):
        """🛡️ Regressione "chat incompleta": un'entry SENT senza id (legacy,
        pre-fix, molto vecchia) NON deve "inghiottire" un messaggio mio
        genuinamente nuovo (es. inviato da un altro client) con lo stesso testo.

        Prima il fallback per testo abbinava QUALSIASI entry senza id con lo
        stesso testo, indipendentemente dall'età: un'entry legacy di giorni fa
        faceva scartare un messaggio nuovo arrivato da un altro client -> la
        chat risultava incompleta (il messaggio era nel DB remoto ma non
        veniva mai mostrato).  Ora il fallback è limitato a una finestra
        temporale (``_ECHO_MATCH_WINDOW_MS``).
        """
        import backend as backend_mod

        backend = _make_backend()
        cid = "391234567890@s.whatsapp.net"
        # Entry legacy SENT senza id, molto vecchia (es. 1 giorno fa).
        backend.cache[cid] = [
            {
                "id": None,
                "text": "ciao",
                "is_mine": True,
                "sender": "You",
                "timestamp": 1700000000000,
                "quote_text": None,
                "msg_type": "text",
                "attachment_info": None,
                "attachment_id": None,
            }
        ]
        # Messaggio mio NUOVO (da un altro client) con lo stesso testo, id reale.
        ts_new = 1700000000000 + 24 * 3600 * 1000  # 1 giorno dopo
        with patch.object(backend_mod, "_add_message_to_cache") as mock_add:
            added = backend.ingest_message(
                cid,
                {
                    "id": "wa-new-1",
                    "text": "ciao",
                    "is_mine": True,
                    "sender": "You",
                    "quote_text": None,
                    "msg_type": "text",
                    "attachment_info": None,
                    "attachment_id": None,
                },
                ts_new,
            )
        # Deve essere aggiunto come messaggio NUOVO (non scartato come echo).
        assert added is True
        mock_add.assert_called_once()
        assert len(backend.cache[cid]) == 2

    def test_echo_with_nested_key_id_deduped_and_upgrades(self):
        """🛡️ Regressione "messaggi duplicati": un echo il cui id è annidato
        sotto ``key.id`` (WAHA) arriva via webhook e viene riconosciuto come
        duplicato (non aggiunto di nuovo) e aggiorna l'entry ottimistica senza id.

        Prima il polling ``GET /api/messages`` leggeva solo ``m.get("id")``: con
        l'id annidato sotto ``key.id`` l'echo veniva deduplicato col solo
        fallback timestamp (finestra 5s) e, se il ts server distava più di 5s
        dal ts client, veniva aggiunto di nuovo -> doppione.  Ora la ricezione
        è via webhook: ``_event_from_message`` estrae il nested ``key.id`` e
        ``handle_webhook`` lo registra in ``_seen_msg_ids``.
        """
        import time

        import backend as backend_mod

        backend = _make_backend()
        cid = "391234567890@s.whatsapp.net"
        ts_echo = int(time.time() * 1000)  # timestamp recenti
        raw = {
            "event": "message",
            "session": "default",
            "payload": {
                "chatId": cid,
                "fromMe": True,
                "key": {"id": "wa-echo-nested"},
                "body": "ciao",
                "timestamp": ts_echo // 1000,
            },
        }
        ok = backend.handle_webhook(raw)
        assert ok is True
        # L'echo è accodato (un evento message) e l'id annidato è stato estratto.
        events = backend.poll_once()
        assert len(events) == 1
        assert events[0].payload["id"] == "wa-echo-nested"
        assert events[0].payload["is_mine"] is True
        # La chiave effimera include id padre e testo (non ri-processato su retry).
        assert (cid, "wa-echo-nested", "ciao", "") in backend._seen_message_keys
        # Prima: entry ottimistica senza id, poi ingest dello stesso echo:
        # il dedup per id in ingest_message NON deve creare un duplicato.
        backend.cache[cid] = [
            {
                "id": None,
                "text": "ciao",
                "is_mine": True,
                "sender": "You",
                "timestamp": ts_echo,
                "quote_text": None,
                "msg_type": "text",
                "attachment_info": None,
                "attachment_id": None,
            }
        ]
        with patch.object(backend_mod, "_update_message_id") as mock_upd:
            added = backend.ingest_message(
                cid,
                {
                    "id": "wa-echo-nested",
                    "text": "ciao",
                    "is_mine": True,
                    "sender": "You",
                    "quote_text": None,
                    "msg_type": "text",
                    "attachment_info": None,
                    "attachment_id": None,
                },
                ts_echo + 60000,
            )
        assert added is False  # duplicato (echo) -> non aggiunto
        # L'entry ottimistica è stata aggiornata con l'id reale.
        assert len(backend.cache[cid]) == 1
        assert backend.cache[cid][0]["id"] == "wa-echo-nested"
        assert backend.cache[cid][0]["timestamp"] >= ts_echo
        mock_upd.assert_called_once()

    def test_load_contacts_single_call(self):
        """_load_contacts fa una singola chiamata a list_contacts() (refactoring opt/wa-link-profile)."""
        backend = _make_backend()
        mock = MagicMock(return_value=[{"id": "wa:1@s.whatsapp.net", "name": "Mario"}])
        with patch.object(backend._rest, "list_contacts", new=mock):
            backend._load_contacts()
        assert len(backend.contacts) == 1
        assert backend.contacts[0].id == "wa:1@s.whatsapp.net"
        assert mock.call_count == 1

    def test_load_contacts_stays_empty_after_single_call(self):
        """Se list_contacts restituisce vuoto, 0 contatti (best-effort, nessuna eccezione)."""
        backend = _make_backend()
        mock = MagicMock(return_value=[])
        with patch.object(backend._rest, "list_contacts", new=mock):
            backend._load_contacts()
        assert backend.contacts == []


# ─── Webhook self-registration (PUT session config) ───────────────────────────


class TestWhatsAppWebhookRegistration:
    """Il backend registra il webhook push sulla sessione WAHA (PUT config).

    Il solo ``WAHA_WEBHOOK_URL`` (env) non fa emettere gli eventi a WAHA: bisogna
    registrare la ``config.webhooks`` sulla sessione via ``PUT /api/sessions/{name}``.
    ``_configure_webhook`` lo fa all'avvio, senza fare restart se gia' presente.
    """

    def test_update_session_config_puts_config(self):
        client = WhatsAppRESTClient("http://api.test")
        seen = []

        def fake_urlopen(req, timeout=30):
            seen.append((req.method, req.full_url, json.loads(req.data or b"{}")))
            resp = MagicMock()
            resp.read.return_value = b"{}"
            resp.status = 200
            ctx = MagicMock()
            ctx.__enter__.return_value = resp
            return ctx

        with patch("urllib.request.urlopen", fake_urlopen):
            client.update_session_config(
                {
                    "config": {
                        "webhooks": [{"url": "http://x/webhook", "events": ["message"]}]
                    }
                }
            )

        assert len(seen) == 1
        method, url, payload = seen[0]
        assert method == "PUT"
        assert url == "http://api.test/api/sessions/default"
        assert payload["config"]["webhooks"][0]["url"] == "http://x/webhook"
        assert payload["config"]["webhooks"][0]["events"] == ["message"]

    def test_configure_webhook_registers_when_missing(self):
        import backends.whatsapp as wa_mod

        backend = _make_backend()
        webhook = "http://host.docker.internal:8090/webhook"
        # La sessione non ha ancora il webhook -> va eseguito il PUT.
        with (
            patch.object(
                backend._rest, "get_session_status", return_value={"config": {}}
            ),
            patch.object(backend._rest, "update_session_config") as mock_put,
            patch.object(wa_mod, "get_whatsapp_webhook_url", return_value=webhook),
        ):
            backend._configure_webhook()
        mock_put.assert_called_once()
        call_config = mock_put.call_args[0][0]
        assert call_config["config"]["webhooks"][0]["url"] == webhook
        assert call_config["config"]["webhooks"][0]["events"] == [
            "message",
            "message.any",
            "message.ack",
            "message.ack.group",
            "presence.update",
            "message.reaction",
        ]

    def test_configure_webhook_skips_when_already_registered(self):
        import backends.whatsapp as wa_mod

        backend = _make_backend()
        webhook = "http://host.docker.internal:8088/webhook"
        # Config già aggiornata con entrambi gli eventi → nessun PUT.
        with (
            patch.object(
                backend._rest,
                "get_session_status",
                return_value={
                    "config": {
                        "webhooks": [
                            {
                                "url": webhook,
                                "events": [
                                    "message",
                                    "message.any",
                                    "message.ack",
                                    "message.ack.group",
                                    "presence.update",
                                    "message.reaction",
                                ],
                            }
                        ]
                    }
                },
            ),
            patch.object(backend._rest, "update_session_config") as mock_put,
            patch.object(wa_mod, "get_whatsapp_webhook_url", return_value=webhook),
        ):
            backend._configure_webhook()
        # Gia' configurato con tutti gli eventi → nessun PUT.
        mock_put.assert_not_called()

    def test_configure_webhook_updates_when_events_outdated(self):
        """Se l'URL c'è ma manca message.ack, esegue comunque il PUT."""
        import backends.whatsapp as wa_mod

        backend = _make_backend()
        webhook = "http://host.docker.internal:8088/webhook"
        with (
            patch.object(
                backend._rest,
                "get_session_status",
                return_value={
                    "config": {"webhooks": [{"url": webhook, "events": ["message"]}]}
                },
            ),
            patch.object(backend._rest, "update_session_config") as mock_put,
            patch.object(wa_mod, "get_whatsapp_webhook_url", return_value=webhook),
        ):
            backend._configure_webhook()
        # Config vecchia (solo message) → PUT con tutti gli eventi.
        mock_put.assert_called_once()
        call_config = mock_put.call_args[0][0]
        assert call_config["config"]["webhooks"][0]["events"] == [
            "message",
            "message.any",
            "message.ack",
            "message.ack.group",
            "presence.update",
            "message.reaction",
        ]

    def test_configure_webhook_never_raises_on_error(self):
        import backends.whatsapp as wa_mod

        backend = _make_backend()
        with (
            patch.object(
                backend._rest, "get_session_status", side_effect=RuntimeError("boom")
            ),
            patch.object(backend._rest, "update_session_config") as mock_put,
            patch.object(
                wa_mod, "get_whatsapp_webhook_url", return_value="http://x/webhook"
            ),
        ):
            backend._configure_webhook()  # non deve sollevare
        mock_put.assert_not_called()

    def test_configure_webhook_noop_without_rest(self):
        import backends.whatsapp as wa_mod

        backend = _make_backend()
        backend._rest = None
        with patch.object(
            wa_mod, "get_whatsapp_webhook_url", return_value="http://x/webhook"
        ):
            backend._configure_webhook()  # nessuna eccezione

    def test_init_registers_webhook_from_connect_sync(self):
        """connect_sync delega la registrazione a _configure_webhook."""
        backend = _make_backend("http://api.test")
        with (
            patch.object(backend, "_load_contacts"),
            patch.object(backend, "_wait_session_ready", return_value=True),
            patch.object(backend, "_configure_webhook") as mock_cfg,
        ):
            backend.connect_sync()
        mock_cfg.assert_called_once()


class TestWhatsAppQuoteMedia:
    """🖼️ Bug #37 — quote di un media WhatsApp → segnaposto tipizzato (Decisione A)."""

    def _raw(self, quote: dict) -> dict:
        return {
            "chatId": "391234567890@c.us",
            "from": "391234567890@c.us",
            "text": "rispondo",
            "timestamp": 1700000000,
            "quotedMessage": quote,
        }

    @pytest.mark.parametrize(
        ("media_key", "expected"),
        [
            ("imageMessage", "🖼️ Immagine"),
            ("videoMessage", "🎬 Video"),
            ("audioMessage", "🎵 Audio"),
            ("documentMessage", "📎 File"),
            ("stickerMessage", "🎨 Sticker"),
        ],
    )
    def test_wa_quote_nested_media_placeholders(self, media_key, expected):
        """Chiavi annidate ``*Message`` nella quote → segnaposto tipizzato."""
        ev = _msg(self._raw({media_key: {"id": "media-id"}}))
        assert ev.payload["quote_text"] == expected

    def test_wa_quote_sticker_fixture(self, wa_event_quoting_sticker):
        """La fixture ``quotedMessage.stickerMessage`` → "🎨 Sticker"."""
        ev = _msg(wa_event_quoting_sticker)
        assert ev.payload["quote_text"] == "🎨 Sticker"

    @pytest.mark.parametrize(
        ("quote", "expected"),
        [
            ({"type": "image"}, "🖼️ Immagine"),
            ({"type": "video"}, "🎬 Video"),
            ({"type": "audio"}, "🎵 Audio"),
            ({"type": "sticker"}, "🎨 Sticker"),
            ({"type": "document"}, "📎 File"),
            ({"mimetype": "image/png"}, "🖼️ Immagine"),
            ({"mimetype": "video/mp4"}, "🎬 Video"),
            ({"mimetype": "audio/ogg"}, "🎵 Audio"),
            ({"mimetype": "application/pdf"}, "📎 File"),
        ],
    )
    def test_wa_quote_flat_type_and_mimetype(self, quote, expected):
        """Detection piatta (``type`` o ``mimetype``) → segnaposto tipizzato."""
        ev = _msg(self._raw(quote))
        assert ev.payload["quote_text"] == expected

    @pytest.mark.parametrize(
        "quote",
        [
            {"caption": "testo reale", "imageMessage": {"id": "x"}},
            {"filename": "testo reale", "type": "image"},
            {"media": {"filename": "testo reale"}, "mimetype": "image/png"},
            {"documentMessage": {"description": "testo reale"}},
            {"body": "testo reale", "imageMessage": {"id": "x"}},
            {"text": "testo reale", "type": "image"},
            {"conversation": "testo reale", "mimetype": "image/png"},
        ],
    )
    def test_wa_quote_media_uses_explicit_description(self, quote):
        """I media usano solo filename/caption/description espliciti di WAHA."""
        ev = _msg(self._raw(quote))
        assert ev.payload["quote_text"] == "testo reale"

    def test_waha_reply_to_image_base64_uses_placeholder(self):
        jpeg_base64 = "/9j/4AAQSkZJRgABAQ" + "A" * 256
        raw = self._raw({})
        raw.pop("quotedMessage")
        raw["replyTo"] = {
            "id": "quoted-image-id",
            "body": jpeg_base64,
            "hasMedia": True,
            "media": {"mimetype": "image/jpeg", "data": jpeg_base64},
        }

        ev = _msg(raw)

        assert ev.payload["quote_text"] == "🖼️ Immagine"
        assert jpeg_base64 not in ev.payload["quote_text"]

    def test_wa_quote_unknown_is_none(self):
        """Quote senza segnali media e senza testo → nessuna bolla."""
        ev = _msg(self._raw({"id": "quoted-id"}))
        assert ev.payload["quote_text"] is None


def test_waha_reply_to_echo_normalizes_text_quote_metadata():
    event = _raw(
        {
            "event": "message.any",
            "payload": {
                "id": "true_391234567890@c.us_ECHO",
                "chatId": "391234567890@c.us",
                "fromMe": True,
                "body": "risposta",
                "timestamp": 1700000001,
                "replyTo": {
                    "id": "false_391234567890@c.us_ORIGINAL",
                    "participant": " 391234567890@c.us\n",
                    "body": " \ndomanda su\npiù righe\n ",
                    "timestamp": 1700000000,
                    "hasMedia": False,
                },
            },
        }
    )

    assert event.payload["quote_text"] == "domanda su\npiù righe"
    assert event.payload["quote_timestamp"] == 1700000000000
    assert event.payload["quote_author"] == "391234567890@c.us"
    assert event.payload["reply_to_message_id"] == ("false_391234567890@c.us_ORIGINAL")


def test_tui_optimistic_reply_is_upgraded_by_waha_echo_without_quote_loss():
    backend = _make_backend()
    optimistic = {
        "text": "risposta",
        "is_mine": True,
        "sender": "You",
        "quote_text": "domanda",
        "quote_timestamp": 1700000000000,
        "quote_author": "391234567890@c.us",
        "msg_type": "text",
    }
    assert backend.ingest_message(
        "391234567890@c.us", optimistic, 1700000001000, persist=False
    )
    echo = _msg(
        {
            "id": "true_391234567890@c.us_ECHO",
            "chatId": "391234567890@c.us",
            "fromMe": True,
            "body": "risposta",
            "timestamp": 1700000002,
            "replyTo": {"body": "domanda"},
        }
    )

    assert not backend.ingest_message(
        echo.contact_id, echo.payload, echo.payload["timestamp"], persist=False
    )
    assert len(backend.cache[echo.contact_id]) == 1
    assert backend.cache[echo.contact_id][0]["quote_text"] == "domanda"


# ─── Architecture-fix regressions (WAHA fetch/logging/read receipt) ───────────


def test_fetch_history_rejects_non_fetchable_jid_without_rest_call():
    backend = _make_backend()
    backend._rest.list_messages = MagicMock(return_value=[])

    assert backend.fetch_history("db@lid") == []

    backend._rest.list_messages.assert_not_called()


def test_fetch_history_negative_caches_no_lid_500_for_session():
    backend = _make_backend()
    backend._rest.list_messages = MagicMock(return_value=[])
    backend._rest.last_status = 500
    backend._rest.last_error = "No LID for user 123"

    assert backend.fetch_history("123@c.us") == []
    assert backend.fetch_history("123@c.us") == []

    backend._rest.list_messages.assert_called_once_with("123@c.us", limit=20)


def test_request_http_error_logs_local_status_and_single_line(caplog):
    import io
    import logging
    import urllib.error

    client = WhatsAppRESTClient("http://api.test")
    client.last_status = 200
    error = urllib.error.HTTPError(
        "http://api.test/api/messages",
        500,
        "Internal Server Error",
        {},
        io.BytesIO(b'{"detail":"first line\\nsecond line"}'),
    )

    with (
        caplog.at_level(logging.ERROR, logger="backends.whatsapp_rest"),
        patch("urllib.request.urlopen", side_effect=error),
    ):
        assert client._request("GET", "/api/messages") is None

    record = next(r for r in caplog.records if "WAHA request failed" in r.getMessage())
    assert "status=500" in record.getMessage()
    assert "status=200" not in record.getMessage()
    assert "first line second line" in record.getMessage()
    assert "\n" not in record.getMessage()


def test_mark_read_uses_send_seen_and_404_still_marks_local(caplog):
    import io
    import logging
    import urllib.error

    backend = _make_backend()
    captured = {}

    def not_found(req, timeout=30):
        captured["method"] = req.method
        captured["path"] = req.full_url
        captured["payload"] = json.loads(req.data)
        raise urllib.error.HTTPError(
            req.full_url,
            404,
            "Not Found",
            {},
            io.BytesIO(b'{"detail":"endpoint unavailable"}'),
        )

    with (
        caplog.at_level(logging.DEBUG, logger="backends.whatsapp_rest"),
        patch("urllib.request.urlopen", side_effect=not_found),
        patch("backend._mark_as_read") as mark_local,
    ):
        backend.mark_read_sync("123@c.us")

    assert captured == {
        "method": "POST",
        "path": "http://api.test/api/sendSeen",
        "payload": {"session": "default", "chatId": "123@c.us"},
    }
    mark_local.assert_called_once_with("123@c.us", protocol=PROTOCOL_WHATSAPP)
    records = [r for r in caplog.records if "path=/api/sendSeen" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
