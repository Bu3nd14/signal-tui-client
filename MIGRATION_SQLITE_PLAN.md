# Piano di Migrazione: Cache da JSON a SQLite

## Contesto

Il flamegraph ha mostrato che `_flush_cache()` è la causa principale dei rallentamenti periodici della UI. Ogni flush fa **3 accessi disco completi** (save → prune → load) tenendo `self._cache_lock` bloccante. La soluzione è migrare il cache da file JSON a **SQLite** (libreria standard Python, nessuna dipendenza aggiuntiva).

**Decisione utente**: migrare completamente a SQLite, scrivere ogni messaggio subito (niente debounce), DB nella stessa directory (`~/.local/share/signal-tui-client/messages.db`).

**Branch**: `feature/sqlite-cache` (già creato)

---

## Stato di avanzamento

> **✅ COMPLETATO** — Tutte le fasi sono state implementate e verificate.

| Fase | Descrizione | Stato |
|------|-------------|-------|
| 1 | Backend SQLite (`backend.py`) | ✅ Fatto |
| 2 | Script di migrazione (`migrate_cache_sqlite.py`) | ✅ Fatto |
| 3 | UI (`signal_tui.py`) | ✅ Fatto |
| 4 | Test (aggiornati + nuovi) | ✅ Fatto |
| 5 | Verifica finale | ✅ Fatto |

**Risultato test**: `142 passed` (suite completa).

---


## Fase 1: Backend SQLite (`backend.py`)

### 1.1 Aggiungere import e costanti

```python
import sqlite3
import threading
DB_FILE = CACHE_DIR / "messages.db"
```

> **Raccomandazione (concorrenza)**: `_poll_worker` (thread) e `on_input_submitted` (UI) scrivono entrambi su SQLite. Per evitare errori `sqlite3.ProgrammingError` ("SQLite objects created in a thread can only be used in that same thread") e race condition, ogni funzione che apre una connessione deve:
> 1. aprire una **connessione locale** (come negli snippet sotto) e chiuderla subito dopo — mai condividere una connessione tra thread;
> 2. usare `check_same_thread=False` **solo** se si decide di riusare una connessione globale (sconsigliato);
> 3. proteggere le scritture con un **lock globale** `_DB_LOCK = threading.RLock()` per serializzare INSERT/UPDATE/DELETE concorrenti.
>
> **Decisione adottata**: connessione locale per chiamata + `_DB_LOCK` globale attorno a ogni operazione di scrittura. Le letture (`_load_cache`, `_count_unread`) possono usare lo stesso lock per semplicità. Questo sostituisce il vecchio `_cache_lock` (che proteggeva il dict in memoria) con un lock dedicato al DB.

### 1.2 Nuova funzione `_init_db()`

```python
_DB_LOCK = threading.RLock()

def _init_db():
    """Create the SQLite database and schema if it doesn't exist."""
    _ensure_cache_dir()
    with _DB_LOCK:
        conn = sqlite3.connect(DB_FILE)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contact_number TEXT NOT NULL,
                    text TEXT,
                    is_mine INTEGER NOT NULL DEFAULT 0,
                    sender TEXT,
                    timestamp INTEGER NOT NULL,
                    quote_text TEXT,
                    msg_type TEXT DEFAULT 'text',
                    attachment_info TEXT,
                    attachment_id TEXT,
                    read INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'read'
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_contact ON messages(contact_number, timestamp)")
            conn.commit()
        finally:
            conn.close()
```


### 1.3 Riscrivere `_load_cache()`

```python
def _load_cache() -> dict[str, list[dict]]:
    """Load all messages from SQLite into a dict {contact: [messages]}."""
    _init_db()
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM messages ORDER BY timestamp").fetchall()
    conn.close()
    cache: dict[str, list[dict]] = {}
    for row in rows:
        contact = row["contact_number"]
        if contact not in cache:
            cache[contact] = []
        cache[contact].append({
            "text": row["text"],
            "is_mine": bool(row["is_mine"]),
            "sender": row["sender"],
            "timestamp": row["timestamp"],
            "quote_text": row["quote_text"],
            "msg_type": row["msg_type"],
            "attachment_info": row["attachment_info"],
            "attachment_id": row["attachment_id"],
            "read": bool(row["read"]),
            "status": row["status"],
        })
    return cache
```

### 1.4 Riscrivere `_add_message_to_cache()`

```python
def _add_message_to_cache(
    contact_number: str,
    text: str,
    is_mine: bool,
    sender: str,
    timestamp: int,
    quote_text: str | None = None,
    msg_type: str = "text",
    attachment_info: str | None = None,
    attachment_id: str | None = None,
):
    """Add a message to the SQLite cache (incremental INSERT)."""
    _init_db()
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """INSERT INTO messages
           (contact_number, text, is_mine, sender, timestamp, quote_text,
            msg_type, attachment_info, attachment_id, read, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (contact_number, text, int(is_mine), sender, timestamp, quote_text,
         msg_type, attachment_info, attachment_id, int(is_mine),
         "sent" if is_mine else "read"),
    )
    conn.commit()
    conn.close()
```

### 1.5 Riscrivere `_prune_cache()`

```python
def _prune_cache():
    """Remove messages older than CACHE_RETENTION_DAYS and limit to 200 per contact."""
    _init_db()
    conn = sqlite3.connect(DB_FILE)
    now_ms = int(time.time() * 1000)
    cutoff = now_ms - CACHE_RETENTION_DAYS * 24 * 60 * 60 * 1000
    # Delete old messages
    conn.execute("DELETE FROM messages WHERE timestamp < ?", (cutoff,))
    # Limit to 200 per contact: delete messages beyond the 200 most recent
    conn.execute("""
        DELETE FROM messages WHERE id NOT IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY contact_number ORDER BY timestamp DESC
                ) AS rn FROM messages
            ) WHERE rn <= 200
        )
    """)
    conn.commit()
    conn.close()
```

### 1.6 Riscrivere `_mark_as_read()`

```python
def _mark_as_read(contact_number: str):
    """Mark all messages for a contact as read."""
    _init_db()
    conn = sqlite3.connect(DB_FILE)
    conn.execute("UPDATE messages SET read = 1 WHERE contact_number = ?", (contact_number,))
    conn.commit()
    conn.close()
```

### 1.7 Riscrivere `_count_unread()`

```python
def _count_unread() -> dict[str, int]:
    """Count unread messages per contact."""
    _init_db()
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT contact_number, COUNT(*) as cnt FROM messages WHERE is_mine = 0 AND read = 0 GROUP BY contact_number"
    ).fetchall()
    conn.close()
    return {row[0]: row[1] for row in rows}
```

> **Raccomandazione (chiarimento)**: `_count_unread()` è attualmente **non chiamato** da `signal_tui.py` — la UI usa `_update_unread_badges()` che legge da `self._cache` in memoria. La riscrittura di `_count_unread()` serve quindi **solo per coerenza API** (mantiene la firma `dict[str, int]` per eventuali chiamanti futuri) e per i test. **Non** va collegato a `_update_unread_badges`, che continua a leggere da `self._cache`. Se si vuole ridurre il codice morto, si può valutare di rimuoverlo del tutto, ma la scelta conservativa (mantenerlo riscritto) è accettabile.


### 1.8 Rimuovere funzioni obsolete

- `_save_cache(data)` — **RIMUOVERE**
- `_write_cache(data)` — **RIMUOVERE**
- `_get_cached_messages(contact_number)` — **RIMUOVERE** (non usato da signal_tui.py)

### 1.9 Mantenere invariata

- `_process_receipt(envelope, cache)` — continua a mutare il dict in memoria (usato da signal_tui.py)

---

## Fase 2: Script di migrazione

### 2.1 Nuovo file `migrate_cache_sqlite.py`

> **Raccomandazione (naming)**: nella root esiste già `migrate_cache_status.py` (uno script diverso, relativo allo stato del cache). Il nuovo file `migrate_cache_sqlite.py` ha un nome simile ma scopo diverso (migrazione JSON→SQLite). Per evitare confusione, mantenere il nome `migrate_cache_sqlite.py` è accettabile, ma va chiarito nel docstring che è **indipendente** da `migrate_cache_status.py`. In alternativa si può rinominare in `migrate_json_to_sqlite.py` per maggiore chiarezza. **Decisione adottata**: mantenere `migrate_cache_sqlite.py` con docstring esplicito.


```python
#!/usr/bin/env python3
"""Migrate messages.json cache to SQLite database."""

import json
import sqlite3
import sys
from pathlib import Path

CACHE_DIR = Path.home() / ".local" / "share" / "signal-tui-client"
CACHE_FILE = CACHE_DIR / "messages.json"
DB_FILE = CACHE_DIR / "messages.db"


def migrate():
    if not CACHE_FILE.exists():
        print("No messages.json found, nothing to migrate.")
        return

    with open(CACHE_FILE) as f:
        cache = json.load(f)

    # Create DB
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_number TEXT NOT NULL,
            text TEXT,
            is_mine INTEGER NOT NULL DEFAULT 0,
            sender TEXT,
            timestamp INTEGER NOT NULL,
            quote_text TEXT,
            msg_type TEXT DEFAULT 'text',
            attachment_info TEXT,
            attachment_id TEXT,
            read INTEGER DEFAULT 0,
            status TEXT DEFAULT 'read'
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_contact ON messages(contact_number, timestamp)")

    count = 0
    for contact, messages in cache.items():
        for msg in messages:
            conn.execute(
                """INSERT INTO messages
                   (contact_number, text, is_mine, sender, timestamp, quote_text,
                    msg_type, attachment_info, attachment_id, read, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (contact, msg.get("text"), int(msg.get("is_mine", False)),
                 msg.get("sender"), msg.get("timestamp", 0),
                 msg.get("quote_text"), msg.get("msg_type", "text"),
                 msg.get("attachment_info"), msg.get("attachment_id"),
                 int(msg.get("read", True)), msg.get("status", "read")),
            )
            count += 1

    conn.commit()
    conn.close()

    # Backup old file
    backup = CACHE_FILE.with_suffix(".json.bak")
    CACHE_FILE.rename(backup)
    print(f"Migrated {count} messages to {DB_FILE}")
    print(f"Old file backed up to {backup}")


if __name__ == "__main__":
    migrate()
```

---

## Fase 3: UI (`signal_tui.py`)

### 3.1 Aggiornare import

Rimuovere da import:
```python
_save_cache,      # RIMUOVERE
_write_cache,     # RIMUOVERE (se importato)
```

Aggiungere:
```python
_add_message_to_cache,  # SE non già importato
```

### 3.2 Rimuovere attributi in `__init__`

Rimuovere:
```python
self._pending_saves = 0
self._CACHE_FLUSH_THRESHOLD = 5
self._CACHE_FLUSH_INTERVAL = 30
self._cache_lock = threading.RLock()
self._last_flush_time = time.time()  # se presente
```

### 3.3 Rimuovere metodi

Rimuovere:
- `_flush_cache()` (righe ~692-704)
- `_maybe_flush_cache()` (righe ~706-717)

### 3.4 Aggiornare `on_exit()`

```python
def on_exit(self):
    """On exit, stop polling and do NOT kill the daemon."""
    self._polling_active = False
    # No flush needed — SQLite writes are incremental
```

### 3.5 Aggiornare `_process_envelope()` (righe ~650-667)

**PRIMA**:
```python
with self._cache_lock:
    if contact.number not in self._cache:
        self._cache[contact.number] = []
    self._cache[contact.number].append({...})
    self._maybe_flush_cache()
```

**DOPO**:
```python
# Save to SQLite (incremental INSERT)
_add_message_to_cache(
    contact.number,
    data["text"],
    data["is_mine"],
    data["sender"],
    ts,
    quote_text=data["quote_text"],
    msg_type=data["msg_type"],
    attachment_info=data["attachment_info"],
    attachment_id=data.get("attachment_id"),
)

# Update in-memory cache for UI
if contact.number not in self._cache:
    self._cache[contact.number] = []
self._cache[contact.number].append({
    "text": data["text"],
    "is_mine": data["is_mine"],
    "sender": data["sender"],
    "timestamp": ts,
    "quote_text": data["quote_text"],
    "msg_type": data["msg_type"],
    "attachment_info": data["attachment_info"],
    "attachment_id": data.get("attachment_id"),
    "read": data["is_mine"],
    "status": "sent" if data["is_mine"] else "read",
})
```

### 3.6 Aggiornare `_process_receipt_envelope()` (righe ~719-750)

**PRIMA**:
```python
with self._cache_lock:
    updated = _process_receipt(envelope, self._cache)
    if not updated:
        return False
    self._maybe_flush_cache()
```

**DOPO**:
```python
updated = _process_receipt(envelope, self._cache)
if not updated:
    return False

# Persist status changes to SQLite
for msg in updated:
    _update_message_status(msg["timestamp"], msg["status"])
```

Serve una nuova funzione in `backend.py`:

```python
def _update_message_status(timestamp: int, status: str):
    """Update the status of a message in SQLite by timestamp."""
    _init_db()
    conn = sqlite3.connect(DB_FILE)
    conn.execute("UPDATE messages SET status = ? WHERE timestamp = ?", (status, timestamp))
    conn.commit()
    conn.close()
```

### 3.7 Aggiornare `_startup()` (righe ~856-860)

**PRIMA**:
```python
self._cache = _load_cache()
_prune_cache()
self._cache = _load_cache()  # reload after prune
```

**DOPO**:
```python
_prune_cache()
self._cache = _load_cache()
```

### 3.8 Aggiornare `_open_chat()` (righe ~1053-1061)

**PRIMA**:
```python
with self._cache_lock:
    if number in self._cache:
        for msg in self._cache[number]:
            if not msg.get("read", True):
                msg["read"] = True
    self._flush_cache()
```

**DOPO**:
```python
if number in self._cache:
    for msg in self._cache[number]:
        if not msg.get("read", True):
            msg["read"] = True
_mark_as_read(number)  # UPDATE in SQLite
```

### 3.9 Aggiornare `on_input_submitted()` (righe ~1744-1759)

**PRIMA**:
```python
with self._cache_lock:
    if number not in self._cache:
        self._cache[number] = []
    self._cache[number].append({...})
    self._maybe_flush_cache()
```

**DOPO**:
```python
# Save to SQLite (incremental INSERT)
_add_message_to_cache(
    number, message, True, "You", ts,
    quote_text=quote_text,
)

# Update in-memory cache for UI
if number not in self._cache:
    self._cache[number] = []
self._cache[number].append({
    "text": message,
    "is_mine": True,
    "sender": "You",
    "timestamp": ts,
    "quote_text": quote_text,
    "msg_type": "text",
    "attachment_info": None,
    "attachment_id": None,
    "read": True,
    "status": "sent",
})
```

### 3.10 Aggiornare `_poll_worker()` (righe ~1368-1373)

**RIMUOVERE** il blocco del timer di sicurezza:
```python
# Safety timer: flush pending cache changes...
if self._pending_saves > 0 and time.time() - self._last_flush_time > self._CACHE_FLUSH_INTERVAL:
    self._flush_cache()
```

---

## Fase 4: Test

### 4.1 Aggiornare `tests/test_backend_cache.py`

I test che usano `_save_cache` e `_write_cache` devono essere riscritti per SQLite. I test che usano `_load_cache`, `_prune_cache`, `_mark_as_read`, `_process_receipt` devono essere adattati.

**Importante**: i test devono patchare `backend.DB_FILE` (non `backend.CACHE_FILE`) per usare un file temporaneo.

Esempio:
```python
from backend import (
    _load_cache,
    _prune_cache,
    _mark_as_read,
    _process_receipt,
    _add_message_to_cache,
    _count_unread,
    CACHE_RETENTION_DAYS,
)

class TestCacheSaveLoad:
    def test_add_and_load(self, tmp_path):
        db_file = tmp_path / "messages.db"
        with patch("backend.DB_FILE", db_file):
            _add_message_to_cache("+39", "Ciao!", False, "Mario", 1000)
            loaded = _load_cache()
        assert "+39" in loaded
        assert loaded["+39"][0]["text"] == "Ciao!"

    def test_load_empty_db(self, tmp_path):
        db_file = tmp_path / "messages.db"
        with patch("backend.DB_FILE", db_file):
            loaded = _load_cache()
        assert loaded == {}
```

### 4.2 Aggiornare `tests/test_cache_debounce.py`

I test sul debounce (`_maybe_flush_cache`, `_flush_cache`, `_pending_saves`) **non servono più** — vanno rimossi. I test su `_update_unread_badges` e `_process_receipt_envelope` vanno mantenuti ma adattati (rimuovere i mock di `_save_cache`, `_prune_cache`, `_load_cache`).

### 4.3 Aggiornare `tests/test_signal_tui_lock.py`

**NON va toccato** — testa il lock file per istanza singola, non il `_cache_lock`.

### 4.4 Nuovo test `tests/test_migrate_sqlite.py`

Test per lo script di migrazione:
- Migrazione da JSON a SQLite
- Backup del file JSON
- Messaggi correttamente inseriti

### 4.5 Nuovi test per SQLite

- `_add_message_to_cache` — INSERT incrementale
- `_load_cache` — lettura da DB
- `_prune_cache` — DELETE con WHERE
- `_mark_as_read` — UPDATE
- `_count_unread` — SELECT con GROUP BY
- `_update_message_status` — UPDATE status

---

## Fase 5: Verifica finale

1. Eseguire l'intera suite di test: `.venv/bin/python -m pytest tests/ -v`
2. Aggiornare `TEST_REPORT.md`
3. Commit e push del branch `feature/sqlite-cache`
4. Merge su main dopo approvazione

---

## Ordine di esecuzione consigliato

> **Raccomandazione (commit/rollback)**: ogni fase deve essere un **commit separato** sul branch `feature/sqlite-cache`, così da poter fare rollback/`git revert` di una singola fase senza toccare le altre. L'ordine è pensato per mantenere il codice sempre in uno stato eseguibile (o quasi) tra una fase e l'altra.

1. **Fase 1** — Modificare `backend.py` (SQLite) → **commit 1**
2. **Eseguire test di regressione** — verificare che i test esistenti non si rompano (alcuni falliranno perché usano `_save_cache`/`_write_cache`; questi fallimenti sono **attesi** e vanno risolti in Fase 4)
3. **Fase 2** — Creare `migrate_cache_sqlite.py` → **commit 2**
4. **Fase 3** — Modificare `signal_tui.py` → **commit 3**
5. **Fase 4** — Aggiornare e creare test → **commit 4**
6. **Eseguire l'intera suite di test** — deve passare al 100%
7. **Fase 5** — Verifica finale e commit finale → **commit 5**

> **Nota**: se una fase introduce un bug, si fa `git revert` del commit di quella fase e si corregge prima di procedere. Non accumulare più fasi in un unico commit.

---

## Note importanti

- `_process_receipt` in `backend.py` **NON va modificato** — continua a mutare il dict in memoria
- `self._cache` in `signal_tui.py` **rimane** come dict in memoria per la UI
- Le funzioni di lettura (`_load_messages_worker`, `_refresh_chat`, `_update_unread_badges`) **non cambiano** — continuano a leggere da `self._cache`
- `_cache_lock` viene **rimosso** — al suo posto si usa `_DB_LOCK` (lock dedicato al DB) per serializzare le operazioni SQLite concorrenti
- Il debounce (`_pending_saves`, `_CACHE_FLUSH_THRESHOLD`, `_CACHE_FLUSH_INTERVAL`) viene **rimosso** — ogni messaggio viene scritto subito
- **Concorrenza**: ogni funzione apre una connessione SQLite locale e la chiude subito; le scritture sono protette da `_DB_LOCK`. Mai condividere una connessione tra thread.
- **`_update_message_status`**: nuova funzione in `backend.py` che aggiorna lo `status` di un messaggio per `timestamp`. Viene chiamata da `_process_receipt_envelope()` in `signal_tui.py` per persistere i cambi di stato (sent/delivered/read) dopo che `_process_receipt` ha mutato il dict in memoria.

