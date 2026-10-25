#!/usr/bin/env bash
#
# tui_handover.sh — Passa il client Signal TUI da una macchina all'altra.
#
# Il client usa account Signal/WhatsApp/Telegram condivisi: la sessione WAHA
# (WhatsApp) e il daemon signal-cli NON devono essere attivi su due macchine
# contemporaneamente. Questi script centralizzano il passaggio pulito.
#
# Uso:
#   ./scripts/tui_handover.sh to-server [--no-docker-limits]  # spegni locale → accendi il server Hetzner
#   ./scripts/tui_handover.sh to-local  [--no-docker-limits]  # spegni il server → accendi il locale
#   ./scripts/tui_handover.sh status                          # stato server/WAHA/daemon su entrambe
#
# Il "client" che si sposta è il server headless (`megachat serve`, processo
# staccato con Web UI su 0.0.0.0:4242 e socket Unix). Dopo l'handover, sulla
# macchina di destinazione la TUI si apre con `.venv/bin/python megachat.py`
# (si collega al server in esecuzione; Detach lo lascia attivo, Quit lo ferma).
#
# --no-docker-limits avvia WAHA (sulla macchina di destinazione) senza i cap
# CPU/RAM di docker-compose.resources.yml (applicati di default).
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
DOCKER_LIMITS=1
# Lock di istanza del server locale (stesso override del launcher Python).
LOCK="${MEGACHAT_LOCK_FILE:-/tmp/signal-tui.lock}"
SERVE_LOG=/tmp/megachat-serve.log

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

# pid nel lock se il processo è vivo (il lock appartiene a `megachat serve`).
lock_pid() {
    local pid=""
    [ -f "$LOCK" ] && pid="$(cat "$LOCK" 2>/dev/null || true)"
    case "$pid" in ''|*[!0-9]*) return 1 ;; esac
    kill -0 "$pid" 2>/dev/null || return 1
    echo "$pid"
}

# Avvia `megachat serve` staccato (Web UI su 0.0.0.0:4242, socket attivo) e
# attende che la porta risponda. Sul server lo stesso comando è nello script
# remoto di to_server (e in launcher/handover.py).
tui_start() {
    local dir="$1"
    ( cd "$dir" && nohup .venv/bin/python megachat.py serve --web-port 4242 --web-host 0.0.0.0 </dev/null >>"$SERVE_LOG" 2>&1 & )
    for _ in $(seq 1 30); do
        if lock_pid >/dev/null && (exec 3<>/dev/tcp/127.0.0.1/4242) 2>/dev/null; then
            ok "Server avviato (megachat serve) in $dir"
            return 0
        fi
        sleep 1
    done
    die "avvio server fallito in $dir — controlla: tail -50 /tmp/signal-tui.log $SERVE_LOG"
}

# SIGINT al processo del lock e attesa della sua uscita (30 s). Se sopravvive
# NON tocca il lock (mai due server). Uccide anche una vecchia sessione tmux
# 'tui' (installazioni precedenti), ma non ne avvia mai una.
tui_stop() {
    local pid
    if pid="$(lock_pid)"; then
        kill -INT "$pid" 2>/dev/null || true
        for _ in $(seq 1 60); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.5
        done
        if kill -0 "$pid" 2>/dev/null; then
            die "il server (pid $pid) non si ferma: handover interrotto"
        fi
    fi
    tmux kill-session -t tui 2>/dev/null || true
    rm -f "$LOCK"
    ok "Server fermato"
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
    WAHA_DOCKER_LIMITS="$DOCKER_LIMITS" bash "$1/scripts/start_whatsapp.sh" >/dev/null 2>&1 \
        && ok "WAHA avviato e pronto" || {
        info "avvio WAHA non riuscito o timeout; provo comunque a far partire il server"
        WAHA_DOCKER_LIMITS="$DOCKER_LIMITS" bash "$1/scripts/start_whatsapp.sh" --no-wait >/dev/null 2>&1 || true
    }
}

# ─── Stato ────────────────────────────────────────────────────────────────────

status() {
    echo "=== LOCALE ($(hostname)) ==="
    local pid
    if pid="$(lock_pid)"; then echo "  Server: ATTIVO (lock pid $pid)"; else echo "  Server: spento"; fi
    pgrep -f "signal-cli.*daemon" >/dev/null && echo "  daemon signal-cli: attivo" || echo "  daemon signal-cli: spento"
    docker ps --format '{{.Names}}' 2>/dev/null | grep -q "signal-tui-whatsapp" && echo "  WAHA: attivo" || echo "  WAHA: spento"
    echo "=== REMOTO ($HZ_USER@$HZ_HOST) ==="
    $HZ 'bash -s' <<'REMOTE'
        LOCK=/tmp/signal-tui.lock
        pid="$(cat "$LOCK" 2>/dev/null || true)"
        case "$pid" in ''|*[!0-9]*) pid="" ;; esac
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then echo "  Server: ATTIVO (lock pid $pid)"; else echo "  Server: spento"; fi
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
    # Script remoto (lock fisso /tmp/signal-tui.lock; il solo `rm -f` è sul lock).
    # Esce 0 con OK_TUI_SERVER / FAIL_TUI_SERVER; il server è staccato (nohup).
    out="$($HZ "WAHA_DOCKER_LIMITS=$DOCKER_LIMITS bash -s" <<'REMOTE'
        LOCK=/tmp/signal-tui.lock
        lock_pid() {
            local pid=""
            [ -f "$LOCK" ] && pid="$(cat "$LOCK" 2>/dev/null || true)"
            case "$pid" in ''|*[!0-9]*) return 1 ;; esac
            kill -0 "$pid" 2>/dev/null || return 1
            echo "$pid"
        }
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
        cd /root/signal-tui-client || { echo FAIL_TUI_SERVER; exit 0; }
        bash scripts/start_whatsapp.sh --no-wait >/dev/null 2>&1 || true
        stop_serve || { echo FAIL_TUI_SERVER; exit 0; }
        ( nohup .venv/bin/python megachat.py serve --web-port 4242 --web-host 0.0.0.0 </dev/null >>/tmp/megachat-serve.log 2>&1 & )
        for _ in $(seq 1 30); do
            if lock_pid >/dev/null && (exec 3<>/dev/tcp/127.0.0.1/4242) 2>/dev/null; then
                echo OK_TUI_SERVER
                exit 0
            fi
            sleep 1
        done
        echo FAIL_TUI_SERVER
REMOTE
)"
    echo "$out"
    case "$out" in
        *OK_TUI_SERVER*) ;;
        *) die "Avvio del server sul server remoto non riuscito." ;;
    esac
    ok "Handover verso il server completato."
    echo
    echo "Web UI:  http://$HZ_HOST:4242  (o via tunnel Cloudflare)"
    echo "Token:   \$(python3 -c 'import json;print(json.load(open(\"/root/signal-tui-client/config.json\"))[\"web\"][\"token\"])')  — vedi config.json del server"
    echo "Log:     tail -f /tmp/signal-tui.log (sul server)"
    echo "TUI:     sul server, da /root/signal-tui-client: .venv/bin/python megachat.py  (si collega al server in esecuzione)"
}

to_local() {
    info "Fermo il client sul SERVER ($HZ_HOST)..."
    # Se il server remoto non si ferma (o ssh fallisce) set -e interrompe: mai due server.
    $HZ 'bash -s' <<'REMOTE'
        LOCK=/tmp/signal-tui.lock
        pid="$(cat "$LOCK" 2>/dev/null || true)"
        case "$pid" in ''|*[!0-9]*) pid="" ;; esac
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill -INT "$pid" 2>/dev/null || true
            for _ in $(seq 1 60); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
            if kill -0 "$pid" 2>/dev/null; then
                echo "server remoto non si ferma (pid $pid)" >&2
                exit 3
            fi
        fi
        tmux kill-session -t tui 2>/dev/null || true
        rm -f "$LOCK"
        pkill -f "signal-cli.*daemon" 2>/dev/null || true
        cd /root/signal-tui-client && docker compose down >/dev/null 2>&1 || true
        echo "server spento"
REMOTE

    info "Accendo il client LOCALE..."
    waha_start "$PROJECT_DIR"
    tui_stop
    tui_start "$PROJECT_DIR"
    ok "Handover verso il locale completato."
    echo
    echo "Web UI:  http://127.0.0.1:4242"
    echo "Token:   \$(python3 -c 'import json;print(json.load(open(\"$PROJECT_DIR/config.json\"))[\"web\"][\"token\"])')"
    echo "Log:     tail -f /tmp/signal-tui.log"
    echo "TUI:     .venv/bin/python megachat.py  (si collega al server in esecuzione)"
}

# ─── Main ─────────────────────────────────────────────────────────────────────

case "${2:-}" in
    --no-docker-limits) DOCKER_LIMITS=0 ;;
    "") ;;
    *) die "Opzione sconosciuta: $2" ;;
esac

case "${1:-}" in
    to-server) to_server ;;
    to-local)  to_local ;;
    status)    status ;;
    *) die "uso: $0 {to-server|to-local|status} [--no-docker-limits]" ;;
esac
