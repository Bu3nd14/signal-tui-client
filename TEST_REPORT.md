# Test Report — Signal TUI Client

**Data:** 2026-08-06
**Git commit:** `6e66f45` (master, merge `whatsapp-image-support`)
**Python:** 3.12.3
**Stato:** ✅ 353/353 test superati

---

## Riepilogo

| Modulo | File | Test | Esito |
|--------|------|------|-------|
| WhatsApp Backend | `test_whatsapp_backend.py` | 92 | ✅ |
| UI Protocol | `test_ui_protocol.py` | 44 | ✅ |
| Typing Indicator | `test_typing_indicator.py` | 29 | ✅ |
| Backends (Manager) | `test_backends.py` | 25 | ✅ |
| Docker Compose | `test_docker_compose_extra_hosts.py` | 2 | ✅ |
| Cache (SQLite) | `test_backend_cache.py` | 18 | ✅ |
| Emoji Picker | `test_emoji_picker.py` | 16 | ✅ |
| Script installazione | `test_install_script.py` | 16 | ✅ |
| Refresh Chat | `test_refresh_chat.py` | 15 | ✅ |
| WA Startup/Resync | `test_wa_startup_resync.py` | 15 | ✅ |
| UI Components | `test_ui_components.py` | 14 | ✅ |
| Migrazione Protocollo | `test_migrate_protocol.py` | 11 | ✅ |
| Cache Debounce | `test_cache_debounce.py` | 10 | ✅ |
| Contact Picker | `test_contact_picker.py` | 9 | ✅ |
| RPC / Daemon | `test_backend_rpc.py` | 8 | ✅ |
| Invio messaggi | `test_backend_send.py` | 6 | ✅ |
| Lock File | `test_signal_tui_lock.py` | 6 | ✅ |
| Attachment | `test_backend_attachments.py` | 5 | ✅ |
| QR ASCII | `test_qr_ascii.py` | 4 | ✅ |
| Migrazione SQLite | `test_migrate_sqlite.py` | 4 | ✅ |
| Contatti | `test_backend_contacts.py` | 4 | ✅ |
| **Totale** | | **353** | **✅ 353/353** |

---

## Novità WhatsApp Image Support

Aggiunti 14 test nel modulo `test_whatsapp_backend.py` per la nuova funzionalità:

| Test | Descrizione |
|------|-------------|
| `test_hasMedia_image` | Estrazione immagine da `hasMedia`/`media` WAHA |
| `test_hasMedia_video` | Estrazione video → `msg_type=attachment` |
| `test_hasMedia_audio` | Estrazione audio → `msg_type=attachment` |
| `test_hasMedia_no_media_dict` | `hasMedia=True` ma `media=None` → testo |
| `test_download_media_direct_binary` | Download via endpoint WAHA binary |
| `test_download_media_falls_back_to_legacy_url` | Fallback a URL legacy |
| `test_download_media_returns_none_when_all_fail` | Tutti i path falliscono → None |
| `test_download_media_direct_url` | Download da URL HTTP diretto |
| `test_download_media_encodes_at_sign` | Encoding `@` → `%40` nell'URL |
| `test_get_attachment_path` | Fast-path: file già in cache |
| `test_get_attachment_path_missing_downloads` | Lazy download quando file assente |
| `test_get_attachment_path_download_fails_returns_none` | Download fallito → None |
| `test_get_attachment_path_no_rest_returns_none` | Nessun REST client → None |









---

## Riepilogo

| Modulo | File | Test | Esito |
|--------|------|------|-------|
| Emoji Picker | `test_emoji_picker.py` | 16 | ✅ |
| Contact Picker | `test_contact_picker.py` | 9 | ✅ |
| Cache (SQLite) | `test_backend_cache.py` | 18 | ✅ |
| Contatti | `test_backend_contacts.py` | 4 | ✅ |
| Attachment | `test_backend_attachments.py` | 5 | ✅ |
| RPC / Daemon | `test_backend_rpc.py` | 8 | ✅ |
| Invio messaggi | `test_backend_send.py` | 6 | ✅ |
| UI Components | `test_ui_components.py` | 11 | ✅ |
| Lock file | `test_signal_tui_lock.py` | 6 | ✅ |
| Script installazione | `test_install_script.py` | 16 | ✅ |
| Cache SQLite (ex debounce) | `test_cache_debounce.py` | 12 | ✅ |
| Migrazione SQLite | `test_migrate_sqlite.py` | 4 | ✅ |
| Refresh chat | `test_refresh_chat.py` | 4 | ✅ |
| Indicatori di typing | `test_typing_indicator.py` | 25 | ✅ |
| **Totale** | | **142** | **✅ 142/142** |








---

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

### 🔌 RPC / Daemon (`test_backend_rpc.py`) — 8 test

| Test | Descrizione |
|------|-------------|
| `test_call_success` | Chiamata RPC con successo |
| `test_call_error` | Errore di connessione → dict con "error" |
| `test_list_contacts_success` | list_contacts con successo |
| `test_list_contacts_error` | list_contacts con errore → lista vuota |
| `test_receive_success` | receive con successo |
| `test_receive_error` | receive con errore → lista vuota |
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

Test della funzionalità "sta scrivendo": gli indicatori di digitazione arrivano da signal-cli come envelope con `typingMessage` (action `STARTED`/`STOPPED`). Sono effimeri: non finiscono mai in cache né nel log chat, ma attivano l'icona `✍️` accanto al contatto nella lista. Quando un contatto smette di scrivere senza inviare **oppure invia un messaggio**, passa allo stato **mumbling** (`💭`) per ~1 minuto. La lista dei contatti è **sempre in ordine alfabetico**: le icone `✍️`/`💭` e i badge `*N` sono mostrati nella label ma **non riordinano mai la lista**, così non "salta".


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
./tests/run_regression_tests.sh
```

Lo script crea un virtualenv `.venv-test/`, installa pytest e le dipendenze, esegue tutti i test e produce un report colorato. Exit code 0 = tutto ok.
