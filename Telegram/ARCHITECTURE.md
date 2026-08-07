# Architettura: Integrazione Telegram nativa (Telethon)

> **Stato**: Proposta architetturale — in attesa di revisione prima dell'implementazione.

## Indice

1. [Analisi della codebase attuale](#1-analisi-della-codebase-attuale)
2. [Configurazione e sessione](#2-configurazione-e-sessione)
3. [Mappatura dei modelli](#3-mappatura-dei-modelli)
4. [Gestione eventi asincroni](#4-gestione-eventi-asincroni)
5. [Piano di integrazione](#5-piano-di-integrazione)
6. [Riepilogo e punti aperti](#6-riepilogo-e-punti-aperti)

---

## 1. Analisi della codebase attuale

### 1.1 Panoramica dei componenti

| File | Ruolo |
|---|---|
| `models.py` | Dataclass neutrali: `ChatContact`, `ChatMessage`, `ChatEvent` + costanti `PROTOCOL_*` |
| `backends/base.py` | Interfaccia astratta `ChatBackend(ABC)` — 6 metodi obbligatori + pairing opzionale |
| `backends/manager.py` | `BackendManager` — registry/facade sui backend, routing per `protocol` |
| `backends/signal.py` | `SignalBackend` — SSE listener in thread → `queue.Queue` → `poll_once()` |
| `backends/whatsapp.py` | `WhatsAppBackend` — Webhook HTTP → `queue.Queue` → `poll_once()` |
| `backends/config.py` | Configurazione WhatsApp da env / `config.json` / `.env` |
| `backends/__init__.py` | Re-export di `ChatBackend`, `BackendManager`, `SignalBackend`, `WhatsAppBackend` |
| `signal_tui.py` | App Textual: `__init__` registra i backend, `_startup` li connette, `_poll_worker` consuma `poll_once()` |
| `link_whatsapp.py` | Script CLI di pairing WhatsApp (QR code via API REST) |


### 1.2 Interfaccia `ChatBackend` (base.py)

```python
class ChatBackend(ABC):
    protocol: str = ""          # es. "signal", "whatsapp"

    # Lifecycle
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...

    # Data
    async def list_contacts(self) -> list[ChatContact]: ...
    async def send_message(self, contact_id, text, quote_*) -> str: ...
    async def mark_read(self, contact_id) -> None: ...
    async def receive(self) -> AsyncIterator[ChatEvent]: ...   # async generator

    # Opzionali
    def get_attachment_path(self, attachment_id) -> Path | None: ...
    @property
    def needs_pairing(self) -> bool: ...
    async def get_pairing_qr(self) -> str | None: ...
```

### 1.3 `BackendManager` (manager.py)

- **`register(backend)`** — keyed by `backend.protocol` (stringa)
- **`connect_all()` / `disconnect_all()`** — itera tutti i backend
- **`list_contacts()`** — accede **sincrono** a `backend.contacts` (lista popolata in `connect`)
- **`send_message(protocol, contact_id, text, ...)`** — routing per protocollo
- **`mark_read(protocol, contact_id)`** — routing per protocollo
- **`get_attachment_path(protocol, attachment_id)`** — routing per protocollo

**Osservazione**: il manager è già completamente generico. **Nessuna modifica necessaria** per Telegram — il routing via `protocol` funziona automaticamente.

### 1.4 Pattern di ricezione eventi (Signal e WhatsApp)

Entrambi i backend seguono lo stesso schema:

```
┌──────────────────────────────────────┐
│  Background Thread                     │
│  (SSE listener / Webhook handler)      │
│       ↓                                │
│  queue.Queue[ChatEvent]                │
│       ↓                                │
│  poll_once() → list[ChatEvent]  (sync) │
│       ↓                                │
│  receive() → AsyncIterator      (async)│
└──────────────────────────────────────┘
                      ↓
┌──────────────────────────────────────┐
│  signal_tui.py: _poll_worker          │
│  for backend in self.manager.all():   │
│      for evt in backend.poll_once():  │
│          self._handle_event(evt)      │
└──────────────────────────────────────┘
```

### 1.5 Wiring nel TUI (`signal_tui.py`)

```python
# __init__ (righe ~356-365)
self.manager = BackendManager()
self.signal_backend = SignalBackend()
self.manager.register(self.signal_backend)

if whatsapp_enabled():
    self.whatsapp_backend = WhatsAppBackend()
    self.manager.register(self.whatsapp_backend)

# _startup — chiama connect_sync() direttamente (non via manager)
self.signal_backend._connect_sync()
if self.whatsapp_backend:
    self.whatsapp_backend.connect_sync()

# _poll_worker
for backend in self.manager.all():
    for event in backend.poll_once():
        self._handle_event(event)
```

### 1.6 Sfida specifica di Telethon

Telethon è **nativamente asincrono**: `TelegramClient` richiede un `asyncio` event loop per connettersi ai server MTProto e ricevere eventi. I backend esistenti usano thread sincroni + `queue.Queue`.

**Strategia**: bridge asyncio→sync:

---

## 2. Configurazione e sessione

### 2.1 Credenziali Telegram

Telethon richiede due credenziali ottenibili da [https://my.telegram.org](https://my.telegram.org):

- **`TELEGRAM_API_ID`** (intero) — identificativo dell'applicazione
- **`TELEGRAM_API_HASH`** (stringa) — secret dell'applicazione

Seguendo il pattern WhatsApp (`backends/config.py`), la configurazione potrà provenire da tre fonti in ordine di priorità:

1. Variabili d'ambiente: `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`
2. Campi in `config.json`: `telegram_api_id`, `telegram_api_hash`
3. File `.env` del progetto

**Funzioni da aggiungere a `backends/config.py`:**

```python
def get_telegram_api_id() -> int:
    """Legge TELEGRAM_API_ID da env, config.json, o .env. Ritorna 0 se non impostato."""

def get_telegram_api_hash() -> str:
    """Legge TELEGRAM_API_HASH da env, config.json, o .env."""

def get_telegram_session_path() -> str:
    """Percorso del file .session di Telethon."""

def telegram_enabled() -> bool:
    """Ritorna True se API_ID e API_HASH sono configurati."""
    return bool(get_telegram_api_id() and get_telegram_api_hash())
```

### 2.2 File di sessione `.session`

Telethon persiste l'autenticazione in un file SQLite con estensione `.session`. Il percorso seguirà la stessa convenzione del cache Signal:

```
~/.local/share/signal-tui-client/telegram.session
```

Dove `CACHE_DIR` è già risolto in `backend.py` come `~/.local/share/signal-tui-client/`.

Il file viene creato automaticamente da Telethon al primo login riuscito. Non serve un parametro `session_name` configurabile (a differenza di WhatsApp/WAHA dove sessioni multiple lato server hanno senso — qui il client è locale).

> **⚠️ Vincolo**: sia `backends/telegram.py` (nel costruttore di `TelegramClient`) sia `link_telegram.py` (nello script di pairing) **devono** chiamare la stessa funzione centralizzata `backends.config.get_telegram_session_path()`. Qualsiasi disallineamento nel percorso del file `.session` causerebbe un errore di autenticazione al primo avvio (Telethon non troverebbe la sessione creata dallo script di pairing).


### 2.3 Script di pairing: `link_telegram.py`

A differenza di Signal (QR code) e WhatsApp (QR code), **Telegram richiede un pairing interattivo testuale**:

```
$ python3 link_telegram.py

🔑 TELEGRAM API ID: 123456
🔑 TELEGRAM API HASH: abc123...
📱 Numero di telefono (formato internazionale): +391234567890
📩 Codice di verifica inviato. Inseriscilo: 12345
🔐 Password 2FA (lascia vuoto se non attiva):
✅ Login completato! Sessione salvata in ~/.local/share/signal-tui-client/telegram.session
```

**Flusso dettagliato:**

| Step | Azione | API Telethon |
|---|---|---|
| 1 | Richiedi `api_id` e `api_hash` (se non già in env/config) | — |
| 2 | Richiedi numero di telefono (formato `+39...`) | — |
| 3 | `client.send_code_request(phone)` | Telegram invia codice via app/SMS |
| 4 | Richiedi codice di verifica all'utente | — |
| 5 | `client.sign_in(phone, code)` | Se 2FA attivo, solleva `SessionPasswordNeededError` |
| 6 | (opzionale) Richiedi password 2FA | `client.sign_in(password=password)` |
| 7 | Salva opzionalmente `api_id`/`api_hash` in `config.json` | — |

Lo script seguirà la struttura di `link_whatsapp.py`:
- `_ensure_venv()` per riavviarsi automaticamente sotto `.venv`
- `try/except KeyboardInterrupt` per uscita pulita
- Nessuna dipendenza grafica (no QR, no `qrcode`)
- `asyncio.run(main())` come entry point

### 2.4 Dipendenze Python

Aggiungere `telethon` al virtual environment:

```
pip install telethon
```

Vincolo di versione: `telethon>=1.36,<2`.

- Eseguire il `TelegramClient` in un thread dedicato con un proprio event loop
- Registrare handler Telethon (`@client.on(...)`) che normalizzano gli eventi in `ChatEvent` e li accodano a una `queue.Queue`
- `poll_once()` consuma la coda (identico contratto di Signal/WhatsApp)
- Scrittura (`send_message`, `mark_read`) usa `asyncio.run_coroutine_threadsafe()` per interagire con l'event loop in modo thread-safe


---

## 3. Mappatura dei modelli

### 3.1 `ChatContact` ← Telethon `User` / `Chat` / `Channel`

Telethon restituisce tre tipi di entità nei dialoghi (`client.iter_dialogs()`):

| Entità Telethon | Esempio | ID |
|---|---|---|
| `User` | Chat privata con @mario | ID positivo (es. `123456789`) |
| `Chat` | Gruppo "Famiglia" | ID negativo (es. `-987654321`) |
| `Channel` | Canale "Notizie" | ID negativo (es. `-1001234567890`) |

**Mappatura:**

| Campo `ChatContact` | Origine Telethon |
|---|---|
| `id` | `str(entity.id)` — preserviamo il segno negativo per distinguere gruppi/canali |
| `display_name` | **User**: `f"{user.first_name or ''} {user.last_name or ''}".strip()` o `user.username` o `str(user.id)`. **Chat/Channel**: `chat.title` |
| `protocol` | `PROTOCOL_TELEGRAM` (`"telegram"`) |
| `extras` | `{"username": user.username, "phone": user.phone, "is_group": ..., "is_channel": ..., "last_message_ts": ...}` |

**Nota su `last_message_ts`**: Telethon include già l'ultimo messaggio in ogni `Dialog` restituito da `iter_dialogs()`, con il timestamp. Possiamo popolare `last_message_ts` negli extras già durante `_load_contacts()` per abilitare l'ordinamento "più recenti in alto" fin dal primo avvio.

### 3.2 `ChatMessage` ← Telethon `Message`

| Campo `ChatMessage` | Origine Telethon | Note |
|---|---|---|
| `id` | `str(msg.id)` | ID intero, unico per chat |
| `contact_id` | `str(msg.chat_id)` | Peer ID del dialogo |
| `protocol` | `PROTOCOL_TELEGRAM` | |
| `text` | `msg.text` o `""` | Vuoto per messaggi solo media |
| `is_mine` | `msg.out` | Booleano di Telethon |
| `sender` | `msg.sender.first_name` se disponibile, altrimenti `str(msg.sender_id)` | |
| `timestamp` | `int(msg.date.timestamp() * 1000)` | Conversione `datetime` → millisecondi |
| `quote_text` | Da `msg.reply_to.reply_to_msg_id` (risolto dal cache) | Telethon espone solo l'ID del messaggio citato, non il testo |
| `msg_type` | Vedi tabella sotto | |
| `attachment_info` | `msg.file.name` o mime type | |
| `attachment_id` | `str(msg.id)` | Per download lazy |
| `status` | `"sent"` per messaggi in uscita | Le API utente Telethon non espongono delivered/read |

**Mappatura `msg_type`:**

| Condizione Telethon | `msg_type` |
|---|---|
| `msg.text` presente, nessun media | `"text"` |
| `msg.photo` presente | `"image"` |
| `msg.sticker` presente | `"sticker"` |
| `msg.document` presente | `"attachment"` |
| `msg.video` o `msg.video_note` presente | `"attachment"` |
| `msg.audio` o `msg.voice` presente | `"attachment"` |

### 3.3 `ChatEvent`

| Tipo evento | Origine Telethon | Dettagli |
|---|---|---|
| `"message"` | `events.NewMessage` | `payload` = dict con i campi di `ChatMessage` |
| `"typing"` | `events.ChatAction` + `action instanceof SendMessageTypingAction` | `payload = {"action": "STARTED"}` |
| `"contact_update"` | Emesso dopo `_load_contacts()` o su aggiornamenti di profilo | `payload` = dict con i campi di `ChatContact` |

**Assenza di `"receipt"`**: Le conferme di lettura (doppia spunta blu) non sono facilmente accessibili via API utente Telethon. Non emetteremo eventi `"receipt"` inizialmente. Questo è coerente con WhatsApp dove i receipt sono gestiti dal container WAHA.


---

## 4. Gestione eventi asincroni

### 4.1 Architettura del bridge asyncio→sync

```
┌──────────────────────────────────────────────────────────┐
│  Thread: "telegram-loop" (daemon)                         │
│  ┌────────────────────────────────────────────────────┐  │
│  │  asyncio event loop dedicato                        │  │
│  │  ┌──────────────────────────────────────────────┐  │  │
│  │  │  TelegramClient                               │  │  │
│  │  │                                              │  │  │
│  │  │  @client.on(events.NewMessage)               │  │  │
│  │  │  @client.on(events.ChatAction)               │  │  │
│  │  │         ↓                                     │  │  │
│  │  │  _event_callback(ChatEvent)                   │  │  │
│  │  │         ↓                                     │  │  │
│  │  │  self._events.put(event)   ← queue.Queue      │  │  │
│  │  └──────────────────────────────────────────────┘  │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  Scrittura (send_message, mark_read):                    │
│  asyncio.run_coroutine_threadsafe(coro, loop)            │
└──────────────────────────────────────────────────────────┘
                           ↓ (thread-safe)
┌──────────────────────────────────────────────────────────┐
│  Thread: TUI / _poll_worker                               │
│                                                          │
│  poll_once() → list[ChatEvent]                           │
│       ↓                                                  │
│  _poll_worker → self._handle_event(evt)                  │
└──────────────────────────────────────────────────────────┘

### 4.2 Implementazione del bridge

#### Costruttore e attributi

```python
class TelegramBackend(ChatBackend):
    protocol = PROTOCOL_TELEGRAM

    def __init__(self):
        self._client: TelegramClient | None = None
        self._events: queue.Queue[ChatEvent] = queue.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._running = False
        self._connected = False
        self.contacts: list[ChatContact] = []
        self.cache: dict[str, list[dict]] = {}
        self._contacts_by_id: dict[str, ChatContact] = {}
```

#### Connessione e avvio del loop asincrono

```python
def connect_sync(self) -> None:
    """Avvia il client Telethon in un thread con un event loop dedicato."""
    self._running = True
    self._loop_thread = threading.Thread(
        target=self._run_telethon_loop,
        name="telegram-loop",
        daemon=True,
    )
    self._loop_thread.start()

    # Attendi che il client sia connesso (con timeout di 30s)
    deadline = time.time() + 30
    while time.time() < deadline:
        if self._client is not None and self._client.is_connected():
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("Telegram client connection timed out")

    self._connected = True
    self._load_contacts()
    self._load_cache()

def _run_telethon_loop(self) -> None:
    """Entry point del thread: crea un event loop e lancia il client."""

```python
async def _connect_and_run(self) -> None:
    """Coroutine: connette Telethon, registra handler, e rimane in ascolto."""
    from telethon import TelegramClient, events

    self._client = TelegramClient(
        get_telegram_session_path(),
        get_telegram_api_id(),
        get_telegram_api_hash(),
    )
    await self._client.start()

    # ── Handler: nuovi messaggi ──
    @self._client.on(events.NewMessage)
    async def on_new_message(event):
        try:
            msg = event.message
            evt = self._message_to_chat_event(msg)
            if evt:
                self._events.put(evt)
        except Exception:
            logger.exception("Telegram on_new_message failed")

    # ── Handler: typing indicator ──
    @self._client.on(events.ChatAction)
    async def on_chat_action(event):
        try:
            if event.user_typing:
                evt = ChatEvent(
                    type="typing",
                    protocol=PROTOCOL_TELEGRAM,
                    contact_id=str(event.chat_id),
                    payload={"action": "STARTED"},
                )
                self._events.put(evt)
        except Exception:
            logger.exception("Telegram on_chat_action failed")

    # Rimani connesso finché _running è True
    while self._running:
        await asyncio.sleep(1)
```

#### Operazioni di scrittura thread-safe

```python
def send_message_sync(
    self, contact_id: str, text: str,
    quote_timestamp: int | None = None,
    quote_author: str | None = None,
    quote_message: str | None = None,
) -> str:
    """Invia un messaggio. Chiamato dal thread della TUI."""
    if not self._client or not self._loop:
        raise RuntimeError("Telegram client not connected")

    async def _send():
        entity = await self._client.get_entity(int(contact_id))
        msg = await self._client.send_message(entity, text)
        return str(int(msg.date.timestamp() * 1000))

    future = asyncio.run_coroutine_threadsafe(_send(), self._loop)
    return future.result(timeout=30)

def mark_read_sync(self, contact_id: str) -> None:
    """Marca i messaggi come letti."""
    if not self._client or not self._loop:
        return
    async def _mark():

#### Disconnessione

```python
def disconnect_sync(self) -> None:
    """Ferma il client e il thread dell'event loop."""
    self._running = False
    self._connected = False
    if self._client and self._loop:
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._client.disconnect(), self._loop
            )
            future.result(timeout=5)
        except Exception:
            pass
    if self._loop_thread and self._loop_thread.is_alive():
        self._loop_thread.join(timeout=5.0)
```

#### Interfaccia `ChatBackend` (wrapper async)

```python
async def connect(self) -> None:
    await asyncio.to_thread(self.connect_sync)

async def disconnect(self) -> None:
    await asyncio.to_thread(self.disconnect_sync)

async def send_message(self, contact_id, text, **kwargs) -> str:
    return await asyncio.to_thread(
        self.send_message_sync, contact_id, text, **kwargs
    )

async def mark_read(self, contact_id) -> None:
    await asyncio.to_thread(self.mark_read_sync, contact_id)

def poll_once(self) -> list[ChatEvent]:
    """Drain non-bloccante della coda eventi."""
    events: list[ChatEvent] = []
    while True:
        try:
            events.append(self._events.get_nowait())
        except queue.Empty:
            break
    return events

async def receive(self):
    """Async generator di interfaccia."""
    self._running = True
    while self._running:
        for evt in self.poll_once():
            yield evt
        await asyncio.sleep(0.2)
```

        await self._client.send_read_acknowledge(int(contact_id))
    future = asyncio.run_coroutine_threadsafe(_mark(), self._loop)
    future.result(timeout=10)

### 4.3 Confine dei thread (TASSATIVO)

La separazione tra i due thread è il vincolo architetturale più importante per evitare crash di SQLite (`sqlite3.ProgrammingError`). La regola è:

> **Gli handler Telethon NON toccano mai SQLite, `self.cache`, `self.contacts` o `ingest_message()`. Solo `poll_once()` (thread TUI) può farlo.**

```
┌─────────────────────────────────────────────────────────────┐
│  Thread secondario: "telegram-loop"                          │
│                                                              │
│  ✅ Consentito:                                              │
│     • Normalizzare eventi in ChatEvent (solo lettura)        │
│     • self._events.put(chat_event)   ← thread-safe           │
│     • Chiamate read-only a Telethon API                      │
│                                                              │
│  ❌ VIETATO:                                                 │
│     • self.cache[...] = ...        ← scrittura cache         │
│     • self.contacts = [...]        ← scrittura contatti      │
│     • ingest_message(...)          ← scrittura SQLite        │
│     • _add_message_to_cache(...)   ← scrittura SQLite        │
│     • Qualsiasi operazione SQLite                            │
└─────────────────────────────────────────────────────────────┘
                           │
                    queue.Queue
                           │
┌─────────────────────────────────────────────────────────────┐
│  Thread principale: TUI / _poll_worker                       │
│                                                              │
│  ✅ Unico deputato a:                                        │
│     • poll_once() — svuota la coda eventi                    │
│     • _handle_event() — consuma ChatEvent                    │
│     • ingest_message(msg_dict) — cache in-memory + SQLite    │
│     • _load_cache() — lettura SQLite (durante connect_sync)  │
│     • self.contacts = [...] — assegnazione contatti          │
│     • _message_already_cached() — lookup cache               │
└─────────────────────────────────────────────────────────────┘
```

**Flusso completo di un nuovo messaggio:**

```
1. Telethon riceve UpdateNewMessage
2. Handler on_new_message (thread telegram-loop):
     msg = event.message
     evt = self._message_to_chat_event(msg)   # normalizzazione (solo lettura)
     self._events.put(evt)                     # .put() thread-safe
3. _poll_worker (thread TUI):
     for evt in backend.poll_once():           # svuota la coda
         self._handle_event(evt)               # → ingest_message() → SQLite
```

### 4.4 Gestione cache e dedup

Seguendo il pattern WhatsApp, ma con la garanzia che tutte le operazioni avvengano dal thread principale:

- **`_load_cache()`**: carica il cache da SQLite. Chiamato in `connect_sync()` dal thread TUI.
- **`_load_contacts()`**: usa `client.get_dialogs()` tramite `asyncio.run_coroutine_threadsafe()`. Il risultato (`self.contacts`) è assegnato nel thread TUI dopo `.result()`.
- **`_message_already_cached(contact_id, msg_id, text, timestamp)`**: dedup. Chiamato da `_handle_event()` nel thread TUI.
- **`ingest_message(msg_dict)`**: cache in-memory + persistenza SQLite. Chiamato ESCLUSIVAMENTE da `_handle_event()` nel thread TUI.

```python
def _load_contacts(self) -> None:
    """Carica i contatti via Telethon in modo thread-safe."""
    async def _load():
        dialogs = await self._client.get_dialogs()
        contacts = []
        for dialog in dialogs:
            entity = dialog.entity
            contact = self._entity_to_contact(entity)
            if dialog.message and dialog.message.date:
                contact.last_message_ts = int(
                    dialog.message.date.timestamp() * 1000
                )
            contacts.append(contact)
        return contacts

    future = asyncio.run_coroutine_threadsafe(_load(), self._loop)
    # Assegnazione nel thread TUI (dopo .result()), MAI nel thread telegram-loop
    self.contacts = future.result(timeout=30)
    self._contacts_by_id = {c.id: c for c in self.contacts}
```

### 4.5 Thread-safety: riepilogo

| Risorsa | Accesso da telegram-loop | Accesso da thread TUI | Meccanismo |
|---|---|---|---|
| `TelegramClient` | Diretto (nativo) | `asyncio.run_coroutine_threadsafe()` | Event loop dedicato |
| `queue.Queue` (`self._events`) | `.put()` | `.get_nowait()` | Thread-safe nativo |
| `self.contacts` | ❌ Vietato | ✅ Lettura/scrittura | Assegnato solo in TUI |
| `self.cache` | ❌ Vietato | ✅ Lettura/scrittura | Assegnato solo in TUI |
| SQLite | ❌ Vietato | ✅ `ingest_message()`, `_load_cache()` | Connessione locale per thread |

---

## 5. Piano di integrazione

### 5.1 File da creare

| File | Descrizione | Righe stimate |
|---|---|---|
| `backends/telegram.py` | `TelegramBackend` completo | ~450 |
| `link_telegram.py` | Script CLI di pairing interattivo | ~180 |
| `tests/test_telegram_backend.py` | Test unitari per il backend Telegram | ~300 |

### 5.2 File da modificare

#### `models.py` — Nuova costante e emoji

```diff
 PROTOCOL_SIGNAL = "signal"
 PROTOCOL_WHATSAPP = "whatsapp"
+PROTOCOL_TELEGRAM = "telegram"

 PROTOCOL_EMOJI: dict[str, str] = {
     PROTOCOL_SIGNAL: "📱",
     PROTOCOL_WHATSAPP: "💬",
+    PROTOCOL_TELEGRAM: "📨",
 }
```

Nessuna modifica ai dataclass: `ChatContact`, `ChatMessage`, `ChatEvent` coprono già tutti i campi necessari per Telegram.

#### `backends/config.py` — Nuova sezione Telegram

Aggiungere in fondo al file, dopo la sezione WhatsApp:

```python
# ─── Telegram configuration ────────────────────────────────────────────

def get_telegram_api_id() -> int:
    """Legge TELEGRAM_API_ID da env, config.json, o .env."""
    raw = os.environ.get("TELEGRAM_API_ID", "")
    if raw:
        try:
            return int(raw)
        except ValueError:
            return 0
    cfg = _load_config()
    val = cfg.get("telegram_api_id", 0)
    try:
        return int(val) if val else 0
    except (ValueError, TypeError):
        return 0

def get_telegram_api_hash() -> str:
    """Legge TELEGRAM_API_HASH da env, config.json, o .env."""
    env = os.environ.get("TELEGRAM_API_HASH", "").strip()
    if env:
        return env
    cfg = _load_config()
    return str(cfg.get("telegram_api_hash", "")).strip()

def get_telegram_session_path() -> str:
    """Percorso del file .session di Telethon."""
    from backend import CACHE_DIR  # ~/.local/share/signal-tui-client/
    return str(Path(CACHE_DIR) / "telegram.session")

def telegram_enabled() -> bool:
    """Ritorna True se le credenziali Telegram sono configurate."""

#### `backends/__init__.py`

```diff
 from .signal import SignalBackend
 from .whatsapp import WhatsAppBackend
+from .telegram import TelegramBackend

-__all__ = ["ChatBackend", "BackendManager", "SignalBackend", "WhatsAppBackend"]
+__all__ = ["ChatBackend", "BackendManager", "SignalBackend",
+            "WhatsAppBackend", "TelegramBackend"]
```

#### `backends/manager.py`

**Nessuna modifica necessaria**. Il manager è completamente generico — opera su `ChatBackend` e usa `protocol` come chiave di routing. Telegram si integra senza toccare una riga.

#### `signal_tui.py` — Tre punti di modifica

**a) Import e dichiarazione attributo** (~riga 100):

```diff
 from models import (
     ChatContact, ChatEvent,
     PROTOCOL_SIGNAL, PROTOCOL_WHATSAPP,
+    PROTOCOL_TELEGRAM,
     contact_cache_key, protocol_emoji,
 )

 from backends import (
     BackendManager,
     SignalBackend,
     WhatsAppBackend,
+    TelegramBackend,
 )
-from backends.config import whatsapp_enabled
+from backends.config import whatsapp_enabled, telegram_enabled
```

**b) `__init__`** — registrazione condizionale (~riga 365):

```python
# Dopo il blocco WhatsApp:
self.telegram_backend: Optional[TelegramBackend] = None
if telegram_enabled():
    self.telegram_backend = TelegramBackend()
    self.manager.register(self.telegram_backend)
```

**c) `_startup`** — connessione (~riga 1000):

```python
# Dopo il blocco WhatsApp:
if self.telegram_backend is not None:
    try:
        self.telegram_backend.connect_sync()
        n = len(self.telegram_backend.contacts)
        self.call_from_thread(
            self._add_message,
            f"📨 Telegram: {n} chats loaded.",
            is_info=True,
        )
    except Exception as exc:
        self.call_from_thread(
            self._add_message,
            f"📨 Telegram unavailable: {exc}",
            is_info=True,
        )
```

**d) `on_exit` / shutdown** — disconnessione pulita:

```python
# Nel metodo di cleanup all'uscita:
if self.telegram_backend is not None:
    try:
        self.telegram_backend.disconnect_sync()
    except Exception:
        pass
```

**e) Filtro protocollo** — modifiche in 4 metodi (~righe 372, 1137, 1240, 1265):

1. `action_cycle_protocol_filter` — aggiungere `"telegram"` al ciclo:
```diff
- protocols = ["all", "signal", "whatsapp"]
+ protocols = ["all", "signal", "whatsapp", "telegram"]
```

2. `_filtered_contacts` — aggiungere `"telegram"` alla tupla (~riga 1137):
```diff
- if self._protocol_filter in ("signal", "whatsapp"):
+ if self._protocol_filter in ("signal", "whatsapp", "telegram"):
```

3. `_apply_contact_filter` — aggiungere variabile e logica per Telegram (~riga 1246-1261):
```diff
 cls_signal = "chat-filter-signal"
 cls_whats = "chat-filter-whatsapp"
+cls_telegram = "chat-filter-telegram"

 widgets = [self.chat_log]
 for selector in ("#contact-list", "#ContactsTitle", "#ChatTitle"):
     try:
         node = self.query_one(selector)
     except Exception:
         continue
-    node.remove_class(cls_signal, cls_whats)
+    node.remove_class(cls_signal, cls_whats, cls_telegram)
     if self._protocol_filter == "signal":
         node.add_class(cls_signal)
     elif self._protocol_filter == "whatsapp":
         node.add_class(cls_whats)
+    elif self._protocol_filter == "telegram":
+        node.add_class(cls_telegram)
```

**f) CSS** — 4 blocchi di stile per Telegram (~righe 160-240):

```diff
+    /* Telegram filter border — contact list e chat */
+    #contact-list.chat-filter-telegram {
+        border: solid #0088cc;
+    }
+
+    #chat-log.chat-filter-telegram {
+        border: solid #0088cc;
+    }
+
+    /* Banner titoli — sincrono col bordo */
+    #ContactsTitle.chat-filter-telegram,
+    #ChatTitle.chat-filter-telegram {
+        background: #0088cc;
+    }
+
+    /* Colore label protocollo per i contatti Telegram nella lista */
+    .protocol-telegram {
+        color: #34aadc;
+    }
```

**Colori scelti**: `#0088cc` (blu Telegram ufficiale) per bordi/banner, `#34aadc` (azzurro chiaro) per label contatto — coerente con `#39c5e0` (Signal) e `#25d366` (WhatsApp).

**g) Cosa funziona gratis** — queste funzionalità non richiedono modifiche:

| Funzionalità | Motivo |
|---|---|
| **Ctrl+S** / ContactPicker | Usa `protocol_emoji(contact.protocol)` — generico, `📨` basta |
| **Emoji nella lista contatti** | `f"{protocol_emoji(c.protocol)} {c.display_name}"` — generico |
| **Banner chat** | `f"[{protocol_emoji} {protocol.title()}] Chat with: {name}"` — generico |
| **Messaggi in chat** | Usano `msg.protocol` per accent CSS — generico, la classe `.protocol-telegram` completa il cerchio |

### 5.3 `link_telegram.py` — struttura

```
#!/usr/bin/env python3
"""
Standalone CLI per il pairing di un account Telegram con telethon.

Usage:
    python3 link_telegram.py
"""

_ensure_venv()                # riavvia sotto .venv se necessario

import sys
import asyncio
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

async def main():
    # 1. Leggi o richiedi api_id / api_hash
    api_id = get_telegram_api_id()
    api_hash = get_telegram_api_hash()
    if not (api_id and api_hash):
        api_id = int(input("🔑 TELEGRAM_API_ID: "))
        api_hash = input("🔑 TELEGRAM_API_HASH: ").strip()

    # 2. Crea client e connetti
    session_path = get_telegram_session_path()
    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()

    # 3. Richiedi numero di telefono
    phone = input("📱 Numero di telefono (+39...): ").strip()
    await client.send_code_request(phone)

    # 4. Richiedi codice
    code = input("📩 Codice di verifica: ").strip()

    # 5. Login (con 2FA opzionale)
    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        password = input("🔐 Password 2FA: ").strip()
        await client.sign_in(password=password)

    print("✅ Login completato!")
    print(f"   Sessione: {session_path}")

    # 6. (Opzionale) Salva credenziali in config.json se non già presenti
    ...

    await client.disconnect()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⏹ Operazione annullata.")
        sys.exit(0)
```

- protocols = ["all", "signal", "whatsapp"]

### 5.4 `tests/test_telegram_backend.py`

Test da implementare (seguendo il pattern di `test_whatsapp_backend.py`):

| Test | Descrizione |
|---|---|
| `test_contact_from_user` | Mappatura `User` Telethon → `ChatContact` |
| `test_contact_from_chat` | Mappatura `Chat` (gruppo) → `ChatContact` |
| `test_message_to_chat_event` | Mappatura `Message` → `ChatEvent` con tutti i campi |
| `test_message_with_media` | Mappatura messaggi con foto/sticker/documento |
| `test_message_is_mine` | `msg.out` mappato correttamente a `is_mine` |
| `test_typing_event` | `ChatAction` di tipo typing → `ChatEvent(type="typing")` |
| `test_poll_once_drains_queue` | `poll_once()` consuma correttamente la coda |
| `test_dedup_prevents_duplicate` | Dedup messaggi duplicati |
| `test_ingest_message_updates_cache` | `ingest_message()` aggiunge al cache |
| `test_disconnect_stops_thread` | `disconnect_sync()` ferma il thread e il client |

### 5.5 Dipendenze

Aggiungere `telethon` al virtual environment:

```
pip install telethon
```

Nel `requirements.txt` o file equivalente:

```
telethon>=1.36,<2
```

---

## 6. Performance con molti contatti

### 6.1 Analisi dell'impatto

Con 500 contatti Telegram aggiuntivi (oltre a Signal e WhatsApp), il totale può superare i 650 contatti. Ecco l'impatto sui vari componenti:

| Componente | Impatto | Costo stimato | Rischio |
|---|---|---|---|
| `_render_contact_list()` — full rebuild (startup / Ctrl+W) | 650 `ListItem` da creare | ~0.5-1s blocco UI | **Medio** |
| `_render_contact_list()` — reorder path (nuovo msg) | `move_child()` su widget esistenti | <10ms | **Nessuno** |
| `_sort_contacts()` | `list.sort()` su 650 elementi | <1ms | **Nessuno** |
| `_filtered_contacts()` | List comprehension O(n) | <1ms | **Nessuno** |
| `get_dialogs()` all'avvio | 500 dialoghi da SQLite Telethon | ~100-200ms blocco | **Basso** |
| Cache in memoria | 200 msg × 650 contatti = 130.000 msg | ~65 MB RAM | **Alto** |
| Telethon event loop | Thread separato, solo `.put()` | 0 nel thread TUI | **Nessuno** |

### 6.2 Soluzione 1: Cache ridotto per Telegram — ✅ IMPLEMENTATO

Il cache attuale mantiene **200 messaggi per contatto** per ogni protocollo.

Il cache attuale mantiene **200 messaggi per contatto** per ogni protocollo. Con 500 contatti Telegram, questo significa 100.000 messaggi solo per Telegram (~50 MB). Un contatto Telegram con 50 messaggi è più che sufficiente per lo scroll-back immediato.

**Implementazione in `TelegramBackend`:**

```python
# In backends/telegram.py, dopo le costanti di dedup
_MAX_CACHE_PER_CONTACT = 50  # invece del default 200 usato da Signal/WhatsApp
```

La funzione `ingest_message()` tronca automaticamente a questo limite.

### 6.3 Soluzione 2: Lazy loading contatti all'avvio — ✅ IMPLEMENTATO

Invece di caricare tutti i 500 dialoghi con `get_dialogs()` e bloccare il thread TUI, carichiamo solo i primi 50 più recenti e rimandiamo il resto in background:

```python
def _load_contacts(self) -> None:
    async def _load():
        # Primo batch: solo i 50 dialoghi più recenti per non bloccare la UI
        dialogs = await self._client.get_dialogs(limit=50)
        contacts = []
        for dialog in dialogs:
            entity = dialog.entity
            contact = self._entity_to_contact(entity)
            if dialog.message and dialog.message.date:
                contact.last_message_ts = int(
                    dialog.message.date.timestamp() * 1000
                )
            contacts.append(contact)
        return contacts

    future = asyncio.run_coroutine_threadsafe(_load(), self._loop)
    self.contacts = future.result(timeout=30)
    self._contacts_by_id = {c.id: c for c in self.contacts}
```

**Implementato**: Il render progressivo usa `_start_progressive_render()` che carica 50 contatti per frame via `set_timer`. Il merge path aggiunge nuovi contatti senza `clear()`. Vedi commit `20255e8` e `6c081ba` in `signal_tui.py`.

### 6.4 Soluzione 3: Nessuna modifica al poll_once() — ✅ CONFERMATO

`poll_once()` consuma solo gli eventi nella coda — non scala col numero di contatti. Il `_poll_worker` fa già batching: un solo re-sort/re-render per batch di eventi, non per evento. Nessuna modifica necessaria.

### 6.5 Verifica con il reorder path — ✅ CONFERMATO

Il percorso più frequente (nuovo messaggio che cambia l'ordine) è ottimizzato con `move_child()`. Il merge path (superset — 4° percorso) aggiunge nuovi ListItem senza `clear()`, preservando i widget esistenti. La visibilità toggle su Ctrl+W evita qualsiasi rebuild.

**Architettura finale di `_render_contact_list` (4 percorsi):**
- stesso ordine → fast path (update label)
- stesso set, ordine diverso → reorder path (move_child)
- superset (nuovi contatti) → merge path (append + move_child, zero clear)
- set diverso → `_start_progressive_render()` (clear + chunked 50/frame)

---

## 7. Riepilogo e punti aperti


### 7.1 Cosa funziona senza modifiche

| Componente | Note |
|---|---|
| `ChatBackend` (base.py) | Interfaccia già completa per Telegram |
| `BackendManager` (manager.py) | Completamente generico — routing via `protocol` |
| `ChatContact` / `ChatMessage` / `ChatEvent` | Campi sufficienti, nessuna estensione necessaria |
| `_poll_worker` in `signal_tui.py` | Itera già `self.manager.all()` — Telegram si integra automaticamente |
| Cache SQLite e persistenza | `backend.py` fornisce già `_add_message_to_cache`, `_load_cache`, `_prune_cache` |

### 7.2 Riepilogo modifiche

| File | Tipo | Righe |
|---|---|---|
| `models.py` | Modifica (+2 righe) | ~2 |
| `backends/config.py` | Modifica (+40 righe) | ~40 |
| `backends/__init__.py` | Modifica (+3 righe) | ~3 |
| `signal_tui.py` | Modifica (+55 righe, 7 punti) | ~55 |
| `backends/telegram.py` | Nuovo file | ~450 |
| `link_telegram.py` | Nuovo file | ~180 |
| `tests/test_telegram_backend.py` | Nuovo file | ~300 |
| **Totale** | | **~1030 righe** |

### 7.3 Punti di attenzione

| # | Tema | Dettaglio |
|---|---|---|
| 1 | **Bridge asyncio→sync** | `asyncio.run_coroutine_threadsafe()` per `send_message` blocca il thread chiamante. Con chiamate veloci (<1s) è accettabile. Per download media servirà un approccio callback-style. |
| 2 | **`disconnect()` obbligatorio** | A differenza di Signal/WhatsApp (thread daemon), Telethon deve chiudere il socket MTProto. Va chiamato esplicitamente in `on_exit()`. |
| 3 | **Quote/reply** | Telethon espone `msg.reply_to.reply_to_msg_id` ma non il testo. Per mostrare il quoting, bisogna cercare nel cache locale. |
| 4 | **Ricevute di lettura** | Non accessibili via API utente Telethon. I messaggi restano con `status="sent"`. |
| 5 | **Thread safety** | Tutta la comunicazione con il `TelegramClient` deve passare per `asyncio.run_coroutine_threadsafe()`. Non accedere mai direttamente al client da thread diversi dal `telegram-loop`. |
| 6 | **Gestione riconnessione** | Se la connessione MTProto cade, Telethon tenta automaticamente la riconnessione. Perdita di connessione durante il funzionamento normale è gestita nativamente. |
| 7 | **Cache ridotto** | Con 500+ contatti Telegram, il limite di 200 msg/contatto saturerebbe ~65 MB di RAM. Telegram usa `_MAX_CACHE_PER_CONTACT = 50` (vedi sezione 6.2). |

### 7.4 Possibile evoluzione futura

- **Eliminazione del bridge**: se Textual esponesse un event loop asyncio condiviso, `receive()` potrebbe diventare un vero async generator collegato direttamente agli handler Telethon, eliminando la `queue.Queue` intermedia.
- **Download media asincrono**: `get_attachment_path()` potrebbe scaricare file in background senza bloccare la TUI.
- **Supporto sticker inline**: I widget `ImageWidget` potrebbero renderizzare sticker WebP scaricati da Telethon.
- **Gruppi e canali**: La mappatura `ChatContact` supporta già gruppi/canali (ID negativo). La TUI potrebbe beneficiare di un'icona specifica (📢 per canali, 👥 per gruppi) da aggiungere negli `extras`.
- **Storico messaggi**: `client.get_messages()` permette di caricare lo storico di una chat. Potremmo esporlo come `resync_history()` simile a WhatsApp.
- ~~Lazy loading contatti~~ — ✅ Implementato (progressive render + merge path + visibility toggle)
