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
import threading
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
from web.send_registry import SendRegistry, build_fingerprint

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
    assert 'item.optimisticStatus === "queued"' in source
    assert "flushOutbox" in source
    assert "retryOutboxItem" in source

    css = Path("web/static/style.css").read_text()
    assert ".message-status.retrying" in css
    assert ".message-status.queued" in css
    assert ".message-status.confirm" in css
    assert ".message-retry" in css

    outbox = Path("web/static/outbox.js").read_text()
    assert "function classifyHttpFailure(" in outbox
    assert "function computeClientDelay(" in outbox
    assert "function buildSendPayload(" in outbox

    html = Path("web/static/index.html").read_text()
    assert "/outbox.js?v=" in html
    assert html.index("/outbox.js?v=") < html.index("/app.js?v=")


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


# ── Registry idempotenza server-side (§8) ────────────────────────────────────


class _Clock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, delta: float) -> None:
        self.value += delta


def _fingerprint(**overrides) -> str:
    params = {
        "protocol": "signal",
        "contact_id": "alice",
        "text": "Ciao",
        "quote_timestamp": None,
        "quote_author": None,
        "quote_message": None,
        "reply_to_message_id": None,
        "quote_content_type": None,
        "quote_attachment_id": None,
    }
    params.update(overrides)
    return build_fingerprint(**params)


def test_send_registry_claim_release_and_mark_sent_require_token():
    registry = SendRegistry()
    key = ("signal", "alice", "cid")
    outcome, token = registry.claim_or_lookup(key, "fp")
    assert outcome == "claimed"
    assert isinstance(token, str)

    registry.release(key, "not-the-token")
    registry.mark_sent(key, "not-the-token", "m", 1)
    assert registry.snapshot(key)["status"] == "inflight"

    registry.mark_sent(key, token, "m-1", 123)
    entry = registry.snapshot(key)
    assert entry["status"] == "sent"
    assert entry["message_id"] == "m-1"
    assert entry["timestamp"] == 123


def test_send_registry_inflight_branch_does_not_mutate():
    clock = _Clock()
    registry = SendRegistry(clock=clock)
    key = ("signal", "alice", "cid")
    assert registry.claim_or_lookup(key, "fp")[0] == "claimed"
    before = registry.snapshot(key)

    outcome, value = registry.claim_or_lookup(key, "fp")
    assert outcome == "inflight"
    assert value is None
    assert registry.snapshot(key) == before


def test_send_registry_sent_ttl_prune_only_after_expiry():
    clock = _Clock()
    registry = SendRegistry(
        ttl_sent=600, max_entries=100, prune_interval=1e9, clock=clock
    )
    key = ("signal", "alice", "cid")
    _, token = registry.claim_or_lookup(key, "fp")
    registry.mark_sent(key, token, "m", 0)

    clock.advance(599)
    registry.prune()
    assert registry.snapshot(key) is not None

    clock.advance(2)
    registry.prune()
    assert registry.snapshot(key) is None


def test_send_registry_capacity_pressure_keeps_young_sent():
    clock = _Clock()
    registry = SendRegistry(max_entries=3, prune_interval=1e9, clock=clock)
    keys = [("signal", "alice", f"c{i}") for i in range(3)]
    for key in keys:
        outcome, token = registry.claim_or_lookup(key, "fp")
        assert outcome == "claimed"
        registry.mark_sent(key, token, key[2], 0)

    newest = ("signal", "alice", "new")
    outcome, token = registry.claim_or_lookup(newest, "fp")
    assert outcome == "claimed"

    # Nessuna `sent` giovane è stata evinta: la struttura si è espansa.
    for key in keys:
        assert registry.snapshot(key)["status"] == "sent"
    assert registry.snapshot(newest)["status"] == "inflight"
    assert len(registry) == 4
    assert token is not None


def test_send_registry_stale_inflight_is_reclaimable():
    clock = _Clock()
    registry = SendRegistry(ttl_inflight=120, clock=clock)
    key = ("signal", "alice", "cid")
    _, token = registry.claim_or_lookup(key, "fp")

    clock.advance(121)
    outcome, new_token = registry.claim_or_lookup(key, "fp")
    assert outcome == "claimed"
    assert new_token != token

    # Il vecchio handler non può più chiudere il claim.
    registry.mark_sent(key, token, "m", 0)
    assert registry.snapshot(key)["status"] == "inflight"


def test_send_registry_concurrent_claim_has_single_winner():
    registry = SendRegistry()
    key = ("signal", "alice", "race")
    barrier = threading.Barrier(8)
    results: list[tuple[str, object]] = []
    guard = threading.Lock()

    def worker() -> None:
        barrier.wait()
        outcome, value = registry.claim_or_lookup(key, "fp")
        with guard:
            results.append((outcome, value))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    claimed = [item for item in results if item[0] == "claimed"]
    assert len(claimed) == 1
    assert all(item[0] == "inflight" for item in results if item[0] != "claimed")


def test_build_fingerprint_stable_and_payload_sensitive():
    assert _fingerprint() == _fingerprint()
    assert _fingerprint(text="Ciao") != _fingerprint(text="Altro")
    assert _fingerprint(contact_id="alice") != _fingerprint(contact_id="bob")


# ── Integrazione API: duplicate / conflict / inflight / release ──────────────


def test_send_duplicate_client_msg_id_is_idempotent():
    manager = _manager()
    calls = _script_sends(manager, [])
    with (
        patch("web.api.push_event"),
        TestClient(make_app(manager)) as client,
    ):
        first = _send_text(client, client_msg_id="cid-dup")
        second = _send_text(client, client_msg_id="cid-dup")
    assert first.status_code == 200
    assert first.json() == {"ok": True}
    assert second.status_code == 200
    assert second.json() == {"ok": True, "duplicate": True}
    assert len(calls) == 1


def test_send_same_client_msg_id_different_payload_conflicts_422():
    manager = _manager()
    calls = _script_sends(manager, [])
    with (
        patch("web.api.push_event"),
        TestClient(make_app(manager)) as client,
    ):
        first = _send_text(client, text="uno", client_msg_id="cid-conflict")
        second = _send_text(client, text="due", client_msg_id="cid-conflict")
    assert first.status_code == 200
    assert second.status_code == 422
    assert second.json() == {"detail": "Client message id conflict"}
    assert len(calls) == 1


def test_send_registry_is_scoped_per_contact():
    manager = FakeManager(
        [
            ChatContact("alice", "Alice", "signal"),
            ChatContact("bob", "Bob", "signal"),
        ]
    )
    calls = _script_sends(manager, [])
    with (
        patch("web.api.push_event"),
        TestClient(make_app(manager)) as client,
    ):
        first = _send_text(client, contact_id="alice", client_msg_id="cid-shared")
        second = _send_text(client, contact_id="bob", client_msg_id="cid-shared")
    assert first.json() == {"ok": True}
    assert second.json() == {"ok": True}
    assert len(calls) == 2


def test_send_502_releases_inflight_claim():
    manager = _manager()
    refused = OSError(errno.ECONNREFUSED, "refused")
    _script_sends(manager, [refused] * 5)
    app = make_app(manager)
    with (
        patch("web.api.push_event"),
        patch("web.api.compute_delay", return_value=0.0),
        patch("web.api.asyncio.sleep", new_callable=AsyncMock),
        TestClient(app) as client,
    ):
        response = _send_text(client, client_msg_id="cid-release")
    assert response.status_code == 502
    registry = app.state.send_registry
    assert registry.snapshot(("signal", "alice", "cid-release")) is None


def test_send_inflight_returns_409_without_registry_mutation():
    manager = _manager()
    calls = _script_sends(manager, [])
    app = make_app(manager)
    with (
        patch("web.api.push_event"),
        TestClient(app) as client,
    ):
        _send_text(client, client_msg_id="cid-seed")
        registry = app.state.send_registry
        key = ("signal", "alice", "cid-inflight")
        fingerprint = _fingerprint()
        outcome, _ = registry.claim_or_lookup(key, fingerprint)
        assert outcome == "claimed"
        before = registry.snapshot(key)

        response = _send_text(client, client_msg_id="cid-inflight")

    assert response.status_code == 409
    assert response.json() == {"detail": "Send in progress"}
    assert registry.snapshot(key) == before
    # Solo il send di seed è arrivato al manager.
    assert len(calls) == 1


def test_send_attachments_are_not_deduplicated():
    manager = _manager()
    with (
        patch("web.api.push_event"),
        TestClient(make_app(manager)) as client,
    ):
        first = client.post(
            "/api/send",
            data={
                "protocol": "signal",
                "contact_id": "alice",
                "text": "",
                "client_msg_id": "att-dup",
            },
            files={"file": ("clipboard.png", _PNG_1X1, "image/png")},
            headers=AUTH,
        )
        second = client.post(
            "/api/send",
            data={
                "protocol": "signal",
                "contact_id": "alice",
                "text": "",
                "client_msg_id": "att-dup",
            },
            files={"file": ("clipboard.png", _PNG_1X1, "image/png")},
            headers=AUTH,
        )
    assert first.status_code == 200
    assert second.status_code == 200
    assert len(manager.attachments_calls) == 2


# ── Unit outbox.js (Node, logica pura + store in-memory) ─────────────────────


def _run_node(source: str) -> None:
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False, timeout=30
    )
    assert completed.returncode == 0, completed.stderr


def test_outbox_classify_and_delay_contract():
    _run_node(r"""
const assert = require("node:assert/strict");
const outbox = require("./web/static/outbox.js");

assert.equal(outbox.classifyHttpFailure(null), "terminal");
assert.equal(outbox.classifyHttpFailure({ name: "AbortError" }), "retryable");
assert.equal(outbox.classifyHttpFailure({ status: 401 }), "suspend");
assert.equal(outbox.classifyHttpFailure(new TypeError("Failed to fetch")), "retryable");
assert.equal(outbox.classifyHttpFailure({ status: 408 }), "retryable");
assert.equal(outbox.classifyHttpFailure({ status: 429 }), "retryable");
assert.equal(outbox.classifyHttpFailure({ status: 409 }), "retryable");
assert.equal(outbox.classifyHttpFailure({ status: 422 }), "terminal");
assert.equal(outbox.classifyHttpFailure({ status: 501 }), "terminal");
assert.equal(outbox.classifyHttpFailure({ status: 502 }), "retryable");
assert.equal(outbox.classifyHttpFailure({ status: 400 }), "terminal");

assert.equal(outbox.computeClientDelay(1, () => 0), 1000);
assert.equal(outbox.computeClientDelay(1, () => 1), 2000);
assert.equal(outbox.computeClientDelay(3, () => 0), 4000);
assert.equal(outbox.computeClientDelay(9, () => 1), 30000);
""")


def test_outbox_build_send_payload_matches_wire_rules():
    _run_node(r"""
const assert = require("node:assert/strict");
const outbox = require("./web/static/outbox.js");

const signalActive = { protocol: "signal", id: "alice" };
const signalQuote = {
  quote_timestamp: 5,
  quote_author: "bob",
  quote_message: "foto",
  reply_to_message_id: null,
  quote_content_type: "image/jpeg",
  quote_attachment_id: "folder/a b",
  isMedia: true,
};
const payload = outbox.buildSendPayload(signalActive, "ciao", signalQuote);
assert.equal(payload.quote_timestamp, 5);
assert.equal(payload.quote_author, "bob");
assert.equal(payload.quote_message, "");
assert.equal(payload.quote_content_type, "image/jpeg");
assert.equal(payload.quote_attachment_id, "folder/a b");
assert.equal("reply_to_message_id" in payload, false);

const waPayload = outbox.buildSendPayload(
  { protocol: "whatsapp", id: "bob" },
  "hey",
  { ...signalQuote, isMedia: false, quote_message: "testo", reply_to_message_id: "w1" },
);
assert.equal(waPayload.quote_message, "testo");
assert.equal(waPayload.reply_to_message_id, "w1");
assert.equal("quote_content_type" in waPayload, false);

const raw = outbox.replyToQuoteRecord(
  { timestamp: 9, quoteAuthor: "a", quoteMessage: "m", id: "x", contentType: "text/plain", attachmentId: "att", isMedia: false },
  "signal",
);
assert.equal(raw.quote_timestamp, 9);
assert.equal(raw.quote_author, "a");
assert.equal(raw.quote_message, "m");
assert.equal(raw.reply_to_message_id, null);
""")


def test_outbox_dispatched_persisted_before_fetch_and_auto_resend_window():
    _run_node(r"""
const assert = require("node:assert/strict");
const outbox = require("./web/static/outbox.js");

function record(overrides) {
  const now = 1000000;
  return {
    id: "out-cid",
    client_msg_id: "cid",
    protocol: "signal",
    contact_id: "alice",
    text: "ciao",
    timestamp: now,
    quote: null,
    status: "queued",
    attempts: 0,
    dispatched: false,
    last_attempt_at: 0,
    next_attempt_at: 0,
    created_at: now,
    updated_at: now,
    ...overrides,
  };
}

(async () => {
  // R-B: il claim PRE-fetch persiste dispatched/last_attempt_at.
  const backend = outbox.createMemoryBackend();
  const store = outbox.openOutbox({ backend, now: () => 1000 });
  await store.enqueue(record({}));
  let claimed = null;
  void store.flush({
    send: () => new Promise(() => {}),
    onUpdate: (item) => { claimed = item; },
  });
  await new Promise((resolve) => setImmediate(resolve));
  const saved = (await backend.all())[0];
  assert.equal(saved.dispatched, true);
  assert.equal(saved.status, "sending");
  assert.ok(saved.last_attempt_at > 0);
  assert.equal(claimed.status, "sending");

  // dispatched oltre la finestra -> confirm, nessun auto-flush.
  const oldBackend = outbox.createMemoryBackend();
  const now = 1000000;
  const old = outbox.openOutbox({ backend: oldBackend, now: () => now });
  await old.enqueue(record({ created_at: now, dispatched: true, last_attempt_at: now - outbox.OUTBOX_AUTO_RESEND_MAX_AGE_MS - 1 }));
  let sent = false;
  await old.flush({ send: async () => { sent = true; return "id"; } });
  assert.equal(sent, false);
  assert.equal((await oldBackend.all())[0].status, "confirm");

  // dispatched=false -> auto-flush sempre, record rimosso al successo.
  const freshBackend = outbox.createMemoryBackend();
  const fresh = outbox.openOutbox({ backend: freshBackend, now: () => now });
  await fresh.enqueue(record({ created_at: now, dispatched: false }));
  let sentFresh = false;
  await fresh.flush({ send: async () => { sentFresh = true; return "id"; } });
  assert.equal(sentFresh, true);
  assert.equal((await freshBackend.all()).length, 0);

  // Terminal -> failed conservato (record non perso).
  const failBackend = outbox.createMemoryBackend();
  const failing = outbox.openOutbox({ backend: failBackend, now: () => now });
  await failing.enqueue(record({ created_at: now }));
  const error = new Error("bad"); error.status = 400;
  await failing.flush({ send: async () => { throw error; } });
  const failed = (await failBackend.all())[0];
  assert.equal(failed.status, "failed");
  assert.equal(failed.attempts, 1);

  // Retry manuale: azzera attempts/dispatched/next_attempt_at e riaccoda.
  const retried = await failing.retry("out-cid");
  assert.equal(retried.status, "queued");
  assert.equal(retried.attempts, 0);
  assert.equal(retried.dispatched, false);
  assert.equal(retried.next_attempt_at, 0);
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_outbox_recover_orphan_sending_records():
    _run_node(r"""
const assert = require("node:assert/strict");
const outbox = require("./web/static/outbox.js");

function record(overrides) {
  const now = 1000000;
  return {
    id: "out-" + (overrides.client_msg_id || "cid"),
    client_msg_id: "cid",
    protocol: "signal",
    contact_id: "alice",
    text: "ciao",
    timestamp: now,
    quote: null,
    status: "sending",
    attempts: 0,
    dispatched: true,
    last_attempt_at: now,
    next_attempt_at: 0,
    created_at: now,
    updated_at: now,
    ...overrides,
  };
}

(async () => {
  const now = 1000000;
  const backend = outbox.createMemoryBackend();
  const store = outbox.openOutbox({ backend, now: () => now });
  await store.enqueue(record({ client_msg_id: "young", last_attempt_at: now - 1000 }));
  await store.enqueue(record({
    client_msg_id: "old",
    last_attempt_at: now - outbox.OUTBOX_AUTO_RESEND_MAX_AGE_MS - 1,
  }));
  await store.recover(now);
  const byId = Object.fromEntries((await backend.all()).map((item) => [item.client_msg_id, item]));
  assert.equal(byId.young.status, "queued");
  assert.equal(byId.old.status, "confirm");

  // 401 -> suspend: resta in coda senza backoff attivo.
  const suspendBackend = outbox.createMemoryBackend();
  const suspending = outbox.openOutbox({ backend: suspendBackend, now: () => now });
  await suspending.enqueue(record({ client_msg_id: "auth", status: "queued", dispatched: false, last_attempt_at: 0 }));
  const unauthorized = new Error("unauthorized"); unauthorized.status = 401;
  await suspending.flush({ send: async () => { throw unauthorized; } });
  const queued = (await suspendBackend.all())[0];
  assert.equal(queued.status, "queued");
  assert.equal(queued.next_attempt_at, 0);
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_app_submit_text_uses_outbox_when_available():
    """R-C: con outbox.js caricato il submit testo accoda e flusha."""
    _run_node(r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const submit = app.slice(app.indexOf("async function submitMessage("), app.indexOf("\nfunction encodeToken"));
let enqueued = null;
let flushed = false;
globalThis.state = {
  active: { id: "alice", protocol: "signal" },
  stagedAttachments: [],
  replyTo: null,
  messages: [],
  optimistic: [],
  optimisticSequence: 0,
  sending: 0,
  outbox: { enqueue: async (record) => { enqueued = record; } },
};
globalThis.elements = { messageInput: { value: "ciao", focus() {} } };
globalThis.window = {
  SignalTuiReconcile: { messageIdentity: (message) => message.id },
  SignalTuiOutbox: { replyToQuoteRecord: () => null },
};
globalThis.resizeComposer = () => {};
globalThis.updateComposer = () => {};
globalThis.renderMessages = () => {};
globalThis.flushOutbox = () => { flushed = true; };
globalThis.showError = assert.fail;
vm.runInThisContext(submit);
(async () => {
  await submitMessage();
  assert.ok(enqueued, "il record deve essere accodato");
  assert.equal(enqueued.status, "queued");
  assert.equal(enqueued.dispatched, false);
  assert.equal(state.optimistic[0].optimisticStatus, "queued");
  assert.equal(state.optimistic[0].outbox_id, "out-" + enqueued.client_msg_id);
  assert.equal(elements.messageInput.value, "");
  assert.equal(flushed, true);
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_outbox_retryable_backoff_and_max_attempts():
    _run_node(r"""
const assert = require("node:assert/strict");
const outbox = require("./web/static/outbox.js");

function record(overrides) {
  const now = 1000000;
  return {
    id: "out-cid",
    client_msg_id: "cid",
    protocol: "signal",
    contact_id: "alice",
    text: "ciao",
    timestamp: now,
    quote: null,
    status: "queued",
    attempts: 0,
    dispatched: false,
    last_attempt_at: 0,
    next_attempt_at: 0,
    created_at: now,
    updated_at: now,
    ...overrides,
  };
}

(async () => {
  const backend = outbox.createMemoryBackend();
  const store = outbox.openOutbox({ backend, now: () => 1000, random: () => 0 });
  await store.enqueue(record({}));
  const error = new Error("down"); error.status = 503;
  const outcome = await store.flush({ send: async () => { throw error; } });
  const retry = (await backend.all())[0];
  assert.equal(retry.status, "queued");
  assert.equal(retry.attempts, 1);
  assert.equal(retry.next_attempt_at, 2000);
  assert.equal(outcome.nextAttemptAt, 2000);

  await store.enqueue(record({ id: "out-cap", client_msg_id: "cap", attempts: 9 }));
  await store.flush({ send: async () => { throw error; } });
  const capped = (await backend.all()).find((item) => item.client_msg_id === "cap");
  assert.equal(capped.attempts, 10);
  assert.equal(capped.status, "failed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")
