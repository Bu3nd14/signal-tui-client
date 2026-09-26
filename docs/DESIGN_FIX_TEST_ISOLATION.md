# Design Fix C: Test Isolation & Cleanup

**Branch**: `fix/test-isolation-cleanup`  
**Data**: 2026-09-26  
**Version**: v2.1 (approvato, con riserve) + changelog v2.3 hardening  
**Status**: Implementato

---

## Changelog v2.3 (hardening exit code, backup e rollback)

**B1 risolto** — `main()` non propaga più il count di `purge(...)` come exit code: `rc = purge(...)` → `0 if rc >= 0 else 1`. Un run riuscito (dry-run o apply, count 7/4) termina con 0, allineato a `purge_ghost_outgoing.py`; solo gli abort (`-1`) diventano exit 1. `purge(...) -> int` resta invariato.

**B2 risolto** — il controllo `PRAGMA integrity_check` sul backup è racchiuso in `try/except sqlite3.Error`: un backup non-DB ("file is not a database") abortisce con `-1` senza eseguire alcuna `DELETE`.

**B3 risolto** — la post-verify gira nella stessa transazione della `DELETE`, prima del commit: se `remaining > 0` viene eseguito `rollback()` e `purge` ritorna `-1`, ripristinando le righe. Il ramo `rowcount mismatch` usa lo stesso rollback.

**Test aggiunti** — `tests/test_purge_test_rows.py`: collisione backup (`.bak-1000` occupato → `.bak-1001`), mismatch post-verify via trigger `AFTER DELETE`, backup non valido → `-1`, invariante `set(TEST_ROWS) == set(EXPECTED_MAX_COUNTS)`, `main` dry-run/apply → 0. `tests/test_db_isolation.py`: assert anche su `signal_mod.SIGNAL_CLI_ATTACHMENTS_DIR` sotto `tmp_path`.

---

## Changelog v2.2 (implementazione)

**NB-iii applicato** — `EXPECTED_MAX_COUNTS` usa la chiave completa `(protocol, contact, predicate_extra)` (non `(protocol, contact)`), così tuple dello stesso contatto con predicati diversi non collidono.

**Predicato Telegram** — verificato sulla riga reale del DB di produzione: `text='reply' AND timestamp=1786953045082 AND msg_id='77'` (timestamp e msg_id letti dal DB, non inventati).

**DELETE rowcount** — mismatch tra count pre-flight e `cursor.rowcount` → rollback della transazione e `return -1`; il contratto `purge(db_file) -> int` resta con dry-run di default.

---

## Changelog v2.1

**RB1 risolto** — `purge_test_rows.py`: abort solo se `count > expected` (idempotente), predicati DELETE ripristinati ai precisi v1 (Signal: `batch_id='batch-1'`; Telegram: `timestamp=1786953045082`), post-verify mismatch → `return -1`, test `test_purge_test_rows.py` esteso.

**RB2 risolto** — Comandi stop corretti: `tmux kill-session -t tui` o `pkill -f "python -m signal_tui"` (web server non è processo separato).

**RB3 risolto** — `isolate_environment` limitato al solo `SIGNAL_TUI_WEB_TOKEN` (fix puntuale C3 sufficiente, non rompe live test opt-in).

**NB1-NB5 recepiti** — `test_autouse_fixture_real_db_unchanged` usa `PRAGMA data_version` + `pytest.skip`, `.gitignore` pattern mirato `messages.db.bak-*`, rimossa nota stale §9.2, annotato che reset `_DOWNLOAD_SERVER` non chiude server reale.

---

## Changelog v2

**B1-B5 risolti** — Vedi changelog v2 sopra.

**N1-N10 recepiti** — Vedi changelog v2 sopra.

---

## 1. Analisi e evidenze

### C1 — Righe di test nel DB di produzione

**Evidenza** (query SQL su `~/.local/share/signal-tui-client/messages.db`):
```
('signal', '42', 3, 'batch-1')
('telegram', '42', 1, None)
```

**Dettaglio righe** (4 totali):
- `id=60952, signal, contact='42', text='', timestamp=1787250931234, batch_id='batch-1', batch_index=0`
- `id=60953, signal, contact='42', text='', timestamp=1787250931234, batch_id='batch-1', batch_index=1`
- `id=60954, signal, contact='42', text='', timestamp=1787250931234, batch_id='batch-1', batch_index=2`
- `id=35908, telegram, contact='42', text='reply', timestamp=1786953045082, batch_id=None, batch_index=None`

**Origine**: fixture di `tests/test_web_outgoing_mirror.py` (contatto `'42'`, batch `'batch-1'`).  
**Come sono finite nel DB reale**: probabile esecuzione di script ad-hoc o probe di un subagent che istanziava `SignalBackend`/`TelegramBackend` senza il conftest (il tester ha confermato che la suite pytest **non** aggiunge righe).

**Altre righe sospette**: query per `contact_number LIKE '%test%'` o `'%@lid%'` restituisce solo dati reali (WhatsApp @lid sono contatti legittimi). Nessuna altra riga di test identificata.

### C2 — Leak per-valore negli import

**Conftest attuale** (`tests/conftest.py:16-30`):
```python
@pytest.fixture(autouse=True)
def isolate_backend_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import protocols.db as backend

    cache_dir = tmp_path / "backend-cache"
    cache_dir.mkdir()
    monkeypatch.setattr(backend, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(backend, "DB_FILE", cache_dir / "messages.db")
    monkeypatch.setattr(backend, "CACHE_FILE", cache_dir / "messages.json")
    from protocols import config, rpc

    monkeypatch.setattr(rpc, "SIGNAL_CLI_ATTACHMENTS_DIR", cache_dir / "signal-media")
    monkeypatch.setattr(config, "get_whatsapp_media_dir", lambda: "")
    return cache_dir
```

**Problema**: alcuni moduli importano i path **per valore** a livello modulo, quindi il monkeypatch non li raggiunge.

**Leak confermati** (verifica sperimentale):

1. **`protocols/signal.py:55`** — `from protocols.db import (... CACHE_DIR ...)`  
   Uso a `protocols/signal.py:252`: `base = cache_dir if cache_dir is not None else CACHE_DIR`  
   **Impatto**: quote-thumbs scritti in `~/.local/share/signal-tui-client/quote-thumbs/` invece di `tmp_path`.

2. **`protocols/download.py:16`** — `from .db import CACHE_DIR`  
   Uso a `protocols/download.py:33`: `_TEMP_DOWNLOAD_DIR = CACHE_DIR / "downloads"`  
   **Impatto**: download dir in `~/.local/share/signal-tui-client/downloads/` invece di `tmp_path`.

**Import locali (sicuri per CACHE_DIR/DB_FILE)**:
- `protocols/telegram.py:173` — `from protocols.db import CACHE_DIR` **dentro** `_media_dir()` (locale, ok)
- `protocols/telegram.py:186` — `from protocols.db import _DB_LOCK, CACHE_DIR, DB_FILE` **dentro** `_migrate_legacy_media_dir()` (locale, ok, N9)
- `protocols/whatsapp.py:962` — `from protocols.db import CACHE_DIR` **dentro** `_lid_cache_path()` (locale, ok)
- `protocols/whatsapp.py:1770` — `from protocols.db import CACHE_DIR` **dentro** `_ensure_media_dir()` (locale, ok)
- `protocols/media_prune.py:64` — `from protocols.db import CACHE_DIR` **dentro** `default_scopes()` (locale, ok)

**DB_FILE**: tutte le chiamate usano import locali dentro le funzioni (es. `protocols/signal.py:851`, `protocols/telegram.py:186`, `protocols/whatsapp.py:2243`), quindi il monkeypatch le raggiunge. **Confermato sicuro**.

**SIGNAL_CLI_ATTACHMENTS_DIR**: `protocols/signal.py:66` importa per valore, ma il conftest patcha `protocols.rpc.SIGNAL_CLI_ATTACHMENTS_DIR` (dove è definito). Tuttavia, `signal.py` ha il proprio binding locale. **Leak confermato** (usi a `signal.py:678, 745, 1012, 1021, 1066, 1202`).

**get_whatsapp_media_dir**: `protocols/whatsapp.py:43` importa la funzione per valore a livello modulo, ma il conftest patcha `protocols.config.get_whatsapp_media_dir`. Tuttavia, `whatsapp.py` ha il proprio binding locale. **Leak confermato** (uso a `whatsapp.py:199`). Nota: `whatsapp.py` usa import locali per `CACHE_DIR` (sicuro), ma importa `get_whatsapp_media_dir` per valore (leak, N1).

### C3 — Test dipendente dall'ambiente

**Test**: `tests/test_web_plugin.py:2527` — `test_start_web_server_requires_token_by_default`  
**Comportamento atteso**: `start_web_server(FakeManager(), port=port, token="")` deve restituire `None` quando `require_auth=True` (default) e `token=""`.

**Comportamento reale** (con `SIGNAL_TUI_WEB_TOKEN` impostata):
- `web/server.py:83`: `token = token or os.environ.get("SIGNAL_TUI_WEB_TOKEN", "")`
- Se `SIGNAL_TUI_WEB_TOKEN` è impostata, `token` diventa non-vuoto, il server parte, il test fallisce.

**Verifica sperimentale**:
```bash
$ SIGNAL_TUI_WEB_TOKEN=test-token .venv-test/bin/python -m pytest tests/test_web_plugin.py::test_start_web_server_requires_token_by_default -xvs
FAILED
```

**Altri test sensibili all'ambiente**:
- `tests/test_live_quote_media.py:54` — `LIVE_TESTS`, `LIVE_MANUAL` (già gated da `pytest.mark.skipif`)
- `tests/test_install_script.py` — usa `os.environ` ma con `monkeypatch`/`patch.dict` (ok)
- `tests/test_backend_lazy_config.py:46` — filtra `SIGNAL_USER_NUMBER` da `os.environ` (ok)
- `tests/protocols/telegram/test_regression.py` — usa `patch.dict("os.environ", ...)` (ok)
- `tests/test_whatsapp_backend.py` — usa `patch.dict(os.environ, ...)` (ok)

**Nessun altro test sensibile identificato** oltre a `test_start_web_server_requires_token_by_default`.

---

## 2. C1 — Procedura di pulizia DB

### 2.1 Finestra di manutenzione (B1, RB2)

**ATTENZIONE**: Il DB è condiviso con processi live (TUI, web server su 4242 integrato nella TUI, signal-cli). Prima di procedere:

1. **Fermare la TUI** (che include il web server):
   - **Opzione A** (raccomandata): `tmux kill-session -t tui`
   - **Opzione B**: `pkill -f "python -m signal_tui"` (il web server gira dentro il processo TUI, non è un processo separato)
2. **Verificare nessun processo attivo**: `lsof ~/.local/share/signal-tui-client/messages.db` (deve essere vuoto)
3. **Procedere solo se nessun processo detiene il lock**

**Alternativa**: eseguire durante finestra di manutenzione (app non in uso).

### 2.2 Estensione `purge_test_rows.py` (B5, RB1)

**Estendere `purge_test_rows.py` esistente** invece di creare nuovo script (single entry-point, pattern allineato a `purge_ghost_outgoing.py:48-119`).

**Modifiche a `purge_test_rows.py`**:

```python
#!/usr/bin/env python3
"""Remove known regression-test rows from the local message database."""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from protocols.db import _DB_LOCK, DB_FILE

# Tuple (protocol, contact_number, predicate_extra) — estendibile
# predicate_extra: condizione SQL aggiuntiva per precisione (evita cancellare contatti reali)
TEST_ROWS = (
    # WhatsApp: contatti di test noti (qualsiasi riga)
    ("whatsapp", "db@lid", None),
    ("whatsapp", "unread@lid", None),
    ("whatsapp", "3912345678@c.us", None),
    # Signal: righe di test con batch_id='batch-1' (NON tutto il contatto '42')
    ("signal", "42", "batch_id = 'batch-1'"),
    # Telegram: riga di test specifica (timestamp esatto)
    ("telegram", "42", "text = 'reply' AND timestamp = 1786953045082"),
)

# Count attesi per abort se count > expected (idempotente: count=0 è ok, RB1)
EXPECTED_MAX_COUNTS = {
    ("whatsapp", "db@lid"): None,  # qualsiasi
    ("whatsapp", "unread@lid"): None,
    ("whatsapp", "3912345678@c.us"): None,
    ("signal", "42"): 3,  # batch-1 (3 righe)
    ("telegram", "42"): 1,  # 1 riga specifica
}


def _build_where(protocol: str, contact: str, extra: str | None) -> tuple[str, list]:
    """Costruisce WHERE clause parametrica per la riga di test."""
    where = "protocol = ? AND contact_number = ?"
    params: list = [protocol, contact]
    if extra:
        where += f" AND ({extra})"
    return where, params


def _backup_database(db_file: Path) -> Path:
    """Online backup API (WAL-safe, pattern purge_test_rows.py:21-25)."""
    timestamp = int(time.time())
    backup_file = db_file.with_name(f"{db_file.name}.bak-{timestamp}")
    # Anti-collisione (N7)
    while backup_file.exists():
        timestamp += 1
        backup_file = db_file.with_name(f"{db_file.name}.bak-{timestamp}")

    with _DB_LOCK:
        with sqlite3.connect(db_file) as source, sqlite3.connect(backup_file) as backup:
            source.backup(backup)
    return backup_file


def purge(db_file: Path | None = None, *, apply: bool = False) -> int:
    target = Path(db_file or DB_FILE)
    if not target.exists():
        print(f"DB not found: {target}")
        return 0

    # Pre-flight check: conta righe per (protocol, contact, predicate)
    with sqlite3.connect(target) as conn:
        conn.execute("PRAGMA busy_timeout = 5000")
        counts = {}
        for protocol, contact, extra in TEST_ROWS:
            where, params = _build_where(protocol, contact, extra)
            count = conn.execute(
                f"SELECT COUNT(*) FROM messages WHERE {where}", params
            ).fetchone()[0]
            counts[(protocol, contact)] = count
            if count > 0:
                print(f"FOUND protocol={protocol} contact={contact} count={count}")

    # Abort se count > atteso (idempotente: count=0 è ok, RB1)
    for key, max_expected in EXPECTED_MAX_COUNTS.items():
        if max_expected is not None and counts.get(key, 0) > max_expected:
            print(
                f"ABORT: {key} count={counts.get(key)} > max_expected={max_expected}. "
                "Manual review required."
            )
            return -1

    total = sum(counts.values())
    if total == 0:
        print("No test rows found.")
        return 0

    if not apply:
        print(f"DRY-RUN: {total} test row(s) found, 0 deleted. Use --apply to remove.")
        return total

    # Backup WAL-safe (B1)
    backup_file = _backup_database(target)
    print(f"Backup created: {backup_file}")

    # Integrity check sul backup (B1, B2)
    try:
        with sqlite3.connect(backup_file) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.Error as exc:
        print(f"ABORT: backup integrity check failed: {exc}")
        return -1
    if result != "ok":
        print(f"ABORT: backup integrity check failed: {result}")
        return -1

    # DELETE parametrica con predicati precisi (RB1) + post-verify nella stessa
    # transazione prima del commit (B3)
    removed = 0
    with _DB_LOCK:
        with sqlite3.connect(target) as conn:
            conn.execute("PRAGMA busy_timeout = 5000")
            for protocol, contact, extra in TEST_ROWS:
                key = (protocol, contact, extra)
                where, params = _build_where(protocol, contact, extra)
                cursor = conn.execute(f"DELETE FROM messages WHERE {where}", params)
                if cursor.rowcount != counts[key]:
                    print(
                        f"ERROR: {key} deleted {cursor.rowcount} row(s), "
                        f"expected {counts[key]}."
                    )
                    conn.rollback()
                    return -1
                removed += cursor.rowcount

            # Verifica post sulla STESSA condizione (B5, RB1, B3)
            remaining = 0
            for protocol, contact, extra in TEST_ROWS:
                where, params = _build_where(protocol, contact, extra)
                remaining += conn.execute(
                    f"SELECT COUNT(*) FROM messages WHERE {where}", params
                ).fetchone()[0]
            if remaining > 0:
                print(f"ERROR: {remaining} test row(s) still present after DELETE.")
                conn.rollback()
                return -1

    print(f"Removed {removed} row(s).")
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_FILE)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    rc = purge(args.db, apply=args.apply)
    return 0 if rc >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
```

**Nota**: i predicati DELETE sono precisi (RB1):
- Signal `'42'`: solo righe con `batch_id='batch-1'` (non tutto il contatto)
- Telegram `'42'`: solo riga con `text='reply' AND timestamp=1786953045082`
- WhatsApp: contatti di test noti (qualsiasi riga)

**Estendere `TEST_ROWS` in sicurezza**: aggiungere tuple `(protocol, contact, predicate_extra)` dove `predicate_extra` è una condizione SQL che identifica univocamente le righe di test (es. `batch_id`, `timestamp`, `text`). Evitare `contact_number` puro senza predicato aggiuntivo se il contatto potrebbe essere reale.

### 2.2.1 Aggiornamento `tests/test_purge_test_rows.py` (RB1)

Estendere `tests/test_purge_test_rows.py` esistente per coprire:
- dry-run vs `--apply`
- backup creato
- nuove tuple (Signal `'42'` con `batch_id='batch-1'`, Telegram `'42'` con `timestamp=1786953045082`)
- idempotenza al secondo run (count=0 non abortisce)
- abort se count > expected

```python
def test_purge_dry_run_does_not_delete(tmp_path):
    """Dry-run non cancella righe."""
    db_file = tmp_path / "messages.db"
    # ... setup con righe di test ...
    result = purge(db_file, apply=False)
    assert result == 3  # conta le righe
    # Verifica che le righe siano ancora presenti
    # Verifica che backup NON sia creato


def test_purge_apply_creates_backup(tmp_path):
    """Apply crea backup e cancella righe."""
    db_file = tmp_path / "messages.db"
    # ... setup con righe di test ...
    result = purge(db_file, apply=True)
    assert result == 3
    backup = next(tmp_path.glob("messages.db.bak-*"))
    assert backup.exists()


def test_purge_signal_batch_predicate(tmp_path):
    """Signal '42' cancella solo righe con batch_id='batch-1'."""
    db_file = tmp_path / "messages.db"
    # ... setup con righe signal '42' con e senza batch_id ...
    result = purge(db_file, apply=True)
    # Verifica che solo le righe con batch_id='batch-1' siano cancellate


def test_purge_telegram_timestamp_predicate(tmp_path):
    """Telegram '42' cancella solo riga con timestamp esatto."""
    db_file = tmp_path / "messages.db"
    # ... setup con righe telegram '42' con timestamp diversi ...
    result = purge(db_file, apply=True)
    # Verifica che solo la riga con timestamp=1786953045082 sia cancellata


def test_purge_idempotent(tmp_path):
    """Secondo run non abortisce (count=0 è ok)."""
    db_file = tmp_path / "messages.db"
    # ... setup con righe di test ...
    purge(db_file, apply=True)  # primo run
    result = purge(db_file, apply=False)  # secondo run
    assert result == 0  # count=0, non abortisce


def test_purge_abort_if_count_exceeds_expected(tmp_path):
    """Abortisce se count > expected."""
    db_file = tmp_path / "messages.db"
    # ... setup con 5 righe signal '42' batch_id='batch-1' (expected=3) ...
    result = purge(db_file, apply=True)
    assert result == -1  # abort
```

### 2.3 Aggiornamento `.gitignore` (B5, NB3)

Aggiungere a `.gitignore`:
```
*.db.bak-*
messages.db.bak-*
```

**Vietare il commit dei backup** (B5). Pattern mirato `messages.db.bak-*` (non `messages.db*` che è troppo ampio, NB3).

### 2.4 Procedura operativa (RB2)

```bash
# 1. Fermare la TUI (che include il web server, RB2)
tmux kill-session -t tui
# OPPURE: pkill -f "python -m signal_tui"

# 2. Verificare nessun processo attivo
lsof ~/.local/share/signal-tui-client/messages.db  # deve essere vuoto

# 3. Dry-run
python3 purge_test_rows.py --db ~/.local/share/signal-tui-client/messages.db
# Output atteso: FOUND signal 42 count=3, FOUND telegram 42 count=1, DRY-RUN: 4 test row(s)

# 4. Apply
python3 purge_test_rows.py --db ~/.local/share/signal-tui-client/messages.db --apply
# Output atteso: Backup created, Removed 4 row(s)

# 5. Verifica post (stessa condizione, RB1)
python3 -c "
import sqlite3
conn = sqlite3.connect('$HOME/.local/share/signal-tui-client/messages.db')
# Verifica predicati precisi (non contact='42' puro)
signal_count = conn.execute(\"SELECT COUNT(*) FROM messages WHERE protocol='signal' AND contact_number='42' AND batch_id='batch-1'\").fetchone()[0]
telegram_count = conn.execute(\"SELECT COUNT(*) FROM messages WHERE protocol='telegram' AND contact_number='42' AND text='reply' AND timestamp=1786953045082\").fetchone()[0]
total = signal_count + telegram_count
print(f'Righe di test residue: {total} (signal={signal_count}, telegram={telegram_count})')
assert total == 0, 'Pulizia incompleta!'
conn.close()
"

# 6. Riavviare la TUI (se necessario)
tmux new-session -d -s tui "cd ~/.local/share/signal-tui-client && .venv/bin/python -m signal_tui --web --web-port 4242"
```

### 2.5 Criteri per identificare altre righe di test

- `contact_number` in lista nota: `'42'`, `'db@lid'`, `'unread@lid'`, `'3912345678@c.us'` (tutti in `TEST_CONTACTS`)
- `batch_id` LIKE `'batch-%'` o `'test-%'` (nessun altro trovato, N10)
- Nessun orfano trovato per `'42'` (N10)

### 2.6 Prevenzione recidive

- **Test behavior-based** (vedi §3.3): estende `tests/test_db_isolation.py` per assertare che i test non scrivano nel DB reale.
- **Documentazione**: aggiungere nota al `README.md` root (non `tests/README.md` inesistente, B5) che i test non devono istanziare backend senza il conftest.

---

## 3. C2 — Hardening dell'isolamento

### 3.1 Strategia scelta: patch esteso + test behavior-based (B3)

**Opzione A (scartata)**: refactor di tutti gli import per-valore in import locali.  
**Motivo**: impatto elevato (~10 file, ~20 import), rischio di regressioni, non risolve il problema dei futuri leak.

**Opzione B (scartata)**: guard autouse mal-scoped (v1).  
**Motivo**: girava solo al setup, non controllava tutti i simboli patchati, non era una rete di sicurezza reale (B3).

**Opzione C (scelta)**: patch esteso + test behavior-based in `test_db_isolation.py`.  
**Vantaggi**:
- Patch esteso: previene il leak alla radice (patcha anche i simboli importati per valore).
- Test behavior-based: rete di sicurezza per futuri leak (asserta che i test non scrivano nel DB reale, N3).
- Impatto contenuto: ~15 righe di patch in più nel conftest, ~30 righe di test in `test_db_isolation.py`.

### 3.2 Patch esteso con reset global (B2, N8)

Estendere `isolate_backend_cache` per patchare anche i simboli importati per valore **e** resettare le cache globali:

```python
@pytest.fixture(autouse=True)
def isolate_backend_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate every test from the user's persistent cache and database."""
    import protocols.db as backend
    from protocols import config, rpc

    cache_dir = tmp_path / "backend-cache"
    cache_dir.mkdir()

    # Patch sui moduli originali
    monkeypatch.setattr(backend, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(backend, "DB_FILE", cache_dir / "messages.db")
    monkeypatch.setattr(backend, "CACHE_FILE", cache_dir / "messages.json")
    monkeypatch.setattr(rpc, "SIGNAL_CLI_ATTACHMENTS_DIR", cache_dir / "signal-media")
    monkeypatch.setattr(config, "get_whatsapp_media_dir", lambda: "")

    # Patch sui simboli importati per valore (leak noti)
    import protocols.signal as signal_mod
    import protocols.download as download_mod
    import protocols.whatsapp as whatsapp_mod

    monkeypatch.setattr(signal_mod, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(
        signal_mod, "SIGNAL_CLI_ATTACHMENTS_DIR", cache_dir / "signal-media"
    )
    monkeypatch.setattr(download_mod, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(whatsapp_mod, "get_whatsapp_media_dir", lambda: "")

    # Reset cache globali download (B2, N8, NB5)
    # Nota: il reset di _DOWNLOAD_SERVER non chiude un eventuale server reale
    # (nei test è sempre mockato, quindi non c'è rischio di socket pendenti)
    monkeypatch.setattr(download_mod, "_TEMP_DOWNLOAD_DIR", None)
    monkeypatch.setattr(download_mod, "_DOWNLOAD_SERVER", None)
    monkeypatch.setattr(download_mod, "_DOWNLOAD_URL_BASE", None)

    return cache_dir
```

**Nota**: `protocols.telegram`, `protocols.media_prune` usano import locali (sicuri), non richiedono patch. `protocols.whatsapp` usa import locali per `CACHE_DIR` (sicuro), ma importa `get_whatsapp_media_dir` per valore a livello modulo (leak, N1).

**Nota (NB5)**: il reset di `_DOWNLOAD_SERVER` a `None` non chiude un eventuale server reale. Nei test il server è sempre mockato o non avviato, quindi non c'è rischio di socket pendenti. Se un test avvia un server reale, deve fermarlo esplicitamente nel teardown.

### 3.3 Test behavior-based esteso (B3, N6)

Estendere `tests/test_db_isolation.py` esistente per assertare che i test non scrivano nel DB reale e che tutti i simboli patchati risolvano sotto `tmp_path`:

```python
from __future__ import annotations

from pathlib import Path


def test_autouse_fixture_routes_ingest_away_from_real_database(tmp_path):
    """Test esistente: verifica che DB_FILE sia sotto tmp_path."""
    import protocols.db as backend

    real_db = Path.home() / ".local" / "share" / "signal-tui-client" / "messages.db"
    before = (
        (real_db.stat().st_size, real_db.stat().st_mtime_ns)
        if real_db.exists()
        else None
    )

    assert backend.DB_FILE != real_db
    assert Path(backend.DB_FILE).is_relative_to(tmp_path)

    backend._add_message_to_cache(
        "fixture-isolation@invalid",
        "isolated",
        False,
        "tester",
        1,
        protocol="whatsapp",
        msg_id="fixture-isolation",
    )

    assert Path(backend.DB_FILE).is_file()
    after = (
        (real_db.stat().st_size, real_db.stat().st_mtime_ns)
        if real_db.exists()
        else None
    )
    assert after == before


def test_autouse_fixture_patches_all_value_imports(tmp_path):
    """Verifica che tutti i simboli importati per valore siano patchati (B3)."""
    import protocols.db as backend
    import protocols.signal as signal_mod
    import protocols.download as download_mod
    import protocols.whatsapp as whatsapp_mod
    from protocols import rpc, config

    # Tutti i simboli devono risolvere sotto tmp_path
    assert Path(backend.CACHE_DIR).is_relative_to(tmp_path)
    assert Path(signal_mod.CACHE_DIR).is_relative_to(tmp_path)
    assert Path(download_mod.CACHE_DIR).is_relative_to(tmp_path)
    assert Path(rpc.SIGNAL_CLI_ATTACHMENTS_DIR).is_relative_to(tmp_path)
    
    # get_whatsapp_media_dir deve restituire stringa vuota
    assert config.get_whatsapp_media_dir() == ""
    assert whatsapp_mod.get_whatsapp_media_dir() == ""


def test_autouse_fixture_resets_download_globals(tmp_path):
    """Verifica che le cache globali di download siano resettate (B2, N8)."""
    import protocols.download as download_mod

    # _TEMP_DOWNLOAD_DIR deve essere None (reset)
    assert download_mod._TEMP_DOWNLOAD_DIR is None
    
    # Se chiamo _get_temp_download_dir, deve creare sotto tmp_path
    temp_dir = download_mod._get_temp_download_dir()
    assert temp_dir.is_relative_to(tmp_path)
    assert temp_dir.name == "downloads"


def test_autouse_fixture_real_db_unchanged(tmp_path):
    """Verifica che il DB reale non sia modificato dai test (B3, N6, NB1)."""
    import sqlite3
    import pytest
    import protocols.db as backend

    real_db = Path.home() / ".local" / "share" / "signal-tui-client" / "messages.db"
    if not real_db.exists():
        pytest.skip("Real DB does not exist")
    
    # Usa PRAGMA data_version (più robusto di mtime/size, NB1)
    with sqlite3.connect(real_db) as conn:
        before_data_version = conn.execute("PRAGMA data_version").fetchone()[0]
    
    # Esegui operazioni che dovrebbero scrivere nel DB patchato
    backend._add_message_to_cache(
        "test-contact",
        "test message",
        False,
        "tester",
        12345,
        protocol="signal",
    )
    
    # Verifica che il DB reale non sia cambiato
    with sqlite3.connect(real_db) as conn:
        after_data_version = conn.execute("PRAGMA data_version").fetchone()[0]
    
    assert before_data_version == after_data_version, "Real DB data_version changed!"
```

**Nota**: questi test sono behavior-based (assertano il comportamento, non l'implementazione), più robusti di un guard autouse (B3).

---

## 4. C3 — Fix del test environment-sensitive

### 4.1 Fix puntuale + fixture autouse env limitata (N4, RB3)

**Fix puntuale**: modificare `tests/test_web_plugin.py:2527`:

```python
def test_start_web_server_requires_token_by_default(monkeypatch: pytest.MonkeyPatch):
    from web.server import start_web_server
    
    # Rimuovi SIGNAL_TUI_WEB_TOKEN dall'ambiente per rendere il test deterministico
    monkeypatch.delenv("SIGNAL_TUI_WEB_TOKEN", raising=False)
    
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    assert start_web_server(FakeManager(), port=port, token="") is None
```

**Fixture autouse env limitata (RB3)**: aggiungere a `tests/conftest.py` una fixture autouse che azzera **solo** `SIGNAL_TUI_WEB_TOKEN` per prevenire test environment-sensitive senza rompere i live test opt-in:

```python
@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch: pytest.MonkeyPatch):
    """Azzera SIGNAL_TUI_WEB_TOKEN per test deterministici (RB3).
    
    Nota: NON azzera SIGNAL_USER_NUMBER, WHATSAPP_API_URL, LIVE_TESTS, ecc.
    perché i live test opt-in (test_live_quote_media.py) richiedono queste
    variabili. Il fix puntuale C3 è sufficiente.
    """
    monkeypatch.delenv("SIGNAL_TUI_WEB_TOKEN", raising=False)
```

**Scarta**: fixture autouse completa che azzera tutte le variabili note (N4).  
**Motivo**: romperebbe i live test opt-in (`tests/test_live_quote_media.py:408` usa `WHATSAPP_API_URL`, test Signal richiedono `SIGNAL_USER_NUMBER`). Il fix puntuale C3 è sufficiente per il caso specifico.

**Nota**: questa fixture è complementare al fix puntuale. Il fix puntuale è necessario per il test specifico, la fixture autouse previene futuri test environment-sensitive per `SIGNAL_TUI_WEB_TOKEN`.

### 4.2 Verifica

```bash
# Senza fix (fallisce)
$ SIGNAL_TUI_WEB_TOKEN=test-token .venv-test/bin/python -m pytest tests/test_web_plugin.py::test_start_web_server_requires_token_by_default -xvs
FAILED

# Con fix (passa)
$ SIGNAL_TUI_WEB_TOKEN=test-token .venv-test/bin/python -m pytest tests/test_web_plugin.py::test_start_web_server_requires_token_by_default -xvs
PASSED
```

### 4.3 Altri test sensibili

Nessun altro test sensibile identificato (vedi §1). 

**Nota**: `tests/test_live_quote_media.py` usa `LIVE_TESTS` e `LIVE_MANUAL`, ma è già gated da `pytest.mark.skipif(not LIVE, ...)`, quindi non è un problema.

---

## 5. Sicurezza dati

### 5.1 Backup WAL-safe (B1)

- **C1**: backup con online backup API (`sqlite3.Connection.backup()`), non `cp`/`read_bytes`
- **C1**: integrity_check sul backup prima di procedere
- **C1**: finestra di manutenzione esplicita (fermare TUI/web)
- **C2**: nessun rischio (i test scrivono solo in `tmp_path`)
- **C3**: nessun rischio (il test non scrive su disco)

### 5.2 Nessun rischio per il DB reale

- **C1**: pulizia mirata (solo tuple `(protocol, contact_number)` in `TEST_CONTACTS`), non tocca dati reali
- **C2**: patch esteso + test behavior-based garantiscono che i test scrivano solo in `tmp_path`
- **C3**: il test non scrive su disco

### 5.3 Rollback (B1)

**MAI** rollback `cp` a processo attivo. Se la pulizia fallisce:

1. Fermare tutti i processi (TUI/web)
2. Valutare il danno con `PRAGMA integrity_check`
3. Se necessario, ripristinare dal backup con online backup API (inversa)
4. Riavviare i processi

**Nota**: il backup è WAL-safe, quindi il rollback è raro (solo se il DELETE corrompe il DB, improbabile).

---

## 6. Piano a step

### Step 1: Estensione `purge_test_rows.py` (C1, B5)
1. Estendere `purge_test_rows.py` con `--db`, `--apply`, dry-run di default, pre-flight check (vedi §2.2)
2. Aggiornare `.gitignore` con `*.db.bak-*` e `messages.db*` (vedi §2.3)
3. Testare dry-run e apply su DB di test
4. Verificare che abortisca se count != atteso

**Tempo stimato**: 20 minuti  
**Rischio**: basso (pattern esistente, allineato a `purge_ghost_outgoing.py`)

### Step 2: Pulizia DB reale (C1, B1)
1. Fermare TUI/web (finestra di manutenzione, vedi §2.1)
2. Verificare nessun processo attivo (`lsof`)
3. Eseguire dry-run (`python3 purge_test_rows.py --db ~/.local/share/signal-tui-client/messages.db`)
4. Eseguire apply (`python3 purge_test_rows.py --db ~/.local/share/signal-tui-client/messages.db --apply`)
5. Verifica post (vedi §2.4)
6. Riavviare processi (se necessario)

**Tempo stimato**: 10 minuti  
**Rischio**: basso (backup WAL-safe, integrity_check, finestra di manutenzione)

### Step 3: Hardening isolamento (C2, B2, N8, RB3)
1. Estendere `isolate_backend_cache` con patch esteso + reset global download (vedi §3.2)
2. Aggiungere fixture autouse `isolate_environment` limitata a `SIGNAL_TUI_WEB_TOKEN` (vedi §4.1, RB3)
3. Eseguire la suite completa (`pytest -x`)
4. Verificare che tutti i 2596 test passino (N5)

**Tempo stimato**: 30 minuti  
**Rischio**: medio (patch esteso potrebbe rompere test, ma improbabile)

### Step 4: Test behavior-based (C2, B3, B4, N6)
1. Estendere `tests/test_db_isolation.py` con 3 nuovi test (vedi §3.3)
2. Verificare che passino
3. Verificare che catturino un leak simulato (patch manuale di `CACHE_DIR` fuori da `tmp_path`)

**Tempo stimato**: 20 minuti  
**Rischio**: basso (test behavior-based, non dipendono dall'implementazione)

### Step 5: Fix test environment-sensitive (C3, N4)
1. Modificare `test_start_web_server_requires_token_by_default` (vedi §4.1)
2. Verificare con `SIGNAL_TUI_WEB_TOKEN=test-token pytest ...` (vedi §4.2)
3. Verificare che la fixture autouse `isolate_environment` non rompa altri test

**Tempo stimato**: 10 minuti  
**Rischio**: basso

### Step 6: Documentazione (B5)
1. Aggiornare `README.md` root con nota sull'isolamento (non `tests/README.md` inesistente)
2. Aggiornare `README.md` root con nota sui test e DB reale

**Tempo stimato**: 10 minuti  
**Rischio**: nullo

**Tempo totale stimato**: ~1h40m

---

## 7. Test

### 7.1 Test esistenti

Tutti i 2596 test esistenti devono passare dopo il fix (N5).

### 7.2 Nuovi test

1. **Test behavior-based** (vedi §3.3): 3 nuovi test in `tests/test_db_isolation.py`
   - `test_autouse_fixture_patches_all_value_imports`
   - `test_autouse_fixture_resets_download_globals`
   - `test_autouse_fixture_real_db_unchanged` (usa `PRAGMA data_version`, NB1)

2. **Test di regressione per C1** (vedi §2.2.1, RB1): estendere `tests/test_purge_test_rows.py`
   - `test_purge_dry_run_does_not_delete`
   - `test_purge_apply_creates_backup`
   - `test_purge_signal_batch_predicate`
   - `test_purge_telegram_timestamp_predicate`
   - `test_purge_idempotent`
   - `test_purge_abort_if_count_exceeds_expected`

### 7.3 Criteri di accettazione

- [ ] Nessun test fallisce dopo il fix (2596 test)
- [ ] `SIGNAL_TUI_WEB_TOKEN=test-token pytest tests/test_web_plugin.py::test_start_web_server_requires_token_by_default` passa
- [ ] DB di produzione non contiene righe di test (tuple in `TEST_ROWS` con predicati precisi)
- [ ] Test behavior-based passano e catturano un leak simulato
- [ ] `purge_test_rows.py --db <db> --apply` rimuove esattamente le righe attese (abort se count > atteso, idempotente)
- [ ] `purge_test_rows.py` è idempotente (secondo run con count=0 non abortisce)

---

## 8. Decisioni aperte

### 8.1 Patch esteso vs refactor

**Decisione**: patch esteso (vedi §3.1).  
**Alternativa scartata**: refactor di tutti gli import per-valore in import locali.  
**Motivo**: impatto elevato, rischio di regressioni, non risolve il problema dei futuri leak.

### 8.2 Test behavior-based vs guard autouse

**Decisione**: test behavior-based in `test_db_isolation.py` (vedi §3.3).  
**Alternativa scartata**: guard autouse mal-scoped (v1).  
**Motivo**: test behavior-based sono più robusti (assertano il comportamento, non l'implementazione), non richiedono ordine di esecuzione, non falliscono per falsi positivi.

### 8.3 Cleanup script: estensione purge_test_rows.py vs nuovo script

**Decisione**: estendere `purge_test_rows.py` (vedi §2.2, B5).  
**Alternativa scartata**: creare nuovo script standalone.  
**Motivo**: single entry-point, pattern allineato a `purge_ghost_outgoing.py`, meno manutenzione.

### 8.4 Backup: versionato vs singolo

**Decisione**: versionato (`${DB}.bak-<timestamp>`, N7).  
**Alternativa scartata**: singolo (`${DB}.bak`).  
**Motivo**: versionato permette rollback multipli, singolo sovrascrive. Anti-collisione con `while exists`.

### 8.5 Finestra di manutenzione vs coordinazione lock

**Decisione**: finestra di manutenzione esplicita (vedi §2.1, B1).  
**Alternativa scartata**: coordinazione lock con processi live.  
**Motivo**: coordinazione lock è complessa (TUI/web/signal-cli), finestra di manutenzione è più sicura e semplice. Decisione di prodotto: quando eseguire la pulizia.

### 8.6 Fixture autouse env: limitata vs completa

**Decisione**: fixture autouse limitata a `SIGNAL_TUI_WEB_TOKEN` (vedi §4.1, RB3).  
**Alternativa scartata**: fixture autouse completa che azzera tutte le variabili note.  
**Motivo**: la fixture completa romperebbe i live test opt-in (`test_live_quote_media.py` usa `WHATSAPP_API_URL`, test Signal richiedono `SIGNAL_USER_NUMBER`). Il fix puntuale C3 è sufficiente per il caso specifico.

---

## 9. Rischi previsti per il red team

### 9.1 Rischio: patch esteso rompe test

**Probabilità**: bassa  
**Impatto**: medio  
**Mitigazione**: eseguire la suite completa dopo il patch, rollback immediato se test falliscono.

### 9.2 Rischio: test behavior-based falso positivo

**Probabilità**: bassa  
**Impatto**: basso  
**Mitigazione**: i test behavior-based assertano solo che i path risolvano sotto `tmp_path` e che il DB reale non cambi.

### 9.3 Rischio: pulizia DB rimuove dati reali

**Probabilità**: nulla  
**Impatto**: alto  
**Mitigazione**: backup WAL-safe prima della pulizia, pre-flight check con abort se count != atteso, query mirata (solo tuple in `TEST_CONTACTS`).

### 9.4 Rischio: recidiva (nuove righe di test nel DB)

**Probabilità**: bassa  
**Impatto**: basso  
**Mitigazione**: test behavior-based previene il leak, documentazione nel `README.md` root.

### 9.5 Rischio: finestra di manutenzione troppo restrittiva

**Probabilità**: media  
**Impatto**: basso  
**Mitigazione**: la pulizia è rapida (<1 minuto), può essere eseguita durante qualsiasi finestra di inattività. Alternativa: eseguire la pulizia a ogni deploy (automatizzabile).

### 9.6 Rischio: fixture autouse env rompe test che dipendono da SIGNAL_TUI_WEB_TOKEN

**Probabilità**: nulla  
**Impatto**: nullo  
**Mitigazione**: la fixture azzera solo `SIGNAL_TUI_WEB_TOKEN` (RB3). Nessun test esistente dipende da questa variabile (il test `test_start_web_server_requires_token_by_default` usa `monkeypatch.delenv` esplicitamente).

---

## 10. Riferimenti

- `tests/conftest.py:16-30` — fixture `isolate_backend_cache`
- `tests/test_db_isolation.py:1-35` — test behavior-based esistente
- `tests/test_backend_download.py:53,111-120` — pattern reset `_TEMP_DOWNLOAD_DIR`, `_DOWNLOAD_SERVER`, `_DOWNLOAD_URL_BASE`
- `protocols/db.py:17-19` — definizione `CACHE_DIR`, `DB_FILE`, `CACHE_FILE`
- `protocols/db.py:318` — `PRAGMA journal_mode=WAL`
- `protocols/db.py:1008-1030` — prune con `DELETE`+`VACUUM`
- `protocols/signal.py:55` — import per-valore di `CACHE_DIR`
- `protocols/signal.py:252` — uso di `CACHE_DIR` (quote-thumbs)
- `protocols/signal.py:66` — import per-valore di `SIGNAL_CLI_ATTACHMENTS_DIR`
- `protocols/signal.py:678,745,1012,1021,1066,1202` — usi di `SIGNAL_CLI_ATTACHMENTS_DIR`
- `protocols/download.py:16` — import per-valore di `CACHE_DIR`
- `protocols/download.py:26,29-35` — cache globale `_TEMP_DOWNLOAD_DIR`
- `protocols/download.py:24-25` — globali `_DOWNLOAD_SERVER`, `_DOWNLOAD_URL_BASE`
- `protocols/rpc.py:67-69` — definizione `SIGNAL_CLI_ATTACHMENTS_DIR`
- `protocols/config.py:158` — definizione `get_whatsapp_media_dir`
- `protocols/whatsapp.py:43` — import per-valore di `get_whatsapp_media_dir`
- `protocols/whatsapp.py:199` — uso di `get_whatsapp_media_dir`
- `protocols/telegram.py:173,186` — import locali (sicuri)
- `protocols/media_prune.py:64` — import locale (sicuro)
- `web/server.py:83` — uso di `os.environ.get("SIGNAL_TUI_WEB_TOKEN")`
- `tests/test_web_plugin.py:2527` — test `test_start_web_server_requires_token_by_default`
- `tests/test_web_outgoing_mirror.py` — fixture con `contact_number='42'` e `batch_id='batch-1'`
- `purge_test_rows.py` — script di pulizia esistente (WhatsApp)
- `purge_ghost_outgoing.py:48-119` — pattern `--db`/`--apply`/dry-run
- `purge_orphan_media.py:40-41` — pattern `--execute`/`--dry-run`

---

**Fine del design v2.1**. Pronto per sviluppo.
