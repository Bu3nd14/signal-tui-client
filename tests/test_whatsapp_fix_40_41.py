"""
Tests for the WhatsApp backend fixes #40 (typing) and #41 (delivered).

Covers the new behaviour introduced by DESIGN_FIX_40_41.md §5/§6:

- official WAHA ack enum (shared constants + ``_ack_value``/``_event_from_ack``);
- ``fetch_history`` delivery/read thresholds (ack >= 2 / ack >= 3);
- ``handle_webhook`` ack=2 ordering (message BEFORE receipt, no duplicates);
- ``_event_from_typing`` official ``presences[].lastKnownPresence`` shape plus
  legacy scalar fallback, and the mandatory online/offline filter;
- ``_configure_webhook`` subscribing ``presence.update``;
- per-chat presence subscribe (REST method, idempotency, sweep + lazy best-effort).

These are additive: they never mutate the shared SQLite DB (any test that would
touch ingestion stubs it out), so they are safe to run alongside the existing
suite.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from models import PROTOCOL_WHATSAPP, ChatContact
from protocols.whatsapp import (
    WhatsAppBackend,
    WhatsAppRESTClient,
    _event_from_ack,
    _event_from_typing,
)
from protocols.whatsapp_events import (
    WAHA_ACK_DEVICE,
    WAHA_ACK_PLAYED,
    WAHA_ACK_READ,
    WAHA_ACK_SERVER,
    _ack_value,
)


def _make_backend(api_url: str = "http://api.test") -> WhatsAppBackend:
    backend = WhatsAppBackend(api_url=api_url, media_dir="")
    backend._rest = MagicMock()
    return backend


# ─── A. Official WAHA ack enum (#41) ─────────────────────────────────────────


class TestWAHAAckConstants:
    """🔢 Costanti condivise dell'enum ufficiale WAHA."""

    def test_constants_match_official_enum(self):
        assert WAHA_ACK_SERVER == 1
        assert WAHA_ACK_DEVICE == 2
        assert WAHA_ACK_READ == 3
        assert WAHA_ACK_PLAYED == 4

    def test_ack_value_names(self):
        """``ackName`` ufficiale → valore numerico corretto (alias Baileys rimossi)."""
        assert _ack_value({"ackName": "ERROR"}) == -1
        assert _ack_value({"ackName": "PENDING"}) == 0
        assert _ack_value({"ackName": "SERVER"}) == 1
        assert _ack_value({"ackName": "DEVICE"}) == 2
        assert _ack_value({"ackName": "READ"}) == 3
        assert _ack_value({"ackName": "PLAYED"}) == 4

    def test_ack_value_int_ack_is_authoritative(self):
        """Il campo intero ``ack`` vince su ``ackName``."""
        assert _ack_value({"ack": 2, "ackName": "READ"}) == 2
        assert _ack_value({"ack": 0}) == 0

    def test_ack_value_unknown_name_returns_none(self):
        assert _ack_value({"ackName": "DELIVERY_ACK"}) is None  # alias Baileys rimosso
        assert _ack_value({"ackName": "UNKNOWN"}) is None

    def test_event_from_ack_new_thresholds(self):
        """Soglie ufficiali: 1→None, 2→delivered, 3→read, 4→read."""
        assert (
            _event_from_ack({"id": "m", "to": "1@c.us", "fromMe": True, "status": 1})
            is None
        )
        delivered = _event_from_ack(
            {"id": "m", "to": "1@c.us", "fromMe": True, "status": 2}
        )
        assert delivered.payload == {"message_ids": ["m"], "is_read": False}
        read = _event_from_ack({"id": "m", "to": "1@c.us", "fromMe": True, "status": 3})
        assert read.payload["is_read"] is True
        played = _event_from_ack(
            {"id": "m", "to": "1@c.us", "fromMe": True, "status": 4}
        )
        assert played.payload["is_read"] is True


class TestFetchHistoryAckThresholds:
    """📥 ``fetch_history`` emette delivered per ack>=2 e read per ack>=3."""

    def test_ack2_delivered_ack3_read_ack1_none(self):
        backend = _make_backend()
        backend._presence_subscribe_lazy = MagicMock()
        backend.ingest_message = lambda cid, data, ts, *, reconcile=False: True
        backend._rest.list_messages.return_value = [
            {
                "id": "m2",
                "from": "1@c.us",
                "fromMe": True,
                "timestamp": 1700000000,
                "body": "delivered",
                "ack": 2,
            },
            {
                "id": "m3",
                "from": "1@c.us",
                "fromMe": True,
                "timestamp": 1700000001,
                "body": "read",
                "ack": 3,
            },
            {
                "id": "m1",
                "from": "1@c.us",
                "fromMe": True,
                "timestamp": 1700000002,
                "body": "server",
                "ack": 1,
            },
        ]

        backend.fetch_history("1@c.us", limit=20)

        receipts = [e for e in backend.poll_once() if e.type == "receipt"]
        by_id = {r.payload["message_ids"][0]: r.payload["is_read"] for r in receipts}
        assert by_id == {"m2": False, "m3": True}


class TestWebhookAck2Ordering:
    """📥 ``handle_webhook`` con ack=2 produce ``[message, receipt(delivered)]``."""

    def test_ack2_enqueues_message_then_receipt(self):
        backend = _make_backend()
        backend._presence_subscribe_lazy = MagicMock()
        payload = {
            "id": "msg-1",
            "to": "1@c.us",
            "fromMe": True,
            "timestamp": 1700000000,
            "body": "hello",
            "status": 2,
        }
        assert (
            backend.handle_webhook({"event": "message.ack", "payload": payload}) is True
        )
        events = backend.poll_once()
        assert [e.type for e in events] == ["message", "receipt"]
        assert events[0].payload["id"] == "msg-1"
        assert events[1].payload == {"message_ids": ["msg-1"], "is_read": False}
        # Un retry dello stesso ack NON ri-accoda il messaggio sintetico (dedup
        # per chiave); il receipt non è deduplicato qui (idempotente a valle via
        # rank guard) quindi può ri-arrivare.
        assert (
            backend.handle_webhook({"event": "message.ack", "payload": payload}) is True
        )
        retry_events = backend.poll_once()
        assert [e.type for e in retry_events] == ["receipt"]


# ─── B. Typing (#40) ─────────────────────────────────────────────────────────


class TestTypingOfficialShape:
    """⌨️ ``_event_from_typing`` sulla shape ufficiale WAHA."""

    def test_typing_started(self):
        ev = _event_from_typing(
            {
                "id": "39123@c.us",
                "presences": [
                    {"participant": "39123@c.us", "lastKnownPresence": "typing"}
                ],
            }
        )
        assert ev is not None
        assert ev.type == "typing"
        assert ev.contact_id == "39123@c.us"
        assert ev.payload["action"] == "STARTED"

    def test_recording_started(self):
        ev = _event_from_typing(
            {"id": "1@c.us", "presences": [{"lastKnownPresence": "recording"}]}
        )
        assert ev.payload["action"] == "STARTED"

    def test_paused_stopped(self):
        ev = _event_from_typing(
            {"id": "1@c.us", "presences": [{"lastKnownPresence": "paused"}]}
        )
        assert ev.payload["action"] == "STOPPED"

    def test_online_offline_unavailable_filtered(self):
        for state in ("online", "offline", "unavailable"):
            assert (
                _event_from_typing(
                    {"id": "1@c.us", "presences": [{"lastKnownPresence": state}]}
                )
                is None
            ), state

    def test_legacy_scalar_fallback(self):
        """Fallback legacy: campi scalari presence/typing/type."""
        started = _event_from_typing({"from": "1@c.us", "presence": "composing"})
        assert started.payload["action"] == "STARTED"
        stopped = _event_from_typing({"from": "1@c.us", "typing": "paused"})
        assert stopped.payload["action"] == "STOPPED"


class TestTypingMultiPresencePriority:
    """👥 Priorità con più ``presences`` (gruppo): composing-like > paused."""

    def test_composing_wins_over_paused(self):
        ev = _event_from_typing(
            {
                "id": "1@g.us",
                "presences": [
                    {"participant": "a@c.us", "lastKnownPresence": "paused"},
                    {"participant": "b@c.us", "lastKnownPresence": "typing"},
                ],
            }
        )
        assert ev.payload["action"] == "STARTED"

    def test_paused_wins_over_online(self):
        ev = _event_from_typing(
            {
                "id": "1@g.us",
                "presences": [
                    {"participant": "a@c.us", "lastKnownPresence": "online"},
                    {"participant": "b@c.us", "lastKnownPresence": "paused"},
                ],
            }
        )
        assert ev.payload["action"] == "STOPPED"

    def test_only_online_offline_filtered(self):
        assert (
            _event_from_typing(
                {
                    "id": "1@g.us",
                    "presences": [
                        {"participant": "a@c.us", "lastKnownPresence": "online"},
                        {"participant": "b@c.us", "lastKnownPresence": "offline"},
                    ],
                }
            )
            is None
        )


class TestTypingEndToEnd:
    """🔗 Envelope ``presence.update`` → evento typing in coda via webhook."""

    def test_webhook_presence_update_enqueues_typing(self):
        backend = _make_backend()
        envelope = {
            "event": "presence.update",
            "payload": {
                "id": "39123@c.us",
                "presences": [
                    {"participant": "39123@c.us", "lastKnownPresence": "typing"}
                ],
            },
        }
        assert backend.handle_webhook(envelope) is True
        events = backend.poll_once()
        assert [e.type for e in events] == ["typing"]
        assert events[0].contact_id == "39123@c.us"
        assert events[0].payload["action"] == "STARTED"


# ─── C. Webhook event subscription + per-chat presence subscribe ─────────────


class TestWebhookDesiredEvents:
    """⚙️ ``_configure_webhook`` sottoscrive anche ``presence.update``."""

    def test_desired_events_include_presence_update(self):
        import protocols.whatsapp as wa_mod

        backend = _make_backend()
        webhook = "http://host.docker.internal:8088/webhook"
        with (
            patch.object(
                backend._rest, "get_session_status", return_value={"config": {}}
            ),
            patch.object(backend._rest, "update_session_config") as mock_put,
            patch.object(wa_mod, "get_whatsapp_webhook_url", return_value=webhook),
        ):
            backend._configure_webhook()
        mock_put.assert_called_once()
        events = mock_put.call_args[0][0]["config"]["webhooks"][0]["events"]
        assert events == [
            "message",
            "message.any",
            "message.ack",
            "message.ack.group",
            "presence.update",
            "message.reaction",
        ]

    def test_no_put_when_presence_update_already_subscribed(self):
        import protocols.whatsapp as wa_mod

        backend = _make_backend()
        webhook = "http://host.docker.internal:8088/webhook"
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
        mock_put.assert_not_called()

    def test_put_when_events_outdated(self):
        import protocols.whatsapp as wa_mod

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
        mock_put.assert_called_once()


class TestPresenceSubscribe:
    """🔔 Subscribe per-chat: REST, idempotenza, sweep e lazy best-effort."""

    @pytest.fixture(autouse=True)
    def _enable_presence(self, monkeypatch):
        # La subscription presence è disabilitata per default (WON'T FIX su
        # WEBJS): riabilita per esercitare il comportamento sotto test.
        monkeypatch.setattr(WhatsAppBackend, "_PRESENCE_SUBSCRIBE_ENABLED", True)

    def test_rest_endpoint_percent_encodes_jid(self):
        client = WhatsAppRESTClient("http://api.test")
        with patch.object(client, "_request", return_value={}) as mock_req:
            client.presence_subscribe("39123@c.us")
        mock_req.assert_called_once_with(
            "POST", "/api/default/presence/39123%40c.us/subscribe"
        )

    def test_idempotent_guard_prevents_double_post(self):
        backend = _make_backend()
        backend._presence_subscribe("39123@c.us")
        backend._presence_subscribe("39123@c.us")
        backend._rest.presence_subscribe.assert_called_once_with("39123@c.us")

    def test_subscribe_best_effort_when_rest_returns_none(self):
        backend = _make_backend()
        backend._rest.presence_subscribe.return_value = None
        backend._presence_subscribe("39123@c.us")  # no exception
        assert "39123@c.us" in backend._presence_subscribed

    def test_subscribe_post_never_raises(self):
        backend = _make_backend()
        backend._rest.presence_subscribe.side_effect = RuntimeError("boom")
        backend._presence_subscribe_post("39123@c.us")  # no exception

    def test_lazy_subscribe_marks_and_spawns_thread(self):
        backend = _make_backend()
        backend._presence_subscribe_lazy("39123@c.us")  # no exception
        assert "39123@c.us" in backend._presence_subscribed

    def test_disabled_by_default_noop(self, monkeypatch):
        monkeypatch.setattr(WhatsAppBackend, "_PRESENCE_SUBSCRIBE_ENABLED", False)
        backend = _make_backend()
        backend.start_presence_subscribe()
        backend._presence_subscribe_lazy("39123@c.us")
        backend._presence_subscribe("39123@c.us")
        assert backend._presence_subscribed == set()
        assert backend._presence_subscribe_started is False
        backend._rest.presence_subscribe.assert_not_called()

    def test_sweep_subscribes_known_chats(self):
        import protocols.whatsapp as wa_mod

        backend = _make_backend()
        backend.contacts = [
            ChatContact(id="1@c.us", display_name="A", protocol=PROTOCOL_WHATSAPP),
            ChatContact(id="2@g.us", display_name="G", protocol=PROTOCOL_WHATSAPP),
        ]
        subscribed = []
        with (
            patch.object(wa_mod.time, "sleep"),
            patch.object(backend, "_presence_subscribe", side_effect=subscribed.append),
        ):
            backend._presence_subscribe_run()
        assert subscribed == ["1@c.us", "2@g.us"]
