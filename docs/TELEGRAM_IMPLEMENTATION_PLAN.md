# Piano di Implementazione — Integrazione Telegram

> **Branch**: `feature/telegram-backend`
> **Basato su**: `docs/TELEGRAM_ARCHITECTURE.md` v2
> **Data**: 2026-08-11

---

## Fase 1 — Fix Race Condition Backend

**Obiettivo**: Eliminare le race condition su `self._cache` e `self.contacts` tra i
worker thread di Signal e WhatsApp.  Prerequisito per aggiungere Telegram.

**Riferimenti architettura**: [§2 — Fix race condition](ARCHITECTURE.md#2-fix-race-condition--prerequisito)

### File modificati

| File | Modifiche |
|------|-----------|
| `signal_tui.py` | `_on_backend_ready()`, semplifica `_connect_signal()`, `_connect_whatsapp()`, `_poll_wa_contacts()`, snapshot in `_poll_worker()`, rimuovi `_update_contacts_ui()` |

### Checklist

- [ ] 1.1  Aggiungere `_on_backend_ready(backend)` — merge atomico cache + contatti
- [ ] 1.2  Semplificare `_connect_signal()` — chiama `_on_backend_ready` invece di rebuild
- [ ] 1.3  Semplificare `_connect_whatsapp()` — `_resync_wa_history()` + `_on_backend_ready`
- [ ] 1.4  Semplificare `_poll_wa_contacts()` — `_resync_wa_history()` + `_on_backend_ready`
- [ ] 1.5  `_poll_worker()` — usare `backends = self.manager.all()` (snapshot)
- [ ] 1.6  Rimuovere `_update_contacts_ui()` (sostituito da `_on_backend_ready`)
- [ ] 1.7  Rimuovere `self._cache = {}` e `self.contacts = ...` dai worker thread

### Test

- [ ] 1.T1  Avvio con solo Signal → contatti visibili
- [ ] 1.T2  Avvio con Signal + WhatsApp → entrambi visibili
- [ ] 1.T3  WhatsApp arriva DOPO Signal (polling lento) → merge corretto
- [ ] 1.T4  Invio messaggio durante il merge → nessun crash
- [ ] 1.T5  Ctrl+W cicla correttamente tra i filtri protocollo
- [ ] 1.T6  Riavvio: cache messaggi preservata

### Criterio di accettazione

```bash
python -m pytest tests/ -x -q
```

---

## Fase 2 — Modelli e Configurazione Telegram

**Obiettivo**: Aggiungere costanti, emoji e funzioni di configurazione per Telegram.

**Riferimenti architettura**: [§3.2](ARCHITECTURE.md#32-funzioni-da-aggiungere-a-backendsconfigpy-40-righe), [§7.1](ARCHITECTURE.md#71-modelspy--nuova-costante-2-righe)

### File modificati

| File | Modifiche |
|------|-----------|
| `models.py` | `PROTOCOL_TELEGRAM`, emoji 📨 in `PROTOCOL_EMOJI` |
| `backends/config.py` | `get_telegram_api_id()`, `get_telegram_api_hash()`, `get_telegram_session_path()`, `telegram_enabled()` |
| `.env.example` | Aggiungere `TELEGRAM_API_ID` e `TELEGRAM_API_HASH` |

### Checklist

- [ ] 2.1  `models.py`: aggiungere `PROTOCOL_TELEGRAM = "telegram"`
- [ ] 2.2  `models.py`: aggiungere `PROTOCOL_TELEGRAM: "📨"` a `PROTOCOL_EMOJI`
- [ ] 2.3  `backends/config.py`: `get_telegram_api_id()`
- [ ] 2.4  `backends/config.py`: `get_telegram_api_hash()`
- [ ] 2.5  `backends/config.py`: `get_telegram_session_path()`
- [ ] 2.6  `backends/config.py`: `telegram_enabled()`
- [ ] 2.7  `.env.example`: documentare `TELEGRAM_API_ID` e `TELEGRAM_API_HASH`

### Test

- [ ] 2.T1  `telegram_enabled()` → `False` senza variabili
- [ ] 2.T2  `telegram_enabled()` → `True` con credenziali configurate
- [ ] 2.T3  `get_telegram_session_path()` punta a `~/.local/share/signal-tui-client/`
- [ ] 2.T4  `PROTOCOL_TELEGRAM` importabile, `protocol_emoji("telegram")` → 📨

### Criterio di accettazione

```bash
python -c "
from models import PROTOCOL_TELEGRAM, protocol_emoji
from backends.config import telegram_enabled, get_telegram_session_path
print(f'Protocol: {PROTOCOL_TELEGRAM}')
print(f'Emoji: {protocol_emoji(PROTOCOL_TELEGRAM)}')
print(f'Enabled: {telegram_enabled()}')
print(f'Session: {get_telegram_session_path()}')
"
```

---

## Fase 3 — Backend Telegram + Thread Event Loop

**Obiettivo**: Implementare `TelegramBackend` con thread dedicato e event loop asyncio.

**Riferimenti architettura**: [§5](ARCHITECTURE.md#5-backend-telegram-backendstelegrampy)

### File creati

| File | Descrizione |
|------|-------------|
| `backends/telegram.py` | `TelegramBackend` completo (~450 righe) |

### Checklist

- [ ] 3.1  Classe `TelegramBackend(ChatBackend)` con `protocol = PROTOCOL_TELEGRAM`
- [ ] 3.2  `__init__`: init code, `_event_queue`, `contacts`, `cache`, dedup sets
- [ ] 3.3  `_connect_sync()`: crea event loop, connette client, carica contatti, avvia thread
- [ ] 3.4  `_telegram_event_loop()`: thread target con `run_until_disconnected()`
- [ ] 3.5  `_on_new_message()`: handler NewMessage → ChatEvent → `_event_queue.put()`
- [ ] 3.6  `_load_contacts()`: `iter_dialogs()` → `ChatContact`
- [ ] 3.7  `poll_once()`: drain non bloccante di `_event_queue`
- [ ] 3.8  `send_message_sync()`: `run_coroutine_threadsafe` verso il telegram-loop
- [ ] 3.9  `mark_read_sync()`: `run_coroutine_threadsafe` verso il telegram-loop
- [ ] 3.10 `ingest_message()`: dedup + cache locale + SQLite
- [ ] 3.11 `_load_protocol_cache()`: carica da SQLite filtrato per `PROTOCOL_TELEGRAM`
- [ ] 3.12 `disconnect_sync()`: ferma thread, disconnette client
- [ ] 3.13 `needs_pairing` property
- [ ] 3.14 `get_pairing_qr()`: crea/riusa client, chiama `qr_login()`, restituisce URL
- [ ] 3.15 `receive()`: async generator
- [ ] 3.16 `list_contacts()`: ritorna copia di `self.contacts`

### Test

- [ ] 3.T1  `test_contact_from_user` — mapping User → ChatContact
- [ ] 3.T2  `test_contact_from_chat` — mapping Chat → ChatContact
- [ ] 3.T3  `test_message_to_chat_event` — mapping Message → ChatEvent
- [ ] 3.T4  `test_message_is_mine` — `msg.out` → `is_mine`
- [ ] 3.T5  `test_poll_once_drains_queue` — drain corretto
- [ ] 3.T6  `test_dedup_prevents_duplicate` — dedup funzionante
- [ ] 3.T7  `test_disconnect_stops_thread` — cleanup corretto
- [ ] 3.T8  `test_qr_login_url` — `get_pairing_qr()` restituisce URL
- [ ] 3.T9  `test_needs_pairing_no_session` — senza .session → True

### Criterio di accettazione

```bash
python -m pytest tests/test_telegram_backend.py -x -q
```


---

## Fase 4 — Integrazione TUI (registrazione, filtro, CSS, shutdown)

**Obiettivo**: Registrare `TelegramBackend` nel `BackendManager`, aggiungere filtro
protocollo e CSS Telegram, connettere all'avvio, cleanup allo shutdown.

**Riferimenti architettura**: [§7.3](ARCHITECTURE.md#73-signal_tuipy--modifiche-60-righe-5-punti)

### File modificati

| File | Modifiche |
|------|-----------|
| `backends/__init__.py` | Re-export `TelegramBackend` |
| `signal_tui.py` | Import, registrazione, `_connect_telegram`, filtro, CSS, shutdown |

### Checklist

- [ ] 4.1  `backends/__init__.py`: import + re-export `TelegramBackend`
- [ ] 4.2  `signal_tui.py`: import `TelegramBackend`, `telegram_enabled`
- [ ] 4.3  `signal_tui.py`: `__init__` — registra `TelegramBackend` se `telegram_enabled()`
- [ ] 4.4  `signal_tui.py`: `on_mount` — avvia `_connect_telegram` se già autenticato
- [ ] 4.5  `signal_tui.py`: `_connect_telegram()` worker
- [ ] 4.6  `signal_tui.py`: filtro protocollo — aggiungere `"telegram"` al ciclo Ctrl+W
- [ ] 4.7  `signal_tui.py`: `_filtered_contacts` — includere `"telegram"`
- [ ] 4.8  `signal_tui.py`: `_apply_contact_filter` — classe `chat-filter-telegram`
- [ ] 4.9  `signal_tui.py`: CSS `#0088cc` / `#34aadc`
- [ ] 4.10 `signal_tui.py`: `on_exit` — `disconnect_sync()` per Telegram

### Test

- [ ] 4.T1  Avvio con `telegram_enabled() == False` → Signal + WA funzionano
- [ ] 4.T2  Avvio con sessione Telegram valida → 3 backend visibili
- [ ] 4.T3  Ctrl+W cicla `all → signal → whatsapp → telegram → all`
- [ ] 4.T4  Filtro Telegram → solo contatti Telegram visibili
- [ ] 4.T5  Banner colorato di blu quando filtro Telegram attivo
- [ ] 4.T6  Uscita → `disconnect_sync()` chiamato, thread fermato

### Criterio di accettazione

```bash
python -m pytest tests/ -x -q
```

---

## Fase 5 — Device Link Screen + QR Telegram

**Obiettivo**: Abilitare il pairing Telegram via QR code nel `DeviceLinkPickerScreen`
(Ctrl+L), con auto-refresh del token ogni 30 secondi.

**Riferimenti architettura**: [§8](ARCHITECTURE.md#8-device_link_screenpy--modifiche)

### File modificati

| File | Modifiche |
|------|-----------|
| `device_link_screen.py` | Abilita Telegram in `_PROTOCOL_ITEMS`, branch QR, polling, refresh |
| `signal_tui.py` | Passare `has_telegram` a `DeviceLinkPickerScreen` |

### Checklist

- [ ] 5.1  `_PROTOCOL_ITEMS`: Telegram `disabled: False`, label `📨 Telegram`
- [ ] 5.2  `__init__`: parametro `has_telegram: bool`
- [ ] 5.3  `_populate_picker_phase`: filtra Telegram se `_has_telegram == False`
- [ ] 5.4  `_should_show_phone_input`: `False` per `"telegram"`
- [ ] 5.5  `_get_qr_data_async`: branch `"telegram"` → `_get_telegram_qr_link()`
- [ ] 5.6  `_get_telegram_qr_link()`: chiama `tb.get_pairing_qr()` in thread
- [ ] 5.7  `_poll_completion`: branch `"telegram"` → `_check_telegram_done()`
- [ ] 5.8  `_check_telegram_done()`: verifica `is_user_authorized()`, refresh QR
- [ ] 5.9  `signal_tui.py:_open_device_link`: passare `has_telegram=`

### Test

- [ ] 5.T1  Ctrl+L → Telegram visibile nel picker (se `telegram_enabled()`)
- [ ] 5.T2  Selezionando Telegram → fase QR mostrata (no fase phone)
- [ ] 5.T3  QR code ASCII renderizzato correttamente
- [ ] 5.T4  Polling rileva login completato → dismiss screen
- [ ] 5.T5  Token scaduto → QR rigenerato automaticamente
- [ ] 5.T6  `test_device_link_telegram_qr` — test unitario con mock

### Criterio di accettazione

```bash
python -m pytest tests/test_device_link_screen.py -x -q -k telegram
```

---

## Fase 6 — Script CLI `link_telegram.py`

**Obiettivo**: Script standalone per pairing Telegram da terminale (senza TUI).

**Riferimenti architettura**: [§9](ARCHITECTURE.md#9-link_telegrampy--script-cli-standalone)

### File creati

| File | Descrizione |
|------|-------------|
| `link_telegram.py` | CLI per pairing Telegram (~80 righe) |

### Checklist

- [ ] 6.1  `_ensure_venv()` — riavvia sotto `.venv` se necessario
- [ ] 6.2  Legge `api_id` / `api_hash` da config
- [ ] 6.3  Crea `TelegramClient`, connette
- [ ] 6.4  Se già autorizzato → messaggio e esce
- [ ] 6.5  Chiama `qr_login()` → genera QR via `print_qr_code()`
- [ ] 6.6  Gestisce `KeyboardInterrupt` per uscita pulita
- [ ] 6.7  Salva credenziali in `config.json` (opzionale)

### Test

- [ ] 6.T1  `python3 link_telegram.py` senza credenziali → errore chiaro
- [ ] 6.T2  `python3 link_telegram.py` con credenziali e sessione → "Already logged in"
- [ ] 6.T3  `python3 link_telegram.py` primo avvio → mostra QR code ASCII

### Criterio di accettazione

```bash
python3 link_telegram.py
```

---

## Dipendenze tra le fasi

```
Fase 1 (Fix Race Condition) ← PREREQUISITO per tutte
  └── Fase 2 (Modelli + Config) ← PREREQUISITO per 3,4,5,6
        ├── Fase 3 (Backend Telegram)
        │     └── Fase 4 (Integrazione TUI)
        │           └── Fase 5 (Device Link QR)
        │
        ├── Fase 5 (può iniziare dopo Fase 2+3, in parallelo a Fase 4)
        │
        └── Fase 6 (indipendente, dopo Fase 2)
```

**Note**:
- **Fase 1**: bloccante — tutte le altre fasi dipendono dalla stabilità dei merge.
- **Fase 2**: bloccante — servono costanti e config per Fasi 3-6.
- **Fase 3 e 5**: possono co-svilupparsi se l'interfaccia `get_pairing_qr()` è stabile.
- **Fase 6**: completamente indipendente, dopo la Fase 2.
- Ogni fase è **testabile indipendentemente**.
