# Documentazione — Signal TUI Client

Documentazione tecnica generata dal codice sorgente (non dai documenti storici in `docs/`, usati solo come contesto). Descrive il comportamento attuale del sistema: client TUI multi-protocollo (Signal / WhatsApp / Telegram) basato su Textual.

Per installazione e uso quotidiano vedere il [README del progetto](../README.md).

## Struttura

```
documentation/
├── README.md                        ← questo indice
├── architecture/
│   ├── ARCHITECTURE_OVERVIEW.md     ← visione d'insieme, flussi, pattern, dipendenze
│   └── BACKEND_COMPONENTS.md        ← componenti backend/ e backends/ nel dettaglio
├── design/
│   ├── DESIGN_OVERVIEW.md           ← principi UI/UX, composizione Textual, data flow
│   ├── DESIGN_MESSAGE_IDENTITY_AND_CACHE.md  ← identità messaggi, livelli cache, batching
│   ├── DESIGN_OUTGOING_MESSAGE_STATUS.md     ← protocollo di stato pending→sent→delivered→read→failed
│   ├── DESIGN_EDIT_MESSAGES.md      ← editing ottimistico con rollback, edit ricevuti
│   └── DESIGN_NATIVE_IMAGES.md      ← miniature native kitty + modal hi-res, fallback catimg
├── api-contracts/
│   ├── API_OVERVIEW.md              ← API interne: ChatBackend, manager, RPC, DB, webhook, REST
│   └── CONTRACTS.md                 ← contratti di dati: campi, tipi, semantica, test di riferimento
└── test-suite/
    ├── TEST_OVERVIEW.md             ← mappa dei 66 file di test per area + come eseguirli
    ├── TEST_COVERAGE.md             ← aree coperte vs lacune
    └── TEST_GUIDELINES.md           ← convenzioni per scrivere nuovi test
```

## Indice dei documenti

### Architettura (`architecture/`)

| Documento | Descrizione | Fonti principali |
|---|---|---|
| [ARCHITECTURE_OVERVIEW.md](architecture/ARCHITECTURE_OVERVIEW.md) | Visione d'insieme del sistema: componenti, flussi TUI ↔ backend ↔ servizi esterni, pattern (Textual + mixin, thread worker, code, cache a due livelli, SQLite), decisioni architetturali chiave, diagramma modulare. | `signal_tui.py`, `tui/app.py`, `tui/polling.py`, `backends/*`, `backend/*`, `models.py` |
| [BACKEND_COMPONENTS.md](architecture/BACKEND_COMPONENTS.md) | Responsabilità e relazioni di ogni componente backend: `db.py` (cache SQLite), `rpc.py` (signal-cli), `webhook.py`, `download.py`, `base.py`, `config.py`, `manager.py`, backend Signal/WhatsApp/Telegram. | `backend/db.py`, `backend/rpc.py`, `backend/webhook.py`, `backend/download.py`, `backends/base.py`, `backends/config.py`, `backends/manager.py`, `backends/signal.py`, `backends/whatsapp*.py`, `backends/telegram.py` |

### Design (`design/`)

| Documento | Descrizione | Fonti principali |
|---|---|---|
| [DESIGN_OVERVIEW.md](design/DESIGN_OVERVIEW.md) | Principi di design, composizione dell'app per mixin, albero widget, convenzioni CSS, widget custom, data flow eventi/thread, stato della UI. | `tui/app.py`, `tui/css.py`, `tui/events.py`, `ui_components.py`, `models.py` |
| [DESIGN_MESSAGE_IDENTITY_AND_CACHE.md](design/DESIGN_MESSAGE_IDENTITY_AND_CACHE.md) | Il modello a identità `(protocol, key, ts, text)`, i quattro livelli di cache, le regole di dedup per protocollo, il merge add-only e il batching ("debounce") del rendering. | `tui/chat_view.py`, `tui/edit.py`, `tui/unread_reply.py`, `backends/signal.py`, `backends/whatsapp.py`, `backend/db.py`; test `test_refresh_chat`, `test_merge_cache_edit`, `test_cache_debounce` |
| [DESIGN_OUTGOING_MESSAGE_STATUS.md](design/DESIGN_OUTGOING_MESSAGE_STATUS.md) | Macchina a stati dei messaggi inviati (pending/sent/delivered/read/**failed**), invio ottimistico con persist off-thread e riordino immediato della lista, transizioni e fallback (by-timestamp/by-text/by-id), rank guard anti-downgrade, retry con guardie. | `tui/send.py`, `tui/events.py`, `backend/db.py`, `ui_components.py`; test `test_send_persist_offthread`, `test_outgoing_status_fallback`, `test_failed_send_status`, `test_telegram_send_reorder` |
| [DESIGN_EDIT_MESSAGES.md](design/DESIGN_EDIT_MESSAGES.md) | Editing dei messaggi: gate Alt+E/Alt+click, submit ottimistico con rollback completo, chirurgia dell'identità `(ts, text)`, update in-place degli edit ricevuti (mai nuove bolle), marker "(modificato)", limiti noti. | `tui/edit.py`, `tui/send.py`, `tui/events.py`, `backends/base.py`; test `test_edit_contract`, `test_edit_flow`, `test_edit_signal/whatsapp`, `test_telegram_edit` |
| [DESIGN_NATIVE_IMAGES.md](design/DESIGN_NATIVE_IMAGES.md) | Immagini native: miniature inline via kitty graphics protocol (`tui/images/`), modal hi-res, rilevamento terminale e fallback catimg/OFF; analisi POC e decisioni registrate. Stato: implementato. | `tui/images/*`, `signal_tui.py`, `tui/app.py`, `ui_components.py::ImageWidget`, `backends/config.py`; test `test_kitty_renderer`, `test_image_detect`, `test_image_modal`, `test_chat_view_images` |

### Code API & Contracts (`api-contracts/`)

| Documento | Descrizione | Fonti principali |
|---|---|---|
| [API_OVERVIEW.md](api-contracts/API_OVERVIEW.md) | Elenco delle superfici interne: interfaccia astratta `ChatBackend`, facade `BackendManager`, protocollo JSON-RPC/SSE di signal-cli, API del layer DB, contratto webhook WAHA, client REST WAHA, tipi evento, server download, configurazione. | `backends/base.py`, `backends/manager.py`, `backend/rpc.py`, `backend/db.py`, `backend/webhook.py`, `backends/whatsapp_rest.py`, `backend/download.py`, `backends/config.py` |
| [CONTRACTS.md](api-contracts/CONTRACTS.md) | Contratti di dati concreti: modelli `ChatContact`/`ChatMessage`/`ChatEvent`, dict messaggio, schema SQLite, stati dei messaggi, contratto di edit, receipt per protocollo, raggruppamento/unread/status bar, payload webhook, quote media (§11), convenzioni d'errore — con i file di test che fissano ogni contratto. | `models.py`, `tests/test_edit_contract.py`, `tests/test_reply_media.py`, `tests/conftest.py`, `tests/test_backend_cache.py`, `tests/test_contact_grouping.py`, `backends/whatsapp_events.py` e altri |

### Test Suite (`test-suite/`)

| Documento | Descrizione | Fonti principali |
|---|---|---|
| [TEST_OVERVIEW.md](test-suite/TEST_OVERVIEW.md) | Come eseguire i test (Makefile, pytest, venv `.venv-test`, test live gated `make live-test`, script legacy, CI con matrice 3.12/3.13) e mappa completa dei 66 file di test organizzati per area. | `pyproject.toml`, `Makefile`, `.github/workflows/ci.yml`, `tests/run_regression_tests.sh`, `tests/*.py`, `Telegram/*.py` |
| [TEST_COVERAGE.md](test-suite/TEST_COVERAGE.md) | Analisi qualitativa di copertura per area funzionale: aree robuste, coperte, parziali e lacune concrete. | `tests/*`, `docs/TEST_REPORT.md` (solo metriche registrate) |
| [TEST_GUIDELINES.md](test-suite/TEST_GUIDELINES.md) | Convenzioni per scrivere test: fixture di `conftest.py`, naming, pattern ricorrenti (DB temporaneo, app headless, minimal backend), configurazione pytest. | `tests/conftest.py`, `Telegram/conftest.py`, `pyproject.toml` |

## Nota metodologica

- Tutti i contenuti sono ricavati leggendo il codice e i test al momento della stesura; dove un comportamento è documentato anche nei vecchi `docs/DESIGN_*.md`, qui vale **solo** ciò che il codice fa oggi.
- I numeri della suite (file, test, esiti) si riferiscono allo stato del repository alla data di generazione (2026-08-25: 66 file, ~1.44k funzioni di test); l'ultima run registrata è in `docs/TEST_REPORT.md` (1389/1389 + 7 test live gated), ma la fonte aggiornata per gli esiti resta la pipeline CI.
