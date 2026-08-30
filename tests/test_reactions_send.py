from __future__ import annotations

import asyncio
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend
from backend.db import _add_message_to_cache, _init_db, _reactions_for_contact
from backend.rpc import SignalRPCClient
from backends.signal import SignalBackend
from backends.telegram import TelegramBackend
from backends.whatsapp import WhatsAppBackend
from tests.test_edit_contract import _MinimalBackend
from web.api import create_api_router


def test_base_send_reaction_default_is_false():
    assert _MinimalBackend().send_reaction_sync("contact", "message", "👍") is False


def test_signal_rpc_and_backend_send_reaction():
    client = SignalRPCClient()
    client._call = MagicMock(
        return_value={"result": {"results": [{"type": "SUCCESS"}]}}
    )
    assert client.send_reaction("+3902", "👍", "+3901", 1234) is True
    client._call.assert_called_once_with(
        "sendReaction",
        {
            "recipient": ["+3902"],
            "emoji": "👍",
            "targetAuthor": "+3901",
            "targetTimestamp": 1234,
        },
    )

    signal = SignalBackend("+3901")
    signal._use_daemon = True
    signal._rpc = client
    client.send_reaction = MagicMock(return_value=True)
    assert signal.send_reaction_sync("+3902", "1234", "❤️", target_author="+3901")
    client.send_reaction.assert_called_once_with("+3902", "❤️", "+3901", 1234)


def test_whatsapp_send_reaction_uses_waha_endpoint():
    backend_instance = WhatsAppBackend.__new__(WhatsAppBackend)
    backend_instance.session_name = "default"
    backend_instance._rest = SimpleNamespace(_request=MagicMock(return_value={}))

    assert backend_instance.send_reaction_sync("chat", "wa-id", "😂") is True
    backend_instance._rest._request.assert_called_once_with(
        "PUT",
        "/api/reaction",
        {"messageId": "wa-id", "reaction": "😂", "session": "default"},
    )


def test_telegram_send_reaction_runs_on_backend_loop():
    backend_instance = TelegramBackend.__new__(TelegramBackend)
    backend_instance._loop = object()
    requests = []

    async def resolve(entity_id):
        return entity_id

    async def client(request):
        requests.append(request)

    backend_instance._resolve_input_entity = resolve
    backend_instance._client = client

    def run(coroutine, loop):
        future = Future()
        future.set_result(asyncio.run(coroutine))
        return future

    with patch("backends.telegram.asyncio.run_coroutine_threadsafe", side_effect=run):
        assert backend_instance.send_reaction_sync("42", "77", "🙏") is True

    assert requests[0].msg_id == 77
    assert requests[0].reaction[0].emoticon == "🙏"


def test_reaction_endpoint_validates_sends_persists_and_pushes(tmp_path):
    db_file = tmp_path / "messages.db"
    signal = SignalBackend("+3901")
    signal.cache = {
        "+3902": [
            {
                "id": "1234",
                "timestamp": 1234,
                "text": "ciao",
                "is_mine": False,
            }
        ]
    }
    signal.send_reaction_sync = MagicMock(return_value=True)
    manager = SimpleNamespace(
        get=lambda protocol: signal if protocol == "signal" else None
    )
    app = FastAPI()
    app.state.manager = manager
    app.include_router(create_api_router())

    with patch.object(backend, "DB_FILE", db_file):
        _init_db()
        _add_message_to_cache(
            "+3902",
            "ciao",
            False,
            "+3902",
            1234,
            protocol="signal",
            msg_id="1234",
        )
        with patch("web.api.push_event") as pushed, TestClient(app) as client:
            assert (
                client.post(
                    "/api/messages/reaction",
                    json={
                        "protocol": "invalid",
                        "contact_id": "+3902",
                        "message_id": "1234",
                        "emoji": "👍",
                    },
                ).status_code
                == 400
            )
            assert (
                client.post(
                    "/api/messages/reaction",
                    json={
                        "protocol": "signal",
                        "contact_id": "+3902",
                        "message_id": "1234",
                        "emoji": "not-emoji",
                    },
                ).status_code
                == 400
            )
            response = client.post(
                "/api/messages/reaction",
                json={
                    "protocol": "signal",
                    "contact_id": "+3902",
                    "message_id": "1234",
                    "emoji": "👍",
                },
            )

        assert response.status_code == 200
        assert response.json()["reactions"][0]["is_mine"] is True
        assert _reactions_for_contact("signal", "+3902")[0]["author_key"] == "me"
        signal.send_reaction_sync.assert_called_once_with(
            "+3902", "1234", "👍", target_author="+3902"
        )
        assert pushed.call_args.args[0]["type"] == "reaction_update"
