# Performance Analysis — UI Reactivity

> **Data:** 2026-08-12
> **Branch:** `feature/telegram-backend`
> **Backend:** Signal, WhatsApp, Telegram

---

## 🔴 CRITICAL — Blocca la UI

### Nessun problema critico rilevato

Tutti i backend eseguono I/O in worker thread separati. La UI non viene mai bloccata
da operazioni di rete o SQLite sincrone sul main thread.

---

## 🟡 MEDIUM — Degrada gradualmente

### R1. `mark_read_sync` scrive SQLite sul thread UI (tutti i backend)
**File:** `backends/telegram.py:369`, `backends/signal.py:342`, `backends/whatsapp.py:1393`

Quando l'utente seleziona un contatto, `_select_contact` chiama `mark_read_sync`
direttamente sul thread UI. Su chat con centinaia di messaggi non letti, la
scrittura SQLite bulk può causare micro-lag visibile.

**Impatto attuale:** Basso (~5-10ms per 100 messaggi). Può peggiorare con chat
molto grandi.

**Fix proposto:** Delegare a `run_worker` o usare `call_from_thread`.

### R2. `fetch_recent_history` sequenziale (Telegram)
**File:** `backends/telegram.py:266`

All'avvio, per ogni contatto Telegram esegue `get_input_entity` + `get_messages`
in serie. Con 4-10 contatti è istantaneo, con 200+ rallenta l'avvio.

**Impatto attuale:** Basso (pochi contatti Telegram).

**Fix proposto:** Parallelizzare con `asyncio.gather()`, o limitare ai contatti
con attività recente.

### R3. `send_message_sync` bloccante (Telegram)
**File:** `backends/telegram.py:347`

`future.result(timeout=30)` blocca il worker thread di invio per tutta la durata
della chiamata MTProto. Durante questo blocco, nuovi invii sono in coda ma i
messaggi in arrivo continuano ad arrivare (poll worker indipendente).

**Impatto attuale:** Solo su rete lenta (>5s). Il timeout di 30s previene
blocchi infiniti.

**Fix proposto:** Nessuno — pattern identico a Signal (`send_subprocess`) e
WhatsApp (`urllib.request`).

---

## 🟢 MINOR — Accettabile

### R4. `_seen_timestamps` / `_seen_message_ids` crescono con la chat
**File:** `signal_tui.py:441,447`

Set in-memory che crescono per ogni messaggio mostrato. Vengono azzerati al
cambio chat → bounded per sessione. Con chat molto lunghe (>1000 msg) può
consumare RAM ma non è un problema reale.

### R5. `_shown_in_log` dedup render-level
**File:** `signal_tui.py:473`

Stesso pattern di R4. Clear al cambio chat.

### R6. `_contact_list_dirty` flush differito
**File:** `signal_tui.py:452`

Invece di re-renderizzare la lista contatti ad ogni messaggio, il poll worker
accumula e fa UN solo refresh a fine batch. Ottimizzazione efficace già in atto.

---

## ✅ RISOLTI (dalla versione precedente)

| ID | Problema | Risoluzione |
|----|----------|-------------|
| R4 | `_refresh_chat` sort ad ogni picker close | Sostituito da atomic mount window |
| R6 | `query_one("#chat-log")` in ogni `_add_message` | Cache `self._chat_log` lazy |
| — | `self._cache = {}` nei worker thread (race condition) | `_on_backend_ready` merge atomico in UI thread |
| — | `_update_contacts_ui` chiamato da 3 worker thread | Sostituito da `_on_backend_ready` |
| — | Rebuild completo contatti ad ogni backend ready | Merge incrementale, no `clear()` |

---

## 📊 Confronto backend

| Pattern | Signal | WhatsApp | Telegram |
|---|---|---|---|
| Connessione | Worker thread (subprocess/daemon) | Worker thread (HTTP) | Worker thread (MTProto) |
| Ricezione | SSE listener thread → queue | Webhook HTTP → queue | Event loop thread → queue |
| Invio | `asyncio.to_thread` o subprocess | `urllib.request` sync | `run_coroutine_threadsafe` |
| Cache | SQLite + in-memory | SQLite + in-memory | SQLite + in-memory |
| Contatti | signal-cli RPC | WAHA REST `/chats` | Telethon `get_dialogs` |
| Storico | SQLite persistente | `_resync_wa_history` REST + SQLite | `fetch_recent_history` MTProto + SQLite |
| Pairing | `signal-cli link` | WAHA QR PNG | Telethon `qr_login` |
| Ricevute | SSE envelope | WAHA webhook ack | `UpdateReadHistoryOutbox` |
