#!/usr/bin/env bash
# =============================================================================
#  Regression Test Suite — Signal TUI Client
# =============================================================================
#  Esegue tutti i test di regressione e produce un report colorato.
#  Usa:  ./tests/run_regression_tests.sh
#  Oppure da qualsiasi directory:  bash tests/run_regression_tests.sh
#
#  Exit code: 0 se tutti i test passano, 1 altrimenti.
# =============================================================================

set -euo pipefail

# ── Colori ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# ── Directory dello script ───────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo ""
echo -e "${CYAN}${BOLD}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}${BOLD}║   Signal TUI Client — Regression Test Suite                ║${NC}"
echo -e "${CYAN}${BOLD}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${YELLOW}Project:${NC} $PROJECT_DIR"
echo ""

# ── Verifica Python ──────────────────────────────────────────────────────────
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        PYTHON="$cmd"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo -e "${RED}❌ Python non trovato. Installa Python 3.8+.${NC}"
    exit 1
fi

echo -e "${YELLOW}Python:${NC} $($PYTHON --version 2>&1)"
echo ""

# ── Virtual environment ──────────────────────────────────────────────────────
VENV_DIR="$PROJECT_DIR/.venv-test"

if [ ! -d "$VENV_DIR" ]; then
    echo -e "${YELLOW}📦 Creazione virtual environment...${NC}"
    $PYTHON -m venv "$VENV_DIR"
    echo "   ✅ Creato $VENV_DIR"
fi

# Attiva il virtualenv
source "$VENV_DIR/bin/activate" || source "$VENV_DIR/Scripts/activate" 2>/dev/null || {
    echo -e "${RED}❌ Impossibile attivare il virtualenv.${NC}"
    exit 1
}

# ── Installa dipendenze ──────────────────────────────────────────────────────
echo -e "${YELLOW}📦 Installazione dipendenze...${NC}"

pip install --quiet --upgrade pip 2>/dev/null

# Installa pytest se non presente
if ! pip show pytest &>/dev/null; then
    pip install --quiet pytest 2>/dev/null
    echo "   ✅ pytest installato"
fi

# Installa le dipendenze del progetto (se requirements.txt esiste)
if [ -f "$PROJECT_DIR/requirements.txt" ]; then
    pip install --quiet -r "$PROJECT_DIR/requirements.txt" 2>/dev/null
    echo "   ✅ Dipendenze progetto installate"
fi

echo ""

# ── Esecuzione test ──────────────────────────────────────────────────────────
echo -e "${CYAN}${BOLD}🧪 Esecuzione test...${NC}"
echo ""

cd "$PROJECT_DIR"

# Esegue pytest con output verbose e report
set +e  # disabilita exit-on-error per catturare il risultato
python -m pytest tests/ \
    -v \
    --tb=short \
    --no-header 2>&1

PYTEST_EXIT_CODE=$?
set -e

echo ""

# ── Report finale ────────────────────────────────────────────────────────────
if [ $PYTEST_EXIT_CODE -eq 0 ]; then
    echo -e "${GREEN}${BOLD}✅ TUTTI I TEST SUPERATI${NC}"
    echo -e "${GREEN}   Il client è pronto per il rilascio.${NC}"
else
    echo -e "${RED}${BOLD}❌ QUALCHE TEST È FALLITO${NC}"
    echo -e "${RED}   Controlla l'output sopra per i dettagli.${NC}"
fi

echo ""
echo -e "${CYAN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Disattiva virtualenv
deactivate 2>/dev/null || true

exit $PYTEST_EXIT_CODE
