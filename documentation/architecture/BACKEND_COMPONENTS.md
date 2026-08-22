# Componenti backend

Descrizione dei moduli di `backend/` (layer condiviso) e `backends/` (implementazioni per protocollo), con responsabilità e relazioni. Ricavato dai sorgenti attuali.

## 1. Package `backend/` — layer condiviso (no Textual)

Il package re-esporta in `backend/__init__.py` l'API storica del vecchio modulo singolo `backend.py`, così `from backend import X` continua a funzionare per UI e test.

### 1.1 `backend/db.py` — cache messaggi SQLite

Responsabilità: persistenza locale dei messaggi per `(protocol, contact_number)`, migrazioni schema, dedup, receipt, unread.

- File DB: `~/.local/share/signal-tui-client/messages.db`; journal mode WAL; accesso serializzato da `_DB_LOCK` (`threading.RLock`) perché scrivono sia il poll worker sia il thread UI.
- Tabella `messages`: colonne `id` (autoincrement), `protocol` (default `'signal'`), `contact_number`, `text`, `is_mine`, `sender`, `timestamp`, `quote_text`, `msg_type` (`text|image|sticker|attachment`), `attachment_info`, `attachment_id`, `read`, `status` (default `'read'`), `msg_id`, `quote_timestamp`, `quote_author`, `reply_to_message_id`, `edited`.
- Schema versioning: `_SCHEMA_VERSION = 3` via `PRAGMA user_version`; `_migrate_protocol_schema()` aggiunge in modo additivo e idempotente le colonne `protocol`, `msg_id`, `quote_timestamp`, `quote_author`, `reply_to_message_id` (la colonna `edited` è assicurata **fuori** dal gate user_version, perché DB già a v3 possono comunque non averla), ricostruisce l'indice `(protocol, contact_number, timestamp)`.
- API principale: `_init_db`, `_load_cache(protocol=None)`, `_add_message_to_cache(...)`, `_update_message_id(...)` (aggancia l'id server alla riga ottimistica senza id), `_prune_cache()` (limita alle 200 righe più recenti per `(protocol, contact_number)`; la pruning temporanea è disabilitata nel codice), `_mark_as_read(contact, protocol)`, `_dedup_messages()`, `_dedup_messages_by_id()` (tiene la riga con status rank più alto), `_count_unread()`, e le tre varianti di aggiornamento stato: `_update_message_status` (per timestamp scoped protocollo+contatto+testo), `_update_message_status_by_id`, `_update_message_status_by_text` (riga outgoing più recente; fallback quando l'echo sostituisce il timestamp). Tutte le mutazioni di status applicano un **rank guard** (`pending/failed=0 < sent=1 < delivered=2 < read=3`): mai downgrade.
- `_update_message_text(...)`: riscrive il testo di una riga (edit) matchando per `msg_id` o `timestamp`, senza mai toccare l'identità temporale; gestisce la flag `edited`.

### 1.2 `backend/rpc.py` — comunicazione signal-cli

- Costanti: `DAEMON_HTTP_PORT = 8080`, `DAEMON_URL = http://127.0.0.1:8080/api/v1/rpc`, `SSE_URL = .../api/v1/events`, `SIGNAL_CLI_ATTACHMENTS_DIR = ~/.local/share/signal-cli/attachments`.
- Configurazione numero utente: `_get_user_number()` legge `SIGNAL_USER_NUMBER` poi `config.json["user_number"]` (best-effort, `""` se assente); `_require_user_number()` solleva il `RuntimeError` canonico al punto d'uso. Risoluzione binario: `_find_signal_cli()` scandisce `./bin/signal-cli-*/bin/signal-cli` (non-raising); `find_signal_cli()` è la variante che solleva `FileNotFoundError`.
- `SignalRPCClient`: client JSON-RPC 2.0 su HTTP POST (timeout 30 s, errori restituiti come `{"error": str}`); metodi `list_contacts()`, `send_message(message, recipient, [timestamp], [quote_*], [edit_timestamp])`, `receive()` (polling legacy), `listen_events(user_number)` generatore SSE che yielda dict con chiave `envelope`, con keep-alive ogni 15 s e timeout socket 30 s (il chiamante riconnette).
- Parsing envelope condiviso: `_process_typing(envelope)` → `(contact, "STARTED"|"STOPPED") | None`; `_process_receipt(envelope, cache)` → lista messaggi aggiornati, con fuzzy match ±1000 ms sui timestamp e rank guard.
- Fallback subprocess: `_run_subprocess(args)` (timeout 60 s, errore → `RuntimeError` con stderr), `_send_subprocess(...)` supporta quote (`--quote-timestamp/-author/-message`) ed edit (`--edit-timestamp`).
- Modello `Contact(number, name, aci)` con `display_name`.

### 1.3 `backend/webhook.py` — server webhook WAHA (push)

- Server `socketserver.TCPServer` su `0.0.0.0:WEBHOOK_PORT` (`CLIENT_WEBHOOK_PORT`, default 8088), thread daemon, avvio idempotente via `ensure_webhook_server(backend)` (ri-bind del target senza restart del socket).
- `_WebhookHTTPHandler.do_POST`: accetta solo path che termina con `/webhook` (altrimenti 404); parsa il JSON body (malformato → 400); inoltra il dict al `backend.handle_webhook` registrato; risponde **sempre** `200 {"ok": true}` per confermare la ricezione ed evitare retry WAHA (errori interni del target non bloccano l'ack).

### 1.4 `backend/download.py` — server download temporaneo

- Server HTTP persistente su porta fissa `DOWNLOAD_PORT = 10042`, thread daemon, avviato al primo download (`_ensure_download_server()`).
- I file sono serviti da `CACHE_DIR/downloads`: symlink al file originale (fallback copia). `serve_text_as_file(text, filename)` e `serve_attachment_for_download(attachment_id)` ritornano l'URL completo o una stringa `ERROR: ...`.
- `get_local_ip()` privilegia `SSH_CONNECTION` (IP server) con fallback UDP probe.

## 2. Package `backends/` — implementazioni per protocollo

Esportazioni pubbliche (`backends/__init__.py`): `ChatBackend`, `BackendManager`, `SignalBackend`, `WhatsAppBackend`, `TelegramBackend`.

### 2.1 `backends/base.py` — interfaccia astratta `ChatBackend`

Contratto che la UI conosce (dettaglio completo in [../api-contracts/API_OVERVIEW.md](../api-contracts/API_OVERVIEW.md)):

- Lifecycle: `connect()` / `disconnect()` (abstract, async).
- Dati: `list_contacts()` (abstract), `send_message(...)` (abstract), `mark_read(contact_id)` (abstract), `receive()` (abstract, async generator).
- Metodi sincroni opzionali usati dai worker della TUI (pattern `*_sync`): `edit_message_sync`, `list_address_book_sync`, più wrapper async che delegano via `asyncio.to_thread`.
- `apply_edit(...)`: punto unico di mutazione per gli edit (cache + DB), idempotente.
- `get_attachment_path(attachment_id)`, `register_contact(contact)`, proprietà `needs_pairing`, `get_pairing_qr()`.
- Attributo di classe obbligatorio: `protocol` (una delle costanti `models.PROTOCOL_*`).

### 2.2 `backends/config.py` — configurazione WhatsApp/Telegram/picker

- Sorgenti: i getter generici leggono env var > `config.json` (poi default); il file `.env` (parsato KEY=VALUE best-effort da `_load_dotenv`) è consultato SOLO per `WAHA_API_KEY` e per le credenziali Telegram `TELEGRAM_API_ID`/`TELEGRAM_API_HASH`.
- WhatsApp: `resolve_whatsapp_api_url()` (configurato oppure `http://127.0.0.1:{WHATSAPP_API_PORT|3005}`), `get_whatsapp_api_key()` (env `WHATSAPP_API_KEY` > config.json > `.env` `WAHA_API_KEY`), session name (default `"default"`), media dir, porta webhook (`CLIENT_WEBHOOK_PORT`, default 8088) e URL webhook (`WAHA_WEBHOOK_URL` esplicito oppure `http://host.docker.internal:{port}/webhook`). `whatsapp_enabled()` = URL configurato OPPURE porta locale raggiungibile (auto-detect).
- Telegram: `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` (env > config.json > .env), path sessione Telethon in `$XDG_DATA_HOME/signal-tui-client/telegram.session`, `telegram_enabled()`.
- Knob tuning: TTL rubrica (300 s), TTL cache `@lid` WhatsApp (30 giorni), max risultati picker (50), backend preferito.

### 2.3 `backends/manager.py` — `BackendManager`

- Registry `protocol → ChatBackend` (`register/get/all/protocols`); `KeyError` per protocolli sconosciuti nelle operazioni async (`send_message`, `mark_read`).
- `connect_all()/disconnect_all()` (disconnect best-effort).
- `list_contacts()`: concatena gli attributi `contacts` già caricati dei backend (sync, niente rete).
- `list_address_book_sync(protocols, force)`: fan-out parallelo con `ThreadPoolExecutor(max_workers=3)` e `future.result(timeout=25)`; errori per-protocollo registrati in `address_book_errors` e il risultato parziale viene comunque restituito.
- Routing: `send_message`, `mark_read`, `get_attachment_path`, `edit_message_sync` (ritorna `False` se assente/non supportato).

### 2.4 `backends/signal.py` — `SignalBackend` (protocol `"signal"`)

- Connessione (`_connect_sync`): carica la cache SQLite del protocollo; se il daemon non gira avvia `signal-cli daemon --http 127.0.0.1:{port} --receive-mode on-connection` come subprocess e subito dopo il listener SSE (retry interno); probe `listContacts` fino a ~15 s, altrimenti fallback subprocess per i contatti.
- Ricezione: thread `_sse_listener` → `SignalRPCClient.listen_events()` → `envelope_to_event()` classifica l'envelope in eventi normalizzati:
  - `editMessage` (top-level o dentro `syncMessage.sentMessage`) → evento `message_edit` con `payload["timestamp"]` = ts ORIGINALE (`targetSentTimestamp`); gli edit envelope non cadono mai nel parsing normale;
  - `typingMessage` → evento `typing`;
  - `receiptMessage` → evento `receipt` (payload `{"receipt": {...}}`);
  - `dataMessage`/`syncMessage.sentMessage` → uno o più eventi `message` (uno per attachment), con `id = str(timestamp)` per gli outgoing sync.
- Ingestion: `ingest_message(contact_id, data, ts, persist=True)` dedup con finestre (`_SEND_DEDUP_WINDOW_MS = 5000` outgoing, `_INCOMING_DEDUP_WINDOW_MS = 2000` incoming) e branch di upgrade che aggancia il vero id server all'ottimistico senza cambiare timestamp.
- Invio: `send_message_sync` via RPC `send` (legge `result.timestamp` reale) o `_send_subprocess`; `edit_message_sync(contact_id, message_id=ts_originale, new_text)` usa `editTimestamp`.
- Receipt: `process_receipt(envelope)` (delega a `backend._process_receipt` sul proprio cache).
- Rubrica: `list_address_book_sync(force)` con cache + TTL da `get_address_book_ttl_s()`.

### 2.5 `backends/whatsapp.py` + `whatsapp_events.py` + `whatsapp_rest.py` — `WhatsAppBackend` (protocol `"whatsapp"`)

- `WhatsAppRESTClient` (`whatsapp_rest.py`): thin client REST verso WAHA — sessioni (`create/start/status/reset`, QR pairing), chat/contatti (`GET /api/{session}/chats` usato da `list_contacts` e da `_discover_active_chats`, `/api/contacts/all`, `/api/{session}/contacts/{jid}`, `check-exists`), invio (`POST /api/sendText` con `chatId`, `text`, opzionale `reply_to`), edit (`PUT /api/{session}/chats/{chatId}/messages/{messageId}`, path percent-encoded), storico (`GET /api/messages?...&limit=N`, timeout 30 s), mark-read (`POST /api/chats/{id}/read`, best-effort), presence subscribe (`POST /api/{session}/presence/{jid}/subscribe`), media download multi-strategia (URL diretto con riscrittura porta container → `/api/{session}/{id}/download` → redirect JSON `/api/messages/{id}/download` → `/api/files/default/{id}`; path sempre percent-encoded per i `@` dei JID). Tutti i metodi sono non-raising: `None`/`[]` su errore.
- Connessione (`connect_sync`): `_wait_session_ready(timeout=40.0)` interroga lo stato sessione finché non è `WORKING` (poll ogni 0,5 s, uscita immediata sugli stati morti tipo failed/stopped); `_configure_webhook()` registra/aggiorna il webhook push sulla sessione via `PUT /api/sessions/{name}` a OGNI avvio — il solo env `WAHA_WEBHOOK_URL` non fa emettere eventi a WAHA — saltando il PUT se URL ed eventi sono già configurati (evita restart inutili); eventi desiderati: `message`, `message.any`, `message.ack`, `message.ack.group`, `presence.update`. Best-effort, mai eccezioni.
- Normalizzazione (`whatsapp_events.py`): funzioni pure `_event_from_raw/_event_from_message/_event_from_receipt/_event_from_ack/_event_from_typing`, costanti ack ufficiali WAHA (`WAHA_ACK_ERROR=-1 … WAHA_ACK_PLAYED=4`), helper JID (`_jid_string`), `canonical_msg_id()` che riduce gli id Baileys (`true_{jid}_{hex}`, forma gruppo, hex puro) a un token confrontabile per i receipt.
- `handle_webhook(raw)`: gestisce `message` e `message.ack`. Per gli ack outgoing costruisce un evento sintetico `message` (l'ack può arrivare INVECE dell'evento, anche con status < 2), rileva gli edit (stesso id + body nuovo → evento `message_edit`) e applica una guardia anti-retry per `(contact, id, testo normalizzato)`. Trigger lazy della presence subscription al primo messaggio di un contatto — subordinato al flag descritto sotto.
- Coda eventi: `_enqueue_event` (coda illimitata con guardia drop-oldest difensiva — mai innescata in pratica, la `queue.Queue` è creata senza `maxsize`) + `poll_once()` che svuota la coda per il poll worker della TUI.
- Cache/storico: `fetch_history(contact_id, limit)` scarica lo storico remoto e popola il backend cache (dedup interna via `ingest_message`); `resync_history(limit)` ri-sincronizza all'avvio unread ∪ chat presenti nel DB; ordinamento deterministico `_msg_sort_key = (timestamp, id|testo)`; `_message_already_cached` con identità id-first e fuzzy window (incoming ±5 s testo+ts, outgoing echo ±10 min con id).
- Presence subscription (typing) OFF di default: gated da `_PRESENCE_SUBSCRIBE_ENABLED`, attiva solo con l'env `WAHA_PRESENCE_SUBSCRIBE=1` (`true`/`yes`/`on` accettati). Motivo: su WAHA 2026.8.1 engine WEBJS `subscribePresence` risponde 500 (WON'T FIX), quindi sweep e lazy subscribe sarebbero lavoro inutile. Di conseguenza il typing WhatsApp oggi NON funziona nella configurazione standard; il parser `_event_from_typing` resta attivo e invariato.
- Extra thread: resolver asincrono `@lid → numero` (cache persistente JSON con TTL); presence subscribe sweep in background e lazy subscribe per messaggio/ack, entrambi subordinati al flag `WAHA_PRESENCE_SUBSCRIBE=1` (no-op quando OFF).
- Edit: `edit_message_sync` via REST PUT; `apply_edit` aggiorna cache+DB (specchio di Signal).
- Media: `get_attachment_path` risolve nella `media_dir` configurata (download on-demand dove previsto).

### 2.6 `backends/telegram.py` — `TelegramBackend` (protocol `"telegram"`)

- Architettura: event loop asyncio dedicato in thread daemon (`_run_event_loop`); `TelegramClient` Telethon con sessione file persistente. `_connect_sync` avvia loop+client, registra handler (`NewMessage`, `MessageEdited`, raw update) e carica contatti/dialoghi.
- Login: QR `tg://login?token=...` tramite `get_pairing_qr()`; supporto 2FA (flag `_needs_2fa`, password gestita dalla schermata device link).
- Eventi in arrivo: handler convertono in `ChatEvent` e li mettono in `self._events` (consumati da `poll_once()`): messaggi (`_message_to_chat_event`), edit, read receipt (raw update → `process_receipt` per message-id), typing (`_handle_typing_update`).
- Dedup: set `_seen_msg_ids` + finestra incoming 2000 ms; cache per contatto limitata a `_MAX_CACHE_PER_CONTACT = 50`.
- Reply: `send_message_sync` accetta `reply_to_message_id` validato come intero server (`_validated_reply_to_message_id`); le reply Telegram devono usare l'id server, non il timestamp.
- Media: attachment id sintetico `tgref:<chat_id>:<msg_id>` per il download lazy via Telethon in `get_attachment_path` (file in temp dir).
- Read state: `_reconcile_read_state()` e `fetch_recent_history(limit)` per la sync iniziale.

## 3. Relazioni tra componenti (sintesi)

```
TUI (tui/) ──usa──► BackendManager ──► ChatBackend implementations
      │                    │                     │
      │                    │              ┌──────┴────────┐
      ▼                    ▼              ▼               ▼
backend/db.py        backends/config.py  backend/rpc.py   whatsapp_rest.py /
(SQLite condivisa)   (env/json/.env)     (signal-cli)     telegram (Telethon)
      ▲                                     ▲               ▲
      └────── scritture incrementali ───────┘               │
                                             backend/webhook.py (riceve WAHA push)
```

Ogni backend possiede: la propria `queue.Queue` eventi, la propria cache in memoria (`cache: {contact_id: [msg_dict]}`), la lista `contacts` normalizzata e la rubrica cache+TTL. La UI non accede direttamente ai servizi esterni: tutto passa dall'interfaccia `ChatBackend`/`BackendManager` o dalle funzioni re-esportate da `backend/`.
