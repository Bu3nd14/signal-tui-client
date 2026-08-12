# Architettura: Integrazione Telegram nativa (Telethon) — v2

> **Stato**: Revisione completa dopo refactoring QR (`qr_utils.py` + `device_link_screen.py`) e analisi race condition backend.
> **Data**: 2026-08-11

## Indice

1. [Analisi della codebase attuale](#1-analisi-della-codebase-attuale)
2. [Fix race condition — prerequisito](#2-fix-race-condition--prerequisito)
3. [Configurazione e sessione Telegram](#3-configurazione-e-sessione-telegram)
4. [Pairing Telegram via QR code](#4-pairing-telegram-via-qr-code)
5. [Backend Telegram (`backends/telegram.py`)](#5-backend-telegram-backendstelegrampy)
6. [Mappatura dei modelli](#6-mappatura-dei-modelli)
7. [Integrazione TUI](#7-integrazione-tui)
8. [`device_link_screen.py` — modifiche](#8-device_link_screenpy--modifiche)
9. [`link_telegram.py` — script CLI standalone](#9-link_telegrampy--script-cli-standalone)
10. [Riepilogo modifiche](#10-riepilogo-modifiche)
11. [Piano di test](#11-piano-di-test)
12. [Punti di attenzione](#12-punti-di-attenzione)

---

## 1. Analisi della codebase attuale

### 1.1 Panoramica dei componenti

| File | Ruolo |
|---|---|
| `models.py` | Dataclass neutrali: `ChatContact`, `ChatMessage`, `ChatEvent` + costanti `PROTOCOL_*` |
| `backends/base.py` | Interfaccia astratta `ChatBackend(ABC)` — 6 metodi obbligatori + pairing opzionale |
| `backends/manager.py` | `BackendManager` — registry/facade, routing per `protocol` |
| `backends/signal.py` | `SignalBackend` — SSE listener in thread dedicato → `queue.Queue` → `poll_once()` |
| `backends/whatsapp.py` | `WhatsAppBackend` — Webhook HTTP → `queue.Queue` → `poll_once()` |
| `backends/config.py` | Configurazione da env / `config.json` / `.env` |
| `backends/__init__.py` | Re-export di `ChatBackend`, `BackendManager`, `SignalBackend`, `WhatsAppBackend` |
| `signal_tui.py` | App Textual: registra backend, poll worker, rendering contatti |
| `qr_utils.py` | QR rendering condiviso: `qr_to_ascii()`, `qr_png_to_ascii()`, decoder PNG |
| `device_link_screen.py` | `DeviceLinkPickerScreen` — picker → phone → QR phases per Signal/WhatsApp |
| `link_account.py` | Script CLI pairing Signal (usa `qr_utils.print_qr_code`) |
| `link_whatsapp.py` | Script CLI pairing WhatsApp (usa `qr_utils`) |
| `backend.py` | Cache SQLite, `SignalRPCClient`, funzioni legacy |

### 1.2 Interfaccia `ChatBackend` (base.py) — invariata

```python
class ChatBackend(ABC):
    protocol: str = ""

    # Lifecycle
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...

    # Data
    async def list_contacts(self) -> list[ChatContact]: ...
    async def send_message(self, contact_id, text, quote_*) -> str: ...
    async def mark_read(self, contact_id) -> None: ...
    async def receive(self) -> AsyncIterator[ChatEvent]: ...

    # Opzionali
    def get_attachment_path(self, attachment_id) -> Path | None: ...
    @property
    def needs_pairing(self) -> bool: ...
    async def get_pairing_qr(self) -> str | None: ...
```

### 1.3 `BackendManager` (manager.py) — invariato

Completamente generico, routing via `protocol`. **Nessuna modifica necessaria.**

### 1.4 Modello di threading attuale

```
┌──────────────────────────────────────────────────────────────┐
│  signal_tui.py (Textual App)                                  │
│                                                               │
│  on_mount():                                                  │
│    run_worker(_poll_worker, thread=True)    ← MAIN POLL LOOP  │
│    run_worker(_connect_signal, thread=True) ← Signal connect  │
│    run_worker(_connect_whatsapp, thread=True) ← WA connect    │
│                                                               │
│  _poll_worker (thread):                                       │
│    for backend in self.manager.all():                         │
│      events = backend.poll_once()   ← drain queue.Queue      │
│      for event in events:                                     │
│        self._handle_event(event)                              │
└──────────────────────────────────────────────────────────────┘

SignalBackend                     WhatsAppBackend
┌────────────────────┐           ┌─────────────────────┐
│ _sse_thread (daemon)│          │ webhook HTTP server   │
│   ↓                 │          │   ↓                  │
│ _event_queue (Queue)│          │ _events (Queue)      │
│   ↓                 │          │   ↓                  │
│ poll_once()         │          │ poll_once()          │
│                     │          │                      │
│ backend.contacts    │          │ backend.contacts     │
│ backend.cache       │          │ backend.cache        │
└────────────────────┘           └─────────────────────┘
```

---

## 2. Fix race condition — prerequisito

### 2.1 Problema attuale

Tre worker thread (`_connect_signal`, `_connect_whatsapp`, `_poll_wa_contacts`) scrivono **direttamente**
`self._cache` e `self.contacts` nel worker thread, non nel UI thread:

```python
# signal_tui.py righe 1047-1055 — ESEGUITO NEL WORKER THREAD
self._cache = {}                              # ❌ azzera tutto
for b in self.manager.all():                  # ❌ rilegge tutti i backend
    self._cache[...] = list(msgs)
self.contacts = self.manager.list_contacts()  # ❌ sostituisce lista intera
```

Lo stesso pattern è duplicato in `_poll_wa_contacts` (righe 1152-1157) e `_finalize_wa_connect`
(righe 1174-1179).

**Race**: due worker thread mutano `self._cache` / `self.contacts` mentre il poll worker li sta
leggendo.

### 2.2 Soluzione: Merge atomico nel UI thread

Le connessioni restano **parallele** (ogni backend al proprio servizio). Solo il merge dello stato
condiviso viene spostato nel UI thread via `call_from_thread`. Textual esegue le callback del
UI thread in ordine FIFO — single-threaded, quindi il merge è naturalmente serializzato senza lock.

```
WORKER THREAD (Signal)        WORKER THREAD (WhatsApp)       UI THREAD
───────────────────           ───────────────────────        ────────
sb._connect_sync()            wb.connect_sync()
  (carica contatti Signal)      (carica contatti WA)
  (carica cache Signal)         (carica cache WA)
    │                             │
    │  call_from_thread(          │  call_from_thread(
    │    _on_backend_ready,       │    _on_backend_ready,
    │    signal_backend)          │    whatsapp_backend)
    │                             │
    └──────────┬──────────────────┘
               │  (Textual serializza in ordine di arrivo)
               ↓
         _on_backend_ready(backend):    ← ESEGUITO NEL UI THREAD
           # Merge cache (aggiunge, non azzera)
           # Merge contatti (aggiunge nuovi)
           self._sort_contacts()
           self._render_contact_list(...)
```

### 2.3 Nuovo metodo `_on_backend_ready` (~60 righe)

```python
def _on_backend_ready(self, backend: ChatBackend) -> None:
    """UI thread: merge atomico di cache e contatti da UN backend.

    Chiamato via ``call_from_thread`` dopo che un backend ha completato
    la connessione.  Poiché Textual esegue le callback del UI thread in
    ordine FIFO, i merge di backend multipli sono naturalmente serializzati
    senza bisogno di lock espliciti.
    """
    proto = backend.protocol

    # ── Merge cache (incrementale, no clear) ──
    for cid, msgs in backend.cache.items():
        key = contact_cache_key(proto, cid)
        if key not in self._cache:
            self._cache[key] = []
        existing_ids = {m.get("id") for m in self._cache[key] if m.get("id")}
        for m in msgs:
            mid = m.get("id")
            if mid and mid in existing_ids:
                continue
            self._cache[key].append(m)
        self._cache[key].sort(key=lambda m: int(m.get("timestamp") or 0))

    # ── Merge contatti (aggiunge nuovi, aggiorna last_message_ts) ──
    existing_ids = {c.cache_key for c in self.contacts}
    for c in backend.contacts:
        if c.cache_key in existing_ids:
            for old in self.contacts:
                if old.cache_key == c.cache_key:
                    if (c.last_message_ts or 0) > (old.last_message_ts or 0):
                        old.last_message_ts = c.last_message_ts
                    break
        else:
            self.contacts.append(c)

    self._sync_last_ts()
    self._sort_contacts()
    self._render_contact_list(self._filtered_contacts())
    self._update_unread_badges()

    n = len(backend.contacts)
    logger.info("Backend %s ready: %d contacts", proto, n)
    self._status(f"✅ {proto.title()}: {n} contacts loaded")
```

### 2.4 Semplifica `_connect_signal` (~20 righe, rimuove ~10)

```python
def _connect_signal(self) -> None:
    """Worker thread: avvia Signal, poi merge nel UI thread."""
    try:
        self.call_from_thread(self._status, "⏳ Signal: starting daemon...")
        sb = self.signal_backend
        sb._connect_sync()
        self.call_from_thread(self._on_backend_ready, sb)
        self.call_from_thread(self._status, "💡 Select a contact to view chat")
    except Exception as e:
        logger.exception("Signal connect failed: %s", e)
        self.call_from_thread(self._status, f"❌ Signal: {e}")
```

### 2.5 Semplifica `_connect_whatsapp` e `_poll_wa_contacts` (~15 righe, rimuove ~30)

```python
def _connect_whatsapp(self) -> None:
    """Worker thread: avvia WhatsApp, poi merge nel UI thread."""
    if self._wa_connecting:
        return
    self._wa_connecting = True
    try:
        self.whatsapp_backend.connect_sync()
        n = len(self.whatsapp_backend.contacts)
        try:
            ensure_webhook_server(self.whatsapp_backend)
        except Exception:
            pass
        if n > 0:
            self._resync_wa_history()  # ← WhatsApp-specific: sync storico
            self.call_from_thread(self._on_backend_ready, self.whatsapp_backend)
            self._wa_connecting = False
        else:
            self.run_worker(self._poll_wa_contacts, thread=True)
    except Exception as exc:
        logger.exception("WhatsApp connect failed: %s", exc)
        self.call_from_thread(self._status, f"❌ WAHA: {exc}")
        self._wa_connecting = False
```

`_poll_wa_contacts`: rimuovere `self._cache = {}` e `self.contacts = ...`, usare `_on_backend_ready`:

```python
def _poll_wa_contacts(self) -> None:
    """Worker thread: attende sync contatti WAHA, poi merge nel UI thread."""
    # ... (logica polling esistente invariata) ...
    # Quando contatti pronti:
    self._resync_wa_history()  # ← WhatsApp-specific: sync storico
    self.call_from_thread(self._on_backend_ready, self.whatsapp_backend)
    self._wa_connecting = False
```

### 2.6 `_poll_worker` — snapshot dei backend

```python
def _poll_worker(self):
    while self._polling_active:
        try:
            backends = self.manager.all()  # snapshot, non itera il dict live
            for backend in backends:
                ...
```

### 2.7 Eliminare `_update_contacts_ui`

Il metodo `_update_contacts_ui` (righe 1521-1533) viene rimosso. La sua logica di sorting + render
+ unread badges è già dentro `_on_backend_ready`.

---

## 3. Configurazione e sessione Telegram

### 3.1 Credenziali Telegram

Telethon richiede:
- **`TELEGRAM_API_ID`** (intero) — da [my.telegram.org](https://my.telegram.org)
- **`TELEGRAM_API_HASH`** (stringa) — da [my.telegram.org](https://my.telegram.org)

Fonti di configurazione (in ordine di priorità):
1. Variabili d'ambiente: `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`
2. Campi in `config.json`: `telegram_api_id`, `telegram_api_hash`
3. File `.env` del progetto

### 3.2 Funzioni da aggiungere a `backends/config.py` (~40 righe)

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
    from backend import CACHE_DIR
    return str(Path(CACHE_DIR) / "telegram.session")

def telegram_enabled() -> bool:
    """Ritorna True se le credenziali Telegram sono configurate."""
    return bool(get_telegram_api_id() and get_telegram_api_hash())
```

### 3.3 File di sessione `.session`

Telethon persiste l'autenticazione in un file SQLite:
```
~/.local/share/signal-tui-client/telegram.session
```

> ⚠️ **Vincolo**: `backends/telegram.py` e `link_telegram.py` **devono** usare la stessa funzione
> `get_telegram_session_path()`.

### 3.4 Dipendenze Python

```
pip install telethon>=1.36,<2
```

Il pacchetto `qrcode` è già disponibile (usato da `qr_utils.py`).

---

## 4. Pairing Telegram via QR code

### 4.1 Flusso QR login

**Fonte**: [`core.telegram.org/api/qr-login`](https://core.telegram.org/api/qr-login)

```
┌──────────────────────────────────────────────────────────────────┐
│  App (Telethon)                                                  │
│                                                                   │
│  1. client.connect()                                              │
│  2. result = await client.qr_login()                              │
│     → restituisce token bytes + expires (30s)                     │
│  3. token → base64url → tg://login?token=<base64url>             │
│  4. Mostra URL come QR code  ← qr_utils.qr_to_ascii(url)         │
│  5. Utente scansiona QR con app Telegram già loggata              │
│  6. App loggata chiama auth.acceptLoginToken                     │
│  7. Telethon riceve updateLoginToken                              │
│  8. Telethon completa login automaticamente                       │
│  9. Sessione salvata in telegram.session                         │
│                                                                   │
│  Se il token scade (30s): ripeti dal passo 2 automaticamente     │
└──────────────────────────────────────────────────────────────────┘
```

### 4.2 Integrazione con `DeviceLinkPickerScreen`

Telegram **usa lo stesso pattern QR** di Signal e WhatsApp. Si integra naturalmente nelle 3 fasi
esistenti:

| Fase | Signal | WhatsApp | **Telegram** |
|------|--------|----------|-------------|
| Picker | `📶 Signal` | `💬 WhatsApp` | `📨 Telegram` |
| Phone | Solo se numero sconosciuto | Mai | **Mai** (QR non richiede telefono) |
| QR | `signal-cli link` → sgnl:// URL | WAHA REST → PNG bytes | **Telethon `qr_login()`** → `tg://login` URL |
| Polling | Subprocess exit code | WAHA session status | **Telethon login completato** |
| Refresh | N/A (URL non scade) | Nuova richiesta REST | **Auto ogni 30s** (token scade) |

---

## 5. Backend Telegram (`backends/telegram.py`)

### 5.1 Modello di threading

Telethon richiede un **event loop asyncio persistente** per ricevere update. Segue il pattern
di `SignalBackend` (thread dedicato + `queue.Queue`):

```
┌──────────────────────────────────────────────────────┐
│  TelegramBackend                                      │
│                                                       │
│  _telegram_thread (daemon)                            │
│  ┌─────────────────────────────────────────┐         │
│  │  asyncio event loop dedicato              │         │
│  │  ├─ TelegramClient connesso              │         │
│  │  ├─ @client.on(NewMessage) → handler     │         │
│  │  └─ run_until_disconnected()             │         │
│  └──────────────┬──────────────────────────┘         │
│                 │                                     │
│  _event_queue (queue.Queue)  ← eventi normalizzati   │
│                 │                                     │
│  poll_once()    ← drain non bloccante                │
│                                                       │
│  backend.contacts  ← popolato in _load_contacts()    │
│  backend.cache     ← popolato da SQLite + live       │
└──────────────────────────────────────────────────────┘

Scrittura (send_message, mark_read):
  asyncio.run_coroutine_threadsafe(coro, self._loop)
  → esegue nel telegram-loop, risultato via Future.result()
```

### 5.2 Struttura della classe (~450 righe)

```python
class TelegramBackend(ChatBackend):
    protocol = PROTOCOL_TELEGRAM

    def __init__(self):
        self._api_id = get_telegram_api_id()
        self._api_hash = get_telegram_api_hash()
        self._session_path = get_telegram_session_path()
        self._client: TelegramClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._polling_active = False

        # Bridge asyncio → sync
        self._event_queue: queue.Queue[ChatEvent] = queue.Queue()
        self._telegram_thread: threading.Thread | None = None

        # Stato
        self.contacts: list[ChatContact] = []
        self._contacts_by_id: dict[str, ChatContact] = {}
        self.cache: dict[str, list[dict]] = {}
        self._seen_msg_ids: set[str] = set()
        self._send_dedup: list[tuple[str, str, float]] = []

    # ─── Lifecycle ──────────────────────────────────────────
    def _connect_sync(self) -> None: ...
    def disconnect_sync(self) -> None: ...

    # ─── Event loop Telegram ────────────────────────────────
    def _telegram_event_loop(self) -> None: ...
    async def _on_new_message(self, event) -> None: ...

    # ─── Contatti ───────────────────────────────────────────
    async def _load_contacts(self) -> None: ...

    # ─── Invio / lettura ────────────────────────────────────
    def send_message_sync(self, contact_id, text, ...) -> str: ...
    def mark_read_sync(self, contact_id) -> None: ...

    # ─── Event queue ────────────────────────────────────────
    def poll_once(self) -> list[ChatEvent]: ...

    # ─── Cache ──────────────────────────────────────────────
    def _load_protocol_cache(self) -> dict[str, list[dict]]: ...
    def ingest_message(self, contact_id, data, ts) -> bool: ...

    # ─── Pairing ────────────────────────────────────────────
    @property
    def needs_pairing(self) -> bool: ...
    async def get_pairing_qr(self) -> str | None: ...
```

### 5.3 Dettaglio: `_connect_sync`

```python
def _connect_sync(self) -> None:
    """Avviato in worker thread dalla TUI."""
    self.cache = self._load_protocol_cache()

    self._loop = asyncio.new_event_loop()
    asyncio.set_event_loop(self._loop)

    self._client = TelegramClient(
        self._session_path, self._api_id, self._api_hash, loop=self._loop)
    self._loop.run_until_complete(self._client.connect())

    if not self._loop.run_until_complete(self._client.is_user_authorized()):
        self._polling_active = False
        return  # L'utente dovrà fare pairing via Ctrl+L

    self._loop.run_until_complete(self._load_contacts())

    from telethon import events
    @self._client.on(events.NewMessage)
    async def handler(event):
        await self._on_new_message(event)

    self._polling_active = True
    self._telegram_thread = threading.Thread(
        target=self._telegram_event_loop, name="telegram-loop", daemon=True)
    self._telegram_thread.start()
```

### 5.4 Dettaglio: `get_pairing_qr` (per DeviceLinkPickerScreen)

```python
async def get_pairing_qr(self) -> str | None:
    """Restituisce il tg://login URL per il QR code.

    Crea (o riusa) ``self._client`` e lo mantiene connesso durante il
    pairing, così Telethon può ricevere ``updateLoginToken`` quando
    l'utente scansiona il QR.
    """
    if self._client is None:
        self._client = TelegramClient(
            self._session_path, self._api_id, self._api_hash)
        await self._client.connect()

    try:
        result = await self._client.qr_login()
        if hasattr(result, 'token'):
            import base64
            token_b64 = base64.urlsafe_b64encode(result.token).decode().rstrip("=")
            return f"tg://login?token={token_b64}"
        return None
    except Exception:
        return None
```

Dopo il pairing riuscito (`is_user_authorized() == True`), il backend mantiene `self._client`
connesso.  Quando `_connect_sync()` viene chiamato, rileva che il client è già autorizzato e
procede direttamente al caricamento contatti e all'avvio dell'event loop.

### 5.5 Dettaglio: `poll_once` e `_on_new_message`

```python
def poll_once(self) -> list[ChatEvent]:
    """Pattern identico a SignalBackend e WhatsAppBackend."""
    events: list[ChatEvent] = []
    while True:
        try:
            events.append(self._event_queue.get_nowait())
        except queue.Empty:
            break
    return events

async def _on_new_message(self, event) -> None:
    """Normalizza un evento NewMessage Telethon in ChatEvent."""
    msg = event.message
    contact_id = str(msg.chat_id) if msg.chat_id else ""
    contact = self._contacts_by_id.get(contact_id)
    display_name = contact.display_name if contact else contact_id

    payload = {
        "id": str(msg.id),
        "text": msg.text or "",
        "is_mine": msg.out,
        "sender": "You" if msg.out else (getattr(msg.sender, 'first_name', None) or display_name),
        "timestamp": int(msg.date.timestamp() * 1000),
        "is_group": contact_id.startswith("-"),
        "msg_type": "text",
        "attachment_info": None,
        "attachment_id": None,
    }
    self._event_queue.put(ChatEvent(
        type="message", protocol=PROTOCOL_TELEGRAM,
        contact_id=contact_id, payload=payload))
```

---

## 6. Mappatura dei modelli

### 6.1 `ChatContact` ← Telethon `User` / `Chat` / `Channel`

| Entità Telethon | Esempio | ID |
|---|---|---|
| `User` | Chat privata @mario | Positivo (`123456789`) |
| `Chat` | Gruppo "Famiglia" | Negativo (`-987654321`) |
| `Channel` | Canale "Notizie" | Negativo (`-1001234567890`) |

| Campo `ChatContact` | Origine Telethon |
|---|---|
| `id` | `str(entity.id)` — preserva segno negativo |
| `display_name` | **User**: `f"{first_name or ''} {last_name or ''}".strip()` o `username`. **Chat/Channel**: `title` |
| `protocol` | `PROTOCOL_TELEGRAM` (`"telegram"`) |
| `extras` | `{"username": ..., "phone": ..., "is_group": ..., "is_channel": ...}` |

### 6.2 `ChatMessage` ← Telethon `Message`

| Campo | Origine Telethon |
|---|---|
| `id` | `str(msg.id)` |
| `contact_id` | `str(msg.chat_id)` |
| `protocol` | `PROTOCOL_TELEGRAM` |
| `text` | `msg.text` o `""` |
| `timestamp` | `msg.date.timestamp()` (float → int ms) |
| `is_mine` | `msg.out` |
| `sender` | `msg.sender.first_name` o `"You"` |

### 6.3 `ChatEvent` — mappatura

| Tipo evento Telethon | `ChatEvent.type` |
|---|---|
| `NewMessage` | `"message"` |
| `MessageEdited` | `"message"` (con flag `edited`) |
| `ChatAction` (typing) | `"typing"` |
| `MessageRead` | `"receipt"` |

---

## 7. Integrazione TUI

### 7.1 `models.py` — nuova costante (~2 righe)

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

### 7.2 `backends/__init__.py` (~2 righe)

```diff
 from .signal import SignalBackend
 from .whatsapp import WhatsAppBackend
+from .telegram import TelegramBackend

-__all__ = ["ChatBackend", "BackendManager", "SignalBackend", "WhatsAppBackend"]
+__all__ = ["ChatBackend", "BackendManager", "SignalBackend",
+           "WhatsAppBackend", "TelegramBackend"]
```

### 7.3 `signal_tui.py` — modifiche (~60 righe, 5 punti)

#### a) Import e `__init__` — registrazione condizionale

```diff
 from backends import (
     BackendManager,
     SignalBackend,
     WhatsAppBackend,
+    TelegramBackend,
 )
-from backends.config import whatsapp_enabled
+from backends.config import whatsapp_enabled, telegram_enabled
```

```python
# Dopo il blocco WhatsApp:
self.telegram_backend: Optional[TelegramBackend] = None
if telegram_enabled():
    self.telegram_backend = TelegramBackend()
    self.manager.register(self.telegram_backend)
```

#### b) `on_mount` — connessione parallela

```diff
     if self.whatsapp_backend and not self.whatsapp_backend.needs_pairing:
         if self.whatsapp_backend.is_working:
             self.run_worker(self._connect_whatsapp, exclusive=False, thread=True)
+    if self.telegram_backend and not self.telegram_backend.needs_pairing:
+        self.run_worker(self._connect_telegram, exclusive=False, thread=True)
```

#### c) `_connect_telegram` worker (~15 righe)

```python
def _connect_telegram(self) -> None:
    """Worker thread: connette Telegram, poi merge nel UI thread."""
    try:
        self.call_from_thread(self._status, "⏳ Telegram: connecting...")
        self.telegram_backend._connect_sync()
        self.call_from_thread(self._on_backend_ready, self.telegram_backend)
    except Exception as e:
        logger.exception("Telegram connect failed: %s", e)
        self.call_from_thread(self._status, f"❌ Telegram: {e}")
```

#### d) Filtro protocollo — aggiungere `"telegram"`

```diff
 # action_cycle_protocol_filter:
-protocols = ["all", "signal", "whatsapp"]
+protocols = ["all", "signal", "whatsapp", "telegram"]

 # _filtered_contacts:
-if self._protocol_filter in ("signal", "whatsapp"):
+if self._protocol_filter in ("signal", "whatsapp", "telegram"):

 # _apply_contact_filter:
 cls_signal = "chat-filter-signal"
 cls_whats = "chat-filter-whatsapp"
+cls_telegram = "chat-filter-telegram"

-node.remove_class(cls_signal, cls_whats)
+node.remove_class(cls_signal, cls_whats, cls_telegram)
+elif self._protocol_filter == "telegram":
+    node.add_class(cls_telegram)
```

#### e) CSS Telegram (~15 righe)

```css
/* Telegram filter border */
#contact-list.chat-filter-telegram,
#chat-log.chat-filter-telegram {
    border: solid #0088cc;
}

#ContactsTitle.chat-filter-telegram,
#ChatTitle.chat-filter-telegram {
    background: #0088cc;
}

.protocol-telegram {
    color: #34aadc;
}
```

Colori: `#0088cc` (blu Telegram) per bordi/banner, `#34aadc` (azzurro) per label contatto.

#### f) Shutdown — disconnessione pulita

```python
# In on_exit / shutdown:
if self.telegram_backend is not None:
    try:
        self.telegram_backend.disconnect_sync()
    except Exception:
        pass
```

---

## 8. `device_link_screen.py` — modifiche

### 8.1 `_PROTOCOL_ITEMS` — abilitare Telegram

```diff
 _PROTOCOL_ITEMS: list[dict[str, str]] = [
     {"id": "signal",   "label": "📶 Signal",   "disabled": False},
     {"id": "whatsapp", "label": "💬 WhatsApp", "disabled": False},
-    {"id": "telegram", "label": "📱 Telegram (coming soon)", "disabled": True},
+    {"id": "telegram", "label": "📨 Telegram",  "disabled": False},
 ]
```

### 8.2 `__init__` — parametro `has_telegram`

```diff
 def __init__(
     self,
     signal_number: str = "",
     has_whatsapp: bool = False,
+    has_telegram: bool = False,
     force_phone_input: bool = False,
 ) -> None:
     ...
+    self._has_telegram = has_telegram
```

### 8.3 `_populate_picker_phase` — filtro Telegram

```diff
 for item in _PROTOCOL_ITEMS:
     if item["id"] == "whatsapp" and not self._has_whatsapp:
         continue
+    if item["id"] == "telegram" and not self._has_telegram:
+        continue
     ...
```

### 8.4 `_should_show_phone_input` — Telegram: `False`

```python
def _should_show_phone_input(self, protocol: str) -> bool:
    if self._force_phone_input:
        return True
    if protocol == "signal":
        return not self._signal_number
    # WhatsApp e Telegram: il pairing è via QR, non serve numero
    return False
```

### 8.5 `_get_qr_data_async` — branch Telegram

```diff
     if proto == "signal":
         return await self._get_signal_link_url()
     elif proto == "whatsapp":
         ascii_qr = await self._get_whatsapp_qr()
         return f"ASCII:{ascii_qr}"
+    elif proto == "telegram":
+        return await self._get_telegram_qr_link()
     return f"fake-{proto}-link"
```

### 8.6 `_get_telegram_qr_link` (~20 righe)

```python
async def _get_telegram_qr_link(self) -> str:
    """Ottieni il tg://login URL dal TelegramBackend."""
    app = self.app
    tb = getattr(app, "telegram_backend", None)
    if tb is None:
        raise RuntimeError("Telegram backend not available")

    import asyncio as _asyncio
    def _run():
        loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(tb.get_pairing_qr())
        finally:
            loop.close()

    result = await _asyncio.to_thread(_run)
    if result is None:
        raise RuntimeError("Could not get Telegram QR link")
    return result
```

### 8.7 `_poll_completion` — branch Telegram

```diff
     if proto == "signal":
         done = await self._check_signal_done()
     elif proto == "whatsapp":
         done = await self._check_whatsapp_done()
+    elif proto == "telegram":
+        done = await self._check_telegram_done()
     else:
         return
```

### 8.8 `_check_telegram_done` (~25 righe)

```python
async def _check_telegram_done(self) -> bool:
    """Verifica se il QR login Telegram è stato completato.

    Controlla se il client è ora autorizzato.  Se il token QR è
    scaduto (30s), refresh automatico: rigenera il QR.
    """
    import asyncio as _asyncio
    app = self.app
    tb = getattr(app, "telegram_backend", None)
    if tb is None:
        return False

    def _check():
        loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(loop)
        try:
            client = tb._client
            if client is None:
                return None
            return loop.run_until_complete(client.is_user_authorized())
        except Exception:
            return None
        finally:
            loop.close()

    result = await _asyncio.to_thread(_check)
    if result is True:
        return True
    if result is False:
        # Token scaduto — rigenera QR
        try:
            qr_link = await self._get_telegram_qr_link()
            qr_ascii = qr_to_ascii(qr_link)
            code_widget = self.query_one("#link-qr-code", Static)
            code_widget.update(qr_ascii)
            code_widget.refresh()
        except Exception:
            pass
    return False
```

### 8.9 `signal_tui.py: _open_device_link` — passare `has_telegram`

```diff
 screen = DeviceLinkPickerScreen(
     signal_number=self.signal_backend.user_number,
     has_whatsapp=self.whatsapp_backend is not None,
+    has_telegram=self.telegram_backend is not None,
     force_phone_input=...,
 )
```

---

## 9. `link_telegram.py` — script CLI standalone

Opzionale. Utile per pairing da terminale senza TUI (~80 righe).

```python
#!/usr/bin/env python3
"""
Standalone CLI per il pairing Telegram via QR code.

Usage:
    python3 link_telegram.py
"""

import sys, asyncio, base64

_ensure_venv()

from backends.config import (
    get_telegram_api_id, get_telegram_api_hash, get_telegram_session_path)
from qr_utils import print_qr_code
from telethon import TelegramClient


async def main():
    api_id = get_telegram_api_id()
    api_hash = get_telegram_api_hash()

    if not (api_id and api_hash):
        print("❌ TELEGRAM_API_ID and TELEGRAM_API_HASH must be configured.")
        sys.exit(1)

    session_path = get_telegram_session_path()
    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()

    if await client.is_user_authorized():
        print("✅ Already logged in!")
        await client.disconnect()
        return

    print("📱 Scan the QR code with your Telegram app:")
    print()

    try:
        result = await client.qr_login()
        token_b64 = base64.urlsafe_b64encode(result.token).decode().rstrip("=")
        url = f"tg://login?token={token_b64}"
        print_qr_code(url)
    except Exception as e:
        print(f"❌ Login failed: {e}")
        sys.exit(1)

    print()
    print("✅ Login successful!")
    print(f"   Session saved to: {session_path}")
    await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⏹ Cancelled.")
        sys.exit(0)
```

---

## 10. Riepilogo modifiche

| File | Tipo | Righe | Descrizione |
|---|---|---|---|
| `signal_tui.py` (fix race) | Modifica | +60 / -40 | `_on_backend_ready`, semplifica connect workers, snapshot poll |
| `signal_tui.py` (Telegram) | Modifica | +50 | Registrazione, connect worker, filtro, CSS, shutdown |
| `models.py` | Modifica | +2 | `PROTOCOL_TELEGRAM` + emoji `📨` |
| `backends/config.py` | Modifica | +40 | Funzioni config Telegram |
| `backends/__init__.py` | Modifica | +2 | Re-export `TelegramBackend` |
| `backends/telegram.py` | **Nuovo** | ~450 | Backend completo con thread+eventloop dedicato |
| `device_link_screen.py` | Modifica | +80 | Abilita Telegram, branch QR, polling con refresh, `has_telegram` |
| `link_telegram.py` | **Nuovo** | ~80 | Script CLI standalone (opzionale) |
| `tests/test_telegram_backend.py` | **Nuovo** | ~300 | Test unitari |
| **Totale** | | **~1024** | |

---

## 11. Piano di test

### 11.1 `tests/test_telegram_backend.py`

| Test | Descrizione |
|---|---|
| `test_contact_from_user` | Mappatura `User` Telethon → `ChatContact` |
| `test_contact_from_chat` | Mappatura `Chat` (gruppo) → `ChatContact` |
| `test_contact_from_channel` | Mappatura `Channel` → `ChatContact` |
| `test_message_to_chat_event` | Mappatura `Message` → `ChatEvent` |
| `test_message_with_media` | Messaggi con foto/sticker/documento |
| `test_message_is_mine` | `msg.out` → `is_mine` |
| `test_typing_event` | `ChatAction` typing → `ChatEvent(type="typing")` |
| `test_poll_once_drains_queue` | `poll_once()` consuma coda correttamente |
| `test_dedup_prevents_duplicate` | Dedup messaggi duplicati |
| `test_ingest_message_updates_cache` | `ingest_message()` aggiunge al cache |
| `test_disconnect_stops_thread` | `disconnect_sync()` ferma thread e client |
| `test_qr_login_url` | `get_pairing_qr()` restituisce `tg://login` URL |
| `test_needs_pairing_no_session` | `needs_pairing` True senza file `.session` |

### 11.2 Test di integrazione

| Test | Descrizione |
|---|---|
| `test_on_backend_ready_merge` | Due backend pronti → contatti merged senza duplicati |
| `test_on_backend_ready_no_clear` | Cache del primo backend sopravvive al merge del secondo |
| `test_parallel_connect_no_race` | Connessioni parallele non corrompono lo stato |
| `test_telegram_filter_cycle` | Ctrl+W cicla attraverso `"telegram"` |
| `test_device_link_telegram_qr` | `DeviceLinkPickerScreen` mostra QR Telegram e completa login |

---

## 12. Punti di attenzione

| # | Tema | Dettaglio |
|---|---|---|
| 1 | **Event loop dedicato** | Telethon richiede un `asyncio` event loop persistente. Pattern identico al `_sse_thread` di Signal. |
| 2 | **Bridge asyncio→sync** | `send_message_sync()` usa `asyncio.run_coroutine_threadsafe()`. Con chiamate <5s è accettabile. |
| 3 | **`disconnect()` obbligatorio** | Telethon deve chiudere il socket MTProto. Chiamato in `on_exit()`. |
| 4 | **Refresh QR ogni 30s** | Il token QR Telegram scade dopo 30s. Il polling rigenera automaticamente il QR. |
| 5 | **Ricevute di lettura** | Non accessibili via API utente Telethon. I messaggi restano con `status="sent"`. |
| 6 | **Quote/reply** | Telethon espone `msg.reply_to.reply_to_msg_id` ma non il testo. Cercare nel cache locale. |
| 7 | **Cache ridotto** | Limite di **50 msg/contatto** per Telegram (vs 200) per non saturare RAM. |
| 8 | **Thread safety** | Tutta la comunicazione col `TelegramClient` passa per `run_coroutine_threadsafe()`. |
| 9 | **Riconnessione automatica** | Se MTProto cade, Telethon tenta automaticamente la riconnessione. |
| 10 | **`_PROTOCOL_ITEMS` disabled** | Durante lo sviluppo, Telegram può restare `disabled: True` finché il backend non è completo. |
| 11 | **Possibile evoluzione futura** | Eliminazione del bridge: se Textual esponesse un event loop asyncio condiviso, `receive()` diventerebbe un async generator diretto. Download media asincrono. Supporto sticker inline. Gruppi e canali. |

---

## Riepilogo differenze vs piano originale (v1)

| Aspetto | Piano v1 | Piano v2 |
|---|---|---|
| Pairing Telegram | Testuale OTP (phone+codice+2FA) | **QR code** via `client.qr_login()` |
| `link_telegram.py` | ~180 righe CLI testuale | ~80 righe (genera QR) |
| `DeviceLinkPickerScreen` | Non menzionato | Branch Telegram completo (QR + polling + refresh) |
| `_should_show_phone_input` | `True` per Telegram | **`False`** (QR non richiede telefono) |
| Connessioni backend | Implicito parallelo | **Parallele**, merge atomico in UI thread |
| `self._cache` / `self.contacts` | Rebuild completo in worker thread | **Merge incrementale** in UI thread via `_on_backend_ready` |
| `_update_contacts_ui` | Mantenuto | **Rimosso**, sostituito da `_on_backend_ready` |
| Lock espliciti | Non considerati | **Non necessari** (merge in UI thread = serializzato) |
| Totale righe | ~1030 | ~1024 |

