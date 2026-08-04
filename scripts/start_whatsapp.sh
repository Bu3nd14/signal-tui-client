#!/usr/bin/env bash
#
# start_whatsapp.sh — Start (or ensure) the WAHA WhatsApp HTTP API via Docker
# Compose and wait until it is reachable, so the Signal TUI can attach cleanly.
#
# Usage:
#   ./scripts/start_whatsapp.sh              # start + wait
#   ./scripts/start_whatsapp.sh --no-wait    # start but don't wait
#   ./scripts/start_whatsapp.sh --stop       # stop the container
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Port defaults to 3005, configurable via WHATSAPP_API_PORT. An explicit
# WHATSAPP_API_URL override wins.
WA_PORT="${WHATSAPP_API_PORT:-3005}"
API_URL="${WHATSAPP_API_URL:-http://127.0.0.1:${WA_PORT}}"
WAIT=1

die() { echo "❌ $*" >&2; exit 1; }

for arg in "$@"; do
    case "$arg" in
        --no-wait) WAIT=0;;
        --stop)    docker compose -f "$PROJECT_DIR/docker-compose.yml" down; echo "WAHA stopped."; exit 0;;
        *)         die "Opzione sconosciuta: $arg";;
    esac
done

command -v docker >/dev/null 2>&1 || { echo "❌ docker non trovato. Installa Docker e riprova."; exit 1; }

echo "🟢 Avvio WhatsApp HTTP API (WAHA) via Docker Compose..."
docker compose -f "$PROJECT_DIR/docker-compose.yml" up -d

if [ "$WAIT" -eq 1 ]; then
    echo "⏳ Attendo che l'API risponda su $API_URL ..."
    for i in $(seq 1 60); do
        if curl -fsS "$API_URL/api/server" >/dev/null 2>&1; then
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
