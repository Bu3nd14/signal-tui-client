#!/usr/bin/env bash
#
# start_whatsapp.sh — Start (or ensure) the WAHA WhatsApp HTTP API via Docker
# Compose and wait until it is reachable, so the Signal TUI can attach cleanly.
#
# Usage:
#   ./scripts/start_whatsapp.sh                    # start + wait (CPU/RAM caps applied)
#   ./scripts/start_whatsapp.sh --no-wait          # start but don't wait
#   ./scripts/start_whatsapp.sh --no-docker-limits # start without CPU/RAM caps
#   ./scripts/start_whatsapp.sh --stop             # stop the container
#
# WAHA_DOCKER_LIMITS=0 in the environment has the same effect as
# --no-docker-limits (used by scripts that delegate to this one, e.g.
# start_on_server.sh / tui_handover.sh).
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Port defaults to 3005, configurable via WHATSAPP_API_PORT. An explicit
# WHATSAPP_API_URL override wins.
WA_PORT="${WHATSAPP_API_PORT:-3005}"
API_URL="${WHATSAPP_API_URL:-http://127.0.0.1:${WA_PORT}}"
WAIT=1
DOCKER_LIMITS="${WAHA_DOCKER_LIMITS:-1}"

die() { echo "❌ $*" >&2; exit 1; }

compose_args() {
    local args=(-f "$PROJECT_DIR/docker-compose.yml")
    if [ "$DOCKER_LIMITS" -eq 1 ]; then
        args+=(-f "$PROJECT_DIR/docker-compose.resources.yml")
    fi
    printf '%s\n' "${args[@]}"
}

for arg in "$@"; do
    case "$arg" in
        --no-wait)          WAIT=0;;
        --no-docker-limits) DOCKER_LIMITS=0;;
        --stop)
            mapfile -t stop_args < <(compose_args)
            docker compose "${stop_args[@]}" down
            echo "WAHA stopped."
            exit 0
            ;;
        *)                  die "Opzione sconosciuta: $arg";;
    esac
done

command -v docker >/dev/null 2>&1 || { echo "❌ docker non trovato. Installa Docker e riprova."; exit 1; }

mapfile -t COMPOSE_ARGS < <(compose_args)
if [ "$DOCKER_LIMITS" -eq 1 ]; then
    echo "🟢 Avvio WhatsApp HTTP API (WAHA) via Docker Compose (cap CPU/RAM attivi)..."
else
    echo "🟢 Avvio WhatsApp HTTP API (WAHA) via Docker Compose (--no-docker-limits: nessun cap)..."
fi
docker compose "${COMPOSE_ARGS[@]}" up -d

if [ "$WAIT" -eq 1 ]; then
    # Carica la chiave API dal .env (stessa che docker-compose passa al container)
    # così l'healthcheck autentica correttamente e non ottiene 401.  Usiamo
    # /api/version (esiste su tutte le build "core"); /api/server non è più
    # esposto su quelle recenti (404).
    WA_API_KEY="${WAHA_API_KEY:-}"
    if [ -f "$PROJECT_DIR/.env" ]; then
        WA_API_KEY="$(grep -E '^WAHA_API_KEY=' "$PROJECT_DIR/.env" | head -1 | cut -d= -f2- | tr -d '[:space:]')"
    fi

    echo "⏳ Attendo che l'API risponda su $API_URL ..."
    for i in $(seq 1 60); do
        if [ -n "$WA_API_KEY" ]; then
            ok=$(curl -fsS -H "X-Api-Key: $WA_API_KEY" "$API_URL/api/version" >/dev/null 2>&1 && echo 1 || echo 0)
        else
            ok=$(curl -fsS "$API_URL/api/version" >/dev/null 2>&1 && echo 1 || echo 0)
        fi
        if [ "$ok" -eq 1 ]; then
            echo "✅ WhatsApp HTTP API pronta: $API_URL"
            exit 0
        fi
        sleep 2
    done
    echo "❌ Timeout: l'API non risponde su $API_URL dopo ~120s." >&2
    echo "   Controlla: docker compose -f \"$PROJECT_DIR/docker-compose.yml\" logs whatsapp"
    exit 1
fi
echo "▶️  Comando di avvio inviato (--no-wait)."
