"""
Regression tests for the WhatsApp backend (backends/whatsapp.py) and the
optional configuration gating (backends/config.py).

REST endpoints are mocked via ``urllib.request.urlopen`` (same style as the
Signal RPC tests); the WebSocket stream is exercised through the internal
event-queue + ``poll_once`` surface.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from models import PROTOCOL_WHATSAPP, contact_cache_key
from backends.whatsapp import (
    WhatsAppBackend,
    WhatsAppRESTClient,
    _event_from_message,
    _event_from_receipt,
    _event_from_typing,
    _event_from_raw,
)


def _json_response(payload):
    """Return a mock urlopen context manager that yields a JSON body."""
    data = json.dumps(payload).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = data
    mock_urlopen = MagicMock()
    mock_urlopen.return_value.__enter__.return_value = mock_resp
    return mock_urlopen


def _make_backend(api_url: str = "http://api.test", media_dir: str = "") -> WhatsAppBackend:
    return WhatsAppBackend(api_url=api_url, media_dir=media_dir)


# ─── WhatsAppRESTClient ───────────────────────────────────────────────────────

class TestWhatsAppRESTClient:
    """🔌 Client REST verso l'API Baileys."""

    def test_protocol(self):
        assert WhatsAppBackend.protocol == PROTOCOL_WHATSAPP

    def test_list_contacts(self):
        client = WhatsAppRESTClient("http://api.test")
        with patch("urllib.request.urlopen", _json_response([
            {"id": "wa:39123@s.whatsapp.net", "name": "Mario"},
            {"id": "wa:45678@s.whatsapp.net", "name": "Luigi"},
        ])):
            contacts = client.list_contacts()
        assert len(contacts) == 2
        assert contacts[0]["id"] == "wa:39123@s.whatsapp.net"

    def test_list_contacts_nested_data(self):
        client = WhatsAppRESTClient("http://api.test")
        with patch("urllib.request.urlopen", _json_response(
            {"data": [{"id": "wa:x@s.whatsapp.net", "name": "A"}]}
        )):
            contacts = client.list_contacts()
        assert len(contacts) == 1

    def test_get_pairing_qr(self):
        client = WhatsAppRESTClient("http://api.test")
        with patch("urllib.request.urlopen", _json_response(
            {"status": "pending", "qr": "2@ABC123"}
        )):
            qr = client.get_pairing_qr()
        assert qr == "2@ABC123"

    def test_send_message(self):
        client = WhatsAppRESTClient("http://api.test")
        with patch("urllib.request.urlopen", _json_response({"id": "msg-1"})):
            result = client.send_message("wa:x@s.whatsapp.net", "Ciao!")
        assert result == {"id": "msg-1"}

    def test_send_message_error_returns_none(self):
        client = WhatsAppRESTClient("http://api.test")

        def boom(*a, **k):
            import urllib.error
            raise urllib.error.URLError("refused")

        with patch("urllib.request.urlopen", boom):
            result = client.send_message("wa:x@s.whatsapp.net", "Ciao!")
        assert result is None


# ─── Event normalization ──────────────────────────────────────────────────────

class TestWhatsAppEvents:
    """📨 Normalizzazione dei WebSocket frame in ChatEvent."""

    def test_message_event(self):
        ev = _event_from_message({
            "id": "m1", "from": "wa:39123@s.whatsapp.net", "timestamp": 1700000000,
            "text": "ciao", "pushName": "Mario", "fromMe": False,
        })
        assert ev is not None
        assert ev.type == "message"
        assert ev.protocol == PROTOCOL_WHATSAPP
        assert ev.contact_id == "wa:39123@s.whatsapp.net"
        assert ev.payload["text"] == "ciao"
        assert ev.payload["timestamp"] == 1700000000000  # seconds → ms
        assert ev.payload["is_mine"] is False

    def test_typing_event(self):
        ev = _event_from_typing({"from": "wa:39123@s.whatsapp.net", "presence": "composing"})
        assert ev is not None
        assert ev.type == "typing"
        assert ev.payload["action"] == "STARTED"

    def test_receipt_event(self):
        ev = _event_from_receipt({
            "from": "wa:39123@s.whatsapp.net",
            "receipt": {"messageIds": ["m1"], "type": "read"},
        })
        assert ev is not None
        assert ev.type == "receipt"
        assert ev.payload["message_ids"] == ["m1"]
        assert ev.payload["is_read"] is True

    def test_dispatch_by_event_type(self):
        raw = {"event": "messages.upsert", "from": "wa:1@s.whatsapp.net", "text": "hi", "timestamp": 1}
        ev = _event_from_raw(raw)
        assert ev is not None and ev.type == "message"


# ─── WhatsAppBackend behaviour ────────────────────────────────────────────────

class TestWhatsAppBackend:
    """📱 Comportamento del backend (contatti, invio, pairing, media)."""

    def test_list_contacts_from_rest(self):
        backend = _make_backend()
        with patch.object(backend._rest, "list_contacts", return_value=[
            {"id": "wa:39123@s.whatsapp.net", "name": "Mario"},
        ]):
            backend._load_contacts()
        assert len(backend.contacts) == 1
        assert backend.contacts[0].id == "wa:39123@s.whatsapp.net"
        assert backend.contacts[0].cache_key == contact_cache_key(
            PROTOCOL_WHATSAPP, "wa:39123@s.whatsapp.net"
        )

    def test_send_message_sync_calls_rest(self):
        backend = _make_backend()
        with patch.object(backend._rest, "send_message", return_value={"id": "m"}) as mock_send:
            ts = backend.send_message_sync("wa:1@s.whatsapp.net", "Ciao!")
        mock_send.assert_called_once()
        assert ts > 0

    def test_send_message_sync_raises_when_unreachable(self):
        backend = _make_backend()
        backend._rest.send_message = MagicMock(return_value=None)
        with pytest.raises(RuntimeError):
            backend.send_message_sync("wa:1@s.whatsapp.net", "x")

    def test_needs_pairing(self):
        backend = _make_backend()
        with patch.object(backend._rest, "get_session_status", return_value={"status": "pending"}):
            assert backend.needs_pairing is True
        with patch.object(backend._rest, "get_session_status", return_value={"status": "connected"}):
            assert backend.needs_pairing is False

    def test_get_attachment_path(self, tmp_path):
        media = tmp_path / "media"
        media.mkdir()
        (media / "att-1.jpg").write_bytes(b"data")
        backend = _make_backend(media_dir=str(media))
        p = backend.get_attachment_path("att-1.jpg")
        assert p is not None and p.exists()
        assert backend.get_attachment_path("missing.jpg") is None

    def test_poll_once_drains_queue(self):
        backend = _make_backend()
        ev = _event_from_message({"id": "m1", "from": "wa:1@s.whatsapp.net", "text": "hi", "timestamp": 1})
        backend._enqueue_event(ev)
        events = backend.poll_once()
        assert len(events) == 1
        assert events[0].type == "message"
        # Second poll is empty.
        assert backend.poll_once() == []

    def test_ingest_message_dedup(self):
        import backend as backend_mod
        backend = _make_backend()
        data = {"text": "ciao", "is_mine": True, "sender": "You", "timestamp": 1000,
                "quote_text": None, "msg_type": "text", "attachment_info": None, "attachment_id": None}
        with patch.object(backend_mod, "_add_message_to_cache") as mock_add:
            assert backend.ingest_message("wa:1@s.whatsapp.net", data, 1000) is True
            assert backend.ingest_message("wa:1@s.whatsapp.net", data, 1050) is False
            mock_add.assert_called_once()
        assert mock_add.call_args.kwargs["protocol"] == PROTOCOL_WHATSAPP


# ─── Optional configuration gating ────────────────────────────────────────────

class TestWhatsAppConfigGating:
    """⚙️ WhatsApp backend è opzionale (non rompe la modalità solo-Signal)."""

    def test_disabled_when_no_api_url_and_no_local(self):
        from backends import config
        with patch.object(config, "get_whatsapp_api_url", return_value=""), \
             patch.object(config, "_local_waha_reachable", return_value=False):
            assert config.whatsapp_enabled() is False

    def test_enabled_with_api_url(self):
        from backends import config
        with patch.object(config, "get_whatsapp_api_url", return_value="http://127.0.0.1:3000"):
            assert config.whatsapp_enabled() is True

    def test_enabled_when_local_waha_detected(self):
        """Auto-detect: WAHA locale raggiungibile abilita il backend anche senza URL."""
        from backends import config
        with patch.object(config, "get_whatsapp_api_url", return_value=""), \
             patch.object(config, "_local_waha_reachable", return_value=True):
            assert config.whatsapp_enabled() is True

    def test_resolve_whatsapp_api_url_prefers_configured(self):
        from backends import config
        with patch.object(config, "get_whatsapp_api_url", return_value="http://waha:9999"):
            assert config.resolve_whatsapp_api_url() == "http://waha:9999"

    def test_resolve_whatsapp_api_url_falls_back_to_local_port(self):
        from backends import config
        with patch.object(config, "get_whatsapp_api_url", return_value=""), \
             patch.object(config, "get_whatsapp_api_port", return_value=3005):
            assert config.resolve_whatsapp_api_url() == "http://127.0.0.1:3005"



# ─── WAHA (WhatsApp HTTP API) contract ────────────────────────────────────────

class TestWAHAContract:
    """📨 Mapping degli endpoint reali di WAHA (devlikeapro/waha)."""

    def test_ws_url_uses_api_server(self):
        backend = _make_backend("http://api.test")
        url = backend._ws_url()
        assert url == "ws://api.test/api/default/server"

    def test_rest_paths_are_api_prefixed(self):
        """I path REST usano il prefisso /api come da contratto WAHA."""
        import urllib.request

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
            client.get_session_qr()

        assert any(m == "GET" and "/api/contacts" in u for m, u in seen)
        assert any(m == "POST" and "/api/sendText" in u for m, u in seen)
        assert any(m == "GET" and "/api/sessions" in u and "/qr" in u for m, u in seen)

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
        ev = _event_from_raw(frame)
        assert ev is not None
        assert ev.type == "message"
        assert ev.contact_id == "39123@s.whatsapp.net"
        assert ev.payload["text"] == "ciao da WAHA"
        assert ev.payload["is_mine"] is False

