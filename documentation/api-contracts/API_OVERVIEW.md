# API e contratti interni — panoramica

Elenco delle superfici programmatiche interne del progetto, ricavate dal codice (non da documenti storici). Per i contratti di dati dettagliati (campi, tipi, semantica) vedere [CONTRACTS.md](CONTRACTS.md).

Indice:

1. [Interfaccia `ChatBackend`](#1-interfaccia-chatbackend-backendsbasepy)
2. [`BackendManager` (facade)](#2-backendmanager-facade-backendsmanagerpy)
3. [Protocollo RPC signal-cli](#3-protocollo-rpc-signal-cli-backendrpcpy)
4. [API layer DB SQLite](#4-api-layer-db-sqlite-backenddbpy)
5. [Webhook server WAHA](#5-webhook-server-waha-backendwebhookpy)
6. [Client REST WAHA](#6-client-rest-waha-backendswhatsapp_restpy)
7. [Eventi `ChatEvent` scambiati TUI ↔ backend](#7-eventi-chatevent-scambiati-tui--backend)
8. [Server download HTTP](#8-server-download-http-backenddownloadpy)
9. [Configurazione](#9-configurazione-backendsconfigpy)

---

## 1. Interfaccia `ChatBackend` (`backends/base.py`)

Contratto implementato da Signal/WhatsApp/Telegram; la UI lo conosce esclusivamente.

### Metodi astratti (obbligatori)

| Firma | Semantica |
|---|---|
| `async connect() -> None` | avvia demone/websocket e carica i dati iniziali |
| `async disconnect() -> None` | arresta e rilascia risorse |
| `async list_contacts() -> list[ChatContact]` | contatti noti come oggetti normalizzati |
| `async send_message(contact_id, text, quote_timestamp=None, quote_author=None, quote_message=None, reply_to_message_id=None) -> str` | invia e ritorna id/timestamp del messaggio |
| `async mark_read(contact_id) -> None` | marca tutti i messaggi del contatto letti |
| `receive() -> AsyncIterator[ChatEvent]` | yielda eventi normalizzati (da eseguire in worker thread) |

Attributo di classe richiesto: `protocol: str` (una delle costanti `PROTOCOL_*` di `models`).

### Metodi con default sicuri (opzionali)

| Metodo | Default | Note |
|---|---|---|
| `get_attachment_path(attachment_id) -> Path \| None` | `None` | risolve un attachment id su file locale |
| `edit_message_sync(contact_id, message_id, new_text) -> bool` | `False` | bloccante, solo da worker thread; semantica di `message_id` per protocollo documentata nel docstring (signal = ts ms come stringa; telegram = id server; whatsapp = Baileys id) |
| `async edit_message(...) -> bool` | delega `edit_message_sync` via `asyncio.to_thread` | |
| `apply_edit(contact_id, message_id, new_text, *, is_mine=None, edit_timestamp=None) -> dict \| None` | `None` | punto UNICO di mutazione per gli edit; ritorna `{"message_id","timestamp","old_text","text","is_mine"}` solo se ha modificato; idempotente |
| `list_address_book_sync(force=False) -> list[ChatContact]` | contacts correnti marcati `extras["address_book"]=True` | bloccante, non solleva mai (errore → cache o `[]`) |
| `async list_address_book()` | delega sync via `asyncio.to_thread` | |
| `register_contact(contact) -> None` | append se assente | rende il contatto noto per lookup eventi/invio |
| `needs_pairing` (property) | `False` | richiede pairing QR interattivo |
| `async get_pairing_qr() -> str \| None` | `None` | link QR corrente |

Convenzione `*_sync`: le versioni sincrone sono pensate per i worker thread della TUI (stesso pattern di `send_message_sync`, presente nelle implementazioni concrete). Estensione per-protocollo: solo `SignalBackend.send_message_sync` accetta il kwarg extra `quote_attachments` (thumbnail della quote media — vedi [CONTRACTS.md](CONTRACTS.md#11-quote-media-reply-con-immagine) §11); non fa parte del contratto astratto.

### Contratto duale: async nominale vs flusso reale (leggere prima di estendere)

La superficie async è **definita ma di fatto inusata dalla TUI** (nessun call site di `manager.send_message`/`mark_read`/`connect_all`/`disconnect_all` né di `backend.receive()` in `tui/`): il flusso reale passa dai metodi `*_sync` invocati nei worker thread. Conseguenze da conoscere:

- gli override async eseguono I/O **bloccante nel corpo della coroutine**: `WhatsAppBackend.connect` → `connect_sync()` (fino a ~40 s di `_wait_session_ready`), `WhatsAppBackend.send_message` (REST fino a 30 s), `TelegramBackend.send_message` (`future.result(timeout=30)`); un chiamante che li awaitasse davvero bloccherebbe l'event loop;
- `get_pairing_qr` ha firme divergenti: `async` nella base (`backends/base.py:188`) ma override **sync** in `TelegramBackend`; la UI lo chiama via `asyncio.to_thread(tb.get_pairing_qr)` per Telegram e per WhatsApp bypassa l'interfaccia chiamando `wa._rest.get_pairing_qr()` direttamente (`device_link_screen.py`);
- `SignalBackend.receive()` è inusato (la TUI usa `poll_once()`) e muta `self._polling_active`: un consumer lo riaccenderebbe dopo un disconnect.

Direzione raccomandata per nuovi backend: sync-first (metodo `*_sync` come contratto primario + wrapper `asyncio.to_thread`, come già fatto per `edit_message`/`list_address_book`).

Test che fissano il contratto: `tests/test_edit_contract.py` (default `False`/`None`, routing manager), `tests/test_address_book.py`.

## 2. `BackendManager` facade (`backends/manager.py`)

| API | Comportamento |
|---|---|
| `register(backend)` | registry chiave `backend.protocol`; `ValueError` senza protocollo |
| `get(protocol) -> ChatBackend \| None` / `all()` / `protocols()` | accesso registry |
| `connect_all()` / `disconnect_all()` | disconnect best-effort (errori loggati) |
| `list_contacts() -> list[ChatContact]` | concatena gli attributi `.contacts` già caricati (nessuna rete) |
| `list_address_book_sync(protocols=None, force=False)` | fan-out parallelo (`ThreadPoolExecutor(max_workers=3)`, timeout 25 s/backend); errori in `address_book_errors[protocol]`, risultato parziale ammesso. Attenzione: i `future.result(timeout=25)` sono valutati **in serie**, quindi con più backend lenti l'attesa si somma (fino a ~75 s con 3 backend appesi) |
| `send_message(protocol, contact_id, text, quote_*, reply_to_message_id=None)` | routing; `KeyError` se protocollo sconosciuto |
| `mark_read(protocol, contact_id)` | routing; `KeyError` se sconosciuto |
| `get_attachment_path(protocol, attachment_id)` | routing; `None` se backend assente |
| `edit_message_sync(protocol, contact_id, message_id, new_text) -> bool` | routing; `False` se assente/non supportato |

## 3. Protocollo RPC signal-cli (`backend/rpc.py`)

- Endpoint: `POST http://127.0.0.1:{DAEMON_HTTP_PORT=8080}/api/v1/rpc`; eventi: `GET .../api/v1/events?account={numero}` (SSE).
- Payload JSON-RPC 2.0: `{"jsonrpc":"2.0","id":<int crescente>,"method":str,"params":{}}`.
- Metodi usati:
  - `listContacts` — probe daemon + lista contatti;
  - `send` — parametri: `message`, `recipient: [numero]`, opzionali `timestamp` (ignorato da signal-cli), `quoteTimestamp`, `quoteAuthor`, `quoteMessage`, `editTimestamp` (quando presente il messaggio è una MODIFICA) e `quoteAttachments` (lista di stringhe `contentType[:filename[:previewFile]]`: la thumbnail che rende visibile una quote media su Signal); la risposta contiene `result.timestamp` = vero timestamp server;
  - `receive` — polling legacy.
- Errori: ogni fallimento HTTP/socket è restituito come `{"error": "<messaggio>"}` (mai eccezioni da `_call`); `send_message_sync` converte `"error"` in `RuntimeError`.
- SSE: `listen_events(user_number)` yielda dict `{"envelope": {...}}`; keep-alive ogni 15 s; timeout socket 30 s → il generatore termina e il chiamante riconnette dopo una breve pausa (~1 s, commento "keep it short (1s)" in `SignalBackend._sse_listener`).
- Limiti SSE noti (stato attuale): nessun backoff/jitter/tetto — il retry resta ~1 Hz indefinitamente durante un outage del daemon, senza alcun segnale di stato verso la UI; il log può essere fuorviante (`if envelope:` a valle del `for` valuta una variabile mai assegnata o stale quando il generatore ritorna senza yield → NameError catturato o "SSE: envelope received" spurio, la causa reale non appare); `restart_sse()` è dead code (nessun chiamante) con una race latente: un restart ri-armerebbe la guardia del vecchio thread (bloccato in `urlopen` fino a 30 s), che al risveglio riconnetterebbe → doppio listener ed eventi duplicati. Nota: il commento nel codice a `backends/signal.py:140` ("reconnects every 5s") è stale, la pausa reale è ~1 s.
- Fallback subprocess: `[signal-cli, "-u", numero, ...args]`, timeout 60 s, stdout testuale; `send` subprocess ritorna il timestamp stampato su stdout e ripete `--quote-attachment <contentType[:filename[:previewFile]]>` per ogni elemento di `quoteAttachments`.

Test: `tests/test_backend_rpc.py`, `tests/test_backend_send.py`, `tests/test_signal_real_timestamp.py`.

## 4. API layer DB SQLite (`backend/db.py`)

Schema v3 (`PRAGMA user_version = 3`) — tabella `messages` descritta in [CONTRACTS.md](CONTRACTS.md#31-tabella-messages-sqlite). Funzioni pubbliche (re-esportate anche da `backend/__init__.py`):

| Funzione | Contratto essenziale |
|---|---|
| `_init_db()` | crea schema + migrazioni additive idempotenti |
| `_load_cache(protocol=None) -> {contact: [msg_dict]}` | carica ordinato per ts; dedup by-id cross-session; filtro per protocollo |
| `_add_message_to_cache(...)` | INSERT incrementale; status default `"sent"` se `is_mine` else `"read"` |
| `_update_message_id(contact, text, is_mine, timestamp, msg_id, protocol)` | aggancia l'id server alle righe id-less `(msg_id IS NULL OR '')` matchate per `(protocol, contact_number, text, is_mine)` — **UPDATE MULTI-RIGA**: nessuna finestra temporale né `LIMIT`, quindi TUTTE le righe id-less con lo stesso testo nella chat ricevono lo stesso `msg_id` (e il timestamp sovrascritto). Combinato con `_dedup_messages_by_id()` — eseguito dentro `_load_cache()` a ogni boot — righe distinte col medesimo testo possono essere cancellate come "duplicati" (rischio perdita dati; caso reale: retry di un messaggio fallito riparte senza id). Mitigazione proposta: finestra echo nel WHERE + `LIMIT 1` via subquery, e dedup difensiva che non cancella partizioni con timestamp fuori finestra |
| `_prune_cache()` | tiene le 200 righe più recenti per `(protocol, contact_number)` |
| `_mark_as_read(contact, protocol="signal")` | `read = 1` per il contatto |
| `_dedup_messages()` / `_dedup_messages_by_id()` | rimozione duplicati (per tupla identità / per msg-id col rank status più alto); ritornano il numero righe rimosse |
| `_count_unread() -> {contact: n}` | `is_mine = 0 AND read = 0` |
| `_update_message_status(timestamp, status, protocol, contact, text=None, expected_statuses=None) -> bool` | update scoped + rank guard (mai downgrade); True se aggiornata almeno una riga |
| `_update_message_status_by_id(msg_id, status, protocol, contact=None) -> bool` | come sopra, per msg-id (receipt Telegram) |
| `_update_message_status_by_text(text, status, protocol, contact, expected_statuses=None) -> bool` | riga outgoing più recente per testo (fallback echo) |
| `_update_message_text(contact, new_text, protocol, msg_id=None, timestamp=None, old_text=None, is_mine=None, mark_edited=True) -> bool` | riscrive il testo (edit), mai l'identità temporale |

Tutte le funzioni aprono connessioni brevi sotto `_DB_LOCK` (RLock condiviso tra thread poll e UI). Test: `tests/test_backend_cache.py`, `tests/test_db_edit.py`, `tests/test_db_schema_versioning.py`, `tests/test_migrate_*.py`.

## 5. Webhook server WAHA (`backend/webhook.py`)

- `ensure_webhook_server(backend) -> int`: avvia (idempotente) il listener su `0.0.0.0:{WEBHOOK_PORT}` e registra `backend.handle_webhook` come target; ritorna la porta (o `0` se il bind fallisce: best-effort).
- Contratto HTTP in ingresso:
  - `POST <qualsiasi>/webhook` con body JSON WAHA `{"event": "message", "session": "...", "payload": {...}}`;
  - path diverso → `404 {"ok": false}`;
  - body non JSON → `400`;
  - **risposta sempre `200 {"ok": true}`** dopo l'inoltro, anche se il target solleva (evita retry WAHA).
- Threat model (stato attuale, pre-mitigazione — trattarlo come superficie NON fidata):
  - bind su `0.0.0.0:8088` e nessuna autenticazione/token: chiunque raggiunga la porta (LAN, container della stessa macchina) può iniettare eventi arbitrari — inclusi messaggi sintetici con `fromMe: true` che vengono persistiti in SQLite e mostrati come autentici;
  - server **single-threaded** (`TCPServer`, non threading) senza `settimeout` sul socket e senza tetto su `Content-Length`: una connessione aperta senza body blocca l'unico thread di gestione → ricezione WhatsApp ferma (DoS, anche accidentale);
  - pipeline **at-most-once**: l'ack `200` viene inviato PRIMA della persistenza (l'evento è solo enqueuato in memoria); un crash tra ack e drain perde gli eventi accodati e WAHA non li rispedisce;
  - mitigazioni candidate: bind ristretto/route via gateway docker, token nel path (registrabile via `_configure_webhook`), `ThreadingTCPServer` + `settimeout` + tetto payload.

Test: `tests/test_backend_webhook.py`.

## 6. Client REST WAHA (`backends/whatsapp_rest.py`)

Tutti i metodi sono non-raising (`None`/`[]` su errore); header `X-Api-Key` dalla config.

| Endpoint | Wrapper |
|---|---|
| sessioni: `POST /api/sessions[/start|/logout|/stop]`, `GET|PUT /api/sessions/{name}`, QR pairing (`GET /api/{session}/auth/qr` PNG raw, fallback testuale `/api/sessions/{name}/qr`) | `create_session`, `start_session`, `get_session_status`, `update_session_config`, `reset_session`, `get_pairing_qr`, `get_fresh_pairing_qr`, `get_session_qr` |
| `GET /api/{session}/chats` (chat attive = contatti; usato anche da `WhatsAppBackend._discover_active_chats`) | `list_contacts` |
| `GET /api/contacts/all` | `list_all_contacts` |
| `GET /api/{session}/contacts/{jid}` | `resolve_contact` |
| `GET /api/contacts/check-exists?phone=` | `check_number_exists` |
| `POST /api/sendText` (`session`, `chatId`, `text`, opz. `reply_to`) | `send_message` |
| `PUT /api/{session}/chats/{chatId}/messages/{messageId}` (`{"text", "linkPreview": true}`, segmenti percent-encoded) | `edit_message` |
| `GET /api/messages?session&chatId&limit` (timeout 30 s, storico-only) | `list_messages` |
| `POST /api/chats/{chatId}/read` (best-effort) | `mark_read` |
| `POST /api/{session}/presence/{jid}/subscribe` (best-effort, JID percent-encoded) | `presence_subscribe` |
| media: `GET /api/messages/{id}/download` (JSON con URL), download binario multi-strategia (`/api/{session}/{id}/download`, redirect legacy, `/api/files/default/{id}`; id/URL percent-encoded) | `get_download_url`, `download_media` |

Test: `tests/test_whatsapp_backend.py`, `tests/test_wa_startup_resync.py`, `tests/test_config.py`.

## 7. Eventi `ChatEvent` scambiati TUI ↔ backend

Tipo (`models.ChatEvent.type`) e payload prodotti dai backend e dispatchati da `tui/events.py::_handle_event`:

| type | payload (campi osservati nel codice) | Produttori |
|---|---|---|
| `"message"` | dict messaggio normalizzato: `text, is_mine, sender, timestamp(ms), id?, msg_type, attachment_info?, attachment_id?, quote_text?, quote_timestamp?, quote_author?, reply_to_message_id?, status?, contact?: ChatContact` (+ `is_group` per WhatsApp ack sintetici) | tutti |
| `"message_edit"` | `{edit_message_id: str, text, timestamp: int (ts ORIGINALE), edit_timestamp: int\|None, is_mine: bool, sender, contact?: ChatContact\|None, msg_type: "text"}` | tutti |
| `"typing"` | `{action: "STARTED"\|"STOPPED"}` | Signal, WhatsApp (presence), Telegram |
| `"receipt"` | Signal: `{receipt: {isDelivery, isRead, timestamps[]}}`; WhatsApp/Telegram: `{message_ids: [...], is_read: bool}` (consumato con `contact_id` dell'evento) | tutti |
| `"contact_update"` | definito in `models.ChatEvent` (payload ChatContact dict); nessun dispatcher UI dedicato oggi | — |

Dettagli campi e semantica: [CONTRACTS.md](CONTRACTS.md#32-eventi-chatevent).

## 8. Server download HTTP (`backend/download.py`)

- `serve_text_as_file(text, filename="message.txt") -> str` / `serve_attachment_for_download(attachment_id) -> str` / `_serve_file_path(path) -> str`: ritornano l'URL completo `http://{ip}:{10042}/{nome}` oppure una stringa d'errore prefissata `ERROR:` (contratto stringa, verificato dai chiamanti con `url.startswith("ERROR:")`).
- Server persistente su porta fissa `10042`, root = `CACHE_DIR/downloads`, file puliti a ogni nuova servizione.

Test: `tests/test_backend_download.py`, `tests/test_download_mode.py`.

## 9. Configurazione (`backends/config.py`)

Getter con precedenza env > `config.json`; il file `.env` è caricato solo per `WAHA_API_KEY` e credenziali Telegram (API_ID/API_HASH):

- Signal: `SIGNAL_USER_NUMBER` / `config.json["user_number"]` (in `rpc.py`);
- WhatsApp: `WHATSAPP_API_URL`, `WHATSAPP_API_PORT` (3005), `WHATSAPP_API_KEY` (fallback `.env` `WAHA_API_KEY`), `WHATSAPP_SESSION_NAME` ("default"), `WHATSAPP_MEDIA_DIR`, `CLIENT_WEBHOOK_PORT` (8088), `WAHA_WEBHOOK_URL`;
- Telegram: `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, sessione in `$XDG_DATA_HOME/signal-tui-client/telegram.session`;
- Tuning: `ADDRESS_BOOK_TTL_S` (300), `WA_LID_CACHE_TTL_DAYS` (30), `PICKER_MAX_RESULTS` (50), `PICKER_PREFERRED_BACKEND`.
- Feature flag: `whatsapp_enabled()` (URL configurato OR porta locale raggiungibile), `telegram_enabled()` (api_id != 0 AND api_hash).

Test: `tests/test_config.py`, `tests/test_backend_lazy_config.py`.
