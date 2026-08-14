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

.PHONY: test lint coverage format-check check

# Esegue l'intera suite (tests/ + Telegram/) — radici definite da testpaths.
test:
	$(PYTHON) -m pytest

# Lint con ruff (regole da [tool.ruff] in pyproject.toml; profiling escluso).
lint:
	ruff check .

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
