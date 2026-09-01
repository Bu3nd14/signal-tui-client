# Piano operativo — Engineering Cleanup (v1 architecture cleanup)

**Origine:** distillato operativo di `docs/ENGINEERING_ASSESSMENT.md` (assessment 1 Sep 2026).
**Stato:** piano approvato a livello di decisioni; esecuzione in fasi separate, protette da test suite + type checker.
**Scopo del documento:** elenco ordinato delle azioni a più alto beneficio, con percorsi concreti, comandi di verifica e criteri di completamento. Nessun codice di implementazione.

---

## 1. Obiettivo e scope

**Obiettivo:** consolidare l'architettura esistente (non aggiungere nuovi strati) eliminando le ambiguità strutturali principali — duplice namespace `backend/`/`backends/` e struttura Telegram asimmetrica — introducendo prima una rete di sicurezza di type checking statico.

**Fuori scope (esplicitamente):**

- **Src-layout** (`src/signal_tui/`): rinviato a sessione dedicata successiva (vedi Fase 3).
- **Release engineering / automazione release**: fuori scope (Fase 3 dell'assessment non attivata).
- **Coverage differenziale**: non si introduce (né Codecov patch-check né diff-cover); il gate globale resta `fail_under = 68`.
- **Split di `tui/app.py`**: solo monitoraggio; nessun intervento.
- **Merge `docs/` + `documentation/`**: non bloccante, non richiesto da questo piano (eventuali micro-spostamenti di singoli file `.md` sono permessi, vedi Fase 2).
- **Nuovi protocolli, nuove feature, rewrite**: fuori scope.

**Stato del repo verificato (baseline per il piano):**

- Duplice namespace: `backend/` (db, download, rpc, webhook + shim `__init__.py` re-export) e `backends/` (base, config, manager, signal, telegram, whatsapp, whatsapp_rest, whatsapp_events).
- Impatto rename: ~172 occurrence di import su `backend` in 68 file; ~144 occurrence su `backends` in 53 file (esclusi `.venv*`, `__pycache__`).
- Test Telegram in directory separata: `Telegram/{conftest.py, test_telegram_backend.py, test_regression.py}` + 2 documenti (`ARCHITECTURE.md`, `IMPLEMENTATION_PLAN.md`).
- `pyproject.toml`: `testpaths = ["tests", "Telegram"]`, `pythonpath = ["."]`, `fail_under = 68`, ruff pinnato `0.16.1`.
- CI (`.github/workflows/ci.yml`): job `lint` (ruff check + format check) → job `test` (matrix 3.12+coverage gate, 3.13 test-only) → upload Codecov (OIDC, non bloccante).
- `requirements-dev.txt`: pytest, pytest-asyncio, pytest-cov, coverage, ruff==0.16.1. Nessun type checker installato oggi.

---

## 2. Decisioni prese (vincolanti)

| # | Decisione | Conseguenza operativa |
|---|-----------|----------------------|
| 1 | **Type checker PRIMA del refactor strutturale** | Fase 0 precede e protegge qualsiasi rename/move. Ogni fase di refactor si conclude solo con il type checker verde. |
| 2 | **Pyright / basedpyright con strictness progressiva** | Tool scelto: **basedpyright** (fallback: pyright). Gate iniziale strict solo su `backends/` + `models.py`, esteso poi per path. Nessuno strict globale one-shot. |
| 3 | **Ordine: prima unificazione namespace e normalizzazione Telegram; src-layout rinviato** | Fase 1 = `backend/`+`backends/` → `protocols/`; Fase 2 = riallineamento test Telegram; Fase 3 = src-layout in sessione futura dedicata, protetta dal type checker. |
| 4 | **Coverage differenziale SALTATA** | Nessun nuovo tooling; il gate resta `coverage fail_under = 68` (pyproject) come oggi. |
| 5 | **`tui/app.py` non si tocca** | Solo monitoraggio LOC/responsabilità; si interviene quando una feature lo richiede. |
| 6 | **Release mechanisms invariati** | Nessun workflow nuovo, nessuna checklist. Fase 3 dell'assessment (release) esclusa. |
| 7 | **No-go accettati (4)** | No rewrite; no split per LOC; no caccia al 100% coverage; no refactor simultaneo protocolli+UI (questo piano lavora solo sul versante protocolli/struttura file). |

---

## 3. Fase 0 — Introduzione type checker (gate progressivo)

### Obiettivo

Disporre di una rete di sicurezza statica sugli import prima di toccare la struttura, con un gate CI che fallisce solo sul subset strict e non genera rumore sul resto del repo.

### Attività

1. **Scelta tool:** `basedpyright` come primaria (fork di pyright con superset di regole; pacchetto pip self-contained, nessun Node esterno; supporta l'opzione `strict` come lista di path, ideale per rollout progressivo). **Fallback documentato:** `pyright` (wrapper pip che richiede Node) con config equivalente.
2. **Pin versione** in `requirements-dev.txt` (stessa disciplina di `ruff==0.16.1`: il gate non deve "muoversi" tra versioni).
3. **Config in `pyproject.toml`** (fonte di verità unica, coerente con ruff/pytest):

```toml
[tool.basedpyright]
typeCheckingMode = "off"              # default: nessun rumore sull'intero repo
strict = ["backends", "models.py"]    # subset a contratto, controllato strict
pythonVersion = "3.12"
exclude = [".venv", ".venv-test", "__pycache__"]
```

   - Se si usa il fallback pyright: sezione `[tool.pyright]`; il meccanismo `strict` come lista non è disponibile in pyright classico → usare due file config (`pyrightconfig.json` rilassato + `pyright-strict.json` con `typeCheckingMode: strict` e `include` limitato al subset) e farli girare entrambi in CI.
   - **Nota di rollout:** se la prima run strict su `backends/`+`models.py` produce errori ingestibili in blocco, ridurre temporaneamente la lista `strict` ai file pulibili (es. `backends/base.py`, `backends/config.py`, `models.py`), registrare gli esclusi in una lista esplicita con follow-up. La DoD richiede comunque che la lista finale copra il subset definito.
4. **Makefile** — nuovo target (stesso stile dei target esistenti):

```make
typecheck:
	$(PYTHON) -m basedpyright
```

   e aggiunta di `typecheck` a `.PHONY`, eventualmente dentro `check`.
5. **CI** — nuovo job in `.github/workflows/ci.yml`, in parallelo con `lint` (niente `needs` proprio, oppure `needs: [lint, typecheck]` sul job `test` per conservare il fail-fast):

```yaml
  typecheck:
    name: Type check (basedpyright)
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v7
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: requirements-dev.txt
      - run: python -m pip install -r requirements-dev.txt
      - run: make typecheck
```

### Definition of done

- Config pinnata e versionata; `make typecheck` verde in locale e in CI.
- Lista `strict` che copre `backends/` + `models.py` (o subset esplicitamente documentato con follow-up ticket).
- Il job CI non aggiunge rumore sul resto del repo (default `off`).

### Verifica

- Locale: `source .venv-test/bin/activate && make typecheck` (o `make typecheck PYTHON=.venv-test/bin/python`).
- CI: run verde del job `typecheck` su PR; il job `test` mantiene gate `needs` aggiornato solo dopo stabilizzazione.

---

## 4. Fase 1 — Unificazione `backend/` + `backends/` → `protocols/`

### Obiettivo

Un solo namespace per il layer backend. Refactor meccanico (move + rewrite import), nessun cambio comportamentale. Eseguito **dopo** Fase 0 verde.

### Mapping concreto (move flat, rischio minimo)

| Origine | Destinazione |
|---|---|
| `backends/base.py` | `protocols/base.py` |
| `backends/config.py` | `protocols/config.py` |
| `backends/manager.py` | `protocols/manager.py` |
| `backends/signal.py` | `protocols/signal.py` |
| `backends/telegram.py` | `protocols/telegram.py` |
| `backends/whatsapp.py` | `protocols/whatsapp.py` |
| `backends/whatsapp_rest.py` | `protocols/whatsapp_rest.py` |
| `backends/whatsapp_events.py` | `protocols/whatsapp_events.py` |
| `backend/db.py` (cache condivisa) | `protocols/db.py` |
| `backend/download.py` (attachment serving condiviso) | `protocols/download.py` |
| `backend/rpc.py` (signal-cli client) | `protocols/rpc.py` |
| `backend/webhook.py` (WAHA webhook) | `protocols/webhook.py` |
| `backends/__init__.py` re-export pubblico | `protocols/__init__.py` (ChatBackend, BackendManager, SignalBackend, TelegramBackend, WhatsAppBackend) |
| `backend/__init__.py` (shim compat) | **eliminato** (rewrite diretto degli import, niente nuovo shim) |

**Nota placement:** `rpc.py` è Signal-specific e `webhook.py` è WhatsApp-specific; `db.py`/`download.py` sono infrastruttura condivisa dai tre protocolli. Il move flat è volutamente conservativo: la granularità in sotto-package (`protocols/{signal,whatsapp,telegram}/`) è differita alla Fase 3 (src-layout), per evitare una seconda ondata di rewrite import.

### Ordine dei passi

1. `git mv backends protocols` e move puntuale dei moduli di `backend/`.
2. Aggiornare `protocols/__init__.py`: re-export pubblico backends + re-export **transitorio** dei simboli ex-`backend` (per non spezzare tutto durante il rewrite).
3. Rewrite import con verifica grep-driven, in quest'ordine:
   - `from backends`/`import backends` → `protocols` (test + sorgente).
   - `from backend import X` → `from protocols.db|rpc|download|webhook import X` (mapping per simbolo, guidato dalla lista in `backend/__init__.py`).
   - `import backend` (siti che fanno monkeypatch) → import diretto del submodulo; aggiornare i target di patch:
     - `tests/conftest.py`: `import backend` + `monkeypatch.setattr(backend, "CACHE_DIR", ...)` → `import protocols.db` + setattr su `protocols.db`.
     - `Telegram/conftest.py`: `patch("backend._add_message_to_cache")` → `patch("protocols.db._add_message_to_cache")`.
   - Chiunque patcha stringhe tipo `"backends.signal.SignalBackend"` → `"protocols.signal.SignalBackend"` (es. `tests/`, `web/api.py`, `tui/`).
4. Eliminare `backend/__init__.py` e ridurre il re-export transitorio in `protocols/__init__.py` al solo API pubblico backends + docstring che marca db/download/rpc/webhook come interni.
5. File root del repo da riallineare (in sorgente e in test): `signal_tui.py`, `link_account.py`, `link_whatsapp.py`, `migrate_*.py`, `purge_*.py`, `device_link_screen.py`, `ui_components.py`, `web/*`, `tui/*`, `tests/*`.

### Definition of done

- `grep -rEn "from backend\b|import backend\b" --include=*.py .` → vuoto (esclusi `.venv*`, docs).
- `pytest --collect-only -q` colleziona lo **stesso numero di test** registrato prima del refactor.
- `make test`, `make coverage` (gate 68) e `make typecheck` verdi.
- Directory `backend/` assente; un solo namespace `protocols/`.

### Verifica

- Locale: count test pre/post, `make typecheck && make test && make coverage`.
- CI: run PR verde su tutti i job (`lint`, `typecheck`, `test` matrix).

---

## 5. Fase 2 — Normalizzazione struttura Telegram (test + testpaths)

### Obiettivo

Telegram smette di essere un caso speciale a livello di tooling: i suoi test vivono dentro `tests/` con la stessa organizzazione degli altri protocolli; `pyproject.toml` torna a un singolo `testpaths`.

### Attività

1. **Spostare i test Telegram** nella struttura comune:
   - `Telegram/test_telegram_backend.py` → `tests/protocols/telegram/test_telegram_backend.py`
   - `Telegram/test_regression.py` → `tests/protocols/telegram/test_regression.py`
   - Creare `tests/protocols/telegram/conftest.py` spostando la fixture autouse che mocka le scritte SQLite (già puntuale a quei test).
   - Nota: i test Telegram già flat in `tests/` (es. `test_telegram.py`, `test_reactions_telegram.py`, `test_web_*`) **non si spostano in questa fase**; la riorganizzazione completa `tests/{unit,integration,regression,protocols,e2e}` suggerita dall'assessment resta follow-up opzionale.
2. **Spostare i documenti Telegram**: `Telegram/ARCHITECTURE.md` e `Telegram/IMPLEMENTATION_PLAN.md` → `docs/` (rename opzionale `TELEGRAM_*` per chiarezza; il merge completo `docs/`+`documentation/` resta fuori scope).
3. **Fix `pyproject.toml`**:
   - `testpaths = ["tests"]`
   - coverage `omit`: rimuovere la voce `"Telegram/*"` (non più necessaria).
4. Eliminare la directory `Telegram/` una volta svuotata.

### Definition of done

- `pytest --collect-only -q` riporta lo stesso numero di test registrato prima (nessun test perso nel move).
- `make test` e `make coverage` verdi con `testpaths` semplificato.
- Directory `Telegram/` assente; nessun riferimento residuo a `Telegram/` in pyproject/Makefile/CI.

### Verifica

- `pytest --collect-only -q | wc -l` pre/post a parità.
- `make typecheck && make coverage` verdi; CI verde.

---

## 6. Fase 3 — Src-layout `src/signal_tui/` (RINVIATA, da pianificare)

### Obiettivo (futuro)

Adottare uno src-layout standard Python e incassare `protocols/`, `tui/`, `web/`, `models.py` e gli script di root in un unico package `signal_tui`. **Non iniziata in questo piano.**

### Precondizioni per la sessione dedicata

- Fasi 0–2 chiuse e stabili su master.
- Type checker verde: rete di sicurezza obbligatoria prima del move definitivo.
- Riorganizzazione opzionale in sotto-package `protocols/{signal,whatsapp,telegram}/` da valutare **insieme** allo src-layout (una sola ondata di rewrite import).
- Verificare i punti che assumono "esegui dalla root": `install.sh` (alias `python -m signal_tui`, venv `.venv`), `signal_tui.py` entry point, `pytest pythonpath = ["."]`, docker-compose, Makefile.
- Introduzione di build backend (setuptools/hatch) e `[project]` in pyproject, con entry point coerente.

### Definition of done (definita quando si pianifica)

Da redigere nella sessione dedicata; questo piano si limita a bloccarne la precondizione.

---

## 7. Rischi e mitigazioni

| Rischio | Probabilità | Mitigazione |
|---|---|---|
| Import residui rotti dopo rename (F1) | Media | Grep-check finale obbligatorio in DoD; `pytest --collect-only` a parità di count; test suite ampia come rete; CI gate su PR. |
| Monkeypatch target non aggiornati (`tests/conftest.py`, `Telegram/conftest.py`) | Medio | Elencati esplicitamente nella checklist F1/F2; falliscono rumorosamente già in collect. |
| Rumore/falsi positivi pyright sull'intero repo | Alto se configurato male | `typeCheckingMode = "off"` di default; solo la lista `strict` fallisce gate; estensione progressiva. |
| Errori strict ingestibili in blocco su `backends/` | Media | Rollout con subset ridotto documentato + follow-up; mai accendere strict globale one-shot. |
| Version drift del tool in CI | Bassa | Pin in `requirements-dev.txt` (stessa disciplina di `ruff==0.16.1`). |
| Durata CI aumentata | Bassa | Job `typecheck` parallelo a `lint` (timeout 5 min); nessun impatto sulla matrice test. |
| Coverage che "scende" per via dei path rinominati | Bassa | Coverage `source = ["."]` e omit non legati al nome package; verificare parità su `make coverage`. |
| Rewrite simultaneo protocolli+UI (no-go) | — | Il piano tocca solo namespace/struttura file; la UI resta intoccata salvo update degli import. |
| Big-bang (troppe fasi in una PR) | Media | Ogni fase = una PR separata, sequenziale; Fase 3 esplicitamente rinviata. |
| `basedpyright` vs `pyright` (tool risk) | Bassa | Config minimale e fallback documentato in Fase 0; la scelta è reversibile. |

---

## 8. Vincoli no-go (riepilogo)

1. **No rewrite:** consolidamento meccanico, nessuna riscrittura di astrazioni funzionanti.
2. **No split per LOC:** nessuna frammentazione di file motivata solo dalla dimensione (regola usata anche per `tui/app.py` = monitoraggio).
3. **No caccia al 100% coverage:** gate globale invariato a 68; nessun coverage differenziale aggiunto.
4. **No refactor simultaneo protocolli+UI:** questo piano agisce solo su namespace/protocol infrastructure; la UI subisce unicamente aggiornamenti di import.

---

## 9. Riferimenti a `docs/ENGINEERING_ASSESSMENT.md`

| Sezione assessment | Uso in questo piano |
|---|---|
| *Executive summary* | Obiettivo: consolidamento, non nuovi strati. |
| *Main weaknesses and recommendations* §1 (Consolidate filesystem) | Fasi 1 e 3 (target `protocols/` e differimento src-layout). |
| *Main weaknesses* §3 (Integrate Telegram) | Fase 2. |
| *Main weaknesses* §4 (Monitor `tui/app.py`) | Decisione 5: solo monitoraggio. |
| *Main weaknesses* §5 (Static type checking) | Fase 0 (opzioni pyright/mypy → scelta basedpyright). |
| *Main weaknesses* §8 (Coverage policy) | Decisione 4: differenziale saltata, gate globale invariato. |
| *Recommended roadmap* (Phase 1/2/3) | Qui riordinate: Phase 2-type-checking promossa a Fase 0; Phase 3-release esclusa. |
| *What should not be done* | Vincoli no-go ripresi in §8 di questo documento. |
| *Recommended priorities at a glance* | Selezione solo delle voci High/medium in scope; release/differenziale/ADR non nel piano. |

---

**Prossimo passo operativo:** Fase 0 in una PR dedicata ("introduce type checker gate"), poi Fase 1 in PR separata. Ogni fase si chiude con le DoD sopra elencate prima di aprire la successiva.
