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
#   ./install.sh --no-web           # non installare le dipendenze opzionali della Web UI
#   ./install.sh --aliases           # installa solo gli alias shell della Web UI
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
DO_CHECK_WHATSAPP=0
DO_ALIASES_ONLY=0
DO_WEB=1
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
  --check-whatsapp     Verifica i prerequisiti WhatsApp (Docker, porte, firewall)
  --no-web            Non installare le dipendenze opzionali della Web UI (requirements-web.txt)
  --aliases            Installa solo gli alias shell della Web UI
  --help               Mostra questo aiuto

Esempi:
  ./install.sh                          # installazione completa
  ./install.sh --no-venv                # senza virtualenv
  ./install.sh --update                 # aggiorna signal-cli
  ./install.sh --check-whatsapp         # controlla solo prerequisiti WhatsApp
  ./install.sh --whatsapp               # avvia WAHA (WhatsApp HTTP API)
  ./install.sh --aliases                # installa solo gli alias della Web UI
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --no-venv)          DO_VENV=0; shift ;;
        --skip-signal-cli)  DO_SIGNAL_CLI=0; shift ;;
        --update)           DO_UPDATE=1; shift ;;
        --whatsapp)         DO_WHATSAPP=1; shift ;;
        --check-whatsapp)   DO_CHECK_WHATSAPP=1; shift ;;
        --no-web)           DO_WEB=0; shift ;;
        --aliases)          DO_ALIASES_ONLY=1; shift ;;
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

install_aliases() {
    local shell_name="${SHELL:-}"
    shell_name="${shell_name##*/}"
    local rc_file
    local begin_marker="# ── BEGIN signal-tui aliases ──"
    local end_marker="# ── END signal-tui aliases ──"
    local ALIASES_BLOCK

    case "$shell_name" in
        *zsh*)  rc_file="$HOME/.zshrc" ;;
        *bash*|"") rc_file="$HOME/.bashrc" ;;
        *)
            warn "Shell '$shell_name' non supportata: gli alias richiedono bash/zsh; fish richiederebbe funzioni (vedi docs/ALIASES.md)."
            return 0
            ;;
    esac

    if [[ "$PROJECT_DIR" == *[[:space:]]* ]]; then
        warn "Il path del progetto contiene spazi; verifica il quoting degli alias dopo l'installazione: $PROJECT_DIR"
    fi

    read -r -d '' ALIASES_BLOCK <<'EOF' || true
# ─── Signal TUI Client: web reader + background via tmux ─────────────────
# Web su 0.0.0.0:4242. Token Bearer: config.json (web.token) o SIGNAL_TUI_WEB_TOKEN.
# web-signal-tui-bg esporta il token nella shell (fast cycle: curl + login Web UI).
SIGNAL_TUI_DIR="__PROJECT_DIR__"
alias web-signal-tui='( cd "$SIGNAL_TUI_DIR" && .venv/bin/python -m signal_tui --web --web-port 4242 --web-host 0.0.0.0 )'
alias web-signal-tui-bg='tmux new-session -d -s tui "cd $SIGNAL_TUI_DIR && .venv/bin/python -m signal_tui --web --web-port 4242 --web-host 0.0.0.0" && export SIGNAL_TUI_WEB_TOKEN="$(python3 -c "import json; print(json.load(open(\"$SIGNAL_TUI_DIR/config.json\"))[\"web\"][\"token\"]))")" && echo "TUI bg avviata — token: $SIGNAL_TUI_WEB_TOKEN"'
alias web-signal-tui-stop='[ -f /tmp/signal-tui.lock ] && kill -INT "$(cat /tmp/signal-tui.lock)" 2>/dev/null; for i in $(seq 1 12); do [ ! -f /tmp/signal-tui.lock ] && break; sleep 0.5; done; tmux kill-session -t tui 2>/dev/null; sleep 0.5; [ -f /tmp/signal-tui.lock ] && rm -f /tmp/signal-tui.lock'
EOF
    ALIASES_BLOCK="${ALIASES_BLOCK//__PROJECT_DIR__/$PROJECT_DIR}"

    if [ ! -e "$rc_file" ]; then
        : > "$rc_file" || return 1
    fi

    local tmp_file
    tmp_file="$(mktemp "${rc_file}.tmp.XXXXXX")" || return 1
    local line in_block=0 replaced=0

    while IFS= read -r line || [ -n "$line" ]; do
        if [ "$line" = "$begin_marker" ]; then
            if [ "$replaced" -eq 0 ]; then
                printf '%s\n%s\n%s\n' "$begin_marker" "$ALIASES_BLOCK" "$end_marker" >> "$tmp_file"
                replaced=1
            fi
            in_block=1
        elif [ "$line" = "$end_marker" ] && [ "$in_block" -eq 1 ]; then
            in_block=0
        elif [ "$in_block" -eq 0 ]; then
            printf '%s\n' "$line" >> "$tmp_file"
        fi
    done < "$rc_file"

    if [ "$in_block" -eq 1 ]; then
        rm -f "$tmp_file"
        warn "Marcatore END mancante in $rc_file; file lasciato invariato."
        return 1
    fi

    if [ "$replaced" -eq 0 ]; then
        printf '\n%s\n%s\n%s\n' "$begin_marker" "$ALIASES_BLOCK" "$end_marker" >> "$tmp_file"
    fi

    if ! command cat "$tmp_file" > "$rc_file"; then
        rm -f "$tmp_file"
        return 1
    fi
    rm -f "$tmp_file"
    ok "Alias shell della Web UI installati in $rc_file"
}

# ── WhatsApp helpers ───────────────────────────────────────────────────────────

WA_PORT="${WHATSAPP_API_PORT:-3005}"
WEBHOOK_PORT="${CLIENT_WEBHOOK_PORT:-8088}"

check_port() {
    local port="$1" label="$2"
    info "Controllo porta ${label} (${port})..."
    if ! ss -tlnp 2>/dev/null | grep -q ":${port}[[:space:]]"; then
        ok "  Porta ${port} disponibile"
        return 0
    fi
    local line pid pname
    line="$(ss -tlnp 2>/dev/null | grep ":${port}[[:space:]]" | head -1)"
    pid="$(echo "$line" | sed -n 's/.*pid=\([0-9]*\).*/\1/p')"
    pname="$(echo "$line" | sed -n 's/.*users:(("\([^"]*\)".*/\1/p')"
    if [ "$port" = "$WEBHOOK_PORT" ] && [ "$pname" = "python" ]; then
        ok "  Porta ${port} gia in uso (pid ${pid}, webhook server) - OK"
        return 0
    fi
    if [ -n "$pid" ]; then
        warn "  Porta ${port} gia in uso da pid ${pid} (${pname:-sconosciuto})."
    else
        warn "  Porta ${port} gia in uso (Docker o demone)."
    fi
    warn "  Cambia porta o ferma il processo se necessario."
    return 1
}

check_firewall() {
    local port="$1" label="$2"
    info "Controllo firewall per ${label} (${port})..."
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
        if ufw status 2>/dev/null | grep -q "^${port}[[:space:]].*ALLOW"; then
            ok "  ufw: porta ${port} consentita"
        else
            warn "  ufw attivo: apri la porta ${port} (ufw allow ${port})"
        fi
        return
    fi
    if command -v iptables >/dev/null 2>&1; then
        if iptables -L INPUT -n 2>/dev/null | grep -q "dpt:${port}"; then
            ok "  iptables: porta ${port} consentita"
            return
        fi
        if iptables -L INPUT -n 2>/dev/null | grep -q '^DROP'; then
            warn "  iptables policy DROP: apri la porta ${port}"
            return
        fi
    fi
    ok "  Nessun firewall restrittivo rilevato"
}

setup_whatsapp() {
    local should_start="${1:-0}"
    echo
    echo "${C_BLUE}${C_BOLD}-- WhatsApp (WAHA) ------------------------------------------------------------${C_RESET}"
    echo
    if ! command -v docker >/dev/null 2>&1; then
        err "Docker non trovato. Installa Docker per usare WhatsApp."
        return 1
    fi
    ok "Docker trovato: $(docker --version 2>/dev/null)"
    if ! docker compose version >/dev/null 2>&1; then
        err "Docker Compose non trovato."
        return 1
    fi
    ok "Docker Compose trovato"
    check_port "$WA_PORT" "WAHA API" || true
    check_port "$WEBHOOK_PORT" "webhook" || true
    check_firewall "$WA_PORT" "WAHA API"
    check_firewall "$WEBHOOK_PORT" "webhook"
    if [ "$should_start" -eq 1 ]; then
        echo
        info "Avvio WAHA via Docker Compose..."
        docker compose -f "$PROJECT_DIR/docker-compose.yml" up -d
        ok "WAHA avviato. API: http://127.0.0.1:${WA_PORT}"
        echo
        info "In attesa che WAHA sia pronto..."
        for i in $(seq 1 30); do
            if curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${WA_PORT}/api/sessions" 2>/dev/null | grep -q '^[23]'; then
                ok "WAHA pronto! (dopo ${i}s)"
                break
            fi
            sleep 1
        done
    fi
    echo
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

    if [ "$DO_WEB" -eq 1 ]; then
        "$pip_cmd" install -r "$PROJECT_DIR/requirements-web.txt" || warn "Dipendenze Web UI non installate; la Web UI resterà disabilitata. Riprovare: $pip_cmd install -r requirements-web.txt"
    else
        info "Dipendenze Web UI saltate (--no-web)."
    fi

    if [ "$DO_VENV" -eq 1 ]; then
        ok "Dipendenze installate nel virtualenv. Attivalo con:"
        echo "    source $PROJECT_DIR/.venv/bin/activate"
    else
        ok "Dipendenze installate nel Python di sistema."
    fi
}

if [ "$DO_ALIASES_ONLY" -eq 1 ]; then
    info "Aggiunta alias web…"
    install_aliases
    exit $?
fi

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

# 5.5 WhatsApp — check prerequisiti o avvio
if [ "$DO_CHECK_WHATSAPP" -eq 1 ]; then
    setup_whatsapp 0
elif [ "$DO_WHATSAPP" -eq 1 ]; then
    setup_whatsapp 1
fi

# 5.6 Alias shell della Web UI
install_aliases || warn "Installazione degli alias shell non riuscita; l'installazione continua."

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
    echo "  3. Collega WhatsApp (QR code):"
    if [ "$DO_VENV" -eq 1 ]; then
        echo "       source .venv/bin/activate"
        echo "       python3 link_whatsapp.py"
    else
        echo "       python3 link_whatsapp.py"
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
if [ "$DO_WHATSAPP" -eq 0 ] && [ "$DO_CHECK_WHATSAPP" -eq 0 ]; then
    if command -v docker >/dev/null 2>&1; then
        echo "${C_YELLOW}Suggerimento:${C_RESET} Hai Docker installato."
        echo "  Per usare anche WhatsApp esegui:  ${C_BOLD}./install.sh --whatsapp${C_RESET}"
        echo "  Per solo verificare i prerequisiti: ${C_BOLD}./install.sh --check-whatsapp${C_RESET}"
        echo
    fi
fi
