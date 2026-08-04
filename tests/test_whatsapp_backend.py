"""
Regression tests for the WhatsApp backend (backends/whatsapp.py) and the
optional configuration gating (backends/config.py).

REST endpoints are mocked via ``urllib.request.urlopen`` (same style as the
Signal RPC tests); the WebSocket stream is exercised through the internal
event-queue + ``poll_once`` surface.
"""

from __future__ import annotations

import json
import os
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
        seen = []

        def fake_urlopen(req, timeout=30):
            seen.append((req.method, req.full_url))
            resp = MagicMock()
            resp.read.return_value = json.dumps([
                {"id": "wa:39123@s.whatsapp.net", "name": "Mario"},
                {"id": "wa:45678@s.whatsapp.net", "name": "Luigi"},
            ]).encode("utf-8")
            resp.status = 200
            ctx = MagicMock()
            ctx.__enter__.return_value = resp
            return ctx

        with patch("urllib.request.urlopen", fake_urlopen):
            contacts = client.list_contacts()

        assert len(contacts) == 2
        assert contacts[0]["id"] == "wa:39123@s.whatsapp.net"
        # L'endpoint contatti deve passare la sessione (su WAHA è obbligatoria).
        assert any("?session=default" in u for m, u in seen)
        assert any("/api/contacts?" in u for m, u in seen)

    def test_list_contacts_nested_data(self):
        client = WhatsAppRESTClient("http://api.test")
        with patch("urllib.request.urlopen", _json_response(
            {"data": [{"id": "wa:x@s.whatsapp.net", "name": "A"}]}
        )):
            contacts = client.list_contacts()
        assert len(contacts) == 1

    def test_list_contacts_falls_back_to_chats(self):
        """Su WAHA core /api/contacts è rotto (500) -> fallback su /api/{sess}/chats."""
        client = WhatsAppRESTClient("http://api.test")
        seen = []

        def fake_urlopen(req, timeout=30):
            seen.append((req.method, req.full_url))
            resp = MagicMock()
            resp.status = 200
            if req.full_url.endswith("/api/contacts?session=default"):
                # contatti endpoint: restituisce un 500/None (JSON non gestito) -> fallback
                resp.read.return_value = b'{"statusCode":500,"error":"Internal Server Error"}'
            else:
                # chats endpoint: lista di chat/contatti
                resp.read.return_value = json.dumps([
                    {"id": {"_serialized": "3112@c.us"}, "name": "Anna", "isGroup": False},
                    {"id": {"_serialized": "139153@lid"}, "pushName": "Bob", "isGroup": False},
                ]).encode("utf-8")
            ctx = MagicMock()
            ctx.__enter__.return_value = resp
            return ctx

        with patch("urllib.request.urlopen", fake_urlopen):
            contacts = client.list_contacts()

        assert any("/api/contacts?session=default" in u for m, u in seen)
        assert any("/api/default/chats" in u for m, u in seen)
        # Il fallback mappa chats -> {id, name}.
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
                resp.read.return_value = json.dumps([
                    {"id": {"_serialized": "3112@c.us"}, "name": "Anna",
                     "t": 1700000000},
                    {"id": {"_serialized": "139153@lid"}, "pushName": "Bob",
                     "last_message": {"timestamp": 1750000000000}},
                    {"id": {"_serialized": "399@lid"}, "name": "Carla",
                     "isGroup": False},   # nessun timestamp -> last_ts 0
                ]).encode("utf-8")
            ctx = MagicMock()
            ctx.__enter__.return_value = resp
            return ctx

        with patch("urllib.request.urlopen", fake_urlopen):
            contacts = client.list_contacts()

        assert len(contacts) == 3
        assert contacts[0]["last_ts"] == 1700000000000   # t in secondi * 1000
        assert contacts[1]["last_ts"] == 1750000000000    # last_message.timestamp (ms)
        assert contacts[2]["last_ts"] == 0                 # assente -> 0

    def test_get_pairing_qr_text_fallback(self):
        """QA testuale (WAHA vecchio): get_session_qr cade sul ramo JSON."""
        client = WhatsAppRESTClient("http://api.test")
        # _request_raw (PNG binario) non disponibile -> fallisce sul path JSON.
        with patch.object(client, "_request_raw", return_value=None), \
             patch("urllib.request.urlopen", _json_response(
                 {"status": "pending", "qr": "2@ABC123"}
             )):
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
        with patch.object(client, "_request_raw", side_effect=[None, b"\x89PNG-img"]), \
             patch.object(client, "start_session",
                          side_effect=lambda: calls.append("start")), \
             patch("urllib.request.urlopen", _json_response({})):
            qr = client.get_pairing_qr()
        assert calls == ["start"]
        assert qr == b"\x89PNG-img"

    def test_get_fresh_pairing_qr_resets_then_starts(self):
        """get_fresh_pairing_qr abbatte la sessione (QR fresco) prima di ripartire."""
        client = WhatsAppRESTClient("http://api.test")
        calls = []
        with patch.object(client, "reset_session",
                          side_effect=lambda logout=True: calls.append("reset")), \
             patch.object(client, "start_session",
                          side_effect=lambda: calls.append("start")), \
             patch.object(client, "get_session_qr", return_value=b"\x89PNG-fresh"):
            qr = client.get_fresh_pairing_qr(reset=True)
        assert calls == ["reset", "start"]
        assert qr == b"\x89PNG-fresh"

    def test_get_fresh_pairing_qr_no_reset(self):
        """Con reset=False get_fresh_pairing_qr riparte senza abbatte la sessione."""
        client = WhatsAppRESTClient("http://api.test")
        calls = []
        with patch.object(client, "reset_session",
                          side_effect=lambda logout=True: calls.append("reset")), \
             patch.object(client, "start_session",
                          side_effect=lambda: calls.append("start")), \
             patch.object(client, "get_session_qr", return_value="2@fresh"):
            qr = client.get_fresh_pairing_qr(reset=False)
        assert calls == ["start"]
        assert qr == "2@fresh"

    def test_reset_session_uses_logout_then_returns(self):
        """reset_session invoca /api/sessions/logout e ne ritorna il risultato."""
        import urllib.request

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
        with patch.object(wh, "get_whatsapp_api_key", return_value="secret-key-123"), \
             patch("urllib.request.urlopen", fake_urlopen):
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

        with patch.object(wh, "get_whatsapp_api_key", return_value=""), \
             patch("urllib.request.urlopen", fake_urlopen):
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


class TestWhatsAppPollingReceiver:
    """📤 Ricezione via polling a due velocità (niente WS/stream).

    La build WAHA CORE/WEBJS non espone ``/api/{session}/server`` (404): gli
    eventi in ingresso arrivano interrogando ``/api/messages`` sulle chat
    attive.  Il discovery lento usa ``GET /chats`` (raro), il fetch veloce
    interroga solo le chat calde.  Questi test coprono discovery, attribuzione
    del contatto e dedup.
    """

    def _backend(self) -> WhatsAppBackend:
        backend = _make_backend("http://api.test")
        backend._rest = MagicMock()
        return backend

    def test_refresh_active_chats_single_round(self):
        """_refresh_active_chats usa UN solo GET /chats (niente /api/contacts)."""
        backend = self._backend()
        backend._rest._request.return_value = [
            {"id": {"_serialized": "15771304468671@lid"}, "isGroup": False,
             "unreadCount": 1, "timestamp": 1785840779},
            {"id": {"_serialized": "393283012118-131@g.us"}, "isGroup": True,  # gruppi esclusi
             "unreadCount": 11, "timestamp": 1785831727},
        ]
        ok = backend._refresh_active_chats()
        assert ok is True
        # un solo giro, verso /chats, senza /api/contacts
        requests = backend._rest._request.call_args_list
        assert len(requests) == 1
        assert requests[0][0][0] == "GET"
        assert "/api/default/chats" in requests[0][0][1]
        assert "/api/contacts" not in requests[0][0][1]
        assert "15771304468671@lid" in backend._active_chats
        assert backend._active_chats["15771304468671@lid"][0] == 1  # unread

    def test_refresh_active_chats_throttled(self):
        """Dentro l'intervallo di refresh, /chats non viene ri-richiamato."""
        backend = self._backend()
        backend._rest._request.return_value = []
        backend._chats_last_refresh = 0.0
        backend._CHATS_REFRESH_INTERVAL = 15.0
        import time
        backend._chats_last_refresh = time.time()  # appena aggiornato
        ok = backend._refresh_active_chats()
        assert ok is False
        backend._rest._request.assert_not_called()

    def test_fetch_fast_recent_attributes_event_to_chat_jid(self):
        """Il fetch veloce attribuisce l'evento alla CHAT (@lid), non al from."""
        import time
        now = int(time.time())
        backend = self._backend()
        backend._active_chats = {"15771304468671@lid": (1, now)}
        backend._rest.list_messages.return_value = [
            {"id": "m1", "from": "393400716440@c.us", "fromMe": False,
             "body": "Grazie", "timestamp": now},
        ]
        backend._fetch_fast_recent()
        ev = backend.poll_once()
        assert len(ev) == 1
        assert ev[0].contact_id == "15771304468671@lid"  # chat, non from
        assert ev[0].payload["text"] == "Grazie"

    def test_fetch_fast_recent_deduplicates(self):
        """Lo stesso id non viene accodato due volte tra giri diversi."""
        import time
        now = int(time.time())
        backend = self._backend()
        backend._active_chats = {"X@lid": (0, now)}
        msg = {"id": "mX", "from": "1@c.us", "fromMe": False,
               "body": "ok", "timestamp": now}
        backend._rest.list_messages.return_value = [msg]
        backend._fetch_fast_recent()
        n1 = len(backend.poll_once())
        backend._fetch_fast_recent()
        n2 = len(backend.poll_once())
        assert n1 == 1
        assert n2 == 0  # già visto

    def test_fetch_fast_recent_enqueues_my_message_sent_from_other_client(self):
        """Un mio messaggio inviato da UN ALTRO client (non in cache) va mostrato."""
        import time
        now = int(time.time())
        backend = self._backend()
        backend.cache = {}  # nessun messaggio noto
        backend._active_chats = {"15771304468671@lid": (0, now)}
        backend._rest.list_messages.return_value = [
            {"id": "m_web", "from": "19645297868955@lid", "fromMe": True,
             "body": "inviato dal telefono", "timestamp": now},
        ]
        backend._fetch_fast_recent()
        ev = backend.poll_once()
        assert len(ev) == 1
        assert ev[0].payload["is_mine"] is True
        assert ev[0].contact_id == "15771304468671@lid"  # attribuito alla chat
        assert ev[0].payload["text"] == "inviato dal telefono"

    def test_fetch_fast_recent_skips_my_message_already_cached(self):
        """Un mio messaggio GIÀ in cache (echo inviato dalla TUI) non va duplicato."""
        import time
        now_ms = int(time.time() * 1000)
        now_s = now_ms // 1000
        cid = "15771304468671@lid"
        backend = self._backend()
        backend.cache = {
            cid: [
                {"text": "già inviato", "is_mine": True, "timestamp": now_ms,
                 "read": True, "status": "sent"},
            ]
        }
        backend._active_chats = {cid: (0, now_s)}
        backend._rest.list_messages.return_value = [
            {"id": "m_echo", "from": "19645297868955@lid", "fromMe": True,
             "body": "già inviato", "timestamp": now_s},
        ]
        backend._fetch_fast_recent()
        assert backend.poll_once() == []  # nessun doppione


    def test_fetch_fast_recent_skips_groups_not_in_map(self):
        """I gruppi non entrano in _active_chats (vengono esclusi a monte)."""
        import time
        now = int(time.time())
        backend = self._backend()
        backend._active_chats = {}  # nessuna chat -> nessuna chiamata
        backend._fetch_fast_recent()
        backend._rest.list_messages.assert_not_called()
        assert backend.poll_once() == []

    def test_active_chat_ids_prefers_observed(self):
        """La chat osservata va SEMPRE in testa, anche se non tra le più attive."""
        import time
        now = int(time.time())
        backend = self._backend()
        backend._active_chats = {
            "hot1@lid": (3, now),       # la più attiva (unread alto)
            "last@lid": (0, now - 999), # meno attiva
        }
        backend.observe_chat("last@lid")
        ids = backend._active_chat_ids()
        # "last@lid" (osservata) viene prima di "hot1@lid"
        assert ids[0] == "last@lid"
        assert "last@lid" in ids and "hot1@lid" in ids

    def test_observe_chat_none_clears(self):
        backend = self._backend()
        backend.observe_chat("X@lid")
        backend.observe_chat(None)
        assert backend._observed_jids == []

    def test_fetch_fast_recent_polls_observed_even_when_active_empty(self):
        """Anche con _active_chats vuoto, la chat osservata viene interrogata."""
        import time
        now = int(time.time())
        backend = self._backend()
        backend._active_chats = {}
        backend.observe_chat("obs@lid")
        backend._rest.list_messages.return_value = [
            {"id": "m_obs", "from": "1@c.us", "fromMe": False,
             "body": "guardami", "timestamp": now},
        ]
        backend._fetch_fast_recent()
        ev = backend.poll_once()
        assert len(ev) == 1
        assert ev[0].contact_id == "obs@lid"  # attribuito alla chat osservata

    def test_list_messages_uses_short_poll_timeout(self):
        """list_messages usa un timeout BREVE (per il giro veloce ~1s)."""
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
        # un GET di poll non deve mai poter affamare per decine di secondi
        assert seen_timeout and seen_timeout[0] == 3

    def test_fetch_fast_recent_polls_all_top_chats_in_parallel(self):
        """Il giro veloce interroga TUTTE le top chat (oggi le prime _POLL_TOP),
        e gli eventi vengono accodati nell'ordine di priorità della mappa."""
        import time
        now = int(time.time())
        backend = self._backend()
        backend._POLL_TOP = 4
        ids = [f"c{i}@lid" for i in range(4)]
        backend._active_chats = {cid: (1, now) for cid in ids}

        def fake_list(cid, limit=1):
            return [{"id": f"m_{cid}", "from": f"{cid}", "fromMe": False,
                     "body": "ciao", "timestamp": now}]

        backend._rest.list_messages.side_effect = fake_list
        backend._fetch_fast_recent()
        ev = backend.poll_once()
        # tutte e 4 le chat sono state interrogate e hanno prodotto un evento
        assert len(ev) == 4
        assert {e.contact_id for e in ev} == set(ids)

    def test_fetch_fast_recent_slow_get_does_not_block_others(self):
        """Un GET lento (oltre il timeout di giro) non deve impedire alle altre
        chat di essere processate nello stesso giro (era il collo di bottiglia)."""
        import time
        now = int(time.time())
        backend = self._backend()
        backend._POLL_TOP = 3
        ids = [f"c{i}@lid" for i in range(3)]

        def fake_list(cid, limit=1):
            if cid == ids[0]:  # la prima chat è lenta
                raise TimeoutError("giro scaduto")  # simula GET appeso oltre il giro
            return [{"id": f"m_{cid}", "from": f"{cid}", "fromMe": False,
                     "body": "ciao", "timestamp": now}]

        backend._rest.list_messages.side_effect = fake_list
        backend._active_chats = {cid: (1, now) for cid in ids}
        backend._fetch_fast_recent()
        ev = backend.poll_once()
        # le altre chat vengono comunque accodate; quella lenta è rimandata al giro dopo
        assert {e.contact_id for e in ev} == set(ids[1:])


    def test_list_messages_rest(self):
        """RESTClient.list_messages costruisce la GET corretta."""
        client = WhatsAppRESTClient("http://api.test")
        seen = []

        def fake_urlopen(req, timeout=30):
            seen.append((req.method, req.full_url))
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = json.dumps([
                {"id": "m1", "from": "1@c.us", "body": "hi", "timestamp": 1700000000},
            ]).encode("utf-8")
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
            {"id": "m_new", "from": "393400716440@c.us", "fromMe": False,
             "body": "più recente", "timestamp": now},
            {"id": "m_my", "from": "19645297868955@lid", "fromMe": True,
             "body": "il mio inviato", "timestamp": now - 50},
            {"id": "m_old", "from": "393400716440@c.us", "fromMe": False,
             "body": "più vecchio", "timestamp": now - 100},
        ]
        # isola ingest_message (toccherebbe SQLite)
        ingested = []
        backend.ingest_message = lambda cid, data, ts: ingested.append(
            (cid, data.get("text"), data.get("is_mine"), ts)
        ) or True
        result = backend.fetch_history("15771304468671@lid", limit=20)
        # la versione REST è stata chiamata col jid e limit
        backend._rest.list_messages.assert_called_once_with(
            "15771304468671@lid", limit=20
        )
        # i 3 messaggi sono stati ingeriti, inclusi i miei (is_mine=True)
        assert len(ingested) == 3
        assert ingested[0][1] == "più vecchio"   # ordinato cronologico
        assert ingested[1][1] == "il mio inviato"
        assert ingested[2][1] == "più recente"
        assert ingested[1][2] is True            # il mio -> is_mine=True
        assert ingested[0][2] is False and ingested[2][2] is False
        # risultato ritornato non vuoto
        assert len(result) == 3


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

    def test_api_key_prefers_env_over_config_and_dotenv(self):
        from backends import config
        with patch.dict(os.environ, {"WHATSAPP_API_KEY": "from-env"}), \
             patch.object(config, "_load_config", return_value={"whatsapp_api_key": "from-cfg"}), \
             patch.object(config, "_load_dotenv", return_value={"WAHA_API_KEY": "from-dotenv"}):
            assert config.get_whatsapp_api_key() == "from-env"

    def test_api_key_falls_back_to_config(self):
        from backends import config
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(config, "_load_config", return_value={"whatsapp_api_key": "from-cfg"}), \
             patch.object(config, "_load_dotenv", return_value={"WAHA_API_KEY": "from-dotenv"}):
            assert config.get_whatsapp_api_key() == "from-cfg"

    def test_api_key_falls_back_to_dotenv_waha(self):
        from backends import config
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(config, "_load_config", return_value={}), \
             patch.object(config, "_load_dotenv", return_value={"WAHA_API_KEY": "from-dotenv"}):
            assert config.get_whatsapp_api_key() == "from-dotenv"

    def test_api_key_empty_when_nowhere(self):
        from backends import config
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(config, "_load_config", return_value={}), \
             patch.object(config, "_load_dotenv", return_value={}):
            assert config.get_whatsapp_api_key() == ""



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
            client.get_session_status()   # /api/sessions/{name}
            client.get_session_qr()       # /api/{name}/auth/qr (PNG binario)

        assert any(m == "GET" and "/api/contacts" in u for m, u in seen)
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
        ev = _event_from_raw(frame)
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
        ev = _event_from_raw(frame)
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
        ev = _event_from_raw(frame)
        assert ev is not None
        assert ev.type == "message"
        assert ev.contact_id == "39125@s.whatsapp.net"
        assert ev.payload["is_mine"] is True

    def test_ws_loop_connects_with_api_key_header(self):
        """Il WebSocket deve inviare X-Api-Key; senza, una WAHA autenticata
        rifiuta lo stream (e la ricezione non parte mai mentre l'invio
        REST funziona)."""
        backend = _make_backend("http://api.test")
        backend._rest.api_key = "secret-key-123"
        backend._rest.get_session_status = lambda: {"status": "WORKING"}

        calls = []
        fake_ws = MagicMock()

        def fake_create_connection(url, timeout=5, header=None):
            calls.append((url, timeout, header))
            if len(calls) == 1:
                frame = json.dumps({
                    "event": "message", "session": "default",
                    "payload": {"id": "w4", "from": "39126@s.whatsapp.net",
                                 "body": "arrivato", "fromMe": False,
                                 "timestamp": 1700000003},
                }).encode("utf-8")
                # Un frame di testo, poi una chiusura (raw None) per uscire
                # dal ciclo interno; poi la connessione successiva ferma tutto.
                fake_ws.recv_data.side_effect = [(1, frame), (None, None)]
                return fake_ws
            # Seconda connessione: segna lo stop e solleva per uscire.
            backend._ws_stop.set()
            raise ConnectionError("stop intenzionale")

        import websocket as ws_mod
        with patch.object(ws_mod, "create_connection", fake_create_connection),              patch.object(backend, "_enqueue_event") as enq:
            backend._ws_loop()

        assert len(calls) >= 1
        url, _timeout, header = calls[0]
        assert url == "ws://api.test/api/default/server"
        assert header == {"X-Api-Key": "secret-key-123"}
        # Il frame deve essere stato normalizzato e accodato.
        assert enq.call_count >= 1
        ev = enq.call_args.args[0]
        assert ev.type == "message"
        assert ev.payload["text"] == "arrivato"

    def test_ws_loop_no_header_when_no_api_key(self):
        """Senza API key, header resta None (WAHA senza auth)."""
        backend = _make_backend("http://api.test")
        backend._rest.api_key = None
        calls = []

        def fake_create_connection(url, timeout=5, header=None):
            calls.append(header)
            if len(calls) >= 2:
                backend._ws_stop.set()  # ferma il retry loop
            raise ConnectionError("stop")

        import websocket as ws_mod
        with patch.object(ws_mod, "create_connection", fake_create_connection):
            backend._ws_loop()
        assert len(calls) >= 1
        assert calls[0] is None

