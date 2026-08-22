# Architettura — Signal TUI Client

Documento ricavato dal codice sorgente attuale (`signal_tui.py`, `tui/`, `backend/`, `backends/`, `models.py`). Descrive cosa il sistema fa oggi, non i piani storici.

## 1. Visione d'insieme

Signal TUI Client è un client di messaggistica multi-protocollo (Signal, WhatsApp, Telegram) con interfaccia a terminale costruita su [Textual](https://textual.textualize.io/). Il principio architetturale centrale è la **separazione netta tra UI e protocolli**:

- ogni protocollo è incapsulato in un *backend* che implementa l'interfaccia astratta `ChatBackend` (`backends/base.py`);
- i backend convertono i dati specifici di protocollo nei modelli neutrali `ChatContact` / `ChatMessage` / `ChatEvent` (`models.py`);
- la UI (Textual) e il `BackendManager` interagiscono solo con questa interfaccia: nessuna dipendenza dalla logica specifica di Signal/WAHA/Telegram.

Tre servizi esterni, tutti opzionali tranne Signal:

| Protocollo | Servizio esterno | Trasporto | Ricezione |
|---|---|---|---|
| Signal | `signal-cli` daemon (JVM) | JSON-RPC over HTTP (`127.0.0.1:8080/api/v1/rpc`) + SSE (`/api/v1/events`); fallback subprocess | stream SSE in thread dedicato |
| WhatsApp | WAHA (container Docker Baileys), default `127.0.0.1:3005` | REST (`whatsapp_rest.py`) + webhook PUSH verso il client | server HTTP webhook locale su porta `CLIENT_WEBHOOK_PORT` (default 8088) |
| Telegram | nessun demone esterno (MTProto via Telethon) | event loop asyncio in thread daemon | handler eventi Telethon → coda |

Persistenza: SQLite locale in `~/.local/share/signal-tui-client/messages.db` (WAL), scritture incrementali serializzate da un lock (`_DB_LOCK`), retention 200 messaggi per contatto.

## 2. Struttura modulare (diagramma)

```
                         signal_tui.py  (entry point)
              lock istanza singola, crash log, logging, SignalTUI()
                                      │
 ┌────────────────────────────────────┴─────────────────────────────────────┐
 │  tui/  (package UI, compone SignalTUI per mixin)                          │
 │    app.py            SignalTUI(App): lifecycle, filtri, status bar        │
 │    polling.py        PollingMixin: thread poll worker                     │
 │    backend_connect.py BackendConnectMixin: connect worker per backend     │
 │    events.py         EventHandlingMixin: dispatch ChatEvent (singolo      │
 │                      punto d'ingresso dati in ingresso)                   │
 │    chat_view.py      ChatViewMixin: rendering bolle, finestra 20 msg,     │
 │                      merge cache backend, fetch history                   │
 │    contacts.py       ContactListMixin: ordinamento, raggruppamento,       │
 │                      filtri Ctrl+W/Ctrl+U, selezione, render progressivo  │
 │    send.py           SendMixin: invio ottimistico + worker send           │
 │    edit.py           EditMessageMixin: flusso edit con rollback           │
 │    unread_reply.py   UnreadReplyMixin: badge unread, reply bar            │
 │    download.py       DownloadModeMixin: modalità download HTTP (Ctrl+D)   │
 │    pickers.py        PickerMixin: emoji picker, contact picker (Ctrl+S),  │
 │                      device link (Ctrl+L), completamento :alias:          │
 │    css.py            APP_CSS (foglio di stile Textual)                    │
 └───────────────┬──────────────────────────────────┬────────────────────────┘
                 │ usa                              │ registra/interroga
                 ▼                                  ▼
 ┌──────────────────────────┐        ┌──────────────────────────────────────┐
 │ ui_components.py         │        │ backends/  (multi-protocollo)         │
 │ MessageWidget, ImageWidget│       │  base.py      ChatBackend (ABC)       │
 │ ImageModalScreen, StatusBar│      │  manager.py   BackendManager          │
 │ ContactList*, ChatArea,   │       │  signal.py    SignalBackend           │
 │ DownloadLink, TextArea    │       │  whatsapp.py  WhatsAppBackend         │
 └──────────────────────────┘        │  whatsapp_events.py normalizzazione   │
                                     │  whatsapp_rest.py client REST WAHA    │
                                     │  telegram.py  TelegramBackend         │
 ┌──────────────────────────┐        │  config.py    config env/json/.env    │
 │ models.py                │◀──────▶└──────────────────────────────────────┘
 │ ChatContact/Message/Event│                        │
 │ contact_cache_key()      │                 usa il layer condiviso
 └──────────────────────────┘                        ▼
 ┌───────────────────────────────────────────────────────────────────────────┐
 │ backend/  (layer condiviso, nessuna dipendenza da Textual)                │
 │   db.py       cache SQLite (schema v3, dedup, unread, status)             │
 │   rpc.py      SignalRPCClient (JSON-RPC/SSE), parsing typing/receipt,     │
 │               fallback subprocess, modello Contact                        │
 │   webhook.py  server HTTP webhook WAHA (porta 8088, ack 200 sempre)       │
 │   download.py server HTTP download temporaneo (porta 10042)               │
 │   __init__.py re-esporta l'API storica del vecchio modulo backend.py      │
 └───────────────────────────────────────────────────────────────────────────┘
                 │                                    │
                 ▼                                    ▼
      ~/.local/share/signal-tui-client/messages.db   servizi esterni:
      (SQLite WAL, schema user_version=3)            signal-cli daemon / WAHA
                                                     container / Telegram MTProto

 Moduli di supporto (root):
   contact_picker.py  emoji_picker.py  emoji_data.py  device_link_screen.py
   qr_utils.py        link_account.py  link_whatsapp.py
   migrate_cache_sqlite.py  migrate_cache_protocol.py  migrate_cache_status.py
   purge_whatsapp_cache.py
```

## 3. Flussi principali

### 3.1 Flusso di un messaggio in arrivo

```
servizio esterno            backend                  TUI (thread)
────────────────           ─────────────             ───────────────────────────
signal-cli SSE      ──►    SignalBackend._sse_listener()
                           └─ envelope_to_event() → queue.Queue
WAHA webhook POST   ──►    backend/webhook.py (HTTP :8088)
                           └─ WhatsAppBackend.handle_webhook()
                              └─ _event_from_raw() / message.ack sintetico
                                 → _enqueue_event() → queue.Queue
Telethon handler    ──►    TelegramBackend._handle_new_message() ecc.
                           └─ _message_to_chat_event() → queue.Queue
                                                    poll worker thread (tui/polling.py)
                                                      └─ backend.poll_once() → batch
                                                      └─ _handle_event(event)   ← events.py
                                                           ├─ "message"     → ingest_message() (cache+DB),
                                                           │                   mirror cache UI, bolla live
                                                           ├─ "message_edit"→ apply_edit(), riscrittura widget
                                                           ├─ "receipt"     → process_receipt(), upgrade status
                                                           └─ "typing"      → icone ✍️/💭 sulla riga contatto
                                                      flush di fine batch (UN solo re-render lista,
                                                      unread incrementale per contatti toccati)
```

Il thread UI non fa mai I/O di rete: il poll worker è un thread "plain" che chiama `call_from_thread` per ogni mutazione della UI.

### 3.2 Flusso di un messaggio in uscita (invio ottimistico)

Vedi `tui/send.py::on_message_text_area_submitted` e `_send_message_worker`:

1. Enter sull'input → conversione alias emoji → **invio ottimistico**: cache in-memory aggiornata subito (via `ingest_message(persist=False)` nel backend corretto + mirror nella cache UI) con `status="pending"` e bolla mostrata immediatamente; l'INSERT nel DB avviene nel worker, prima dell'invio di rete.
2. Un worker thread chiama `backend.send_message_sync(...)` (bloccante).
3. Al successo: transizione atomica `pending → sent` su tutti i layer (DB per timestamp, fallback per testo; rank guard mai in downgrade) e ingest dell'echo col vero id server (`_update_message_id` aggancia l'id alla riga ottimistica senza duplicare).
4. I receipt successivi (`delivered`/`read`) arrivano come eventi `receipt` e aggiornano lo stato per id o timestamp.

### 3.3 Avvio applicazione (`tui/app.py::on_mount`)

- parte subito il poll worker (thread);
- in parallelo partono i connect worker dei backend già configurati (`_connect_signal`, `_connect_whatsapp`, `_connect_telegram`);
- ogni backend pronto esegue il merge atomico di cache+contatti nel UI thread (`_on_backend_ready`);
- quando **tutti** i backend attesi hanno riportato esito (`_pending_backends` vuoto), l'auto-selezione apre il primo contatto.

## 4. Pattern architetturali usati

| Pattern | Dove | Scopo |
|---|---|---|
| Facade + registry | `backends/manager.py` | superficie unica protocol-agnostica per la UI; routing per `protocol` |
| Adapter per protocollo | `SignalBackend`, `WhatsAppBackend`, `TelegramBackend` | convertono envelope/frame nativi in `ChatEvent` normalizzati |
| Mixin composition | `tui/app.py` (`SignalTUI` eredita 10 mixin funzionali) | spezza un'app Textual altrimenti monolitica |
| Producer/consumer con coda thread-safe | `queue.Queue` in ogni backend + `poll_once()` | disaccoppiamento tra ricezione (push/poll) e UI |
| Thread worker + `call_from_thread` | polling, send, load-messages, address book, connessioni | mai bloccare il loop reattivo Textual |
| Cache a due livelli | cache per-backend (`backend.cache`, chiave id grezzo) + cache UI (`self._cache`, chiave `protocol:id`) | ingest/dedup lato protocollo vs rendering lato UI |
| Persistenza incrementale con lock | `backend/db.py` (`_DB_LOCK`, INSERT per messaggio) | niente flush batch; scritture concorrenti sicure (poll worker + UI) |
| Schema versioning | `PRAGMA user_version = 3` + migrazioni additive `ALTER TABLE` | upgrade automatico dei DB legacy |
| Token di validazione | `_chat_reload_token`, `_address_book_token` | invalida worker in volo dopo un cambio selezione/chiusura picker |
| Ottimistic UI con rollback | invio (`pending`) ed edit (apply locale + rollback su errore) | reattività percepita; coerenza ripristinata su fallimento |

## 5. Decisioni architetetturali chiave (riscontrabili nel codice)

1. **Daemon-first con fallback**: Signal prova prima il daemon JSON-RPC (`_is_daemon_running()`); se assente avvia `signal-cli daemon --http` come subprocess e, se anche questo non risponde entro ~15 s, degrada a chiamate subprocess one-shot (`rpc.py::_run_subprocess`).
2. **WhatsApp push-only**: la ricezione live arriva SOLO via webhook (`handle_webhook`); `GET /api/messages` non è mai usato in polling ma solo on-demand dal backend: all'apertura di una chat (`fetch_history`) e all'avvio da `resync_history()` per ri-sincronizzare l'unione di unread e chat presenti nel DB. Il retry breve (fino a 3 tentativi, pausa 0,8 s) sta nel chiamante `tui/chat_view.py::_load_messages_worker`, perché WAHA può rispondere vuoto subito dopo l'avvio.
3. **Telegram in-process**: nessun demone esterno; Telethon gira in un event loop asyncio dedicato dentro un thread daemon; login QR (`tg://login?token=...`) con supporto 2FA.
4. **Identità messaggio multi-protocollo**: le chiavi cache sono namespaced per protocollo (`contact_cache_key = f"{protocol}:{id}"`); l'identità di un messaggio a livello render è `(protocol, cache_key, timestamp, text)` perché il timestamp al secondo non è unico.
5. **Dedup difensiva a più livelli**: guardie webhook per `(contatto, id, testo normalizzato)` in `WhatsAppBackend`, dedup per identità/fuzzy-window in `ingest_message` (±2 s incoming, ±5 s outgoing echo, ±10 min per echo con id), dedup cross-sessione in SQLite per `msg_id`.
6. **Batching del render lista**: durante un batch di eventi la lista non viene ricostruita per messaggio; flag `_contact_list_dirty` + `_dirty_contact_keys` producono UN solo flush a fine giro (unread incrementale O(M) per contatto fino a `_CONTACT_UPDATE_BATCH_MAX = 4`, poi full).
7. **Configurazione**: precedenza env var > `config.json`; il file `.env` è consultato solo per `WAHA_API_KEY` e credenziali Telegram (`TELEGRAM_API_ID`/`TELEGRAM_API_HASH`) (`backends/config.py`); backend opzionali registrati solo se configurati/rilevati (`whatsapp_enabled()`, `telegram_enabled()`), quindi il client resta utilizzabile Signal-only.
8. **Dipendenza da Textual confinata**: solo `tui/` e i moduli widget della root (`ui_components`, `contact_picker`, `emoji_picker`, `device_link_screen`) importano Textual; `models.py`, `backend/` e `backends/` sono importabili e testabili headless (il conftest dei test si affida a questa proprietà).

## 6. Dipendenze tra moduli (riepilogo)

- `tui/*` dipende da: `models`, `backends` (manager + classi), `backend` (funzioni DB/download/webhook), `ui_components`, `contact_picker`, `emoji_picker`, `device_link_screen`.
- `backends/*` dipende da: `models`, `backend` (db/rpc helpers). NON dipende da `tui/`.
- `backend/*` dipende solo da stdlib (+ `backend/__init__.py` aggrega i sotto-moduli).
- `models.py` non dipende da nulla (solo dataclasses/std lib): è il contratto condiviso.
- Root modules (`ui_components`, `contact_picker`, `emoji_picker`, `device_link_screen`, `qr_utils`) dipendono da `models`/Textual ma non dai backend, tranne dove ricevono callback (es. device link screen usa `backends.config` e client REST tramite funzioni locali).

## 7. Punti di estensione

- Aggiungere un protocollo = implementare `ChatBackend` (`backends/base.py`), registrarlo in `tui/app.py::__init__` e gestire eventuali nuovi tipi evento in `tui/events.py::_handle_event`. I tipi evento oggi supportati sono: `message`, `message_edit`, `typing`, `receipt`, `contact_update` (definito in `models.ChatEvent`; i primi quattro sono quelli effettivamente dispatchati dalla UI).
- Le funzionalità opzionali del contratto base (edit, rubrica, pairing) hanno default sicuri: `edit_message_sync → False`, `apply_edit → None`, `list_address_book_sync → contacts correnti`, `needs_pairing → False`.

## Documenti collegati

- [BACKEND_COMPONENTS.md](BACKEND_COMPONENTS.md) — dettaglio dei componenti backend.
- [../api-contracts/API_OVERVIEW.md](../api-contracts/API_OVERVIEW.md) — contratti interni (RPC, DB, webhook, eventi).
- [../design/DESIGN_OVERVIEW.md](../design/DESIGN_OVERVIEW.md) — principi di design UI/data flow.
