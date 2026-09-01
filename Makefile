# =============================================================================
# Makefile — Signal TUI Client
# Comandi condivisi tra locale e CI (runner Ubuntu).
#
# Prerequisito: virtualenv ATTIVO (l'interprete `python` è quello del venv).
#   Locale :  source .venv-test/bin/activate
#   CI     :  python è fornito da actions/setup-python + pip install
#   (opz.) :  make test PYTHON=.venv-test/bin/python   ← senza attivare il venv
#
# I flag di pytest/coverage/ruff vivono in pyproject.toml: qui restano i comandi
# minimi. Va eseguito dalla ROOT del repository.
# =============================================================================

PYTHON ?= python

.PHONY: test lint typecheck coverage format-check check live-test live-test-manual

# Esegue l'intera suite in tests/ — radice definita da testpaths.
test:
	$(PYTHON) -m pytest

# Lint con ruff (regole da [tool.ruff] in pyproject.toml; profiling escluso).
lint:
	ruff check .

# Type checking progressivo (config da [tool.basedpyright] in pyproject.toml).
typecheck:
	$(PYTHON) -m basedpyright

# Test + coverage con gate (fail_under in [tool.coverage.report]).
# Produce coverage.xml per Codecov (Fase 5).
coverage:
	$(PYTHON) -m pytest --cov --cov-report=term-missing --cov-report=xml

# Verifica formattazione — attivare nel gate solo in Fase 4 (codice già formattato).
format-check:
	ruff format --check .

# Gate locale rapido: lint + test. La coverage resta un target separato
# (diventerà parte del gate CI in Fase 5).
check: lint test

# Test di integrazione "live" (su filo reale) — RUN OPZIONALE, NON in CI.
# Verificano il comportamento end-to-end contro un account di test reale
# (es. "Roberto BMW" sui 3 protocolli) e inviano messaggi REALI, quindi
# richiedono i servizi locali attivi (daemon signal-cli, WAHA docker, sessione
# Telethon autorizzata) e le credenziali configurate. Senza LIVE_TESTS=1 i test
# restano sempre skippati (vedi tests/test_live_quote_media.py). Runbook nel README.
live-test:
	LIVE_TESTS=1 $(PYTHON) -m pytest tests/test_live_quote_media.py -v

# E4 — verifica d'ingresso manuale (serve quotare dal client ufficiale del
# contatto di test mentre il test è in attesa). Richiede LIVE_MANUAL=1.
live-test-manual:
	LIVE_TESTS=1 LIVE_MANUAL=1 $(PYTHON) -m pytest tests/test_live_quote_media.py::test_e4_ingest_manual -v
