#!/usr/bin/env bash
#
# install.sh — Installazione automatica di Signal TUI Client
#
# Scarica la build JVM completa di signal-cli (quella giusta, con il comando
# `daemon` per il JSON-RPC), crea un virtualenv (opzionale) e installa le
# dipendenze Python.
#
# Uso:
#   ./install.sh                     # installazione completa
#   ./install.sh --no-venv           # senza creare il virtualenv
#   ./install.sh --version 0.14.7    # scarica una versione specifica di signal-cli
#   ./install.sh --skip-signal-cli   # non scaricare signal-cli (se già presente)
#   ./install.sh --update            # aggiorna signal-cli all'ultima versione
#   ./install.sh --help              # mostra questo aiuto
#
set -euo pipefail

# ─── Colori ───────────────────────────────────────────────────────────────────
if [ -t 1 ]; then
    C_RED=$'\033[31m'
    C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'
    C_BLUE=$'\033[34m'
    C_BOLD=$'\033[1m'
    C_RESET=$'\033[0m'
else
    C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""; C_RESET=""
fi

info()  { echo "${C_BLUE}${C_BOLD}[INFO]${C_RESET} $*"; }
ok()    { echo "${C_GREEN}${C_BOLD}[OK]${C_RESET} $*"; }
warn()  { echo "${C_YELLOW}${C_BOLD}[WARN]${C_RESET} $*"; }
err()   { echo "${C_RED}${C_BOLD}[ERROR]${C_RESET} $*" >&2; }
die()   { err "$*"; exit 1; }

# ─── Configurazione ───────────────────────────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$PROJECT_DIR/bin"
REPO="AsamK/signal-cli"
REQUIRED_PYTHON_MAJOR=3
REQUIRED_PYTHON_MINOR=10
REQUIRED_JAVA_MAJOR=25

# Flag di default
DO_VENV=1
DO_SIGNAL_CLI=1
DO_UPDATE=0
DO_WHATSAPP=0
SPECIFIC_VERSION=""

# ─── Parsing argomenti ────────────────────────────────────────────────────────
usage() {
    cat <<EOF
${C_BOLD}install.sh — Installazione automatica di Signal TUI Client${C_RESET}

Uso:
  ./install.sh [opzioni]

Opzioni:
  --no-venv            Non creare il virtualenv Python (usa il Python di sistema)
  --version X.Y.Z      Scarica una versione specifica di signal-cli (es. 0.14.7)
  --skip-signal-cli    Non scaricare signal-cli (se già presente in ./bin/)
  --update             Aggiorna signal-cli all'ultima versione disponibile
  --whatsapp           Avvia il WhatsApp HTTP API (WAHA) via Docker Compose
  --help               Mostra questo aiuto

Esempi:
  ./install.sh                          # installazione completa
  ./install.sh --no-venv                # senza virtualenv
  ./install.sh --update                 # aggiorna signal-cli
  ./install.sh --whatsapp               # avvia WAHA (WhatsApp HTTP API)
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --no-venv)          DO_VENV=0; shift ;;
        --skip-signal-cli)  DO_SIGNAL_CLI=0; shift ;;
        --update)           DO_UPDATE=1; shift ;;
        --whatsapp)         DO_WHATSAPP=1; shift ;;
        --version)
            [ $# -lt 2 ] && die "--version richiede un argomento (es. 0.14.7)"
            SPECIFIC_VERSION="$2"; shift 2 ;;
        --help|-h)          usage; exit 0 ;;
        *)                  die "Opzione sconosciuta: $1 (usa --help)" ;;
    esac
done

# ─── Funzioni di supporto ─────────────────────────────────────────────────────

# Verifica che un comando esista
require_cmd() {
    local cmd="$1"
    if ! command -v "$cmd" >/dev/null 2>&1; then
        die "Comando '$cmd' non trovato. Installalo e riprova."
    fi
}

# Ottiene l'ultima versione di signal-cli dalla GitHub API
get_latest_version() {
    local url="https://api.github.com/repos/$REPO/releases/latest"
    if command -v curl >/dev/null 2>&1; then
        curl -s "$url" | sed -n 's/.*"tag_name": *"v\([^"]*\)".*/\1/p' | head -1
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- "$url" | sed -n 's/.*"tag_name": *"v\([^"]*\)".*/\1/p' | head -1
    else
        die "Serve curl o wget per determinare l'ultima versione di signal-cli."
    fi
}

# Ottiene la versione di signal-cli attualmente installata in ./bin/
get_installed_version() {
    if [ -d "$BIN_DIR" ]; then
        for d in "$BIN_DIR"/signal-cli-*; do
            if [ -d "$d" ]; then
                basename "$d" | sed 's/^signal-cli-//'
                return 0
            fi
        done
    fi
    echo ""
}

# Verifica la versione di Python
check_python() {
    if ! command -v python3 >/dev/null 2>&1; then
        die "Python 3 non trovato. Installa Python ${REQUIRED_PYTHON_MAJOR}.${REQUIRED_PYTHON_MINOR}+ e riprova."
    fi
    local ver
    ver="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    local major="${ver%%.*}"
    local minor="${ver#*.}"; minor="${minor%%.*}"
    if [ "$major" -lt "$REQUIRED_PYTHON_MAJOR" ] || \
       { [ "$major" -eq "$REQUIRED_PYTHON_MAJOR" ] && [ "$minor" -lt "$REQUIRED_PYTHON_MINOR" ]; }; then
        die "Python ${REQUIRED_PYTHON_MAJOR}.${REQUIRED_PYTHON_MINOR}+ richiesto, trovato $ver."
    fi
    ok "Python $ver trovato."
}

# Verifica la versione di Java (richiesta dalla build JVM di signal-cli)
check_java() {
    if ! command -v java >/dev/null 2>&1; then
        warn "Java non trovato. La build JVM di signal-cli richiede Java ${REQUIRED_JAVA_MAJOR}+."
        warn "Installa Java ${REQUIRED_JAVA_MAJOR} (es. su Debian/Ubuntu: 'sudo apt install openjdk-${REQUIRED_JAVA_MAJOR}-jre')."
        return 1
    fi
    local ver
    ver="$(java -version 2>&1 | head -1 | sed 's/.*version "\([0-9]*\).*/\1/')"
    if [ -z "$ver" ]; then
        warn "Impossibile determinare la versione di Java."
        return 1
    fi
    if [ "$ver" -lt "$REQUIRED_JAVA_MAJOR" ]; then
        warn "Java $ver trovato, ma signal-cli richiede Java ${REQUIRED_JAVA_MAJOR}+."
        warn "Aggiorna Java (es. su Debian/Ubuntu: 'sudo apt install openjdk-${REQUIRED_JAVA_MAJOR}-jre')."
        return 1
    fi
    ok "Java $ver trovato."
    return 0
}

# Scarica ed estrae la build JVM completa di signal-cli in ./bin/
download_signal_cli() {
    local version="$1"
    local url="https://github.com/$REPO/releases/download/v$version/signal-cli-$version.tar.gz"
    local tarball="/tmp/signal-cli-$version.tar.gz"

    info "Scaricamento signal-cli v$version (build JVM completa)..."
    info "  $url"

    if command -v curl >/dev/null 2>&1; then
        curl -fSL -o "$tarball" "$url" || die "Download fallito (curl). Verifica la versione '$version'."
    elif command -v wget >/dev/null 2>&1; then
        wget -O "$tarball" "$url" || die "Download fallito (wget). Verifica la versione '$version'."
    else
        die "Serve curl o wget per scaricare signal-cli."
    fi

    info "Estrazione in $BIN_DIR ..."
    mkdir -p "$BIN_DIR"
    tar xzf "$tarball" -C "$BIN_DIR"
    rm -f "$tarball"

    # Verifica la struttura attesa: bin/signal-cli-*/bin/signal-cli
    local exe="$BIN_DIR/signal-cli-$version/bin/signal-cli"
    if [ ! -f "$exe" ]; then
        die "Struttura inattesa: '$exe' non trovato. La build scaricata non è quella JVM completa."
    fi
    chmod +x "$exe"
    ok "signal-cli v$version installato in $BIN_DIR/signal-cli-$version/"
}

# Rimuove le versioni di signal-cli diverse da quella specificata
remove_old_versions() {
    local keep="$1"
    if [ ! -d "$BIN_DIR" ]; then
        return 0
    fi
    for d in "$BIN_DIR"/signal-cli-*; do
        if [ -d "$d" ] && [ "$(basename "$d")" != "signal-cli-$keep" ]; then
            info "Rimozione vecchia versione: $(basename "$d")"
            rm -rf "$d"
        fi
    done
}

# Installa le dipendenze Python (con o senza venv)
install_python_deps() {
    local pip_cmd="pip3"
    local python_cmd="python3"

    if [ "$DO_VENV" -eq 1 ]; then
        if [ ! -d "$PROJECT_DIR/.venv" ]; then
            info "Creazione virtualenv in $PROJECT_DIR/.venv ..."
            python3 -m venv "$PROJECT_DIR/.venv" || die "Creazione del virtualenv fallita."
        else
            info "Virtualenv già presente in $PROJECT_DIR/.venv"
        fi
        python_cmd="$PROJECT_DIR/.venv/bin/python"
        pip_cmd="$PROJECT_DIR/.venv/bin/pip"
    fi

    info "Installazione delle dipendenze Python (requirements.txt) ..."
    "$python_cmd" -m pip install --upgrade pip >/dev/null 2>&1 || true
    "$pip_cmd" install -r "$PROJECT_DIR/requirements.txt" || die "Installazione delle dipendenze Python fallita."

    if [ "$DO_VENV" -eq 1 ]; then
        ok "Dipendenze installate nel virtualenv. Attivalo con:"
        echo "    source $PROJECT_DIR/.venv/bin/activate"
    else
        ok "Dipendenze installate nel Python di sistema."
    fi
}

# ─── Esecuzione principale ────────────────────────────────────────────────────
echo
echo "${C_BOLD}=== Signal TUI Client — Installazione ===${C_RESET}"
echo

# 1. Verifica prerequisiti di base
require_cmd tar
require_cmd python3

# 2. Aggiornamento signal-cli
if [ "$DO_UPDATE" -eq 1 ]; then
    info "Modalità aggiornamento signal-cli ..."
    latest="$(get_latest_version)"
    [ -z "$latest" ] && die "Impossibile determinare l'ultima versione di signal-cli."
    installed="$(get_installed_version)"

    if [ -n "$installed" ] && [ "$installed" = "$latest" ]; then
        ok "signal-cli è già all'ultima versione ($latest)."
    else
        info "Versione installata: ${installed:-nessuna} → ultima: $latest"
        download_signal_cli "$latest"
        remove_old_versions "$latest"
        ok "signal-cli aggiornato alla versione $latest."
    fi
    echo
    echo "${C_GREEN}${C_BOLD}=== Aggiornamento completato ===${C_RESET}"
    exit 0
fi

# 3. Verifica Python e Java
check_python
check_java || true   # Java è un warning, non blocca l'installazione (ma signal-cli non funzionerà senza)

# 4. Download signal-cli (se richiesto)
if [ "$DO_SIGNAL_CLI" -eq 1 ]; then
    version="$SPECIFIC_VERSION"
    if [ -z "$version" ]; then
        version="$(get_latest_version)"
        [ -z "$version" ] && die "Impossibile determinare l'ultima versione di signal-cli."
        info "Ultima versione di signal-cli rilevata: $version"
    fi

    installed="$(get_installed_version)"
    if [ -n "$installed" ] && [ "$installed" = "$version" ]; then
        ok "signal-cli v$version già installato. (usa --update per aggiornare)"
    else
        download_signal_cli "$version"
        remove_old_versions "$version"
    fi
else
    info "Download di signal-cli saltato (--skip-signal-cli)."
    installed="$(get_installed_version)"
    if [ -z "$installed" ]; then
        warn "Nessuna versione di signal-cli trovata in $BIN_DIR. Il client non funzionerà."
    else
        ok "signal-cli v$installed trovato in $BIN_DIR."
    fi
fi

# 5. Installazione dipendenze Python
install_python_deps

# 5.5 Avvio WhatsApp HTTP API (WAHA) se richiesto (--whatsapp)
if [ "$DO_WHATSAPP" -eq 1 ]; then
    echo
    info "Avvio del WhatsApp HTTP API (WAHA) via Docker Compose ..."
    if command -v docker >/dev/null 2>&1; then
        if docker compose version >/dev/null 2>&1; then
            docker compose -f "$PROJECT_DIR/docker-compose.yml" up -d
            WA_PORT="${WHATSAPP_API_PORT:-3005}"
            ok "WAHA avviato. URL: http://127.0.0.1:${WA_PORT}"
        else
            err "Docker Compose non trovato. Installalo oppure usa 'scripts/start_whatsapp.sh'."
        fi
    else
        err "Docker non trovato. Per usare WhatsApp installa Docker (vedi README) oppure lancia l'API manualmente (nessuna Node.js nel tuo server? segui il README sezione WhatsApp)."
    fi
    echo
fi

# 6. Riepilogo finale
echo
echo "${C_GREEN}${C_BOLD}=== Installazione completata! ===${C_RESET}"
echo
echo "Prossimi passi:"
echo "  1. Configura il tuo numero di telefono:"
echo "       export SIGNAL_USER_NUMBER=\"+1234567890\""
echo "     oppure crea config.json:"
echo "       echo '{\"user_number\": \"+1234567890\"}' > config.json"
echo "  2. Collega il tuo account Signal (QR code):"
if [ "$DO_VENV" -eq 1 ]; then
    echo "       source .venv/bin/activate"
    echo "       python3 link_account.py"
else
    echo "       python3 link_account.py"
fi
if [ "$DO_WHATSAPP" -eq 1 ]; then
    echo "  3. Avvia il WhatsApp HTTP API (WAHA) e collega WhatsApp (QR):"
    echo "       docker compose up -d                 # avvia WAHA su porta 3005"
    echo "       export WHATSAPP_API_URL=\"http://127.0.0.1:3005\""
    if [ "$DO_VENV" -eq 1 ]; then
        echo "       source .venv/bin/activate"
        echo "       python3 link_whatsapp.py"
    else
        echo "       python3 link_whatsapp.py   # oppure: ./scripts/start_whatsapp.sh"
    fi
    echo "  4. Avvia il client:"
else
    echo "  3. Avvia il client:"
fi
if [ "$DO_VENV" -eq 1 ]; then
    echo "       source .venv/bin/activate"
fi
echo "       python3 signal_tui.py"
echo
