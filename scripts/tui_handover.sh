#!/usr/bin/env bash
#
# tui_handover.sh — Passa il client Signal TUI da una macchina all'altra.
#
# Il client usa account Signal/WhatsApp/Telegram condivisi: la sessione WAHA
# (WhatsApp) e il daemon signal-cli NON devono essere attivi su due macchine
# contemporaneamente. Questi script centralizzano il passaggio pulito.
#
# Uso:
#   ./scripts/tui_handover.sh to-server    # spegni locale → accendi il server Hetzner
#   ./scripts/tui_handover.sh to-local     # spegni il server → accendi il locale
#   ./scripts/tui_handover.sh status       # stato TUI/WAHA/daemon su entrambe
#
# Configurazione:
#   HZ_HOST   — IP del server Hetzner (default 167.233.140.207)
#   HZ_USER   — utente SSH sul server (default root)
#   Richiede chiave SSH già installata sul server (ssh-copy-id).
#
# L'IP può cambiare (es. dopo snapshot/recreate del server): aggiorna il file
#   ~/.config/signal-tui-handover.conf  contenente  HZ_HOST=<nuovo-ip>
#   oppure esporta HZ_HOST nella shell. La password NON viene mai usata:
#   l'accesso avviene solo con la chiave SSH.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Config opzionale (~/.config/signal-tui-handover.conf) per l'IP che cambia.
CONF_FILE="${SIGNAL_TUI_HANDOVER_CONF:-$HOME/.config/signal-tui-handover.conf}"
if [ -f "$CONF_FILE" ]; then
    # shellcheck disable=SC1090
    . "$CONF_FILE"
fi

HZ_HOST="${HZ_HOST:-167.233.140.207}"
HZ_USER="${HZ_USER:-root}"
HZ="ssh -o StrictHostKeyChecking=accept-new $HZ_USER@$HZ_HOST"

# Identità della macchina: "local" = dove gira questo script.
if [ -d "$PROJECT_DIR/.git" ]; then
    MY_MACHINE="local"
else
    MY_MACHINE="unknown"
fi

die() { echo "❌ $*" >&2; exit 1; }
info() { echo "ℹ️  $*"; }
ok()   { echo "✅ $*"; }

# ─── Comandi di base (stessi su entrambe le macchine) ────────────────────────

tui_start() {
    local dir="$1"
    tmux new-session -d -s tui "cd '$dir' && .venv/bin/python -m signal_tui --web --web-port 4242 --web-host 0.0.0.0"
    sleep 2
    tmux list-sessions 2>/dev/null | grep -q "^tui:" && ok "TUI avviata (tmux 'tui') in $dir" || die "avvio TUI fallito in $dir"
}

tui_stop() {
    if [ -f /tmp/signal-tui.lock ]; then
        kill -INT "$(cat /tmp/signal-tui.lock)" 2>/dev/null || true
        for _ in $(seq 1 12); do
            [ ! -f /tmp/signal-tui.lock ] && break
            sleep 0.5
        done
    fi
    tmux kill-session -t tui 2>/dev/null || true
    sleep 0.5
    rm -f /tmp/signal-tui.lock
    ok "TUI fermata"
}

signal_daemon_stop() {
    pkill -f "signal-cli.*daemon" 2>/dev/null && ok "daemon signal-cli fermato" || info "nessun daemon signal-cli attivo"
}

waha_stop() {
    docker compose -f "$1/docker-compose.yml" down 2>/dev/null && ok "WAHA fermato" || info "WAHA non era attivo"
}

waha_start() {
    # Attende che WAHA sia pronto (con wait) così la TUI lo trova subito attivo:
    # se la TUI parte prima di WAHA il backend WhatsApp resta idle (nessun retry).
    bash "$1/scripts/start_whatsapp.sh" >/dev/null 2>&1 && ok "WAHA avviato e pronto" || {
        info "avvio WAHA non riuscito o timeout; provo comunque a far partire la TUI"
        bash "$1/scripts/start_whatsapp.sh" --no-wait >/dev/null 2>&1 || true
    }
}

# ─── Stato ────────────────────────────────────────────────────────────────────

status() {
    echo "=== LOCALE ($(hostname)) ==="
    [ -f /tmp/signal-tui.lock ] && echo "  TUI: ATTIVA (lock pid $(cat /tmp/signal-tui.lock))" || echo "  TUI: spenta"
    pgrep -f "signal-cli.*daemon" >/dev/null && echo "  daemon signal-cli: attivo" || echo "  daemon signal-cli: spento"
    docker ps --format '{{.Names}}' 2>/dev/null | grep -q "signal-tui-whatsapp" && echo "  WAHA: attivo" || echo "  WAHA: spento"
    echo "=== REMOTO ($HZ_USER@$HZ_HOST) ==="
    $HZ 'bash -s' <<'REMOTE'
        [ -f /tmp/signal-tui.lock ] && echo "  TUI: ATTIVA (lock pid $(cat /tmp/signal-tui.lock))" || echo "  TUI: spenta"
        pgrep -f "signal-cli.*daemon" >/dev/null && echo "  daemon signal-cli: attivo" || echo "  daemon signal-cli: spento"
        docker ps --format '{{.Names}}' 2>/dev/null | grep -q "signal-tui-whatsapp" && echo "  WAHA: attivo" || echo "  WAHA: spento"
REMOTE
}

# ─── Handover ─────────────────────────────────────────────────────────────────

to_server() {
    info "Fermo il client LOCALE..."
    tui_stop
    signal_daemon_stop
    waha_stop "$PROJECT_DIR"

    info "Accendo il client sul SERVER ($HZ_HOST)..."
    $HZ "cd /root/signal-tui-client && bash scripts/start_whatsapp.sh --no-wait >/dev/null 2>&1; tmux kill-session -t tui 2>/dev/null; rm -f /tmp/signal-tui.lock; true"
    $HZ 'bash -s' <<'REMOTE'
        cd /root/signal-tui-client
        bash scripts/start_whatsapp.sh --no-wait >/dev/null 2>&1 || true
        tmux new-session -d -s tui "cd /root/signal-tui-client && .venv/bin/python -m signal_tui --web --web-port 4242 --web-host 0.0.0.0"
        sleep 3
        if tmux list-sessions 2>/dev/null | grep -q "^tui:"; then
            echo "OK_TUI_SERVER"
        else
            echo "FAIL_TUI_SERVER"
        fi
REMOTE
    ok "Handover verso il server completato."
    echo
    echo "Web UI:  http://$HZ_HOST:4242  (o via tunnel Cloudflare)"
    echo "Token:   \$(python3 -c 'import json;print(json.load(open(\"/root/signal-tui-client/config.json\"))[\"web\"][\"token\"])')  — vedi config.json del server"
    echo "Log:     tail -f /tmp/signal-tui.log (sul server)"
}

to_local() {
    info "Fermo il client sul SERVER ($HZ_HOST)..."
    $HZ 'bash -s' <<'REMOTE'
        if [ -f /tmp/signal-tui.lock ]; then
            kill -INT "$(cat /tmp/signal-tui.lock)" 2>/dev/null || true
            for _ in $(seq 1 12); do [ ! -f /tmp/signal-tui.lock ] && break; sleep 0.5; done
        fi
        tmux kill-session -t tui 2>/dev/null || true
        rm -f /tmp/signal-tui.lock
        pkill -f "signal-cli.*daemon" 2>/dev/null || true
        cd /root/signal-tui-client && docker compose down >/dev/null 2>&1 || true
        echo "server spento"
REMOTE

    info "Accendo il client LOCALE..."
    waha_start "$PROJECT_DIR"
    tui_start "$PROJECT_DIR"
    ok "Handover verso il locale completato."
    echo
    echo "Web UI:  http://127.0.0.1:4242"
    echo "Token:   \$(python3 -c 'import json;print(json.load(open(\"$PROJECT_DIR/config.json\"))[\"web\"][\"token\"])')"
    echo "Log:     tail -f /tmp/signal-tui.log"
}

# ─── Main ─────────────────────────────────────────────────────────────────────

case "${1:-}" in
    to-server) to_server ;;
    to-local)  to_local ;;
    status)    status ;;
    *) die "uso: $0 {to-server|to-local|status}" ;;
esac
