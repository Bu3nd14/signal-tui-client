# Test Report — Signal TUI Client

**Data:** 2026-08-02  
**Git commit:** `1904c9f`  
**Python:** 3.12.3  
**Stato:** ✅ 88/88 test superati

---

## Riepilogo

| Modulo | File | Test | Esito |
|--------|------|------|-------|
| Emoji Picker | `test_emoji_picker.py` | 14 | ✅ |
| Cache | `test_backend_cache.py` | 16 | ✅ |
| Contatti | `test_backend_contacts.py` | 4 | ✅ |
| Attachment | `test_backend_attachments.py` | 5 | ✅ |
| RPC / Daemon | `test_backend_rpc.py` | 8 | ✅ |
| Invio messaggi | `test_backend_send.py` | 6 | ✅ |
| UI Components | `test_ui_components.py` | 11 | ✅ |
| Lock file | `test_signal_tui_lock.py` | 6 | ✅ |
| Script installazione | `test_install_script.py` | 16 | ✅ |
| **Totale** | | **88** | **✅ 88/88** |

---

## Dettaglio test

### 🔍 Emoji Picker (`test_emoji_picker.py`) — 14 test

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

### 💾 Cache (`test_backend_cache.py`) — 16 test

| Test | Descrizione |
|------|-------------|
| `test_save_and_load` | Salva e ricarica messaggi |
| `test_load_missing_file` | File inesistente → dict vuoto |
| `test_load_corrupted_json` | JSON corrotto → dict vuoto |
| `test_save_creates_directory` | Directory creata automaticamente |
| `test_prune_old_messages` | Messaggi vecchi rimossi |
| `test_prune_max_200_messages` | Limite 200 messaggi per contatto |
| `test_prune_empty_contact_removed` | Contatto senza messaggi → rimosso |
| `test_prune_no_modification` | Contenuto invariato se nulla da potare |
| `test_mark_as_read` | Messaggi non letti → letti |
| `test_mark_as_read_no_contact` | Contatto inesistente → nessun errore |
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

---

## Come eseguire

```bash
./tests/run_regression_tests.sh
```

Lo script crea un virtualenv `.venv-test/`, installa pytest e le dipendenze, esegue tutti i test e produce un report colorato. Exit code 0 = tutto ok.
