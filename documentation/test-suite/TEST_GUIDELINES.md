# Linee guida per i test

Convenzioni desunte dai test esistenti (`tests/conftest.py`, `Telegram/conftest.py` e i file di test più recenti). Seguile per mantenere la suite coerente, deterministica e offline.

## 1. Principi

1. **Nessun I/O reale**: niente daemon signal-cli, chiamate WAHA/Telegram, socket in ascolto o scritture nella home. Tutto passa da mock, monkeypatch e `tmp_path`.
2. **Patcha dove il simbolo è USATO**, non dove è definito: es. `patch("tui.app.BackendManager")`, `patch("backend.DB_FILE", ...)` (pattern usato in `conftest.py` e `test_cache_debounce.py`).
3. **Test headless della TUI**: mai avviare l'app reale; usare le fixture `app_for_test` / `app_for_test_with_mocks` con `App.run_test()` di Textual.
4. **Determinismo**: nessun `sleep` arbitrario dove evitabile; i worker di sfondo sono neutralizzati (`run_worker = MagicMock()`) nelle fixture app.
5. **Nomi espliciti**: file `test_<area>[_<dettaglio>].py`; classi di gruppo `Test<Cosa>`; metodi/funzioni che descrivono il comportamento, anche in italiano quando aiuta la lettura.

## 2. Struttura standard di un file di test

```python
"""
<Descrizione dell'area coperta.>
"""

from __future__ import annotations

from unittest.mock import patch  # MagicMock quando serve

from models import ChatContact
# ...import dei moduli sotto test


class TestArea:
    """🧱 <cosa copre la classe>."""

    def test_comportamento_atteso(self): ...
```

Nota sugli import: `pyproject.toml` definisce `pythonpath = ["."]`, quindi i moduli del progetto sono importabili direttamente senza manipolare `sys.path`. Alcuni file storici contengono ancora un ridondante `sys.path.insert(0, PROJECT_ROOT)` (senza commenti `noqa`): non replicarlo nei nuovi test.

Convenzioni osservate:

- docstring di modulo che dichiara lo scopo e le garanzie (vedi `test_edit_contract.py`, `test_cache_debounce.py`);
- classi `Test*` per raggruppare (con emoji tematiche nel docstring in molti file esistenti);
- helper `_MinimalBackend(ChatBackend)` / mock backend minimi per testare i default del contratto base senza I/O;
- marker `@pytest.mark.integration` per i test che lanciano davvero la TUI headless;
- **test live** (filo reale): marker `@pytest.mark.live` + skipif su env gate, sul modello di `tests/test_live_quote_media.py` (`LIVE_TESTS=1`; `LIVE_MANUAL=1` per i passi che richiedono intervento manuale sul device). Restano FUORI dalla CI: si eseguono solo con `make live-test`. Qualsiasi nuovo test che parli con backend reali deve adottare lo stesso pattern (skip di default, target/account sovrascrivibili via env).

## 3. Fixture disponibili (`tests/conftest.py`)

| Fixture | Fornisce |
|---|---|
| `tmp_cache_dir(tmp_path)` | directory cache temporanea |
| `tmp_cache_file(tmp_cache_dir)` | path `messages.json` legacy dentro la tmp dir |
| `sample_messages()` | cache in memoria di esempio `{contatto: [msg_dict]}` con timestamp recenti |
| `sample_envelope_text()` | envelope Signal dataMessage testuale |
| `sample_envelope_image()` | envelope Signal con attachment immagine + caption |
| `sample_envelope_receipt()` | envelope receiptMessage (isDelivery) |
| `sample_contacts_rpc_output()` | lista contatti formato RPC |
| `sample_contacts_subprocess_output()` | lista contatti formato testo subprocess |
| `app_for_test()` | `SignalTUI` headless: backends mockati, WA/TG disabilitati, `on_mount` neutralizzato (monta UI e renderizza contatti fake), worker no-op |
| `app_for_test_with_mocks()` | come sopra ma yield `(app, backend_signal_mockato)` |

In `Telegram/conftest.py`: fixture autouse `_mock_sqlite_writes` che patcha `backend._add_message_to_cache` e `backend._update_message_id` — ogni nuova suite che tocca SQLite dovrebbe avere una protezione equivalente.

## 4. Pattern ricorrenti (da riusare)

- **DB temporaneo**: 
  ```python
  @pytest.fixture
  def tmp_db(tmp_path):
      db = tmp_path / "messages.db"
      with patch("backend.DB_FILE", db), patch("backend.CACHE_DIR", tmp_path):
          yield db
  ```
- **App headless**: usare `app_for_test`; per azioni async usare `async with app.run_test():` e poi invocare i metodi del mixin direttamente.
- **Contratti opzionali**: per verificare i default di `ChatBackend` scrivere un subclass minimo che implementa solo gli abstract (pattern `_MinimalBackend` di `test_edit_contract.py`/`test_address_book.py`).
- **Envelope/frame come dati**: costruire payload di test come dict letterali fedeli al protocollo (vedere le fixture `sample_envelope_*`) invece di mock complessi.
- **Thread**: preferire il test delle funzioni pure e delle code; se serve un thread, joinare con timeout e non dipendere dai tempi reali.

## 5. Configurazione rilevante (`pyproject.toml`)

- `testpaths = ["tests", "Telegram"]`, `pythonpath = ["."]`.
- `addopts = "-ra --tb=short --strict-markers"` → marker non registrati fanno fallire la suite: registrare nuovi marker in `markers` (oggi registrati: `integration`, `live`, `live-manual`).
- `asyncio_mode = "auto"` → i coroutine test si scrivono con `async def test_...` senza decorator.
- Coverage gate: `fail_under = 68` (branch on); per la CI viene prodotto `coverage.xml`.

## 6. Checklist prima di aggiungere un test

- [ ] Il test gira offline e in memoria (nessun tocco a `~/.local/share/signal-tui-client/`)?
- [ ] Usa le fixture esistenti invece di reinventare setup?
- [ ] Patcha i simboli nei moduli d'uso?
- [ ] Il nome del file/classe/test descrive area e comportamento?
- [ ] Non introduce marker non registrati né dipendenze nuove?
- [ ] `make check` (lint + suite) passa?
