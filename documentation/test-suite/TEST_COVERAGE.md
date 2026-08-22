# Analisi di copertura funzionale

Confronto tra aree funzionali del prodotto (ricavate dal codice) e test presenti. La copertura misurata dall'ultima run registrata (`docs/TEST_REPORT.md`, 2026-08-22) è ~90% globale con gate `fail_under = 68` in `pyproject.toml`; questa analisi è qualitativa, per area funzionale.

Legenda: **Robusta** = suite dedicata ampia; **Coperta** = test presenti ma su percorsi principali; **Parziale** = solo casi limite/indiretti; **Lacuna** = nessun test dedicato.

## 1. Aree per backend

| Area | Stato | Evidenze |
|---|---|---|
| Signal RPC client + fallback subprocess | Robusta | `test_backend_rpc`, `test_backend_send`, `test_signal_real_timestamp`, `test_backend_lazy_config` |
| Envelope parsing Signal (message/typing/receipt/edit) | Robusta | `test_ui_protocol`, fixture envelope in conftest, `test_edit_signal` |
| Cache SQLite (insert, dedup, status, unread, pruning) | Robusta | `test_backend_cache`, `test_cache_debounce`, `test_db_edit`, `test_open_or_create` |
| Migrazioni schema/versioning | Robusta | `test_db_schema_versioning`, `test_migrate_sqlite/protocol/status` |
| WhatsApp webhook ingest + ack sintetici | Robusta | `test_whatsapp_backend` (121), `test_whatsapp_fix_40_41` |
| WhatsApp receipt/canonicalizzazione id | Robusta | `test_whatsapp_receipt_id_match`, `test_whatsapp_read_receipt_fix` |
| WhatsApp storico/resync startup | Coperta | `test_wa_startup_resync`; il retry-loop di `fetch_history` è coperto indirettamente |
| WhatsApp resolver @lid / presence subscribe background | Parziale | esercitati in `test_whatsapp_backend` e `test_address_book`; i thread in background non hanno test di ciclo di vita dedicati |
| Telegram backend (QR, 2FA, entità, eventi) | Robusta | `Telegram/test_telegram_backend`, `Telegram/test_regression`, `tests/test_telegram` |
| Telegram receipt per id / reorder send | Coperta | `test_telegram_read_receipt_fix`, `test_telegram_send_reorder` |
| Manager multi-backend + routing | Robusta | `test_backends`, `test_address_book`, `test_edit_contract` |

## 2. Aree TUI/UI

| Area | Stato | Evidenze |
|---|---|---|
| Dispatch eventi (`tui/events.py`) | Robusta | `test_ui_protocol` (55) |
| Lista contatti: sort/filtri Ctrl+W/U/A | Robusta | `test_unread_filter`, `test_contact_grouping*` |
| Raggruppamento per persona | Robusta | `test_contact_grouping` (56) + integration |
| Contact picker (Ctrl+S) e rubrica | Robusta | `test_contact_picker`, `test_address_book` |
| Device link (Ctrl+L, QR, 2FA) | Coperta | `test_device_link_screen` — i worker di polling completamento sono mockati |
| Emoji picker + completamento alias | Coperta | `test_emoji_picker` |
| Chat view (finestra 20, refresh, load-more, merge) | Robusta | `test_refresh_chat`, `test_merge_cache_edit`, `test_image_caption` |
| Invio ottimistico + stato outgoing | Robusta | `test_send_persist_offthread`, `test_failed_send_status`, `test_outgoing_status_fallback`, `test_send_timing` |
| Edit messaggi (3 protocolli) | Robusta | `test_edit_*` (contract/flow/signal/whatsapp/telegram/db/merge) |
| Typing indicator | Robusta | `test_typing_indicator` |
| Status bar unread per-backend | Coperta | `test_status_backend_unread` |
| Download mode (Ctrl+D) + server HTTP | Coperta | `test_download_mode`, `test_backend_download` |
| Widget di basso livello | Coperta | `test_ui_components` |
| Render progressivo chunked lista | Parziale | esercitato via integration/grouping; nessun test dedicato al timer `_render_next_chunk` sotto carico |

## 3. Infrastruttura

| Area | Stato | Evidenze |
|---|---|---|
| Lock istanza singola / crash log entry point | Coperta | `test_signal_tui_lock` (crash log non verificato direttamente) |
| `install.sh` | Coperta | `test_install_script` |
| docker-compose (extra_hosts) | Parziale | `test_docker_compose_extra_hosts` (2 test puntuali) |
| Script CLI standalone (`link_account.py`, `link_whatsapp.py`, `purge_whatsapp_cache.py`) | Lacuna | la logica QR è coperta (`test_qr_ascii`, device link screen) ma gli script come processi no |
| Script migrazione one-shot (`migrate_cache_*.py`) | Coperta | `test_migrate_sqlite/protocol/status` |
| Webhook server socket-level (bind fallito, riavvio) | Parziale | handler testato; comportamento bind-in-uso non coperto |
| Polling worker loop completo (timeout typing, flush batch) | Parziale | effetti testati (`test_typing_indicator`, `test_cache_debounce`); il loop thread vero è neutralizzato nei test UI |
| `backend/download.py::get_local_ip` | Parziale | percorso SSH_CONNECTION implicito nei test download |

## 4. Sintesi delle lacune (candidati a nuovi test)

1. **Thread lifecycle**: SSE listener (retry ~1 s), poll worker (exit promptivo, flush differito), resolver @lid, presence sweep — oggi testati solo per effetto collaterale.
2. **Script CLI** di linking standalone e `purge_whatsapp_cache.py`.
3. **Casi degradati del webhook server**: porta occupata (`ensure_webhook_server → 0`), body enorme/chunked.
4. **Telegram media lazy** (`tgref:`): risoluzione path coperta parzialmente dentro `tests/test_telegram`; mancano test del download reale mockato.
5. **Render progressivo** sotto liste grandi (>50 righe) e interazione con filtri.
6. **`get_local_ip()`** fallback UDP.
7. **Integrità id/dedup al boot**: scenario `_update_message_id` multi-riga (UPDATE senza finestra/LIMIT) + `_dedup_messages_by_id` eseguita da `_load_cache` a ogni avvio — es. retry di un messaggio fallito con stesso testo → righe legittime cancellate come "duplicati" al riavvio. I test attuali coprono il caso a riga singola. (review P1-3)
8. **Lifecycle SSE**: doppio listener via `restart_sse` (oggi dead code, race latente) e comportamento del loop quando il generatore `listen_events` ritorna senza yield (log NameError/stale, nessun segnale UI durante un outage prolungato). (review P1-4)
9. **Race check-then-act sugli ingest**: due ingest concorrenti dello stesso messaggio (echo webhook vs `fetch_history` nel pool resync `ThreadPoolExecutor(4)`) che superano entrambi il dedup → riga doppia in DB; nessun lock applicativo sulle cache in-memory è testato. (review P1-2)

Queste lacune NON indicano codice rotto: sono aree dove l'esecuzione reale è difficile da simulare o dove la copertura arriva tramite percorsi indiretti.

## 5. Metriche registrate

- File di test: 57 (`tests/`) + 2 (`Telegram/`) = 59.
- Funzioni `test_*`: ~1.242 (prima di parametrizzazioni).
- Ultima run documentata (`docs/TEST_REPORT.md`): **1268/1268 pass**, coverage ~90%, lint/format puliti.
- Gate configurati: coverage `fail_under=68` (branch on), marker `integration` separato, `--strict-markers`.
