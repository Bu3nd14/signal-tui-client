"use strict";

// Cache client-side dei messaggi per chat (IndexedDB): apertura chat
// istantanea e consultabile offline, e sync incrementale col server.
//
// Ogni chat ("proto:id") salva gli ultimi N messaggi così come li restituisce
// /api/messages.  La sync chiede solo la coda: i messaggi con timestamp >= quello
// del K-esimo messaggio più recente in cache (DELTA_OVERLAP).  La sovrapposizione
// riallinea anche spunte, modifiche e reazioni dei messaggi recenti, che il DB
// non marca con un "updated_at".  Se il server restituisce la coda piena
// (>= limit) potrebbe esserci un buco: si scarta la parte vecchia in cache.
//
// Senza indexedDB (private mode) degrada a una cache in-memory con lo stesso
// contratto.  La logica pura è esportata per i test (nessun npm).

const MESSAGE_CACHE_DB_NAME = "signal-tui-web-messages";
const MESSAGE_CACHE_DB_VERSION = 1;
const MESSAGE_CACHE_STORE = "chats";
const DELTA_OVERLAP = 30;
const MAX_CACHED_CHATS = 100;

// ── Logica pura ──────────────────────────────────────────────────────────────

function cacheKey(protocol, contactId) {
  return `${protocol}:${contactId}`;
}

function messageTimestamp(message) {
  const value = Number(message?.timestamp);
  return Number.isFinite(value) ? value : 0;
}

// Timestamp da cui chiedere il delta, o null (nessuna cache: finestra completa).
function deltaSince(cached, overlap = DELTA_OVERLAP) {
  if (!Array.isArray(cached) || !cached.length) return null;
  return messageTimestamp(cached[Math.max(0, cached.length - overlap)]);
}

// Unisce la coda fresca alla cache: tiene i messaggi in cache più vecchi di
// `since`, sostituisce il resto con `fresh` (il server è la verità sulla coda).
function mergeDelta(cached, fresh, since, limit) {
  let merged;
  if (since == null || !Array.isArray(cached) || fresh.length >= limit) {
    merged = [...fresh];
  } else {
    const freshIds = new Set(fresh.map((message) => String(message.id)));
    const kept = cached.filter((message) =>
      messageTimestamp(message) < since && !freshIds.has(String(message.id)));
    merged = [...kept, ...fresh];
  }
  return merged.slice(-limit);
}

// ── Store ────────────────────────────────────────────────────────────────────

function createMemoryStore() {
  const chats = new Map();
  return {
    persistent: false,
    async get(key) {
      return chats.get(key) || null;
    },
    async put(record) {
      chats.set(record.key, record);
    },
    async all() {
      return [...chats.values()];
    },
    async delete(key) {
      chats.delete(key);
    },
  };
}

function createIndexedDbStore(idb) {
  let dbPromise = null;
  function open() {
    if (!dbPromise) {
      dbPromise = new Promise((resolve, reject) => {
        const request = idb.open(MESSAGE_CACHE_DB_NAME, MESSAGE_CACHE_DB_VERSION);
        request.onupgradeneeded = () => {
          if (!request.result.objectStoreNames.contains(MESSAGE_CACHE_STORE)) {
            request.result.createObjectStore(MESSAGE_CACHE_STORE, { keyPath: "key" });
          }
        };
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      });
    }
    return dbPromise;
  }
  async function run(mode, action) {
    const db = await open();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(MESSAGE_CACHE_STORE, mode);
      const request = action(tx.objectStore(MESSAGE_CACHE_STORE));
      tx.oncomplete = () => resolve(request?.result);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
  }
  return {
    persistent: true,
    async get(key) {
      return (await run("readonly", (store) => store.get(key))) || null;
    },
    async put(record) {
      await run("readwrite", (store) => store.put(record));
    },
    async all() {
      return (await run("readonly", (store) => store.getAll())) || [];
    },
    async delete(key) {
      await run("readwrite", (store) => store.delete(key));
    },
  };
}

function openMessageCache(options = {}) {
  const idb = options.indexedDB
    ?? (typeof globalThis !== "undefined" ? globalThis.indexedDB : null);
  let store = idb ? createIndexedDbStore(idb) : createMemoryStore();
  // Un errore IndexedDB (quota, storage bloccato) non deve rompere la chat:
  // si passa alla cache in-memory per il resto della sessione.
  async function guarded(method, ...args) {
    try {
      return await store[method](...args);
    } catch (error) {
      if (!store.persistent) throw error;
      store = createMemoryStore();
      return store[method](...args);
    }
  }
  return {
    async load(protocol, contactId) {
      const record = await guarded("get", cacheKey(protocol, contactId));
      return record ? record.messages : null;
    },
    async save(protocol, contactId, messages) {
      await guarded("put", {
        key: cacheKey(protocol, contactId),
        messages,
        savedAt: Date.now(),
      });
    },
    // Tiene solo le `max` chat usate più di recente.
    async prune(max = MAX_CACHED_CHATS) {
      const records = await guarded("all");
      if (records.length <= max) return;
      records.sort((a, b) => b.savedAt - a.savedAt);
      for (const record of records.slice(max)) await guarded("delete", record.key);
    },
  };
}

const SignalTuiMessageCache = {
  DELTA_OVERLAP,
  MAX_CACHED_CHATS,
  cacheKey,
  deltaSince,
  mergeDelta,
  openMessageCache,
  createMemoryStore,
};

if (typeof window !== "undefined") window.SignalTuiMessageCache = SignalTuiMessageCache;
if (typeof module !== "undefined") module.exports = SignalTuiMessageCache;
