"use strict";

// Outbox client-side persistente (IndexedDB) per il retry testo che sopravvive
// alla disconnessione.  Vedi docs/DESIGN_WEB_SEND_RETRY_DISCONNECTION.md §7.
//
// Solo testo (v1): gli allegati restano sul percorso legacy sincrono.  Lo store
// è costruito da una factory iniettabile (`openOutbox`); se `indexedDB` è
// assente (private mode) si degrada a un outbox in-memory asincrono con lo
// stesso contratto.  La logica pura è esportata per i test (nessun npm).

const OUTBOX_DB_NAME = "signal-tui-web";
const OUTBOX_DB_VERSION = 1;
const OUTBOX_STORE = "outbox";
const OUTBOX_LOCK_NAME = "outbox-flush";
const OUTBOX_CHANNEL_NAME = "signal-tui-outbox";

// Finestra di auto-resend: allineata al TTL `sent` del registry server (§7.7).
const OUTBOX_AUTO_RESEND_MAX_AGE_MS = 10 * 60 * 1000;
// Retention di cancellazione del record, ortogonale alla finestra (§11, D5).
const OUTBOX_RETENTION_MS = 2 * 24 * 60 * 60 * 1000;
const OUTBOX_BASE_DELAY_MS = 1000;
const OUTBOX_JITTER_MAX_MS = 1000;
const OUTBOX_MAX_DELAY_MS = 30000;
// Difesa anti-spam: oltre questo numero di fallimenti il record non si
// auto-ritenta più (resta `failed`, retry manuale).
const OUTBOX_MAX_ATTEMPTS = 10;

const PERSISTED_STATUSES = ["queued", "sending", "confirm", "failed"];

// ── Logica pura ──────────────────────────────────────────────────────────────

// Converte la reply della SPA (shape camelCase di state.replyTo) nella forma
// RAW persistita nel record.  `buildSendPayload` rigenera SEMPRE il payload dal
// record, così submit e flush non possono divergere (N6).
function replyToQuoteRecord(reply, protocol) {
  if (!reply) return null;
  return {
    quote_timestamp: reply.timestamp ?? null,
    quote_author: reply.quoteAuthor ?? null,
    quote_message: reply.quoteMessage ?? null,
    reply_to_message_id: protocol === "signal" ? null : (reply.id ?? null),
    quote_content_type: reply.contentType ?? null,
    quote_attachment_id: reply.attachmentId ?? null,
    isMedia: Boolean(reply.isMedia),
  };
}

// Unica fonte del body POST /api/send per il testo; specchio di
// app.js:2340-2350.  `reply` è la forma RAW persistita (snake_case).
function buildSendPayload(active, text, reply) {
  const payload = {
    protocol: active.protocol,
    contact_id: active.id,
    text,
  };
  if (!reply) return payload;
  payload.quote_timestamp = reply.quote_timestamp ?? null;
  payload.quote_author = reply.quote_author ?? null;
  payload.quote_message =
    active.protocol === "signal" && reply.isMedia
      ? ""
      : (reply.quote_message ?? null);
  if (active.protocol === "signal") {
    if (reply.quote_content_type) {
      payload.quote_content_type = reply.quote_content_type;
    }
    if (reply.quote_attachment_id) {
      payload.quote_attachment_id = reply.quote_attachment_id;
    }
  } else {
    payload.reply_to_message_id = reply.reply_to_message_id ?? null;
  }
  return payload;
}

// Tabella ordinata §7.2: prima corrispondenza vince.  Terzo esito `suspend`
// (401) che non scarta e non attiva il backoff.
function classifyHttpFailure(error) {
  if (error == null) return "terminal";
  if (error.name === "AbortError") return "retryable";
  if (error.status === 401) return "suspend";
  if (error.status == null) return "retryable";
  if (error.status === 408 || error.status === 429) return "retryable";
  if (error.status === 409) return "retryable";
  if (error.status === 422) return "terminal";
  if (error.status === 501) return "terminal";
  if (error.status >= 500) return "retryable";
  if (error.status >= 400) return "terminal";
  return "terminal";
}

// Backoff esponenziale con jitter, cap 30s.  `attempt` è 1-indexed (numero di
// fallimenti già registrati).
function computeClientDelay(attempt, random = Math.random) {
  const exponent = Math.max(1, Number(attempt) || 1);
  const base = OUTBOX_BASE_DELAY_MS * 2 ** (exponent - 1);
  const jitter = random() * OUTBOX_JITTER_MAX_MS;
  return Math.min(OUTBOX_MAX_DELAY_MS, base + jitter);
}

function normalizeRecord(record) {
  const now = Date.now();
  return {
    id: record.id || `out-${record.client_msg_id}`,
    client_msg_id: record.client_msg_id,
    optimistic_id: record.optimistic_id ?? null,
    protocol: record.protocol,
    contact_id: String(record.contact_id),
    text: record.text || "",
    timestamp: Number.isFinite(record.timestamp) ? record.timestamp : now,
    quote: record.quote ? { ...record.quote } : null,
    batch_id: record.batch_id ?? null,
    attachments: Array.isArray(record.attachments) ? record.attachments : [],
    status: PERSISTED_STATUSES.includes(record.status) ? record.status : "queued",
    attempts: Number.isFinite(record.attempts) ? record.attempts : 0,
    dispatched: Boolean(record.dispatched),
    last_attempt_at: Number.isFinite(record.last_attempt_at)
      ? record.last_attempt_at
      : 0,
    next_attempt_at: Number.isFinite(record.next_attempt_at)
      ? record.next_attempt_at
      : 0,
    created_at: Number.isFinite(record.created_at) ? record.created_at : now,
    updated_at: Number.isFinite(record.updated_at) ? record.updated_at : now,
  };
}

// ── Backend ──────────────────────────────────────────────────────────────────

function cloneRecord(record) {
  return record ? JSON.parse(JSON.stringify(record)) : null;
}

function createMemoryBackend() {
  const records = new Map();
  return {
    async all() {
      return [...records.values()].map(cloneRecord);
    },
    async get(id) {
      return cloneRecord(records.get(id) || null);
    },
    async put(record) {
      records.set(record.id, cloneRecord(record));
    },
    async delete(id) {
      records.delete(id);
    },
    async clear() {
      records.clear();
    },
  };
}

function idbRequest(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function createIdbBackend(factory) {
  const memory = createMemoryBackend();
  let dbPromise = null;
  let degraded = false;

  function open() {
    if (!dbPromise) {
      dbPromise = new Promise((resolve, reject) => {
        const request = factory.open(OUTBOX_DB_NAME, OUTBOX_DB_VERSION);
        request.onupgradeneeded = () => {
          const db = request.result;
          if (!db.objectStoreNames.contains(OUTBOX_STORE)) {
            db.createObjectStore(OUTBOX_STORE, { keyPath: "id" });
          }
        };
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      });
    }
    return dbPromise;
  }

  async function run(mode, operation, primary, ...args) {
    if (degraded) return memory[operation](...args);
    let db;
    try {
      db = await open();
    } catch {
      // IDB presente ma inutilizzabile (Safari private mode / SecurityError /
      // quota all'open): degrada all'outbox in-memory asincrono (§11/R-C).
      degraded = true;
      return memory[operation](...args);
    }
    const store = db.transaction(OUTBOX_STORE, mode).objectStore(OUTBOX_STORE);
    return primary(store);
  }

  return {
    all() {
      return run("readonly", "all", (store) => idbRequest(store.getAll()));
    },
    get(id) {
      return run("readonly", "get", (store) => idbRequest(store.get(id)), id);
    },
    put(record) {
      return run(
        "readwrite",
        "put",
        (store) => idbRequest(store.put(record)),
        record,
      );
    },
    delete(id) {
      return run(
        "readwrite",
        "delete",
        (store) => idbRequest(store.delete(id)),
        id,
      );
    },
    clear() {
      return run("readwrite", "clear", (store) => idbRequest(store.clear()));
    },
  };
}

// ── Store ────────────────────────────────────────────────────────────────────

function openOutbox(options = {}) {
  const backend =
    options.backend ||
    (() => {
      const factory =
        options.idbFactory ||
        (typeof globalThis !== "undefined" ? globalThis.indexedDB : null);
      return factory ? createIdbBackend(factory) : createMemoryBackend();
    })();
  return createStore(backend, options);
}

function createStore(backend, options = {}) {
  const clock = options.now || Date.now;
  const random = options.random || Math.random;
  let channel;
  let channelReady = false;

  function getChannel() {
    if (!channelReady) {
      channelReady = true;
      if (typeof window !== "undefined" && typeof BroadcastChannel !== "undefined") {
        try {
          channel = new BroadcastChannel(OUTBOX_CHANNEL_NAME);
        } catch {
          channel = null;
        }
      }
    }
    return channel || null;
  }

  function broadcast(message) {
    const ch = getChannel();
    if (!ch) return;
    try {
      ch.postMessage(message);
    } catch {
      // Il fan-out tra tab è best-effort.
    }
  }

  function subscribe(handler) {
    const ch = getChannel();
    if (!ch) return () => {};
    const listener = (event) => handler(event.data);
    ch.addEventListener("message", listener);
    return () => ch.removeEventListener("message", listener);
  }

  function shouldConfirm(record, now) {
    return (
      record.dispatched &&
      now - record.last_attempt_at > OUTBOX_AUTO_RESEND_MAX_AGE_MS
    );
  }

  async function pruneExpired(now = clock()) {
    const records = await backend.all();
    for (const record of records) {
      if (now - record.created_at > OUTBOX_RETENTION_MS) {
        await backend.delete(record.id);
      }
    }
  }

  async function enqueue(record) {
    const normalized = normalizeRecord(record);
    await pruneExpired();
    await backend.put(normalized);
    broadcast({ type: "enqueue", id: normalized.id });
    return normalized;
  }

  async function list() {
    return backend.all();
  }

  async function remove(id) {
    await backend.delete(id);
    broadcast({ type: "remove", id });
  }

  // Retry manuale: rimette in coda azzerando il backoff (R-D3).
  async function retry(id) {
    const record = await backend.get(id);
    if (!record) return null;
    record.status = "queued";
    record.attempts = 0;
    record.dispatched = false;
    record.last_attempt_at = 0;
    record.next_attempt_at = 0;
    record.updated_at = clock();
    await backend.put(record);
    broadcast({ type: "retry", id });
    return record;
  }

  // Recovery al boot dei record orfani `sending` (tab/processo ucciso dopo il
  // claim PRE-fetch): entro la finestra → `queued`, oltre → `confirm`.
  async function recover(now = clock()) {
    const records = await backend.all();
    const updated = [];
    for (const record of records) {
      if (record.status !== "sending") continue;
      record.status = shouldConfirm(record, now) ? "confirm" : "queued";
      record.updated_at = now;
      await backend.put(record);
      updated.push(record);
    }
    return updated;
  }

  async function clear() {
    await backend.clear();
    broadcast({ type: "clear" });
  }

  async function flush({ send, onUpdate } = {}) {
    const run = () => runFlush({ send, onUpdate });
    if (
      typeof navigator !== "undefined" &&
      navigator.locks &&
      typeof navigator.locks.request === "function"
    ) {
      return navigator.locks.request(OUTBOX_LOCK_NAME, run);
    }
    return run();
  }

  async function runFlush({ send, onUpdate } = {}) {
    await pruneExpired();
    const records = (await backend.all()).sort(
      (a, b) => a.created_at - b.created_at,
    );
    let nextAttemptAt = 0;
    const noteNext = (value) => {
      if (value && (!nextAttemptAt || value < nextAttemptAt)) {
        nextAttemptAt = value;
      }
    };

    for (const record of records) {
      if (record.status === "failed" || record.status === "confirm") continue;
      if (record.status === "sending") continue;
      if (shouldConfirm(record, clock())) {
        record.status = "confirm";
        record.updated_at = clock();
        await backend.put(record);
        onUpdate?.(record);
        continue;
      }
      if (record.next_attempt_at && clock() < record.next_attempt_at) {
        noteNext(record.next_attempt_at);
        continue;
      }

      // Claim atomico PRE-fetch (R-B): anche se il tab muore subito dopo, il
      // record riflette "tentativo partito".
      record.status = "sending";
      record.dispatched = true;
      record.last_attempt_at = clock();
      record.updated_at = clock();
      await backend.put(record);
      onUpdate?.(record);

      try {
        const result = await send(record);
        await backend.delete(record.id);
        broadcast({ type: "sent", id: record.id });
        onUpdate?.({ ...record, status: "sent", result });
      } catch (error) {
        const kind = classifyHttpFailure(error);
        if (kind === "terminal") {
          // `attempts` conta i tentativi FALLITI (incremento post-fetch).
          record.attempts += 1;
          record.status = "failed";
          record.updated_at = clock();
          await backend.put(record);
          onUpdate?.(record);
        } else if (kind === "suspend") {
          // 401 non deliberato: resta in coda, nessun backoff attivo.
          record.status = "queued";
          record.next_attempt_at = 0;
          record.updated_at = clock();
          await backend.put(record);
          onUpdate?.(record);
          break;
        } else {
          record.attempts += 1;
          if (record.attempts >= OUTBOX_MAX_ATTEMPTS) {
            // Difesa anti-spam: oltre il cap resta `failed` (retry manuale).
            record.status = "failed";
          } else {
            record.status = "queued";
            record.next_attempt_at =
              clock() + computeClientDelay(record.attempts, random);
            noteNext(record.next_attempt_at);
          }
          record.updated_at = clock();
          await backend.put(record);
          onUpdate?.(record);
        }
      }
    }
    return { nextAttemptAt };
  }

  return {
    enqueue,
    list,
    remove,
    retry,
    recover,
    clear,
    flush,
    subscribe,
    pruneExpired,
  };
}

const SignalTuiOutbox = {
  OUTBOX_AUTO_RESEND_MAX_AGE_MS,
  OUTBOX_RETENTION_MS,
  OUTBOX_MAX_ATTEMPTS,
  classifyHttpFailure,
  computeClientDelay,
  buildSendPayload,
  replyToQuoteRecord,
  normalizeRecord,
  createMemoryBackend,
  createIdbBackend,
  openOutbox,
};

if (typeof window !== "undefined") window.SignalTuiOutbox = SignalTuiOutbox;
if (typeof module !== "undefined") module.exports = SignalTuiOutbox;
