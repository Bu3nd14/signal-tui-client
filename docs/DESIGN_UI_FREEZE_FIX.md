# Design — Eliminazione dei freeze UI (Fix 1–3)

**Branch:** `fix/ui-freeze-sqlite`
**Vincolo assoluto:** i fix sono **behavior-preserving, ZERO regressioni**. Non cambia mai:
ordine messaggi, dedup (echo/invio ottimistico), status `sent/delivered/read`, contenuto di
cache in-memory e SQLite, schema DB, thread-safety, attribuzione del messaggio al backend per
protocollo. Si sposta **solo** lavoro fuori dal thread UI o si elimina lavoro ridondante.

**Baseline di riferimento:** `507 passed` (suite completa, 18s).

---

## Fix 1 — H2 (root cause): versioning schema con `PRAGMA user_version`

**File: solo `backend/db.py`.**

Oggi `_init_db()` chiama `_migrate_protocol_schema()` a ogni invocazione; quest'ultima fa
`DROP INDEX IF EXISTS idx_messages_contact` + `CREATE INDEX` **a ogni scrittura**.

Modifiche:
1. Aggiungere `_SCHEMA_VERSION = 1` (accanto a `CACHE_RETENTION_DAYS`).
2. Aggiungere helper `_current_schema_version(conn)` che legge `PRAGMA user_version`.
3. `_migrate_protocol_schema(conn)`:
   - **gate in testa:** `if _current_schema_version(conn) >= _SCHEMA_VERSION: return`.
   - **in coda:** `conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")` (persistito dal
     `commit()` già esistente in `_init_db`).
   - il corpo (check colonne `protocol`/`msg_id` + DROP/CREATE index) resta identico.
4. `_init_db()` resta invariato (continua a chiamare `_migrate_protocol_schema` + `commit`).

Invarianti: schema finale identico (stessa tabella, colonne `protocol`/`msg_id`, indice
`(protocol, contact_number, timestamp)`); idempotenza; retro-compatibilità DB legacy.

Test esistenti che DEVONO restare verdi: `tests/test_migrate_protocol.py` (tutti),
`tests/test_backend_cache.py`, `tests/test_migrate_sqlite.py`, `tests/test_wa_startup_resync.py`,
`tests/test_backends.py`, `tests/test_whatsapp_backend.py`.

---

## Fix 2 — H1/H5: persistenza SQLite fuori dal thread UI (invio)

**File: `backends/signal.py`, `backends/whatsapp.py`, `backends/telegram.py`, `tui/send.py`.**

Oggi `on_input_submitted` esegue `ingest_message(...)` (scrittura SQLite ~37ms) sincrona sul
thread UI.

Modifiche backend (tutti e tre):
1. Nuovo kwarg `persist: bool = True` su `ingest_message(contact_id, data, ts, persist=True)`.
   Se `persist=False` esegue solo dedup + append cache in-memory (identico a oggi) e **salta**
   `_add_message_to_cache`. Tutti i chiamanti esistenti (poll, webhook, fetch_history,
   resync, ingest real-id Telegram) usano il default `True` → comportamento invariato.
2. Nuovo helper `_persist_message(contact_id, data, ts)` che racchiude la chiamata
   `_add_message_to_cache(...)` con gli stessi identici argomenti oggi inline in
   `ingest_message` (Signal: default `protocol='signal'`/`msg_id=None`; WhatsApp:
   `protocol=PROTOCOL_WHATSAPP`+`msg_id=data.get("id")`; Telegram:
   `protocol=PROTOCOL_TELEGRAM`+`msg_id=data.get("id")` mantenendo il try/except attuale).
   Il ramo "upgrade echo" (`_update_message_id`) e `_add_cached_message` restano verbatim.

Modifiche `tui/send.py`:
- `ingest_backend.ingest_message(contact_id, data, ts, persist=False)` (riga 86).
- append `self._cache`, `_add_message`, `_seen_*`, clear input, `_cancel_reply` → invariati.
- `run_worker` passa al worker un payload `persist=(backend, contact_id, data, ts)`.
- `_send_message_worker(..., persist=None)`: **in cima**, prima di `if not self.selected_contact`,
  esegue `if persist is not None: backend._persist_message(contact_id, data, ts)`.
  → la INSERT avviene **prima** del `send_message_sync` di rete, garantendo che l'echo
  (che arriva solo dopo il send) trovi sempre la riga ottimistica (upgrade `_update_message_id`).

Race chiave mitigata: il seeding della cache backend resta **sincrono** sul thread UI
(`persist=False`), quindi il dedup echo continua a funzionare.

Test esistenti che DEVONO restare verdi: `tests/test_ui_protocol.py::TestSendOptimisticRouting::test_whatsapp_send_ingest_uses_whatsapp_backend`
(critico: `args[0]` invariato), `tests/test_backends.py::TestIngestDedup`,
`tests/test_whatsapp_backend.py`, `tests/test_backend_cache.py`, `tests/test_backend_send.py`,
`tests/test_backend_rpc.py`.

---

## Fix 3 — H24: download immagini WhatsApp asincrono

**File: `ui_components.py`, `tui/chat_view.py`.**

Oggi `_render_image_in_chat` → `manager.get_attachment_path` → per WhatsApp `download_media`
(urlopen timeout 60s) + `write_bytes`, tutto **sincrono sul thread UI**.

Modifiche `ui_components.py` (`ImageWidget`): aggiungere
`update_attachment(self, attachment_path, fallback_text)` che setta `self.attachment_path` e
chiama `self.update(fallback_text)`.

Modifiche `tui/chat_view.py` (`_render_image_in_chat`):
- Monta subito un `ImageWidget` placeholder (`path=None`, `attachment_id` valorizzato,
  testo `[🖼️ Image: {info} — loading…]`), `classes` in base a `is_mine`, `mount`+`scroll_end`.
  Se `attachment_id` vuoto → fallback permanente e `return`.
- `run_worker(..., thread=True, exclusive=False)` su un nuovo `_resolve_attachment_worker`
  che calcola `path = manager.get_attachment_path(protocol, attachment_id)` (captura
  `protocol` prima, non riletto da `selected_contact`), poi
  `call_from_thread(self._finish_attachment_resolve, widget, path, info)`.
- `_finish_attachment_resolve` (thread UI): `if not widget.is_mounted: return`; se
  `path is None` → `update_attachment(None, f"[🖼️ Image: {info}]")`; altrimenti
  `update_attachment(path, f"[🖼️ Image: {path.name} — Click Enter to View]")`.

Invarianti: testo finale identico a oggi; `ImageWidget` con path=None resta cliccabile
(`on_click`/`key_enter` emettono `ImageClicked` se `attachment_path or attachment_id`), la
lazy-resolution su click resta in `download.py`. Path di render da cache (`_build_message_widgets`)
invariato.

Test esistenti che DEVONO restare verdi: `tests/test_refresh_chat.py::test_image_messages_mount_from_cache`
(critico), `tests/test_ui_components.py::TestImageWidget`, `tests/test_whatsapp_backend.py`,
`tests/test_backend_attachments.py`, `tests/test_backends.py`.

---

## Ordine e commit

1. **Fix 1** (db.py) → commit. Poi `pytest` completo: deve restare 507 passed.
2. **Fix 2** (backends + send.py) → commit. Poi `pytest` completo: 507 passed.
3. **Fix 3** (chat_view + ui_components) → commit. Poi `pytest` completo: 507 passed.

Un commit per fix. In nessun punto la suite deve scendere sotto 507 passed.
