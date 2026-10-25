#!/usr/bin/env bash
#
# start_on_server.sh — Avvia (o ferma) il server Signal TUI direttamente SUL SERVER.
#
# Utility lato server, pensata per il caso "disaster recovery": se la macchina
# locale è irraggiungibile, si può avviare tutto da qui (via console Hetzner o
# SSH) senza dipendere dagli script di handover che girano sul locale.
#
# Avvia `megachat serve` (headless, processo staccato, Web UI + socket Unix),
# non più la TUI in tmux. Per l'uso interattivo, sul server:
#   .venv/bin/python megachat.py     # si collega al server in esecuzione
# (Detach lascia il server attivo, Quit lo ferma).
#
# Uso (dal server, dentro /root/signal-tui-client):
#   ./scripts/start_on_server.sh start                   # WAHA + server + Web UI (default, cap CPU/RAM attivi)
#   ./scripts/start_on_server.sh start --no-docker-limits # come sopra, senza cap CPU/RAM su WAHA
#   ./scripts/start_on_server.sh stop                     # spegne server + WAHA in modo pulito
#   ./scripts/start_on_server.sh status                   # stato attuale
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export WAHA_DOCKER_LIMITS="${WAHA_DOCKER_LIMITS:-1}"
# Lock di istanza (stesso override del server e del launcher Python).
LOCK="${MEGACHAT_LOCK_FILE:-/tmp/signal-tui.lock}"
SERVE_LOG=/tmp/megachat-serve.log

info() { echo "ℹ️  $*"; }
ok()   { echo "✅ $*"; }
die()  { echo "❌ $*" >&2; exit 1; }

# pid nel lock se il processo è vivo (il lock appartiene a `megachat serve`).
lock_pid() {
    local pid=""
    [ -f "$LOCK" ] && pid="$(cat "$LOCK" 2>/dev/null || true)"
    case "$pid" in ''|*[!0-9]*) return 1 ;; esac
    kill -0 "$pid" 2>/dev/null || return 1
    echo "$pid"
}

# SIGINT al processo del lock e attesa della sua uscita (30 s). Se sopravvive
# NON tocca il lock e fallisce (mai due server). Uccide anche una vecchia
# sessione tmux 'tui' (installazioni precedenti), ma non ne avvia mai una.
stop_serve() {
    local pid
    if pid="$(lock_pid)"; then
        kill -INT "$pid" 2>/dev/null || true
        for _ in $(seq 1 60); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.5
        done
        kill -0 "$pid" 2>/dev/null && return 1
    fi
    tmux kill-session -t tui 2>/dev/null || true
    rm -f "$LOCK"
    return 0
}

start_all() {
    info "Avvio WAHA (WhatsApp HTTP API)..."
    bash "$PROJECT_DIR/scripts/start_whatsapp.sh" >/dev/null 2>&1 \
        && ok "WAHA avviato e pronto" \
        || info "WAHA non partito (vedi docker compose logs whatsapp)"

    info "Avvio il server headless (megachat serve) con Web UI su 0.0.0.0:4242..."
    # Riavvio: ferma l'istanza in corso (lock pid, SIGINT) prima di riprendere il lock.
    stop_serve || die "Il server (pid $(lock_pid)) non si ferma: non ne avvio un altro. Controlla: tail -50 /tmp/signal-tui.log"
    ( ( cd "$PROJECT_DIR" && exec nohup .venv/bin/python megachat.py serve --web-port 4242 --web-host 0.0.0.0 ) </dev/null >>"$SERVE_LOG" 2>&1 & )

    for _ in $(seq 1 30); do
        if lock_pid >/dev/null && (exec 3<>/dev/tcp/127.0.0.1/4242) 2>/dev/null; then
            ok "Server attivo — Web UI su http://$(hostname -I 2>/dev/null | awk '{print $1}'):4242"
            # Solo informativo: env var o config.json; non deve mai far fallire l'avvio.
            token="${SIGNAL_TUI_WEB_TOKEN:-$("$PROJECT_DIR/.venv/bin/python" -c 'import json,sys;print(json.load(open(sys.argv[1]))["web"]["token"])' "$PROJECT_DIR/config.json" 2>/dev/null || echo "(non configurato)")}"
            echo "   Token Web UI: $token"
            echo "   Log:          tail -f /tmp/signal-tui.log"
            echo "   Stop:         $0 stop"
            echo "   Interattivo:  .venv/bin/python megachat.py  (si collega al server)"
            return 0
        fi
        sleep 1
    done
    die "Il server non risulta attivo sulla porta 4242. Controlla: tail -50 /tmp/signal-tui.log $SERVE_LOG"
}

stop_all() {
    info "Spegnimento server (SIGINT pulito)..."
    stop_serve || die "Il server (pid $(lock_pid)) non si ferma; WAHA lasciato acceso."
    ok "Server fermato"

    info "Spegnimento WAHA..."
    docker compose -f "$PROJECT_DIR/docker-compose.yml" down >/dev/null 2>&1 \
        && ok "WAHA fermato" || info "WAHA non era attivo"
    ok "Server spento. Per riaccendere: $0 start"
}

status() {
    local pid
    if pid="$(lock_pid)"; then echo "Server: ATTIVO (pid $pid)"; else echo "Server: spento"; fi
    pgrep -f "signal-cli.*daemon" >/dev/null && echo "daemon signal-cli: attivo" || echo "daemon signal-cli: spento"
    docker ps --format '{{.Names}}' 2>/dev/null | grep -q "signal-tui-whatsapp" \
        && echo "WAHA: attivo" || echo "WAHA: spento"
    (exec 3<>/dev/tcp/127.0.0.1/4242) 2>/dev/null && echo "Web UI: in ascolto su 4242" || echo "Web UI: spenta"
}

case "${2:-}" in
    --no-docker-limits) WAHA_DOCKER_LIMITS=0 ;;
    "") ;;
    *) die "Opzione sconosciuta: $2" ;;
esac

case "${1:-start}" in
    start)  start_all ;;
    stop)   stop_all ;;
    status) status ;;
    *) die "uso: $0 {start|stop|status} [--no-docker-limits]" ;;
esac
