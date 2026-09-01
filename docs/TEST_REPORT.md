# Test Report — Signal TUI Client

**Data:** 2026-08-24
**Git commit:** `7d483cc` (master)
**Python:** 3.12.3
**Stato:** ✅ **1389/1389 test superati** + 7 test live opzionali (gated da `LIVE_TESTS=1`) · coverage 80% (gate 68%) · lint/format puliti

---

## Riepilogo

| Modulo | File | Test | Esito |
|--------|------|------|-------|
| WhatsApp Backend | `test_whatsapp_backend.py` | 141 | ✅ |
| Address Book | `test_address_book.py` | 82 | ✅ |
| Grouping Contatti | `test_contact_grouping.py` | 66 | ✅ |
| Unread Filter | `test_unread_filter.py` | 65 | ✅ |
| Backends (Manager) | `test_backends.py` | 61 | ✅ |
| UI Protocol | `test_ui_protocol.py` | 55 | ✅ |
| Telegram Backend | `test_telegram.py` | 52 | ✅ |
| Contact Picker | `test_contact_picker.py` | 49 | ✅ |
| UI Components | `test_ui_components.py` | 37 | ✅ |
| Telegram Edit | `test_telegram_edit.py` | 34 | ✅ |
| Edit WhatsApp | `test_edit_whatsapp.py` | 33 | ✅ |
| Device Link Screen | `test_device_link_screen.py` | 32 | ✅ |
| Typing Indicator | `test_typing_indicator.py` | 29 | ✅ |
| Send Persist Offthread | `test_send_persist_offthread.py` | 28 | ✅ |
| Image Caption | `test_image_caption.py` | 28 | ✅ |
| Edit Signal | `test_edit_signal.py` | 28 | ✅ |
| Refresh Chat | `test_refresh_chat.py` | 27 | ✅ |
| WhatsApp Fix 40/41 | `test_whatsapp_fix_40_41.py` | 26 | ✅ |
| Emoji Picker | `test_emoji_picker.py` | 23 | ✅ |
| Cache (SQLite) | `test_backend_cache.py` | 21 | ✅ |
| WA Receipt ID Match | `test_whatsapp_receipt_id_match.py` | 20 | ✅ |
| TG Read Receipt Fix | `test_telegram_read_receipt_fix.py` | 20 | ✅ |
| TUI Integration | `test_tui_integration.py` | 18 | ✅ |
| Config | `test_config.py` | 18 | ✅ |
| DB Edit | `test_db_edit.py` | 17 | ✅ |
| Install Script | `test_install_script.py` | 16 | ✅ |
| Failed Send Status | `test_failed_send_status.py` | 16 | ✅ |
| WA Startup/Resync | `test_wa_startup_resync.py` | 15 | ✅ |
| Backend Download | `test_backend_download.py` | 15 | ✅ |
| WA Read Receipt Fix | `test_whatsapp_read_receipt_fix.py` | 14 | ✅ |
| Status Backend Unread | `test_status_backend_unread.py` | 14 | ✅ |
| Signal Real Timestamp | `test_signal_real_timestamp.py` | 14 | ✅ |
| RPC / Daemon | `test_backend_rpc.py` | 14 | ✅ |
| Edit Flow | `test_edit_flow.py` | 13 | ✅ |
| Backend Connect | `test_backend_connect.py` | 13 | ✅ |
| Migrazione Protocollo | `test_migrate_protocol.py` | 12 | ✅ |
| Open/Create | `test_open_or_create.py` | 11 | ✅ |
| Backend Webhook | `test_backend_webhook.py` | 11 | ✅ |
| Edit Contract | `test_edit_contract.py` | 10 | ✅ |
| Download Mode | `test_download_mode.py` | 10 | ✅ |
| Cache Debounce | `test_cache_debounce.py` | 10 | ✅ |
| Merge Cache Edit | `test_merge_cache_edit.py` | 9 | ✅ |
| Image Async Download | `test_image_async_download.py` | 9 | ✅ |
| Reply Media (#37) | `test_reply_media.py` | 7 | ✅ |
| Outgoing Status Fallback | `test_outgoing_status_fallback.py` | 7 | ✅ |
| Models (#37) | `test_models.py` | 7 | ✅ |
| Live integration (#37) | `test_live_quote_media.py` | 7 | ⏭️ gated (`LIVE_TESTS=1`) |
| Lock File | `test_signal_tui_lock.py` | 6 | ✅ |
| Migrazione Status | `test_migrate_status.py` | 6 | ✅ |
| DB Schema Versioning | `test_db_schema_versioning.py` | 6 | ✅ |
| Invio messaggi | `test_backend_send.py` | 6 | ✅ |
| Migrazione SQLite | `test_migrate_sqlite.py` | 5 | ✅ |
| Grouping Integration | `test_contact_grouping_integration.py` | 5 | ✅ |
| Lazy Config (CI) | `test_backend_lazy_config.py` | 5 | ✅ |
| Attachment | `test_backend_attachments.py` | 5 | ✅ |
| QR ASCII | `test_qr_ascii.py` | 4 | ✅ |
| Contatti | `test_backend_contacts.py` | 4 | ✅ |
| Send Timing | `test_send_timing.py` | 3 | ✅ |
| Docker Compose | `test_docker_compose_extra_hosts.py` | 2 | ✅ |
| TG Send Reorder | `test_telegram_send_reorder.py` | 1 | ✅ |
| Telegram Regression | `tests/protocols/telegram/test_regression.py` | 39 | ✅ |
| Telegram Backend | `tests/protocols/telegram/test_telegram_backend.py` | 35 | ✅ |
| **Totale** | | **1396** | **✅ 1389 + 7 gated** |

---

## Novità — Ultimi aggiornamenti

### Bug #37 — Quote media (PR #53: V2 + piano B, 24/08/2026) ✅ RISOLTO
- **V2** (design `DESIGN_QUOTE_MEDIA_37_V2.md`): ingresso con segnaposto tipizzato
  `quote_text` nei 3 backend (`media_quote_placeholder` in `models.py`, priorità caption);
  uscita con `ImageWidget.ReplyRequested` (Alt+click/Alt+R, click/Enter → modal);
  contratto display-vs-filo (`quote_message` = caption o `""`, mai segnaposto/omesso).
- **Piano B** (design `DESIGN_QUOTE_MEDIA_37_PLANB.md`): su Signal serve anche
  `quoteAttachments` (`contentType:filename:previewFile`) per mostrare la thumbnail;
  persistenza di `content_type` (colonna + migrazione) e backfill per i media legacy
  (`migrate_content_type.py`).
- **Nuovi test**: `test_models.py` (7), `test_reply_media.py` (7), `test_live_quote_media.py`
  (7, live/gated); +13 su `test_backends.py`/`test_send_persist_offthread.py`/
  `test_ui_components.py`/`test_refresh_chat.py`/`test_backend_rpc.py`/`test_whatsapp_backend.py`/
  `test_telegram.py`.
- **Test live sul filo reale** (account "Roberto BMW", `make live-test`): E1/E2/E3/E7 Signal +
  E5 WhatsApp + E6 Telegram **verdi**; confermato sul device che la quote Signal mostra
  l'immagine quotata. E4 (ingresso) manuale (`make live-test-manual`).
- `docs/BUGS.md`: #37 → RISOLTO (PR #54); follow-up **#57** (quote media non-immagine Signal) tracciato.

### Raggruppamento contatti per persona (PR #25)
- La lista principale raggruppa la stessa persona sui diversi backend in **un solo header** (+ una riga membro per protocollo: `📱 Signal`, `💬 WhatsApp`, `📨 Telegram`).
- Gruppi **collassati di default**; toggle con click/`Enter`/`space` sull'header (solo display, nessun rebuild).
- Ordine gruppi per recency del membro default; membri in ordine **fisso** Signal→WhatsApp→Telegram.
- `backends/whatsapp.py`: `extras["phone"]` da `@c.us`/`@lid` → i contatti WhatsApp si fondono con Signal/Telegram.
- Design: `docs/DESIGN_CONTACT_GROUPING.md`.

### Vista piatta nei filtri + badge per-backend (PR #32, #34)
- Con filtro singolo (`Ctrl+W`) la lista diventa **flat** (solo header, senza chevron); click/Enter apre la chat del protocollo filtrato.
- Badge unread per-backend con icone in vista filtro, ordine fisso; poi **rifinito**: in ogni filtro il badge mostra **solo l'unread della vista filtrata** (l'informativa cross-backend è la status bar).

### Filtro "solo non letti" + status bar clickabile (PR #37)
- **`Ctrl+U`**: toggle "solo non letti" (ortogonale al filtro backend); **`Ctrl+A`**: torna alla vista All.
- **Status bar** con contatore per-backend (`📱 N  💬 N  📨 N`, `-` se 0), segmenti **clickabili** (con non letti → filtro+unread; senza → solo filtro protocollo) e allineati a destra.
- Precedenza default < transiente < errore permanente; `_select_contact` riapplica la visibilità sotto unread.
- Design: `docs/DESIGN_UNREAD_FILTER.md`.

### Altre feature recenti
- **Foto Telegram dallo storico scaricabili** (download lazy via `tgref:`, PR #22).
- **Edit messaggi** Signal/WhatsApp/Telegram (Alt+click o `Alt+E`, PR #23).
- **Caption foto come bolla dedicata** + allineamento foto inviate da cache (PR #21).
- **Contatore unread per-backend in status bar** (PR #36).

> Nota: le tabelle dettagliate per modulo nelle sezioni successive sono snapshot storici; la tabella riepilogativa sopra è la fonte aggiornata.

### Telegram Backend (branch `feature/telegram-backend`, merged)

- **`backends/telegram.py`** (~780 righe): `TelegramBackend` con Telethon, pattern identico a Signal
  - Thread + event loop asyncio dedicato per MTProto
  - Queue-based event bridge: handler Telethon → `poll_once()`
  - `_entity_to_contact()` per User/Chat/Channel → `ChatContact`
  - `_message_to_chat_event()` per Message → `ChatEvent` con supporto media/quote
  - `ingest_message()` con dedup + persistenza SQLite
  - `fetch_recent_history()`: recupera ultimi 20 messaggi per contatto all'avvio
  - `get_pairing_qr()`: QR login con supporto 2FA (`complete_2fa`)
  - `process_receipt()`: ricevute di lettura via `UpdateReadHistoryOutbox`
  - `mark_read_sync()`: persiste stato lettura su SQLite
  - `send_message_sync()`: invio con `run_coroutine_threadsafe`
- **`device_link_screen.py`**: QR Telegram nel picker (Ctrl+L) con password 2FA inline
- **`signal_tui.py`**: `_on_backend_ready` merge atomico, `_connect_telegram` worker, filtro Ctrl+W a 4 protocolli
- **CSS**: colore unificato `#0088cc` per tutti i protocolli, distinzione via emoji (📱📨💬)
- **74 nuovi test** in `Telegram/`: 35 backend + 39 regressione
- **`PERF_ANALYSIS.md`**: aggiornato con pattern Telegram e stato attuale

### Lazy Contact Render (10 commit, `87f52c5..2ba5e05`)
- **Progressive render**: contatti caricati 50 per frame via `set_timer`, UI mai bloccata
- **Visibility toggle (Ctrl+W)**: il filtro protocollo imposta `display=True/False` senza distruggere widget
- **Merge path**: quando nuovi contatti arrivano (WhatsApp dopo Signal), solo i nuovi ListItem vengono creati — zero `clear()`, zero flash
- **Early paint**: `_update_contacts_ui` chiamata subito dopo Signal, poi dopo WhatsApp — contatti visibili entro ~200ms
- **System messages auto-dismiss**: i messaggi `is_info` si auto-cancellano dopo 3 secondi
- **Test Telegram**: 74 test scritti (35 backend + 39 regressione) in `Telegram/`, in attesa del backend

### WhatsApp Image Support
Aggiunti 14 test nel modulo `test_whatsapp_backend.py` per download e rendering immagini WhatsApp.

### Backend Manager + Seed Cache + Webhook Registration
Aggiunti 9 test aggiuntivi in `test_whatsapp_backend.py` portando il totale a 101:
- **SeedCacheFromDB** (7 test): seeding cache da SQLite, dedup messaggi, echo invio ottimistico
- **WhatsAppWebhookRegistration** (7 test): registrazione webhook WAHA
- **WAHAContract** (8 test): percorsi REST, frame WS, estrazione immagini

### Nuovi test RPC (da 8 → 12)
Aggiunti 4 test in `test_backend_rpc.py` per timeout HTTP e retry.

### Altro
- `test_install_script.py`: +1 test (verifica Java 25)
- Refactoring multi-backend: envelope parsing spostato da `signal_tui.py` a `backends/signal.py`

## Dettaglio test

### 🔍 Emoji Picker (`test_emoji_picker.py`) — 16 test

| Test | Descrizione |
|------|-------------|
| `test_search_by_name` | Cerca "smile" → trova risultati |
| `test_search_case_insensitive` | Cerca "SMILE" (maiuscolo) → stesso risultato |
| `test_search_with_underscores` | Cerca "face_with_tears" → trova 😂 |
| `test_search_no_results` | Stringa inesistente → lista vuota |
| `test_search_max_results` | Limite massimo risultati (5) |
| `test_search_empty_query` | Query vuota → max 30 risultati |
| `test_suggestions_start_with_prefix` | Prefisso "smi" → suggerimenti che iniziano con "smile" |
| `test_suggestions_no_match` | Prefisso inesistente → lista vuota |
| `test_suggestions_max_results` | Limite massimo suggerimenti (3) |
| `test_replace_simple` | `:smile:` → 😄 |
| `test_replace_multiple` | `:smile: :wave:` → 😄 👋 |
| `test_replace_no_alias` | Testo senza alias → invariato |
| `test_replace_invalid_alias` | Alias inesistente → lasciato com'è |
| `test_replace_partial` | Solo alias validi vengono sostituiti |
| `test_alias_to_emoji_populated` | Mappa _ALIAS_TO_EMOJI contiene "smile" |
| `test_emoji_to_alias_populated` | Mappa _EMOJI_TO_ALIAS popolata |

### 🔍 Contact Picker (`test_contact_picker.py`) — 9 test

Test della funzionalità di ricerca contatti (attivata con `Ctrl+S`): l'helper `search_contacts()` filtra la lista dei contatti per nome o numero, case-insensitive, e restituisce i risultati in ordine.

| Test | Descrizione |
|------|-------------|
| `test_search_by_name` | Cerca "alice" → trova Alice Rossi |
| `test_search_case_insensitive` | Cerca "ALICE" (maiuscolo) → stesso risultato di "alice" |
| `test_search_by_number` | Cerca parte del numero → trova il contatto |
| `test_search_partial_name` | Cerca "ross" → trova Alice Rossi (substring) |
| `test_search_no_results` | Stringa inesistente → lista vuota |
| `test_search_empty_query` | Query vuota → restituisce tutti i contatti |
| `test_search_whitespace_query` | Query di soli spazi → trattata come vuota |
| `test_search_max_results` | Verifica il limite massimo di risultati |
| `test_search_contact_without_name` | Contatto senza nome → match sul numero |

### 💾 Cache SQLite (`test_backend_cache.py`) — 18 test

Test della cache messaggi migrata da JSON a **SQLite**. Le scritture sono incrementali (INSERT/UPDATE/DELETE) e protette da `_DB_LOCK`.

| Test | Descrizione |
|------|-------------|
| `test_add_and_load` | Aggiunge messaggi e li ricarica da SQLite |
| `test_load_empty_db` | DB vuoto → dict vuoto |
| `test_add_creates_directory` | Directory creata automaticamente |
| `test_add_preserves_optional_fields` | Campi opzionali (quote, attachment, msg_type) salvati |
| `test_prune_old_messages` | Messaggi vecchi rimossi |
| `test_prune_max_200_messages` | Limite 200 messaggi per contatto |
| `test_prune_empty_contact_removed` | Contatto senza messaggi → rimosso |
| `test_prune_no_modification` | Contenuto invariato se nulla da potare |
| `test_mark_as_read` | Messaggi non letti → letti |
| `test_mark_as_read_no_contact` | Contatto inesistente → nessun errore |
| `test_update_status` | Status aggiornato per timestamp |
| `test_update_status_no_match` | Timestamp inesistente → nessuna modifica |
| `test_receipt_delivery` | Receipt delivery → status "delivered" |
| `test_receipt_read` | Receipt read → status "read" |
| `test_receipt_no_match` | Timestamp non matcha → lista vuota |
| `test_receipt_no_timestamps` | Receipt senza timestamps → lista vuota |
| `test_receipt_no_source` | Envelope senza source → lista vuota |
| `test_receipt_only_upgrades_status` | Non deve downgradare lo status |


### 📇 Contatti (`test_backend_contacts.py`) — 4 test

| Test | Descrizione |
|------|-------------|
| `test_contact_with_name` | Contact con nome → display_name = nome |
| `test_contact_without_name` | Contact senza nome → display_name = numero |
| `test_contact_with_aci` | Contact con ACI |
| `test_contact_empty_name_fallback` | Contact con name="" → display_name = numero |

### 📎 Attachment (`test_backend_attachments.py`) — 5 test

| Test | Descrizione |
|------|-------------|
| `test_attachment_found` | Attachment esistente → Path |
| `test_attachment_not_found` | Attachment inesistente → None |
| `test_attachment_empty_id` | ID vuoto → None |
| `test_attachment_none_id` | ID None → None |
| `test_attachment_is_directory` | ID è una directory → None |

### 🔌 RPC / Daemon (`test_backend_rpc.py`) — 12 test

| Test | Descrizione |
|------|-------------|
| `test_call_success` | Chiamata RPC con successo |
| `test_call_error` | Errore di connessione → dict con "error" |
| `test_list_contacts_success` | list_contacts con successo |
| `test_list_contacts_error` | list_contacts con errore → lista vuota |
| `test_receive_success` | receive con successo |
| `test_receive_error` | receive con errore → lista vuota |
| `test_listen_events_yields_envelope` | SSE listener → envelope JSON valido |
| `test_listen_events_skips_keepalive` | SSE listener → ignora keepalive (riga vuota) |
| `test_listen_events_connection_error` | SSE listener → errore di connessione gestito |
| `test_listen_events_bad_json` | SSE listener → JSON malformato non crasha |
| `test_daemon_running` | Demone attivo → True |
| `test_daemon_not_running` | Demone non attivo → False |

### 📤 Invio messaggi (`test_backend_send.py`) — 6 test

| Test | Descrizione |
|------|-------------|
| `test_send_basic` | Send subprocess base |
| `test_send_with_quote` | Send con quote (reply) |
| `test_send_with_partial_quote` | Send con solo quote_timestamp |
| `test_send_message_basic` | Send RPC base |
| `test_send_message_with_timestamp` | Send RPC con timestamp |
| `test_send_message_with_quote` | Send RPC con quote |

### 🖼️ UI Components (`test_ui_components.py`) — 11 test

| Test | Descrizione |
|------|-------------|
| `test_initial_state` | MessageWidget creato con testo e timestamp |
| `test_initial_state_mine` | Widget per messaggio proprio |
| `test_set_status` | Cambio status sent→delivered→read |
| `test_set_selected` | Toggle selezione |
| `test_message_clicked_event` | Click → emette MessageClicked |
| `test_initial_state_with_path` | ImageWidget con path valido |
| `test_initial_state_no_path` | ImageWidget senza path |
| `test_image_clicked_event` | Click → emette ImageClicked |
| `test_click_no_path_no_event` | Click senza path → nessun evento |
| `test_url_stored` | DownloadLinkWidget con URL |
| `test_custom_label` | DownloadLinkWidget con label personalizzato |

### 🔒 Lock file (`test_signal_tui_lock.py`) — 6 test

| Test | Descrizione |
|------|-------------|
| `test_acquire_lock_success` | Lock acquisito |
| `test_acquire_lock_alive_process` | Lock rifiutato se processo vivo |
| `test_acquire_lock_dead_process` | Lock acquisito se processo morto |
| `test_release_lock` | Lock rilasciato |
| `test_release_lock_not_ours` | Lock non nostro → non rimosso |
| `test_acquire_lock_exception_safe` | Eccezione → fail-safe True |

### 🛠️ Script installazione (`test_install_script.py`) — 16 test

Test dello script `install.sh` (eseguito come subprocess in ambienti temporanei isolati, con `curl`/`wget`/`python3`/`java`/`pip` mockati tramite stub in un PATH finto — nessun download o installazione reale).

| Test | Descrizione |
|------|-------------|
| `test_help_flag` | `--help` mostra l'uso ed esce con 0 |
| `test_unknown_flag` | Flag sconosciuto → errore, exit ≠ 0 |
| `test_missing_version_arg` | `--version` senza argomento → errore |
| `test_installed_version_detected` | Rileva la versione da `bin/signal-cli-*/` |
| `test_no_installed_version` | Nessuna versione in `bin/` → stringa vuota |
| `test_update_already_latest` | `--update` con versione già aggiornata → nessun download |
| `test_update_downloads_new` | `--update` con versione vecchia → scarica la nuova e rimuove la vecchia |
| `test_skip_no_version` | `--skip-signal-cli` senza versioni → warning |
| `test_skip_with_version` | `--skip-signal-cli` con versione presente → OK |
| `test_download_specific_version` | `--version X.Y.Z` → scarica quella versione specifica |
| `test_download_creates_correct_structure` | Struttura `bin/signal-cli-*/bin/signal-cli` corretta dopo il download |
| `test_remove_old_versions` | Le versioni vecchie vengono rimosse dopo il download |
| `test_python_version_check` | Python troppo vecchio → errore |
| `test_java_old_warns` | Java troppo vecchio → warning ma non blocca |
| `test_venv_created` | Crea `.venv` quando `DO_VENV=1` |
| `test_no_venv_flag` | `--no-venv` non crea `.venv` |

### 🔄 Cache SQLite (ex debounce) (`test_cache_debounce.py`) — 12 test

Il vecchio meccanismo di debounce (`_maybe_flush_cache`/`_flush_cache`) è stato **rimosso**: con SQLite ogni messaggio viene scritto subito (incrementale). I test verificano la persistenza immediata e l'aggiornamento incrementale dei badge unread.

| Test | Descrizione |
|------|-------------|
| `test_add_message_persists_immediately` | Ogni `_add_message_to_cache()` scrive subito su SQLite |
| `test_multiple_adds_all_persisted` | Più messaggi vengono tutti persistiti (nessun batch) |
| `test_no_debounce_attributes` | Gli attributi di debounce non esistono più |
| `test_mark_as_read_persists` | `_mark_as_read()` aggiorna lo stato read nel DB |
| `test_update_status_persists` | `_update_message_status()` aggiorna lo status nel DB |
| `test_receipt_updates_cache_and_db` | `_process_receipt_envelope` aggiorna `self._cache` e il DB |
| `test_receipt_no_match_returns_false` | Se il timestamp del receipt non matcha, non aggiorna nulla |
| `test_incremental_only_updates_given_contact` | Con `contact_number`, calcola solo per quel contatto |
| `test_incremental_no_change_returns_early` | Se il conteggio non cambia, non ricostruisce la lista |
| `test_full_update_all_contacts` | Senza `contact_number`, calcola per tutti i contatti |

### 🔄 Migrazione SQLite (`test_migrate_sqlite.py`) — 4 test

Test dello script `migrate_cache_sqlite.py` che converte la cache JSON esistente in SQLite.

| Test | Descrizione |
|------|-------------|
| `test_migrate_creates_db` | La migrazione crea `messages.db` con i messaggi |
| `test_migrate_backs_up_json` | Il file JSON originale viene rinominato in `.bak` |
| `test_migrate_no_json_is_noop` | Se `messages.json` non esiste, la migrazione non fa nulla |
| `test_migrate_preserves_optional_fields` | I campi opzionali (quote, attachment, msg_type) vengono salvati |


### 🔄 Refresh chat (`test_refresh_chat.py`) — 4 test

| Test | Descrizione |
|------|-------------|
| `test_refresh_chat_does_not_readd_old_messages` | `_refresh_chat()` non ri-aggiunge messaggi già mostrati |
| `test_refresh_chat_adds_only_newer_messages` | `_refresh_chat()` aggiunge solo messaggi più recenti |
| `test_refresh_chat_no_selected_contact` | Nessun contatto selezionato → nessuna operazione |
| `test_refresh_chat_empty_seen_timestamps` | `_seen_timestamps` vuoto → aggiunge tutti i messaggi |

### ✍️ Indicatori di typing (`test_typing_indicator.py`) — 25 test

Test della funzionalità "sta scrivendo": gli indicatori di digitazione arrivano da signal-cli come envelope con `typingMessage` (action `STARTED`/`STOPPED`). Sono effimeri: non finiscono mai in cache né nel log chat, ma attivano l'icona `✍️` accanto al contatto nella lista. Quando un contatto smette di scrivere senza inviare **oppure invia un messaggio**, passa allo stato **mumbling** (`💭`) per ~1 minuto.

> **Nota (aggiornamento 2026-08-22):** con il raggruppamento contatti la lista è ora **raggruppata per persona e ordinata per recency** (non più alfabetica); le icone `✍️`/`💭` e i badge `*N` sono mostrati nelle label ma non riordinano i gruppi di per sé (si veda `Ctrl+U` per il filtro non-letti).


| Test | Descrizione |
|------|-------------|
| `test_started` | `_process_typing` estrae `(numero, "STARTED")` |
| `test_stopped` | `_process_typing` estrae `(numero, "STOPPED")` |
| `test_not_typing_envelope` | Envelope normale (senza `typingMessage`) → `None` |
| `test_unknown_action` | Action sconosciuta → `None` |
| `test_missing_source` | Envelope senza source → `None` |
| `test_started_adds_to_typing_contacts` | `STARTED` aggiunge il contatto a `_typing_contacts` |
| `test_stopped_moves_to_mumbling` | `STOPPED` rimuove da `_typing_contacts` e passa allo stato mumbling |
| `test_typing_envelope_not_saved_to_cache` | Un envelope di typing non finisce nella cache messaggi |
| `test_message_moves_typing_contact_to_mumbling` | Un messaggio da chi stava scrivendo lo sposta in mumbling (💭) |
| `test_message_from_non_typing_contact_no_mumbling` | Un messaggio da un contatto che non scriveva non crea mumbling |
| `test_message_refreshes_existing_mumbling` | Un messaggio da un contatto già in mumbling aggiorna il timer |
| `test_new_started_after_message_readds_indicator` | Dopo un messaggio, un nuovo `STARTED` riattiva l'indicatore `✍️` |
| `test_contact_label_includes_typing_icon` | La label del contatto include `✍️` quando sta scrivendo |
| `test_contact_label_icon_after_unread_badge` | L'icona `✍️` va a destra del badge `*N` quando presente |
| `test_contact_label_includes_mumbling_icon` | Un contatto in stato mumbling mostra `💭` (non `✍️`) |
| `test_typing_timeout_moves_to_mumbling` | Dopo il timeout, `✍️` sparisce ma il contatto passa a `💭` |
| `test_mumbling_expiry_removes_indicator` | Dopo la scadenza del mumbling, il contatto viene rimosso |
| `test_started_clears_mumbling` | Un nuovo `STARTED` rimuove lo stato mumbling (sta scrivendo di nuovo) |
| `test_sort_keeps_alphabetical_when_typing` | La lista resta alfabetica anche quando un contatto sta scrivendo |
| `test_sort_keeps_alphabetical_with_unread` | La lista resta alfabetica anche con messaggi non letti |
| `test_sort_keeps_alphabetical_when_mumbling` | La lista resta alfabetica anche in stato mumbling |
| `test_sort_selected_typing_not_reordered` | Il contatto selezionato che sta scrivendo resta nel suo posto alfabetico |
| `test_sort_selected_mumbling_not_reordered` | Il contatto selezionato in mumbling resta nel suo posto alfabetico |
| `test_selected_typing_still_shows_icon` | Il contatto selezionato che scrive mostra comunque `✍️` |
| `test_selected_mumbling_still_shows_icon` | Il contatto selezionato in mumbling mostra comunque `💭` |






---

## Come eseguire


```bash
# Test completi (tests/) — richiede il venv attivo o PYTHON=...
make test

# Test con coverage + gate (soglia 68%) + report XML per Codecov
make coverage

# Lint (ruff check) e format check
make lint
make format-check

# Test di integrazione "live" (run opzionale, su filo reale — NON in CI)
# Richiede i servizi locali attivi + account di test reale; invia messaggi veri.
# Vedere la sezione "Testing" del README per prerequisiti e dettagli.
make live-test PYTHON=.venv-test/bin/python
make live-test-manual PYTHON=.venv-test/bin/python   # E4 (ingresso, manuale)
```

I comandi del `Makefile` usano la config condivisa in `pyproject.toml` (`testpaths = ["tests"]`), quindi raccolgono l'intera suite dalla radice comune. Lo script legacy `tests/run_regression_tests.sh` è stato sostituito dal Makefile.
