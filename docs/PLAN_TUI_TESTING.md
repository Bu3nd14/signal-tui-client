# Piano: Test automatici con lancio della TUI

**Data:** 2026-08-11
**Progetto:** signal-tui-client
**Obiettivo:** Poter eseguire test automatici che lanciano l'applicativo TUI (Textual) e
simulano interazioni utente reali.

---

## Stato attuale

- **396 test** (100% passanti) — tutti **unit test** che testano componenti in isolamento
  con mock (`unittest.mock.patch`, `MagicMock`, `_FakeListView`, ecc.)
- **Nessun test lancia mai la TUI vera:** nessuna chiamata a `app.run()` o
  `app.run_test()` in tutta la suite
- **Textual 8.2.8** già installato in `.venv-test`, con supporto nativo a
  `App.run_test()` (async context manager che restituisce un `Pilot` headless)
- **pytest 9.1.1** installato, ma **senza `pytest-asyncio`**

---

## Cosa manca — 5 elementi

### 1. Dipendenza: `pytest-asyncio`

Textual è asincrono. Il metodo di test ufficiale è `App.run_test()`, che è un
**async generator**. Senza `pytest-asyncio` non si possono scrivere funzioni
`async def test_...`.

**File:** `requirements-dev.txt`
```diff
 ruff>=0.12.0
+pytest-asyncio>=0.25.0
```

---

### 2. Fixture dell'App in `conftest.py`

Non esiste una fixture che crei l'app `SignalTUI` in modalità test. L'app vera
all'`__init__` e `on_mount` fa **troppo I/O reale**:

| Operazione | File / metodo | Effetto |
|---|---|---|
| `SignalBackend()` | `backends/signal.py` | Pronto a chiamare signal-cli via HTTP |
| `isEnabled()` | `config.json` + `.env` | Legge file system |
| `on_mount()` → `_connect_signal()` | `signal_tui.py:494` | Avvia connessione reale a signal-cli |
| `on_mount()` → `_connect_whatsapp()` | `signal_tui.py:499` | Avvia connessione WhatsApp |
| `on_mount()` → `_poll_worker()` | `signal_tui.py:491` | Avvia polling loop infinito |

Serve una fixture `app_for_test` che:

- Mocki `isEnabled()` → `False` (niente WhatsApp)
- Mocki `BackendManager.register()` e `SignalBackend` (niente connessioni reali)
- Inietti una lista di contatti fake predefinita
- Disabiliti il polling worker (o lo faccia girare una sola iterazione)
- Restituisca l'istanza `SignalTUI` pronta per `run_test()`

**File:** `tests/conftest.py` (aggiunta)

```python
@pytest.fixture
def app_for_test():
    """Crea un SignalTUI con backend mockati per test TUI."""
    with (
        patch("signal_tui.SignalBackend") as mock_sb,
        patch("signal_tui.whatsapp_enabled", return_value=False),
    ):
        app = SignalTUI()
        app.contacts = [
            ChatContact(id="+391234567890", display_name="Mario", protocol="signal"),
            ChatContact(id="+393331234567", display_name="Anna", protocol="signal"),
        ]
        yield app
```

---

### 3. Configurazione pytest (`pyproject.toml`)

Manca un file di configurazione pytest che attivi la modalità asyncio
automatica, così che ogni `async def test_*` venga riconosciuta senza dover
aggiungere decoratori manuali.

**File:** `pyproject.toml` (nuovo, in radice progetto)

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = [
    "asyncio: test asincrono",
    "integration: test che lancia la TUI (lento, richiede headless terminal)",
]
```

---

### 4. Nuovo file di test: `tests/test_tui_integration.py`

File dedicato ai test che **lanciano davvero la TUI** via `App.run_test()`.
Il Pilot di Textual permette di:

- `await pilot.press("ctrl+q")` — premere tasti
- `await pilot.click("#contact-list")` — cliccare widget
- `await pilot.wait_for_animation()` — attendere animazioni
- Ispezionare lo stato dell'app dopo le azioni

#### Test da implementare

| # | Test | Cosa verifica | Complessità |
|---|------|---------------|-------------|
| 1 | `test_app_launches` | L'app si avvia senza crash, Header e Footer visibili | Bassa |
| 2 | `test_quit_via_ctrl_q` | `Ctrl+Q` chiama `app.exit()` e termina il test | Bassa |
| 3 | `test_quit_via_binding` | Il binding `action_quit` funziona correttamente | Bassa |
| 4 | `test_contact_list_renders` | I contatti mockati appaiono nella `ListView` | Media |
| 5 | `test_select_contact` | Cliccando un contatto, `selected_contact` si aggiorna | Media |
| 6 | `test_input_field_exists` | L'`Input` per scrivere messaggi è presente | Bassa |
| 7 | `test_protocol_filter_cycle` | `Ctrl+W` cicla il filtro `all` → `signal` → `whatsapp` → `all` | Media |
| 8 | `test_send_message_mocked` | Scrivere nell'Input e premere Invio chiama il backend mockato | Alta |
| 9 | `test_status_bar_shows` | La `#status-bar` mostra messaggi di stato | Bassa |
| 10 | `test_emoji_picker_opens` | `Ctrl+E` apre l'EmojiPickerScreen | Alta |

**File:** `tests/test_tui_integration.py` (nuovo)

---

### 5. Aggiornamento `run_regression_tests.sh`

Lo script deve installare anche `pytest-asyncio` e può marcare i test di
integrazione con un marker per escluderli dalla suite veloce.

**File:** `tests/run_regression_tests.sh`

Modifiche:
```diff
 # Installa pytest se non presente
 if ! pip show pytest &>/dev/null; then
     pip install --quiet pytest 2>/dev/null
     echo "   ✅ pytest installato"
 fi

+if ! pip show pytest-asyncio &>/dev/null; then
+    pip install --quiet pytest-asyncio 2>/dev/null
+    echo "   ✅ pytest-asyncio installato"
+fi
```

---

## Piano di esecuzione

### Fase 1 — Preparazione (15 min)

- [ ] **Step 1.1:** Aggiungere `pytest-asyncio>=0.25.0` a `requirements-dev.txt`
- [ ] **Step 1.2:** Creare `pyproject.toml` con `asyncio_mode = "auto"`
- [ ] **Step 1.3:** Installare `pytest-asyncio` nel `.venv-test`

### Fase 2 — Fixture (30 min)

- [ ] **Step 2.1:** Aggiungere fixture `app_for_test` in `tests/conftest.py`
  che mocka `SignalBackend`, `isEnabled`, e inietta contatti finti
- [ ] **Step 2.2:** Aggiungere fixture helper per scenari comuni (es. app con
  5 contatti, app con WhatsApp abilitato, app con unread)

### Fase 3 — Test base TUI (1 ora)

- [ ] **Step 3.1:** Creare `tests/test_tui_integration.py` con i test 1-3, 6, 9
  (avvio, quit, input, status bar)
- [ ] **Step 3.2:** Verificare che girino in headless mode sul sistema locale
- [ ] **Step 3.3:** Verificare che girino nel `.venv-test` senza rompere i
  test esistenti

### Fase 4 — Test interattivi (2 ore)

- [ ] **Step 4.1:** Test 4-5: render contatti e selezione
- [ ] **Step 4.2:** Test 7: ciclo filtro protocollo `Ctrl+W`
- [ ] **Step 4.3:** Test 8: invio messaggio mockato con verifica chiamata backend
- [ ] **Step 4.4:** Test 10: emoji picker

### Fase 5 — Robustezza (30 min)

- [ ] **Step 5.1:** Aggiornare `run_regression_tests.sh` per installare `pytest-asyncio`
- [ ] **Step 5.2:** Aggiungere marker `integration` e comando per eseguire solo
  i test veloci: `pytest tests/ -m "not integration"`
- [ ] **Step 5.3:** Verificare che `run_regression_tests.sh` passi ancora con exit code 0

---

## Riferimenti

- **Textual testing docs:** `App.run_test()` con parametri
  `headless=True, size=(80, 24)`, restituisce `Pilot`
- **Pilot API:** `.press()`, `.click()`, `.wait_for_animation()`,
  `.wait_for_scheduled_animations()`
- **pytest-asyncio:** modalità `auto` rende ogni `async def test_*` un test
  asyncio senza decoratori
- **File esistenti da non rompere:** 396 test in `tests/` — tutti devono
  continuare a passare

---

## Note / Rischi

1. **Textual in headless mode potrebbe non supportare alcune feature visuali**
   (es. rendering immagini, emoji complesse). I test vanno limitati alla logica
   di interazione e alla struttura widget.
2. **Il polling worker va disabilitato o mockato** altrimenti il test non
   termina mai (loop infinito in `_poll_worker`).
3. **I test di integrazione sono più lenti** dei test unitari (qualche secondo
   ciascuno). Vanno marcati con `@pytest.mark.integration` per poterli
   escludere durante lo sviluppo rapido.
4. **Compatibilità CI/CD:** L'headless mode di Textual funziona senza terminale
   fisico, ma potrebbe richiedere `TERM=xterm-256color` su alcuni runner.

