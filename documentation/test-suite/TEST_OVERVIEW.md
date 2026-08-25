# Test suite — panoramica e mappa

Stato ricavato dai sorgenti dei test (`tests/`, `Telegram/`) e dalla configurazione (`pyproject.toml`, `Makefile`, `.github/workflows/ci.yml`). Numeri verificati sul repository alla data di aggiornamento di questa documentazione: **66 file di test** (64 in `tests/` + 2 in `Telegram/`), ~1.44k funzioni di test prima di eventuali parametrizzazioni; l'ultima run registrata in `docs/TEST_REPORT.md` (2026-08-24) riporta **1389/1389 superati** più 7 test live opzionali, con coverage ~80% (gate minimo 68%). La fonte aggiornata per gli esiti resta la pipeline CI.

## 1. Come eseguire i test

### Configurazione unica (`pyproject.toml`)

- `testpaths = ["tests", "Telegram"]` — una semplice `pytest` copre entrambe le radici.
- `pythonpath = ["."]` — i test importano i moduli dalla root del progetto.
- `addopts = "-ra --tb=short --strict-markers"`; marker registrati: `integration` (test che lanciano la TUI headless, più lenti), `live` (filo reale contro i backend, richiede `LIVE_TESTS=1`), `live-manual` (richiede inoltre intervento manuale sul device, `LIVE_MANUAL=1`).
- `asyncio_mode = "auto"` (pytest-asyncio).
- Coverage: `source = "."`, branch on, gate `fail_under = 68`.

### Comandi

```bash
# venv dedicato ai test
source .venv-test/bin/activate        # creato da run_regression_tests.sh / installazione

make test          # pytest completo (tests/ + Telegram/)
make coverage      # pytest --cov (term-missing + coverage.xml per Codecov)
make lint          # ruff check .
make format-check  # ruff format --check .
make check         # lint + test (gate locale rapido)

# equivalente senza attivare il venv:
make test PYTHON=.venv-test/bin/python

# test live opzionali (bug #37 quote media su filo reale) — NON fanno parte della CI:
make live-test             # LIVE_TESTS=1 pytest tests/test_live_quote_media.py -v
make live-test-manual      # richiede anche LIVE_MANUAL=1 (ingresso manuale E4)

# script legacy (soppiantato dal Makefile, ancora funzionante):
./tests/run_regression_tests.sh    # crea .venv-test se manca, installa dipendenze, pytest -v su tests/
```

Note:

- `run_regression_tests.sh` esegue solo `tests/` (non la cartella `Telegram/`) ed è marcato come legacy nel README del progetto; il Makefile è la via canonica.
- I test standard sono offline/in-memory: nessun demone signal-cli, nessuna chiamata WAHA/Telegram reale (fixture mock in `conftest.py`). I test `live` sono l'unica eccezione: inviano messaggi REALI a un account di test e restano **sempre skippati** senza `LIVE_TESTS=1`.

### CI (`.github/workflows/ci.yml`)

- Job `lint`: `ruff check` + `ruff format --check` su Python 3.12, fail-fast per il job `test`.
- Job `test`: matrice Python **3.12/3.13** (`fail-fast: false`) — la gamba 3.12 è quella "canonica" (`make coverage` con gate `fail_under = 68` + upload `coverage.xml` su Codecov via OIDC), la gamba 3.13 esegue solo `make test` (verifica cross-versione senza overhead di coverage).
- Trigger: push su `master` + ogni pull request; concorrenza con cancel-in-progress.

## 2. Mappa dei test per area

I numeri indicano le funzioni `test_*` definite nel file (prima di parametrizzazioni).

### 2.1 Backend condiviso — RPC, cache SQLite, download, webhook

| File | N. | Cosa copre |
|---|---|---|
| `tests/test_backend_rpc.py` | 14 | client JSON-RPC, parsing typing/receipt, fallback subprocess |
| `tests/test_backend_cache.py` | 21 | scritture incrementali SQLite, dedup, unread |
| `tests/test_backend_download.py` | 15 | server download HTTP, serve text/attachment |
| `tests/test_backend_webhook.py` | 11 | handler webhook (200 sempre, 404/400, forward) |
| `tests/test_backend_connect.py` | 13 | connessione Signal daemon/subprocess, merge backend ready |
| `tests/test_backend_contacts.py` | 4 | parsing contatti RPC/subprocess |
| `tests/test_backend_attachments.py` | 5 | risoluzione path attachment |
| `tests/test_backend_send.py` | 6 | send via RPC/subprocess |
| `tests/test_backend_lazy_config.py` | 5 | risoluzione lazy di numero utente/binario all'uso |
| `tests/test_signal_tui_lock.py` | 6 | lock istanza singola `/tmp/signal-tui.lock` |

### 2.2 DB e migrazioni cache

| File | N. | Cosa copre |
|---|---|---|
| `tests/test_db_edit.py` | 17 | `_update_message_text` (edit persistito) |
| `tests/test_db_schema_versioning.py` | 6 | `PRAGMA user_version`, migrazioni additive |
| `tests/test_migrate_sqlite.py` | 5 | migrazione JSON → SQLite |
| `tests/test_migrate_protocol.py` | 12 | colonna `protocol` |
| `tests/test_migrate_status.py` | 6 | colonna `status` |
| `tests/test_open_or_create.py` | 11 | apertura chat/open-or-create ghost WhatsApp |
| `tests/test_cache_debounce.py` | 10 | comportamento "debounce" post-SQLite, unread incrementale |

### 2.3 Multi-backend e manager

| File | N. | Cosa copre |
|---|---|---|
| `tests/test_backends.py` | 53 | `BackendManager` (routing, registry), contratti cross-backend |
| `tests/test_address_book.py` | 82 | rubrica completa: base defaults, fan-out manager, TTL, per-backend |
| `tests/test_config.py` | 18 | config env/config.json/.env WhatsApp+Telegram |

### 2.4 WhatsApp

| File | N. | Cosa copre |
|---|---|---|
| `tests/test_whatsapp_backend.py` | 126 | suite principale: webhook ingest, contatti, media, presence, storico |
| `tests/test_whatsapp_fix_40_41.py` | 26 | fix webhook ack/edit (bug 40/41) |
| `tests/test_whatsapp_read_receipt_fix.py` | 14 | read receipt WAHA (id Baileys, reconciliazione storico) |
| `tests/test_whatsapp_receipt_id_match.py` | 20 | `canonical_msg_id` e matching receipt per id |
| `tests/test_wa_startup_resync.py` | 15 | resync storico all'avvio, attesa sync contatti |

### 2.5 Telegram (in `Telegram/` e `tests/`)

| File | N. | Cosa copre |
|---|---|---|
| `Telegram/test_telegram_backend.py` | 35 | suite backend Telethon |
| `Telegram/test_regression.py` | 39 | regressione generale Telegram |
| `tests/test_telegram.py` | 40 | backend Telegram (eventi, entità, QR) |
| `tests/test_telegram_read_receipt_fix.py` | 20 | read receipt per message-id, dedup, reconcile |
| `tests/test_telegram_send_reorder.py` | 1 | riordino immediato della lista dopo il submit ottimistico |

Note: `Telegram/conftest.py` ha una fixture autouse che patcha le scritture SQLite (`backend._add_message_to_cache`, `backend._update_message_id`) per evitare contaminazioni del DB reale. `tests/test_telegram_edit.py` è censito una sola volta in §2.8 (Edit messaggi cross-protocollo).

### 2.6 TUI / UI

| File | N. | Cosa copre |
|---|---|---|
| `tests/test_ui_protocol.py` | 55 | protocollo UI eventi/receipt/status (suite larga) |
| `tests/test_ui_components.py` | 37 | widget custom (MessageWidget status/stili/edit marker, MessageTextArea multi-riga, ecc.) |
| `tests/test_tui_integration.py` | 18 | integrazione app headless (`app_for_test`) |
| `tests/test_typing_indicator.py` | 29 | ✍️/💭, timeout, label riga |
| `tests/test_refresh_chat.py` | 24 | finestra 20 msg, dedup stessa-secondo |
| `tests/test_unread_filter.py` | 65 | filtro Ctrl+U/Ctrl+W/A, pinning selezione, auto-selezione |
| `tests/test_contact_grouping.py` | 66 | grouping per persona, header/member, collapse |
| `tests/test_contact_grouping_integration.py` | 5 | grouping integrato nella lista reale |
| `tests/test_contact_picker.py` | 49 | picker Ctrl+S, ricerca, BackendChoiceScreen |
| `tests/test_device_link_screen.py` | 32 | flusso Ctrl+L QR (Signal/WA/TG, 2FA) |
| `tests/test_emoji_picker.py` | 23 | picker emoji, categorie, completamento alias |
| `tests/test_image_async_download.py` | 9 | risoluzione attachment async (ramo CATIMG) |
| `tests/test_image_caption.py` | 19 | caption foto per protocollo |
| `tests/test_download_mode.py` | 10 | modalità Ctrl+D |
| `tests/test_status_backend_unread.py` | 14 | status bar per-backend clickabile |

### 2.7 Immagini native kitty (PR images)

| File | N. | Cosa copre |
|---|---|---|
| `tests/test_kitty_renderer.py` | 48 | formato DCS (`a=t` chunked `q=2/m`, `a=p` source rect, delete/clear), cell size, `compute_source_rect` |
| `tests/test_image_detect.py` | 30 | `detect_image_support()` (override config, isatty, guardia tmux/screen, query TGP, fallback catimg/OFF) + getter config immagini |
| `tests/test_image_modal.py` | 11 | `ImageModalScreen` strategia nativa + semantica OFF |
| `tests/test_chat_view_images.py` | 15 | ramo KITTY della risoluzione attachment in chat (`_resolve_attachment_worker`, `_finish_native_thumbnail`, `_resolve_mounted_image_paths`) |

### 2.8 Edit messaggi (cross-protocollo)

| File | N. | Cosa copre |
|---|---|---|
| `tests/test_edit_contract.py` | 10 | contratto opzionale di edit (default, routing manager) |
| `tests/test_edit_flow.py` | 12 | flusso UI edit con rollback |
| `tests/test_edit_signal.py` | 27 | edit Signal (editTimestamp) |
| `tests/test_edit_whatsapp.py` | 33 | edit WhatsApp (REST PUT) |
| `tests/test_telegram_edit.py` | 27 | edit Telegram |
| `tests/test_merge_cache_edit.py` | 9 | riconciliazione edit nel merge cache |

### 2.9 Quote media (reply con immagine, bug #37)

| File | N. | Cosa copre |
|---|---|---|
| `tests/test_models.py` | 7 | segnaposto media canonici, `media_quote_placeholder`, `is_media_quote_placeholder` |
| `tests/test_reply_media.py` | 7 | flusso Alt+click/Alt+R su immagine: `_reply_to` con `quote_wire_body`, reply bar, toggle/annullamento, mutua esclusione edit, download mode |
| `tests/test_live_quote_media.py` | 7 | verifica empirica su filo reale dei tre protocolli — **gated**: skippati senza `LIVE_TESTS=1` (E4 richiede `LIVE_MANUAL=1`); target/account configurabili via env |

### 2.10 Invio e stato outgoing

| File | N. | Cosa copre |
|---|---|---|
| `tests/test_send_persist_offthread.py` | 26 | persistenza ottimistica nel worker, transizioni/fallback |
| `tests/test_send_timing.py` | 3 | timing/durata send |
| `tests/test_failed_send_status.py` | 15 | transizione failed, retry e guardie |
| `tests/test_outgoing_status_fallback.py` | 7 | fallback by-text pending→sent |
| `tests/test_signal_real_timestamp.py` | 14 | vero timestamp server Signal/echo |

### 2.11 Infrastruttura e utility

| File | N. | Cosa copre |
|---|---|---|
| `tests/test_install_script.py` | 17 | `install.sh` (opzioni, download signal-cli) |
| `tests/test_docker_compose_extra_hosts.py` | 2 | `host.docker.internal` in docker-compose |
| `tests/test_qr_ascii.py` | 4 | rendering QR ASCII (`qr_utils.py`) |

## 3. Fixture condivise (`tests/conftest.py`)

Due famiglie:

- **dati di esempio** per i backend Signal/cache: `tmp_cache_dir`, `tmp_cache_file`, `sample_messages`, `sample_envelope_text`, `sample_envelope_image`, `sample_envelope_receipt`, `sample_contacts_rpc_output`, `sample_contacts_subprocess_output`;
- **app headless**: `app_for_test` (SignalTUI con backends mockati e worker neutralizzati) e `app_for_test_with_mocks` (restituisce anche il backend mockato per le asserzioni).

Il dettaglio completo e lo stile d'uso sono in [TEST_GUIDELINES.md](TEST_GUIDELINES.md).

## Documenti collegati

- [TEST_COVERAGE.md](TEST_COVERAGE.md) — aree coperte vs lacune.
- [TEST_GUIDELINES.md](TEST_GUIDELINES.md) — convenzioni per scrivere nuovi test.
