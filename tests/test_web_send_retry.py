"""Test per il retry automatico di invio web (design APPROVATO v2.2).

Copre la logica pura di ``web/retry.py`` (classificazione errori e calcolo
delay) e il ramo TESTO della route ``POST /api/send`` in ``web/api.py``.
L'harness HTTP riusa ``FakeManager``/``make_app`` di ``tests.test_web_plugin``.
"""

from __future__ import annotations

import concurrent.futures
import errno
import random
import socket
import subprocess
import urllib.error
import uuid
from base64 import b64decode
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from models import ChatContact
from tests.test_web_plugin import AUTH, FakeManager, make_app
from web.retry import (
    SEND_RETRY_BASE_DELAY_S,
    SEND_RETRY_JITTER_MAX_S,
    SEND_RETRY_MAX_ATTEMPTS,
    classify_send_error,
    compute_delay,
)

_PNG_1X1 = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@pytest.fixture(autouse=True)
def _disable_ws_broadcaster(monkeypatch):
    """Tieni fuori dai test il fan-out WebSocket.

    Diversi test mockano ``web.api.asyncio.sleep``; poiché ``web.api`` importa
    il modulo ``asyncio`` reale, la patch è globale e trasforma il loop del
    broadcaster (``web.ws._broadcast``) in uno spin senza yield che blocca
    ``TestClient``.  Il broadcaster non è sotto test qui.
    """
    import web.ws

    async def _noop(app):
        return

    monkeypatch.setattr(web.ws, "_broadcast", _noop)


# ── Unit: classify_send_error ────────────────────────────────────────────────


class FloodWaitError(Exception):
    def __init__(self, seconds: int) -> None:
        super().__init__(f"flood wait {seconds}s")
        self.seconds = seconds


class UserIsBlocked(Exception):
    pass


class _ExplodingRuntimeError(RuntimeError):
    """RuntimeError il cui ``__str__`` solleva: classify non deve propagare."""

    def __str__(self) -> str:
        raise ValueError("str() exploded")


@pytest.mark.parametrize(
    "exc,expected",
    [
        pytest.param(
            OSError(errno.ECONNREFUSED, "refused"), "retryable", id="econnrefused"
        ),
        pytest.param(OSError(errno.ECONNRESET, "reset"), "terminal", id="econnreset"),
        pytest.param(OSError(errno.ETIMEDOUT, "timeout"), "terminal", id="etimedout"),
        pytest.param(OSError(errno.ENETUNREACH, "net"), "retryable", id="enetunreach"),
        pytest.param(
            OSError(errno.EHOSTUNREACH, "host"), "retryable", id="ehostunreach"
        ),
        pytest.param(
            socket.gaierror(socket.EAI_AGAIN, "again"), "retryable", id="eai_again"
        ),
        pytest.param(
            socket.gaierror(socket.EAI_NONAME, "noname"), "retryable", id="eai_noname"
        ),
        pytest.param(RuntimeError("Connection refused"), "retryable", id="rt_refused"),
        pytest.param(
            RuntimeError("signal-cli error (code 1): boom"),
            "terminal",
            id="rt_signal_cli_error",
        ),
        pytest.param(
            RuntimeError("not configured"), "terminal", id="rt_not_configured"
        ),
        pytest.param(
            subprocess.TimeoutExpired(cmd="x", timeout=1),
            "terminal",
            id="timeout_expired",
        ),
        pytest.param(FileNotFoundError("missing"), "terminal", id="file_not_found"),
        pytest.param(PermissionError("denied"), "terminal", id="permission"),
    ],
)
def test_classify_send_error_signal(exc, expected):
    assert classify_send_error("signal", exc) == expected


@pytest.mark.parametrize(
    "exc,expected",
    [
        pytest.param(
            RuntimeError("boom status=502 upstream"), "retryable", id="status_502"
        ),
        pytest.param(RuntimeError("boom status=429"), "retryable", id="status_429"),
        pytest.param(RuntimeError("boom status=500"), "retryable", id="status_500"),
        pytest.param(RuntimeError("boom status=400"), "terminal", id="status_400"),
        pytest.param(
            RuntimeError("status=0 Connection refused"),
            "retryable",
            id="status_0_refused",
        ),
        pytest.param(
            RuntimeError("status=0 JSON decode error"),
            "terminal",
            id="status_0_json",
        ),
        pytest.param(
            RuntimeError("WhatsApp API is not configured"),
            "terminal",
            id="not_configured",
        ),
        pytest.param(TimeoutError("timed out"), "terminal", id="timeout"),
    ],
)
def test_classify_send_error_whatsapp(exc, expected):
    assert classify_send_error("whatsapp", exc) == expected


@pytest.mark.parametrize(
    "exc,expected",
    [
        pytest.param(
            concurrent.futures.TimeoutError(),
            "terminal",
            id="futures_timeout",
        ),
        pytest.param(
            RuntimeError("Telegram backend not connected"),
            "terminal",
            id="not_connected",
        ),
        pytest.param(
            ValueError("Invalid Telegram contact id"),
            "terminal",
            id="invalid_contact",
        ),
        pytest.param(FloodWaitError(3), "retryable", id="floodwait_3s"),
        pytest.param(FloodWaitError(10), "terminal", id="floodwait_10s"),
        pytest.param(UserIsBlocked("blocked"), "terminal", id="user_is_blocked"),
    ],
)
def test_classify_send_error_telegram(exc, expected):
    assert classify_send_error("telegram", exc) == expected


def test_classify_send_error_unknown_protocol_is_terminal():
    assert classify_send_error("unknown", RuntimeError("boom")) == "terminal"


@pytest.mark.parametrize("protocol", ["signal", "whatsapp", "telegram"])
def test_classify_send_error_never_raises_when_str_explodes(protocol):
    assert classify_send_error(protocol, _ExplodingRuntimeError()) == "terminal"


# ── Unit: compute_delay ──────────────────────────────────────────────────────


def test_compute_delay_grows_with_attempt():
    rng = random.Random(1234)
    delays = [compute_delay(attempt, rng) for attempt in (1, 2, 3)]
    assert delays[0] < delays[1] < delays[2]


def test_compute_delay_is_deterministic_with_seed():
    assert compute_delay(1, random.Random(42)) == compute_delay(1, random.Random(42))
    assert compute_delay(2, random.Random(42)) == compute_delay(2, random.Random(42))


@pytest.mark.parametrize("attempt,base", [(1, 1.0), (2, 2.0), (3, 4.0)])
def test_compute_delay_bounds(attempt, base):
    for seed in range(50):
        delay = compute_delay(attempt, random.Random(seed))
        assert base * SEND_RETRY_BASE_DELAY_S <= delay
        assert delay <= base * SEND_RETRY_BASE_DELAY_S + SEND_RETRY_JITTER_MAX_S


# ── Integration: POST /api/send ──────────────────────────────────────────────


def _manager(contact_id: str = "alice", protocol: str = "signal") -> FakeManager:
    return FakeManager([ChatContact(contact_id, "Alice", protocol)])


def _script_sends(manager: FakeManager, outcomes):
    """Sostituisce ``send_message_sync`` con una sequenza di esiti.

    ``outcomes`` è la lista di eccezioni da sollevare in ordine; gli invii
    successivi a ``len(outcomes)`` riescono. Ritorna la lista delle chiamate.
    """
    calls: list[dict] = []

    def send(protocol, contact_id, text, **kwargs):
        calls.append(
            {
                "protocol": protocol,
                "contact_id": contact_id,
                "text": text,
                "kwargs": kwargs,
            }
        )
        index = len(calls) - 1
        if index < len(outcomes) and outcomes[index] is not None:
            raise outcomes[index]
        return "sent-id"

    manager.send_message_sync = send
    return calls


def _retry_payloads(pushed) -> list[dict]:
    payloads = []
    for call in pushed.call_args_list:
        event = call.args[0]
        if isinstance(event, dict) and event.get("type") == "send_retry":
            payloads.append(event["payload"])
    return payloads


def _send_text(client, *, contact_id="alice", protocol="signal", text="Ciao", **extra):
    body = {"protocol": protocol, "contact_id": contact_id, "text": text, **extra}
    return client.post("/api/send", json=body, headers=AUTH)


def test_send_success_first_attempt_no_retry():
    manager = _manager()
    calls = _script_sends(manager, [])
    with (
        patch("web.api.push_event") as pushed,
        patch("web.api.compute_delay", return_value=0.0) as delay_mock,
        TestClient(make_app(manager)) as client,
    ):
        response = _send_text(client, client_msg_id="cid-1")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert len(calls) == 1
    assert _retry_payloads(pushed) == []
    assert delay_mock.call_count == 0


def test_send_retries_twice_then_succeeds():
    manager = _manager()
    refused = OSError(errno.ECONNREFUSED, "refused")
    calls = _script_sends(manager, [refused, refused])
    with (
        patch("web.api.push_event") as pushed,
        patch("web.api.compute_delay", return_value=0.0) as delay_mock,
        TestClient(make_app(manager)) as client,
    ):
        response = _send_text(client, client_msg_id="cid-retry")
    assert response.status_code == 200
    assert len(calls) == 3
    payloads = _retry_payloads(pushed)
    assert [p["attempt"] for p in payloads] == [2, 3]
    assert all(p["client_msg_id"] == "cid-retry" for p in payloads)
    assert all(p["max_attempts"] == SEND_RETRY_MAX_ATTEMPTS for p in payloads)
    assert all(p["protocol"] == "signal" for p in payloads)
    assert all(p["contact_id"] == "alice" for p in payloads)
    assert all(isinstance(p["error"], str) and p["error"] for p in payloads)
    assert delay_mock.call_count == 2


def test_send_terminal_error_does_not_retry():
    manager = _manager()
    calls = _script_sends(manager, [PermissionError("denied")])
    with (
        patch("web.api.push_event") as pushed,
        patch("web.api.compute_delay", return_value=0.0) as delay_mock,
        TestClient(make_app(manager)) as client,
    ):
        response = _send_text(client, client_msg_id="cid-terminal")
    assert response.status_code == 502
    assert response.json() == {"detail": "Message send failed"}
    assert len(calls) == 1
    assert _retry_payloads(pushed) == []
    assert delay_mock.call_count == 0


def test_send_exhausts_retries_returns_502():
    manager = _manager()
    refused = OSError(errno.ECONNREFUSED, "refused")
    calls = _script_sends(manager, [refused] * 5)
    with (
        patch("web.api.push_event") as pushed,
        patch("web.api.compute_delay", return_value=0.0) as delay_mock,
        TestClient(make_app(manager)) as client,
    ):
        response = _send_text(client, client_msg_id="cid-exhaust")
    assert response.status_code == 502
    assert response.json() == {"detail": "Message send failed"}
    assert len(calls) == SEND_RETRY_MAX_ATTEMPTS
    payloads = _retry_payloads(pushed)
    assert [p["attempt"] for p in payloads] == [2, 3]
    assert delay_mock.call_count == 2


def test_send_attachments_never_retries():
    manager = _manager()
    manager.send_attachments_sync = MagicMock(
        side_effect=OSError(errno.ECONNREFUSED, "refused")
    )
    with (
        patch("web.api.push_event") as pushed,
        patch("web.api.compute_delay", return_value=0.0) as delay_mock,
        TestClient(make_app(manager)) as client,
    ):
        response = client.post(
            "/api/send",
            data={"protocol": "signal", "contact_id": "alice", "text": ""},
            files={"file": ("clipboard.png", _PNG_1X1, "image/png")},
            headers=AUTH,
        )
    assert response.status_code == 502
    assert response.json() == {"detail": "Message send failed"}
    assert manager.send_attachments_sync.call_count == 1
    assert _retry_payloads(pushed) == []
    assert delay_mock.call_count == 0


def test_send_without_client_msg_id_still_succeeds():
    manager = _manager()
    calls = _script_sends(manager, [])
    with (
        patch("web.api.push_event"),
        TestClient(make_app(manager)) as client,
    ):
        response = _send_text(client)
    assert response.status_code == 200
    assert len(calls) == 1


def test_send_client_msg_id_over_128_falls_back_to_uuid():
    manager = _manager()
    long_id = "x" * 200
    refused = OSError(errno.ECONNREFUSED, "refused")
    calls = _script_sends(manager, [refused])
    with (
        patch("web.api.push_event") as pushed,
        patch("web.api.compute_delay", return_value=0.0),
        TestClient(make_app(manager)) as client,
    ):
        response = _send_text(client, client_msg_id=long_id)
    assert response.status_code == 200
    assert len(calls) == 2
    payloads = _retry_payloads(pushed)
    assert len(payloads) == 1
    fallback_id = payloads[0]["client_msg_id"]
    assert fallback_id != long_id
    assert uuid.UUID(fallback_id).version == 4


# ── Contratto statico frontend (come test_web_ui_static_contracts) ───────────


def test_web_send_retry_frontend_contract():
    source = Path("web/static/app.js").read_text()
    assert "const clientMsgId = " in source
    assert "client_msg_id: clientMsgId" in source
    assert 'body.set("client_msg_id", clientMsgId)' in source
    assert 'case "send_retry":' in source
    assert (
        'item.optimisticStatus === "sending" || item.optimisticStatus === "retrying"'
        in source
    )

    css = Path("web/static/style.css").read_text()
    assert ".message-status.retrying" in css


# ── Edge cases aggiunti in fase di verifica (bug hunting) ────────────────────


class _ExplodingRetryableError(OSError):
    """OSError retryable (ECONNREFUSED) il cui ``__str__`` solleva."""

    def __str__(self) -> str:
        raise ValueError("str() exploded")


def test_send_retry_survives_exploding_str_and_succeeds():
    """Un errore retryable con ``__str__`` rotto deve comunque fare retry.

    ``classify_send_error`` è protetto, ma la costruzione dell'evento
    ``send_retry`` usa ``str(exc)[:200]`` senza protezione (web/api.py:1453):
    il ValueError sfugge al loop, finisce nell'handler generico e produce un
    502 saltando del tutto il retry.
    """
    manager = _manager()
    calls = _script_sends(manager, [_ExplodingRetryableError(errno.ECONNREFUSED, "x")])
    with (
        patch("web.api.push_event"),
        patch("web.api.asyncio.sleep", new_callable=AsyncMock),
        TestClient(make_app(manager)) as client,
    ):
        response = _send_text(client, client_msg_id="cid-exploding")
    assert response.status_code == 200
    assert len(calls) == 2


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_classify_whatsapp_http_error_retryable(code):
    """Un vero ``urllib.error.HTTPError`` deve usare il proprio status code.

    ``_classify_whatsapp`` controlla ``URLError`` prima di ``HTTPError`` e
    ``HTTPError`` è una sottoclasse di ``URLError``: il ramo sullo status è
    codice morto e ogni HTTPError viene classificato "terminal".
    """
    exc = urllib.error.HTTPError("http://waha/api", code, "boom", {}, None)
    assert classify_send_error("whatsapp", exc) == "retryable"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "compute_delay solleva OverflowError per attempt >= 1025 "
        "(non raggiungibile dalla route, max 3): rischio di robustezza"
    ),
)
def test_compute_delay_large_attempt_is_finite():
    assert compute_delay(2000) > 0


@pytest.mark.parametrize("attempt", [0, -1, -5])
def test_compute_delay_non_positive_attempt_is_positive(attempt):
    assert compute_delay(attempt, random.Random(0)) > 0


@pytest.mark.parametrize(
    "raw,expected_fallback",
    [
        pytest.param("cid-ok", False, id="valid"),
        pytest.param("", True, id="empty"),
        pytest.param("   ", True, id="whitespace"),
        pytest.param("\t\n", True, id="control_ws"),
        pytest.param(123, True, id="int"),
        pytest.param(None, True, id="none"),
        pytest.param(["x"], True, id="list"),
    ],
)
def test_client_msg_id_fallback_variants(raw, expected_fallback):
    manager = _manager()
    refused = OSError(errno.ECONNREFUSED, "refused")
    _script_sends(manager, [refused])
    with (
        patch("web.api.push_event") as pushed,
        patch("web.api.asyncio.sleep", new_callable=AsyncMock),
        TestClient(make_app(manager)) as client,
    ):
        response = _send_text(client, client_msg_id=raw)
    assert response.status_code == 200
    payloads = _retry_payloads(pushed)
    assert len(payloads) == 1
    got = payloads[0]["client_msg_id"]
    if expected_fallback:
        assert got != raw
        assert uuid.UUID(got).version == 4
    else:
        assert got == raw


def test_send_retry_error_message_is_truncated_to_200_chars():
    manager = _manager()
    _script_sends(manager, [OSError(errno.ECONNREFUSED, "x" * 500)])
    with (
        patch("web.api.push_event") as pushed,
        patch("web.api.asyncio.sleep", new_callable=AsyncMock),
        TestClient(make_app(manager)) as client,
    ):
        response = _send_text(client, client_msg_id="cid-long")
    assert response.status_code == 200
    payloads = _retry_payloads(pushed)
    assert len(payloads) == 1
    assert 0 < len(payloads[0]["error"]) <= 200


def test_index_html_bumps_static_asset_versions():
    html = Path("web/static/index.html").read_text()
    assert "/app.js?v=" in html
    assert "/style.css?v=" in html
