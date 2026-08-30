#!/usr/bin/env bash
#
# start_on_server.sh — Avvia (o ferma) il client Signal TUI direttamente SUL SERVER.
#
# Utility lato server, pensata per il caso "disaster recovery": se la macchina
# locale è irraggiungibile, si può avviare tutto da qui (via console Hetzner o
# SSH) senza dipendere dagli script di handover che girano sul locale.
#
# Uso (dal server, dentro /root/signal-tui-client):
#   ./scripts/start_on_server.sh start   # WAHA + TUI + Web UI (default)
#   ./scripts/start_on_server.sh stop    # spegne TUI + WAHA in modo pulito
#   ./scripts/start_on_server.sh status  # stato attuale
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

info() { echo "ℹ️  $*"; }
ok()   { echo "✅ $*"; }
die()  { echo "❌ $*" >&2; exit 1; }

start_all() {
    info "Avvio WAHA (WhatsApp HTTP API)..."
    bash "$PROJECT_DIR/scripts/start_whatsapp.sh" >/dev/null 2>&1 \
        && ok "WAHA avviato e pronto" \
        || info "WAHA non partito (vedi docker compose logs whatsapp)"

    info "Avvio la TUI in tmux (sessione 'tui') con Web UI su 0.0.0.0:4242..."
    tmux kill-session -t tui 2>/dev/null || true
    rm -f /tmp/signal-tui.lock
    tmux new-session -d -s tui \
        "cd '$PROJECT_DIR' && .venv/bin/python -m signal_tui --web --web-port 4242 --web-host 0.0.0.0"

    sleep 12
    if tmux list-sessions 2>/dev/null | grep -q "^tui:" && ss -tlnp 2>/dev/null | grep -q ":4242"; then
        ok "TUI attiva — Web UI su http://$(hostname -I | awk '{print $1}'):4242"
        token="$("$PROJECT_DIR/.venv/bin/python" -c 'import json;print(json.load(open("'$PROJECT_DIR'/config.json"))["web"]["token"])')"
        echo "   Token Web UI: $token"
        echo "   Log:          tail -f /tmp/signal-tui.log"
        echo "   Stop:         $0 stop"
    else
        die "La TUI non risulta attiva sulla porta 4242. Controlla: tail -50 /tmp/signal-tui.log"
    fi
}

stop_all() {
    info "Spegnimento TUI (SIGINT pulito)..."
    if [ -f /tmp/signal-tui.lock ]; then
        kill -INT "$(cat /tmp/signal-tui.lock)" 2>/dev/null || true
        for _ in $(seq 1 12); do
            [ ! -f /tmp/signal-tui.lock ] && break
            sleep 0.5
        done
    fi
    tmux kill-session -t tui 2>/dev/null || true
    rm -f /tmp/signal-tui.lock
    ok "TUI fermata"

    info "Spegnimento WAHA..."
    docker compose -f "$PROJECT_DIR/docker-compose.yml" down >/dev/null 2>&1 \
        && ok "WAHA fermato" || info "WAHA non era attivo"
    ok "Server spento. Per riaccendere: $0 start"
}

status() {
    [ -f /tmp/signal-tui.lock ] && echo "TUI: ATTIVA (pid $(cat /tmp/signal-tui.lock))" || echo "TUI: spenta"
    pgrep -f "signal-cli.*daemon" >/dev/null && echo "daemon signal-cli: attivo" || echo "daemon signal-cli: spento"
    docker ps --format '{{.Names}}' 2>/dev/null | grep -q "signal-tui-whatsapp" \
        && echo "WAHA: attivo" || echo "WAHA: spento"
    ss -tlnp 2>/dev/null | grep -q ":4242" && echo "Web UI: in ascolto su 4242" || echo "Web UI: spenta"
}

case "${1:-start}" in
    start)  start_all ;;
    stop)   stop_all ;;
    status) status ;;
    *) die "uso: $0 {start|stop|status}" ;;
esac
