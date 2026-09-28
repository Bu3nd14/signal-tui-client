"""Test INDIPENDENTI di verifica v1 (retry web resiliente alla disconnessione).

Scritti dal tester per validare il design congelato
``docs/DESIGN_WEB_SEND_RETRY_DISCONNECTION.md`` (§7, §8, §9, §13, §14) senza
fidarsi dei test dello sviluppatore.  Non modificano il codice di produzione.
"""

from __future__ import annotations

import asyncio
import errno
import subprocess
import threading
from base64 import b64decode
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from models import ChatContact
from tests.test_web_plugin import AUTH, FakeManager, make_app
from web.send_registry import SendRegistry, build_fingerprint

_PNG_1X1 = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@pytest.fixture(autouse=True)
def _disable_ws_broadcaster(monkeypatch):
    """Evita lo spin del fan-out WebSocket quando asyncio.sleep è mockato."""
    import web.ws

    async def _noop(app):
        return

    monkeypatch.setattr(web.ws, "_broadcast", _noop)


def _manager(contacts=("alice",)) -> FakeManager:
    return FakeManager([ChatContact(c, c.title(), "signal") for c in contacts])


def _fp(**overrides) -> str:
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


def _send(client, *, contact_id="alice", protocol="signal", text="Ciao", **extra):
    body = {"protocol": protocol, "contact_id": contact_id, "text": text, **extra}
    return client.post("/api/send", json=body, headers=AUTH)


def _run_node(source: str) -> None:
    completed = subprocess.run(
        ["node", "-e", source],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, (
        f"node exited {completed.returncode}\nSTDERR:\n{completed.stderr}\n"
        f"STDOUT:\n{completed.stdout}"
    )


# ── Area 2 · Idempotenza server ──────────────────────────────────────────────


def test_indep_duplicate_same_payload_single_send():
    manager = _manager()
    app = make_app(manager)
    with patch("web.api.push_event"), TestClient(app) as client:
        first = _send(client, client_msg_id="cid-x")
        second = _send(client, client_msg_id="cid-x")
    assert first.status_code == 200 and first.json() == {"ok": True}
    assert second.status_code == 200 and second.json() == {
        "ok": True,
        "duplicate": True,
    }
    assert len(manager.send_calls) == 1
    # La risposta duplicate omette message_id/timestamp (design §8.3).
    assert "message_id" not in second.json()
    assert "timestamp" not in second.json()


def test_indep_conflict_same_id_different_text_422_never_drops():
    manager = _manager()
    app = make_app(manager)
    with patch("web.api.push_event"), TestClient(app) as client:
        first = _send(client, text="uno", client_msg_id="cid-c")
        second = _send(client, text="due", client_msg_id="cid-c")
    assert first.status_code == 200
    assert second.status_code == 422
    assert second.json() == {"detail": "Client message id conflict"}
    # Mai un 200 che scarta: il testo diverso NON viene rispedito né scartato.
    assert len(manager.send_calls) == 1
    assert manager.send_calls[0][2] == "uno"


def test_indep_conflict_same_text_different_quote_422():
    manager = _manager()
    app = make_app(manager)
    with patch("web.api.push_event"), TestClient(app) as client:
        first = _send(
            client, client_msg_id="cid-q", quote_message="a", quote_author="b"
        )
        second = _send(
            client, client_msg_id="cid-q", quote_message="a", quote_author="c"
        )
    assert first.status_code == 200
    assert second.status_code == 422
    assert len(manager.send_calls) == 1


def test_indep_different_contact_is_separate_key_not_conflict():
    """Il design congela la chiave scoped al contatto (B1/§8.2): stesso id su
    contatti diversi NON è un conflitto e produce due invii reali."""
    manager = _manager(("alice", "bob"))
    app = make_app(manager)
    with patch("web.api.push_event"), TestClient(app) as client:
        first = _send(client, contact_id="alice", client_msg_id="cid-shared")
        second = _send(client, contact_id="bob", client_msg_id="cid-shared")
    assert first.status_code == 200 and second.status_code == 200
    assert first.json() == {"ok": True}
    assert second.json() == {"ok": True}
    assert len(manager.send_calls) == 2


def test_indep_502_releases_inflight_and_allows_later_retry():
    manager = _manager()
    refused = OSError(errno.ECONNREFUSED, "refused")
    state = {"fail": True}

    def send(protocol, contact_id, text, **kwargs):
        manager.send_calls.append((protocol, contact_id, text, kwargs))
        if state["fail"]:
            raise refused
        return "sent-id"

    manager.send_message_sync = send
    app = make_app(manager)
    with (
        patch("web.api.push_event"),
        patch("web.api.compute_delay", return_value=0.0),
        patch("web.api.asyncio.sleep", new_callable=AsyncMock),
        TestClient(app) as client,
    ):
        first = _send(client, client_msg_id="cid-502")
        registry = app.state.send_registry
        key = ("signal", "alice", "cid-502")
        assert first.status_code == 502
        # Il finally deve aver rilasciato il claim: nessun 409 bloccante.
        assert registry.snapshot(key) is None
        state["fail"] = False
        second = _send(client, client_msg_id="cid-502")
    assert second.status_code == 200
    assert second.json() == {"ok": True}
    assert len(manager.send_calls) == 1 + 3  # 3 tentativi falliti + 1 riuscito


def test_indep_inflight_409_then_release_allows_retry():
    manager = _manager()
    calls: list = []

    def send(protocol, contact_id, text, **kwargs):
        calls.append(text)
        return "sent-id"

    manager.send_message_sync = send
    app = make_app(manager)
    with patch("web.api.push_event"), TestClient(app) as client:
        # Forza la creazione del registry con un primo invio (chiave diversa).
        _send(client, text="seed-msg", client_msg_id="seed")
        registry = app.state.send_registry
        key = ("signal", "alice", "cid-inflight")
        outcome, token = registry.claim_or_lookup(key, _fp(text="Ciao"))
        assert outcome == "claimed"
        before = registry.snapshot(key)

        blocked = _send(client, client_msg_id="cid-inflight")
        assert blocked.status_code == 409
        assert blocked.json() == {"detail": "Send in progress"}
        assert registry.snapshot(key) == before  # nessuna mutazione sul 409

        registry.release(key, token)
        allowed = _send(client, client_msg_id="cid-inflight")
    assert allowed.status_code == 200
    assert allowed.json() == {"ok": True}
    assert registry.snapshot(key)["status"] == "sent"
    assert calls.count("Ciao") == 1


def test_indep_attachments_never_touch_registry():
    manager = _manager()
    app = make_app(manager)
    with patch("web.api.push_event"), TestClient(app) as client:
        # Un testo con id A marchia la chiave (signal, alice, A) come sent.
        _send(client, client_msg_id="shared-id")
        registry = app.state.send_registry
        # Un allegato con lo STESSO id non deve né leggere né mutare il registry.
        before = registry.snapshot(("signal", "alice", "shared-id"))
        for _ in range(2):
            response = client.post(
                "/api/send",
                data={
                    "protocol": "signal",
                    "contact_id": "alice",
                    "text": "",
                    "client_msg_id": "shared-id",
                },
                files={"file": ("clipboard.png", _PNG_1X1, "image/png")},
                headers=AUTH,
            )
            assert response.status_code == 200
        after = registry.snapshot(("signal", "alice", "shared-id"))
    assert len(manager.attachments_calls) == 2
    assert before == after  # gli allegati non modificano la voce testo


def test_indep_legacy_missing_client_msg_id_not_deduped():
    manager = _manager()
    app = make_app(manager)
    with patch("web.api.push_event"), TestClient(app) as client:
        a = _send(client)
        b = _send(client)
    # Senza client_msg_id il server genera uuid distinti → nessuna dedup.
    assert a.status_code == 200 and b.status_code == 200
    assert len(manager.send_calls) == 2


def test_indep_unicode_and_whitespace_text_distinct():
    manager = _manager()
    app = make_app(manager)
    with patch("web.api.push_event"), TestClient(app) as client:
        a = _send(client, text="Ciao ", client_msg_id="cid-ws")
        b = _send(client, text="Ciao", client_msg_id="cid-ws")
    # Testo diverso (spazio) → fingerprint diverso → 422, mai drop.
    assert a.status_code == 200 and b.status_code == 422
    assert len(manager.send_calls) == 1


# ── Area 2b · Registry unit (eviction/TTL/token) ─────────────────────────────


class _Clock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, delta: float) -> None:
        self.value += delta


def test_indep_registry_young_sent_survives_capacity_pressure():
    clock = _Clock()
    registry = SendRegistry(max_entries=2, prune_interval=1e9, clock=clock)
    for i in range(2):
        key = ("signal", "alice", f"c{i}")
        _, token = registry.claim_or_lookup(key, "fp")
        registry.mark_sent(key, token, key[2], int(clock.value))
    outcome, _ = registry.claim_or_lookup(("signal", "alice", "new"), "fp")
    assert outcome == "claimed"
    for i in range(2):
        assert registry.snapshot(("signal", "alice", f"c{i}"))["status"] == "sent"
    assert len(registry) == 3  # espansione, nessuna evict di sent giovani


def test_indep_registry_sent_removed_only_after_ttl():
    clock = _Clock()
    registry = SendRegistry(ttl_sent=600, prune_interval=1e9, clock=clock)
    key = ("signal", "alice", "cid")
    _, token = registry.claim_or_lookup(key, "fp")
    registry.mark_sent(key, token, "m", 0)
    clock.advance(599)
    registry.prune()
    assert registry.snapshot(key) is not None
    clock.advance(2)
    registry.prune()
    assert registry.snapshot(key) is None


def test_indep_registry_release_and_mark_sent_wrong_token_noop():
    registry = SendRegistry()
    key = ("signal", "alice", "cid")
    _, token = registry.claim_or_lookup(key, "fp")
    registry.release(key, "bogus")
    registry.mark_sent(key, "bogus", "m", 1)
    assert registry.snapshot(key)["status"] == "inflight"
    registry.mark_sent(key, token, "m", 1)
    assert registry.snapshot(key)["status"] == "sent"
    # release su una voce sent non deve rimuoverla.
    registry.release(key, token)
    assert registry.snapshot(key)["status"] == "sent"


def test_indep_registry_stale_inflight_reclaimable_and_different_token():
    clock = _Clock()
    registry = SendRegistry(ttl_inflight=120, clock=clock)
    key = ("signal", "alice", "cid")
    _, old_token = registry.claim_or_lookup(key, "fp")
    clock.advance(121)
    outcome, new_token = registry.claim_or_lookup(key, "fp")
    assert outcome == "claimed" and new_token != old_token
    registry.mark_sent(key, old_token, "m", 0)
    assert registry.snapshot(key)["status"] == "inflight"


def test_indep_registry_thread_safety_single_winner():
    registry = SendRegistry()
    key = ("signal", "alice", "race")
    barrier = threading.Barrier(16)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        outcome, _ = registry.claim_or_lookup(key, "fp")
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("claimed") == 1
    assert outcomes.count("inflight") == 15


# ── Area 3 · Outbox.js (Node) ─────────────────────────────────────────────────


def test_indep_outbox_classify_full_table():
    _run_node(r"""
const assert = require("node:assert/strict");
const o = require("./web/static/outbox.js");
const C = o.classifyHttpFailure;
assert.equal(C(null), "terminal");
assert.equal(C(undefined), "terminal");
assert.equal(C({ name: "AbortError" }), "retryable");
assert.equal(C(new TypeError("Failed to fetch")), "retryable");
assert.equal(C({ status: 401 }), "suspend");
assert.equal(C({ status: 408 }), "retryable");
assert.equal(C({ status: 429 }), "retryable");
assert.equal(C({ status: 409 }), "retryable");
assert.equal(C({ status: 422 }), "terminal");
assert.equal(C({ status: 501 }), "terminal");
assert.equal(C({ status: 500 }), "retryable");
assert.equal(C({ status: 502 }), "retryable");
assert.equal(C({ status: 503 }), "retryable");
assert.equal(C({ status: 504 }), "retryable");
assert.equal(C({ status: 599 }), "retryable");
assert.equal(C({ status: 400 }), "terminal");
assert.equal(C({ status: 403 }), "terminal");
assert.equal(C({ status: 404 }), "terminal");
assert.equal(C({ status: 413 }), "terminal");
assert.equal(C({ status: 415 }), "terminal");
// Ordine tabella: AbortError vince su 401.
assert.equal(C({ name: "AbortError", status: 401 }), "retryable");
// 401 vince sul default "nessuno status".
assert.equal(C({ status: 401, name: "Error" }), "suspend");
""")
    # Tabella attesa condivisa con i test dello sviluppatore; qui verifichiamo
    # esplicitamente il terzo esito `suspend` e l'ordine.
    _run_node(r"""
const assert = require("node:assert/strict");
const o = require("./web/static/outbox.js");
assert.equal(o.computeClientDelay(1, () => 0), 1000);
assert.equal(o.computeClientDelay(2, () => 0), 2000);
assert.equal(o.computeClientDelay(3, () => 0), 4000);
assert.equal(o.computeClientDelay(10, () => 1), 30000);
assert.equal(o.computeClientDelay(0, () => 0), 1000);
assert.equal(o.computeClientDelay(-3, () => 0), 1000);
""")


def test_indep_outbox_build_send_payload_matches_legacy_wire():
    _run_node(r"""
const assert = require("node:assert/strict");
const o = require("./web/static/outbox.js");

// Testo semplice.
assert.deepEqual(o.buildSendPayload({ protocol: "signal", id: "alice" }, "ciao", null),
  { protocol: "signal", contact_id: "alice", text: "ciao" });

// Signal reply testuale.
const textReply = o.replyToQuoteRecord(
  { id: "x", timestamp: 5, quoteAuthor: "bob", quoteMessage: "ciao", contentType: null, attachmentId: null, isMedia: false },
  "signal");
const p1 = o.buildSendPayload({ protocol: "signal", id: "alice" }, "risposta", textReply);
assert.equal(p1.quote_timestamp, 5);
assert.equal(p1.quote_author, "bob");
assert.equal(p1.quote_message, "ciao");
assert.equal("reply_to_message_id" in p1, false);
assert.equal("quote_content_type" in p1, false);

// Signal MEDIA reply: quote_message forzato a "" (specchia app.js legacy).
const mediaReply = o.replyToQuoteRecord(
  { id: "y", timestamp: 7, quoteAuthor: "bob", quoteMessage: "didascalia", contentType: "image/jpeg", attachmentId: "folder/a b", isMedia: true },
  "signal");
const p2 = o.buildSendPayload({ protocol: "signal", id: "alice" }, "cap", mediaReply);
assert.equal(p2.quote_message, "");
assert.equal(p2.quote_content_type, "image/jpeg");
assert.equal(p2.quote_attachment_id, "folder/a b");
assert.equal("reply_to_message_id" in p2, false);

// WhatsApp reply: reply_to_message_id = id, niente campi signal.
const waReply = o.replyToQuoteRecord(
  { id: "w1", timestamp: 3, quoteAuthor: "bob", quoteMessage: "hey", contentType: null, attachmentId: null, isMedia: false },
  "whatsapp");
assert.equal(waReply.reply_to_message_id, "w1");
const p3 = o.buildSendPayload({ protocol: "whatsapp", id: "bob" }, "ok", waReply);
assert.equal(p3.reply_to_message_id, "w1");
assert.equal("quote_content_type" in p3, false);
assert.equal("quote_attachment_id" in p3, false);
""")
    # Confronto diretto con il blocco legacy di app.js: stesse chiavi per testo.
    _run_node(r"""
const assert = require("node:assert/strict");
const o = require("./web/static/outbox.js");
function legacyQuotePayload(active, reply) {
  return reply ? {
    quote_timestamp: reply.timestamp,
    quote_author: reply.quoteAuthor,
    quote_message: active.protocol === "signal" && reply.isMedia ? "" : reply.quoteMessage,
    ...(active.protocol === "signal"
      ? {
        ...(reply.contentType ? { quote_content_type: reply.contentType } : {}),
        ...(reply.attachmentId ? { quote_attachment_id: reply.attachmentId } : {}),
      }
      : { reply_to_message_id: reply.id }),
  } : {};
}
const cases = [
  [{ protocol: "signal", id: "alice" }, null],
  [{ protocol: "signal", id: "alice" }, { id: "x", timestamp: 5, quoteAuthor: "b", quoteMessage: "m", isMedia: false }],
  [{ protocol: "signal", id: "alice" }, { id: "x", timestamp: 5, quoteAuthor: "b", quoteMessage: "cap", contentType: "image/png", attachmentId: "a/b", isMedia: true }],
  [{ protocol: "whatsapp", id: "b" }, { id: "w1", timestamp: 5, quoteAuthor: "b", quoteMessage: "m", isMedia: false }],
  [{ protocol: "telegram", id: "t" }, { id: "42", timestamp: 5, quoteAuthor: "b", quoteMessage: "m", isMedia: false }],
];
for (const [active, reply] of cases) {
  const record = o.replyToQuoteRecord(reply, active.protocol);
  const viaOutbox = o.buildSendPayload(active, "body", record);
  const legacy = { protocol: active.protocol, contact_id: active.id, text: "body", ...legacyQuotePayload(active, reply) };
  assert.deepEqual(viaOutbox, legacy, JSON.stringify({ active, reply, viaOutbox, legacy }));
}
""")
    # Nessun URL effimero persistito.
    _run_node(r"""
const assert = require("node:assert/strict");
const o = require("./web/static/outbox.js");
const raw = o.replyToQuoteRecord(
  { id: "y", timestamp: 7, quoteAuthor: "b", quoteMessage: "c", contentType: "image/jpeg", attachmentId: "a/b", isMedia: true, quoteThumbUrl: "/api/media/x", thumbUrl: "blob:x" },
  "signal");
assert.equal("quote_thumb_url" in raw, false);
assert.equal("thumbUrl" in raw, false);
assert.equal("quoteThumbUrl" in raw, false);
const rec = o.normalizeRecord({ id: "out-1", client_msg_id: "cid", protocol: "signal", contact_id: "alice", text: "t", quote: raw });
assert.equal("quote_thumb_url" in rec.quote, false);
assert.equal("thumbUrl" in rec.quote, false);
""")


def test_indep_outbox_dispatched_persisted_before_fetch_and_confirm_window():
    _run_node(r"""
const assert = require("node:assert/strict");
const o = require("./web/static/outbox.js");
function record(overrides) {
  const now = 1000000;
  return {
    id: "out-cid", client_msg_id: "cid", protocol: "signal", contact_id: "alice",
    text: "ciao", timestamp: now, quote: null, status: "queued", attempts: 0,
    dispatched: false, last_attempt_at: 0, next_attempt_at: 0,
    created_at: now, updated_at: now, ...overrides,
  };
}
(async () => {
  // Il record è già persistito come dispatched/sending QUANDO send() parte.
  const backend = o.createMemoryBackend();
  const store = o.openOutbox({ backend, now: () => 5000 });
  await store.enqueue(record({}));
  let observed = null;
  await store.flush({ send: async (rec) => { observed = await backend.get(rec.id); return "id"; } });
  assert.equal(observed.status, "sending");
  assert.equal(observed.dispatched, true);
  assert.equal(observed.last_attempt_at, 5000);

  // dispatched=true oltre la finestra → confirm, NIENTE auto-flush.
  const oldBackend = o.createMemoryBackend();
  const now = 9000000;
  const old = o.openOutbox({ backend: oldBackend, now: () => now });
  await old.enqueue(record({ created_at: now, dispatched: true, last_attempt_at: now - o.OUTBOX_AUTO_RESEND_MAX_AGE_MS - 1 }));
  let called = false;
  await old.flush({ send: async () => { called = true; return "id"; } });
  assert.equal(called, false);
  assert.equal((await oldBackend.all())[0].status, "confirm");

  // dispatched=true ENTRO la finestra → auto-flush.
  const inWinBackend = o.createMemoryBackend();
  const inWin = o.openOutbox({ backend: inWinBackend, now: () => now });
  await inWin.enqueue(record({ created_at: now, dispatched: true, last_attempt_at: now - 1000 }));
  let sentInWin = false;
  await inWin.flush({ send: async () => { sentInWin = true; return "id"; } });
  assert.equal(sentInWin, true);
})().catch((e) => { console.error(e); process.exitCode = 1; });
""")


def test_indep_outbox_retention_two_days():
    _run_node(r"""
const assert = require("node:assert/strict");
const o = require("./web/static/outbox.js");
function record(overrides) {
  const now = 1000000;
  return { id: "out-x", client_msg_id: "x", protocol: "signal", contact_id: "alice",
    text: "t", timestamp: now, quote: null, status: "queued", attempts: 0,
    dispatched: false, last_attempt_at: 0, next_attempt_at: 0,
    created_at: now, updated_at: now, ...overrides };
}
(async () => {
  const now = 1000000000;
  const backend = o.createMemoryBackend();
  const store = o.openOutbox({ backend, now: () => now });
  await store.enqueue(record({ id: "out-young", client_msg_id: "young", created_at: now - o.OUTBOX_RETENTION_MS + 1000 }));
  await backend.put(record({ id: "out-old", client_msg_id: "old", created_at: now - o.OUTBOX_RETENTION_MS - 1 }));
  await store.pruneExpired();
  const ids = (await backend.all()).map((r) => r.client_msg_id).sort();
  assert.deepEqual(ids, ["young"]);
  assert.equal(o.OUTBOX_RETENTION_MS, 2 * 24 * 60 * 60 * 1000);
  assert.equal(o.OUTBOX_MAX_ATTEMPTS, 10);
})().catch((e) => { console.error(e); process.exitCode = 1; });
""")


def test_indep_outbox_recover_retry_reset_and_suspend():
    _run_node(r"""
const assert = require("node:assert/strict");
const o = require("./web/static/outbox.js");
function record(overrides) {
  const now = 1000000;
  return { id: "out-" + (overrides.client_msg_id || "cid"), client_msg_id: "cid",
    protocol: "signal", contact_id: "alice", text: "t", timestamp: now, quote: null,
    status: "sending", attempts: 3, dispatched: true, last_attempt_at: now,
    next_attempt_at: 0, created_at: now, updated_at: now, ...overrides };
}
(async () => {
  const now = 1000000;
  const backend = o.createMemoryBackend();
  const store = o.openOutbox({ backend, now: () => now });
  await store.enqueue(record({ client_msg_id: "young", last_attempt_at: now - 1000 }));
  await store.enqueue(record({ client_msg_id: "old", last_attempt_at: now - o.OUTBOX_AUTO_RESEND_MAX_AGE_MS - 1 }));
  await store.recover(now);
  const by = Object.fromEntries((await backend.all()).map((r) => [r.client_msg_id, r]));
  assert.equal(by.young.status, "queued");
  assert.equal(by.old.status, "confirm");

  // Retry manuale resetta il backoff.
  const b2 = o.createMemoryBackend();
  const s2 = o.openOutbox({ backend: b2, now: () => now });
  await s2.enqueue(record({ client_msg_id: "r", status: "failed", attempts: 10, dispatched: true, last_attempt_at: now - 999999, next_attempt_at: now + 12345 }));
  const retried = await s2.retry("out-r");
  assert.equal(retried.status, "queued");
  assert.equal(retried.attempts, 0);
  assert.equal(retried.dispatched, false);
  assert.equal(retried.last_attempt_at, 0);
  assert.equal(retried.next_attempt_at, 0);

  // 401 → suspend: resta queued senza backoff attivo.
  const b3 = o.createMemoryBackend();
  const s3 = o.openOutbox({ backend: b3, now: () => now });
  await s3.enqueue(record({ client_msg_id: "auth", status: "queued", dispatched: false, last_attempt_at: 0 }));
  const err = new Error("unauthorized"); err.status = 401;
  const outcome = await s3.flush({ send: async () => { throw err; } });
  const q = (await b3.all())[0];
  assert.equal(q.status, "queued");
  assert.equal(q.next_attempt_at, 0);
  assert.equal(outcome.nextAttemptAt, 0);
})().catch((e) => { console.error(e); process.exitCode = 1; });
""")


def test_indep_outbox_inmemory_fallback_when_no_indexeddb():
    _run_node(r"""
const assert = require("node:assert/strict");
const o = require("./web/static/outbox.js");
assert.equal(typeof globalThis.indexedDB, "undefined");
(async () => {
  const store = o.openOutbox();
  await store.enqueue({ id: "out-1", client_msg_id: "cid", protocol: "signal", contact_id: "alice", text: "t" });
  const list = await store.list();
  assert.equal(list.length, 1);
  assert.equal(list[0].status, "queued");
  let sent = false;
  await store.flush({ send: async () => { sent = true; return "id"; } });
  assert.equal(sent, true);
  assert.equal((await store.list()).length, 0);
})().catch((e) => { console.error(e); process.exitCode = 1; });
""")


def test_indep_outbox_max_attempts_cap():
    _run_node(r"""
const assert = require("node:assert/strict");
const o = require("./web/static/outbox.js");
function record(overrides) {
  const now = 1000000;
  return { id: "out-cap", client_msg_id: "cap", protocol: "signal", contact_id: "alice",
    text: "t", timestamp: now, quote: null, status: "queued", attempts: 0,
    dispatched: false, last_attempt_at: 0, next_attempt_at: 0,
    created_at: now, updated_at: now, ...overrides };
}
(async () => {
  const backend = o.createMemoryBackend();
  const store = o.openOutbox({ backend, now: () => 1000, random: () => 0 });
  await store.enqueue(record({ attempts: 9 }));
  const err = new Error("down"); err.status = 503;
  await store.flush({ send: async () => { throw err; } });
  const capped = (await backend.all())[0];
  assert.equal(capped.attempts, 10);
  assert.equal(capped.status, "failed");
  // Da failed NON è più auto-flushata.
  let called = false;
  await store.flush({ send: async () => { called = true; } });
  assert.equal(called, false);
})().catch((e) => { console.error(e); process.exitCode = 1; });
""")


# ── Area 4 · Integrazione SPA (vm Node) ──────────────────────────────────────


def _submit_slice() -> str:
    app = Path("web/static/app.js").read_text()
    return app[
        app.index("async function submitMessage(") : app.index("\nfunction encodeToken")
    ]


def test_indep_spa_composer_cleared_only_after_enqueue():
    _run_node(r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const submit = app.slice(app.indexOf("async function submitMessage("), app.indexOf("\nfunction encodeToken"));
globalThis.state = {
  active: { id: "alice", protocol: "signal" }, stagedAttachments: [], replyTo: null,
  messages: [], optimistic: [], optimisticSequence: 0, sending: 0,
  outbox: { enqueue: async () => { throw new Error("idb boom"); } },
};
globalThis.elements = { messageInput: { value: "ciao", focus() {} } };
globalThis.window = { SignalTuiReconcile: { messageIdentity: (m) => m.id }, SignalTuiOutbox: { replyToQuoteRecord: () => null } };
globalThis.resizeComposer = () => {};
globalThis.updateComposer = () => {};
globalThis.renderMessages = () => {};
globalThis.flushOutbox = () => { throw new Error("non deve flushare su enqueue fallito"); };
let shown = null;
globalThis.showError = (m) => { shown = m; };
vm.runInThisContext(submit);
(async () => {
  await submitMessage();
  assert.equal(elements.messageInput.value, "ciao", "composer NON svuotato se l'enqueue fallisce");
  assert.equal(state.optimistic[0].optimisticStatus, "failed");
  assert.ok(shown);
})().catch((e) => { console.error(e); process.exitCode = 1; });
""")


def test_indep_spa_legacy_path_when_outbox_absent():
    _run_node(r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const submit = app.slice(app.indexOf("async function submitMessage("), app.indexOf("\nfunction encodeToken"));
globalThis.state = {
  active: { id: "alice", protocol: "signal" }, stagedAttachments: [], replyTo: null,
  messages: [], optimistic: [], optimisticSequence: 0, sending: 0, outbox: null,
};
globalThis.elements = { messageInput: { value: "ciao", focus() {} } };
globalThis.window = { SignalTuiReconcile: { messageIdentity: (m) => m.id } };
globalThis.resizeComposer = () => {};
globalThis.updateComposer = () => {};
globalThis.renderMessages = () => {};
globalThis.showError = () => {};
let fetched = 0;
globalThis.apiFetch = async (path, options) => { fetched += 1; return {}; };
vm.runInThisContext(submit);
(async () => {
  await submitMessage();
  assert.equal(fetched, 1, "outbox assente → percorso legacy sincrono");
  assert.equal(state.optimistic[0].optimisticStatus, "sent");
  assert.equal(elements.messageInput.value, "");
})().catch((e) => { console.error(e); process.exitCode = 1; });
""")


def test_indep_spa_static_contracts_for_triggers_and_logout():
    source = Path("web/static/app.js").read_text()
    # flush su onopen WS
    assert source.count("flushOutbox()") >= 4
    assert 'window.addEventListener("online", () => flushOutbox());' in source
    assert 'document.visibilityState === "visible") flushOutbox();' in source
    assert "void initOutbox();" in source
    # rotazione token deliberata → clear
    assert "if (previousToken && previousToken !== token) void clearOutbox();" in source
    # 401 non deliberato: handleUnauthorized NON svuota l'outbox
    start = source.index("function handleUnauthorized()")
    end = source.index("}", start)
    assert "clearOutbox" not in source[start:end]
    # il record outbox è escluso dal ricalcolo known_message_ids (N5)
    assert "known_message_ids: []" in source


# ── Area 5 · Bug hunting ─────────────────────────────────────────────────────


def test_indep_reconcile_restored_outbox_must_not_match_older_row():
    _run_node(r"""
const assert = require("node:assert/strict");
const rec = require("./web/static/reconcile.js");
const optimistic = [{
  optimistic_id: "opt1", client_msg_id: "cid", protocol: "signal", contactId: "alice",
  text: "Ciao", direction: "out", timestamp: 2000, known_message_ids: [],
  restored: true, optimisticStatus: "queued",
}];
const messages = [{
  id: "old1", direction: "out", text: "Ciao", timestamp: 1000,
  quote_timestamp: null, quote_author: null, quote_message: null,
  reply_to_message_id: null, attachment: null,
}];
const out = rec.reconcileOptimisticMessages(messages, optimistic, "signal", "alice");
// Design §9.1: l'eco precedente (ts 1000 < 2000) NON deve accoppiarsi.
assert.equal(out.optimistic[0].confirmed_message_id, undefined);
assert.equal(out.visible.length, 1);
""")


def test_indep_reconcile_cross_contact_isolated():
    _run_node(r"""
const assert = require("node:assert/strict");
const rec = require("./web/static/reconcile.js");
const optimistic = [
  { optimistic_id: "a1", client_msg_id: "a", protocol: "signal", contactId: "alice", text: "ciao", direction: "out", timestamp: 1000, known_message_ids: [] },
  { optimistic_id: "b1", client_msg_id: "b", protocol: "signal", contactId: "bob", text: "ehi", direction: "out", timestamp: 1000, known_message_ids: [] },
];
const out = rec.reconcileOptimisticMessages([], optimistic, "signal", "alice");
assert.equal(out.visible.length, 1);
assert.equal(out.visible[0].optimistic_id, "a1");
assert.equal(out.optimistic.length, 2);
""")


def test_indep_spa_no_innerhtml_with_user_payload_in_outbox_ui():
    source = Path("web/static/app.js").read_text()
    # I bottoni retry/discard devono usare textContent, non innerHTML.
    block = source[
        source.index(
            "item.optimistic_id\n    && (item.optimisticStatus"
        ) : source.index("return { el: message, textEl")
    ]
    assert "innerHTML" not in block
    assert "textContent" in block


# ── Area 2c · Concorrenza API reale (stesso client_msg_id) ───────────────────


async def test_indep_concurrent_same_id_produces_single_send():
    """Due richieste simultanee con lo stesso id → una sola send_message_sync.

    La seconda deve ricevere 409 (inflight) o 200 duplicate, MAI un doppio
    invio (design §8.2.1, requisito N2).
    """
    manager = _manager()
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    lock = threading.Lock()

    def send(protocol, contact_id, text, **kwargs):
        with lock:
            calls.append(text)
            is_first = len(calls) == 1
        if is_first:
            started.set()
            release.wait(timeout=10)
        return "sent-id"

    manager.send_message_sync = send
    app = make_app(manager)
    body = {
        "protocol": "signal",
        "contact_id": "alice",
        "text": "Ciao",
        "client_msg_id": "cid-conc",
    }

    transport = httpx.ASGITransport(app=app)
    with patch("web.api.push_event"):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            first = asyncio.create_task(
                client.post("/api/send", json=body, headers=AUTH)
            )
            assert await asyncio.to_thread(started.wait, 5), (
                "il primo invio non è partito"
            )
            second = asyncio.create_task(
                client.post("/api/send", json=body, headers=AUTH)
            )
            response2 = await second
            release.set()
            response1 = await first

    assert response1.status_code == 200
    assert response1.json() == {"ok": True}
    assert response2.status_code in (409, 200)
    if response2.status_code == 200:
        assert response2.json() == {"ok": True, "duplicate": True}
    else:
        assert response2.json() == {"detail": "Send in progress"}
    assert calls == ["Ciao"], f"invii multipli: {calls}"


def test_indep_manual_retry_while_flush_in_progress_is_not_lost():
    _run_node(r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const code = app.slice(
  app.indexOf("function onOutboxUpdate"),
  app.indexOf("async function initOutbox"),
);

let resolveSend;
const store = {
  flushCalls: 0,
  retryCalls: 0,
  records: {
    "out-1": { id: "out-1", client_msg_id: "c1", status: "queued" },
    "out-2": { id: "out-2", client_msg_id: "c2", status: "failed" },
  },
  flush({ send }) {
    this.flushCalls += 1;
    return send(this.records["out-1"]).then(() => ({ nextAttemptAt: 0 }));
  },
  async retry(id) {
    this.retryCalls += 1;
    this.records[id] = { ...this.records[id], status: "queued" };
    return { ...this.records[id] };
  },
};
globalThis.state = {
  outbox: store, outboxFlushing: false, outboxFlushTimer: null,
  token: "t", optimistic: [], active: null,
};
globalThis.window = { clearTimeout, setTimeout };
globalThis.renderMessages = () => {};
globalThis.sendOutboxRecord = () => new Promise((resolve) => { resolveSend = resolve; });

vm.runInThisContext(code);
(async () => {
  flushOutbox();                    // avvia il flush lento
  await new Promise((r) => setImmediate(r));
  assert.equal(store.flushCalls, 1);
  await retryOutboxRecord("out-2"); // retry manuale durante il flush
  await new Promise((r) => setImmediate(r));
  resolveSend();
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));
  // Il retry manuale DEVE produrre un nuovo flush (desiderato).
  assert.equal(store.flushCalls, 2, "il retry manuale è stato inghiottito dalla guardia");
  assert.equal(store.records["out-2"].status, "queued");
})().catch((e) => { console.error(e); process.exitCode = 1; });
""")


def test_indep_outbox_falls_back_when_idb_open_fails():
    _run_node(r"""
const assert = require("node:assert/strict");
const o = require("./web/static/outbox.js");
const failingFactory = {
  open() {
    const req = {};
    setTimeout(() => { req.error = new Error("SecurityError"); req.onerror && req.onerror(); }, 0);
    return req;
  },
};
const store = o.openOutbox({ idbFactory: failingFactory });
(async () => {
  // Contratto R-C/§11: se IDB non è utilizzabile, lo store deve funzionare
  // (fallback in-memory async) invece di rifiutare ogni operazione.
  await store.enqueue({ id: "out-1", client_msg_id: "cid", protocol: "signal", contact_id: "alice", text: "t" });
  assert.equal((await store.list()).length, 1);
})().catch((e) => { console.error(e); process.exitCode = 1; });
""")


# ── Re-verifica fix post-review (2026-09-28) ─────────────────────────────────


def test_indep_reconcile_restored_matches_newer_echo_and_flag_path():
    _run_node(r"""
const assert = require("node:assert/strict");
const rec = require("./web/static/reconcile.js");
// Percorso esplicito `restored: true` + eco SUCCESSIVA → riconcilia.
const optimistic = [{
  optimistic_id: "o1", client_msg_id: "c", protocol: "signal", contactId: "a",
  text: "Ciao", direction: "out", timestamp: 2000, known_message_ids: [],
  restored: true, optimisticStatus: "queued",
}];
const messages = [{
  id: "new1", direction: "out", text: "Ciao", timestamp: 2050,
  quote_timestamp: null, quote_author: null, quote_message: null,
  reply_to_message_id: null, attachment: null,
}];
const out = rec.reconcileOptimisticMessages(messages, optimistic, "signal", "a");
assert.equal(out.optimistic[0].confirmed_message_id, "new1");
assert.equal(out.visible.length, 0);
""")


def test_indep_fresh_send_reconciliation_not_regressed_by_guard():
    _run_node(r"""
const assert = require("node:assert/strict");
const rec = require("./web/static/reconcile.js");
// Invio fresco: known_message_ids NON vuoto → il guard temporale NON si applica
// e matcha il nuovo eco, non la riga vecchia con la stessa firma.
const optimistic = [{
  optimistic_id: "o2", client_msg_id: "c2", protocol: "signal", contactId: "a",
  text: "Ciao", direction: "out", timestamp: 3000, known_message_ids: ["old1"],
  optimisticStatus: "queued",
}];
const messages = [
  { id: "old1", direction: "out", text: "Ciao", timestamp: 1000, quote_timestamp: null, quote_author: null, quote_message: null, reply_to_message_id: null, attachment: null },
  { id: "new1", direction: "out", text: "Ciao", timestamp: 3050, quote_timestamp: null, quote_author: null, quote_message: null, reply_to_message_id: null, attachment: null },
];
const out = rec.reconcileOptimisticMessages(messages, optimistic, "signal", "a");
assert.equal(out.optimistic[0].confirmed_message_id, "new1");
""")


def test_indep_init_outbox_falls_back_to_memory_on_recover_failure():
    _run_node(r"""
const assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const code = app.slice(app.indexOf("async function initOutbox"), app.indexOf("async function clearOutbox"));
const opened = [];
const module = {
  openOutbox(opts) {
    const isMem = Boolean(opts && opts.backend);
    opened.push(isMem ? "mem" : "idb");
    if (!isMem) {
      return { recover: async () => { throw new Error("SecurityError"); }, list: async () => { throw new Error("x"); },
        subscribe: () => {}, flush: async () => ({ nextAttemptAt: 0 }) };
    }
    return { recover: async () => {}, list: async () => [], subscribe: () => {}, flush: async () => ({ nextAttemptAt: 0 }) };
  },
  createMemoryBackend() { return {}; },
};
globalThis.outboxModule = () => module;
globalThis.state = { outbox: null, outboxFlushing: false, outboxFlushPending: false, outboxFlushTimer: null, token: "", optimistic: [], active: null, messages: [] };
globalThis.window = { clearTimeout, setTimeout };
globalThis.renderMessages = () => {};
globalThis.flushOutbox = () => {};
vm.runInThisContext(code);
(async () => {
  await initOutbox();
  assert.deepEqual(opened, ["idb", "mem"], "deve ripiegare su memoria se recover/list fallisce");
  assert.ok(state.outbox, "state.outbox deve essere valorizzato con lo store in-memory");
})().catch((e) => { console.error(e); process.exitCode = 1; });
""")


def test_indep_registry_stale_sent_beyond_ttl_is_reclaimed():
    from web.send_registry import SendRegistry

    clock = _Clock()
    registry = SendRegistry(
        ttl_sent=600, max_entries=10**9, prune_interval=10**9, clock=clock
    )
    key = ("signal", "alice", "cid-hot")
    _, token = registry.claim_or_lookup(key, "fp")
    registry.mark_sent(key, token, "m", 0)
    clock.advance(10000)  # oltre il TTL, senza far scattare il prune
    outcome, new_token = registry.claim_or_lookup(key, "fp")
    assert outcome == "claimed", "una sent scaduta non deve più essere idempotente"
    assert new_token is not None
    entry = registry.snapshot(key)
    assert entry["status"] == "inflight"
    # L'handler precedente non può più chiudere il nuovo claim.
    registry.mark_sent(key, token, "old", 0)
    assert registry.snapshot(key)["status"] == "inflight"


def test_indep_fresh_empty_chat_echo_earlier_than_client_still_reconciles():
    _run_node(r"""
const assert = require("node:assert/strict");
const rec = require("./web/static/reconcile.js");
const optimistic = [{
  optimistic_id: "o", client_msg_id: "c", protocol: "signal", contactId: "a",
  text: "Ciao", direction: "out", timestamp: 5000, known_message_ids: [],
  optimisticStatus: "queued",
}];
const messages = [{
  id: "echo", direction: "out", text: "Ciao", timestamp: 4900,
  quote_timestamp: null, quote_author: null, quote_message: null,
  reply_to_message_id: null, attachment: null,
}];
const out = rec.reconcileOptimisticMessages(messages, optimistic, "signal", "a");
assert.equal(out.optimistic[0].confirmed_message_id, "echo");
""")
