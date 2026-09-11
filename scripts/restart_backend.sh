#!/usr/bin/env bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$PROJECT_DIR/docker-compose.yml"
COMPOSE=(docker compose -f "$COMPOSE_FILE")
WAIT=1
WA_PORT="${WHATSAPP_API_PORT:-3005}"
API_URL="${WHATSAPP_API_URL:-http://127.0.0.1:${WA_PORT}}"
API_URL="${API_URL%/}"
WAIT_TIMEOUT_SECONDS="${WAHA_API_TIMEOUT_SECONDS:-120}"
SIGNAL_DAEMON_TIMEOUT_SECONDS="${SIGNAL_DAEMON_TIMEOUT_SECONDS:-30}"
SIGNAL_RPC_URL="http://127.0.0.1:8080/api/v1/rpc"

die() { echo "❌ $*" >&2; exit 1; }
info() { echo "ℹ️  $*"; }

usage() {
    echo "Uso: $0 [--no-wait]"
}

get_signal_number() {
    if [ -n "${SIGNAL_USER_NUMBER:-}" ]; then
        printf '%s\n' "$SIGNAL_USER_NUMBER"
        return
    fi

    [ -f "$PROJECT_DIR/config.json" ] || return 0
    python3 - "$PROJECT_DIR/config.json" <<'PY' 2>/dev/null || true
import json
import sys

try:
    with open(sys.argv[1]) as config_file:
        number = json.load(config_file).get("user_number", "")
    if number:
        print(number)
except (OSError, json.JSONDecodeError):
    pass
PY
}

port_8080_is_listening() {
    local table local_address state _
    for table in /proc/net/tcp /proc/net/tcp6; do
        [ -r "$table" ] || continue
        while read -r _ local_address _ state _; do
            if [[ "$local_address" == *:1F90 && "$state" = "0A" ]]; then
                return 0
            fi
        done < "$table"
    done
    return 1
}

is_configured_signal_daemon() {
    local pid="$1" arg next_arg="" has_daemon=0 has_account=0 has_signal_cli=0
    local -a args

    [ -O "/proc/$pid" ] || return 1
    mapfile -d '' -t args < "/proc/$pid/cmdline" 2>/dev/null || return 1
    [ "${#args[@]}" -gt 0 ] || return 1

    for arg in "${args[@]}"; do
        [ "$arg" = "daemon" ] && has_daemon=1
        [[ "$arg" == *signal-cli* || "$arg" == *org.asamk.signal* ]] && has_signal_cli=1
        if [ "$next_arg" = "account" ] && [ "$arg" = "$SIGNAL_NUMBER" ]; then
            has_account=1
        fi
        case "$arg" in
            -u|--account) next_arg="account" ;;
            *) next_arg="" ;;
        esac
    done

    [ "$has_daemon" -eq 1 ] && [ "$has_account" -eq 1 ] && [ "$has_signal_cli" -eq 1 ]
}

stop_configured_signal_daemon() {
    local pid deadline found=0
    local -a pids=()

    for pid_path in /proc/[0-9]*; do
        pid="${pid_path##*/}"
        if is_configured_signal_daemon "$pid"; then
            pids+=("$pid")
            found=1
        fi
    done

    if [ "$found" -eq 0 ]; then
        info "Nessun daemon signal-cli dell'account configurato da fermare."
        return
    fi

    info "Arresto del daemon signal-cli dell'account configurato..."
    for pid in "${pids[@]}"; do
        is_configured_signal_daemon "$pid" && kill -TERM "$pid" 2>/dev/null || true
    done

    deadline=$((SECONDS + 10))
    while (( SECONDS < deadline )); do
        if ! port_8080_is_listening; then
            return 0
        fi
        sleep 1
    done

    for pid in "${pids[@]}"; do
        is_configured_signal_daemon "$pid" && kill -KILL "$pid" 2>/dev/null || true
    done
}

restart_signal_daemon() {
    local signal_cli="" candidate deadline rpc_response

    SIGNAL_NUMBER="$(get_signal_number)"
    if [ -z "$SIGNAL_NUMBER" ]; then
        info "Backend Signal non configurato (SIGNAL_USER_NUMBER o config.json[user_number]): salto signal-cli."
        return
    fi

    for candidate in "$PROJECT_DIR"/bin/signal-cli-*/bin/signal-cli; do
        if [ -x "$candidate" ]; then
            signal_cli="$candidate"
            break
        fi
    done
    if [ -z "$signal_cli" ]; then
        info "Backend Signal non disponibile (binario signal-cli non trovato sotto bin/): salto signal-cli."
        return
    fi

    [[ "$SIGNAL_DAEMON_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || die "SIGNAL_DAEMON_TIMEOUT_SECONDS deve essere un intero positivo."
    stop_configured_signal_daemon

    deadline=$((SECONDS + SIGNAL_DAEMON_TIMEOUT_SECONDS))
    while port_8080_is_listening; do
        (( SECONDS < deadline )) || die "La porta 8080 non si è liberata: non avvio signal-cli per non toccare altri servizi."
        sleep 1
    done

    info "Avvio del daemon signal-cli..."
    nohup "$signal_cli" -u "$SIGNAL_NUMBER" daemon --http 127.0.0.1:8080 --receive-mode on-connection --no-receive-stdout \
        >/dev/null 2>&1 &

    if [ "$WAIT" -eq 0 ]; then
        info "Daemon signal-cli avviato (--no-wait)."
        return
    fi

    command -v curl >/dev/null 2>&1 || die "curl non trovato: impossibile verificare signal-cli. Riprova con --no-wait oppure installa curl."
    info "Attendo il JSON-RPC signal-cli su $SIGNAL_RPC_URL (timeout: ${SIGNAL_DAEMON_TIMEOUT_SECONDS}s)..."
    deadline=$((SECONDS + SIGNAL_DAEMON_TIMEOUT_SECONDS))
    while (( SECONDS < deadline )); do
        if rpc_response="$(curl --fail --silent --show-error --connect-timeout 2 --max-time 5 \
            -H 'Content-Type: application/json' \
            --data '{"jsonrpc":"2.0","method":"listContacts","id":"restart-healthcheck"}' \
            "$SIGNAL_RPC_URL" 2>/dev/null)" \
            && [[ "$rpc_response" == *'"jsonrpc"'* && "$rpc_response" == *'"result"'* ]]; then
            echo "✅ signal-cli pronto: $SIGNAL_RPC_URL"
            return
        fi
        sleep 1
    done

    die "Timeout: signal-cli non risponde via JSON-RPC entro ${SIGNAL_DAEMON_TIMEOUT_SECONDS}s."
}

for arg in "$@"; do
    case "$arg" in
        --no-wait) WAIT=0 ;;
        --help|-h) usage; exit 0 ;;
        *) die "Opzione sconosciuta: $arg" ;;
    esac
done

restart_signal_daemon

command -v docker >/dev/null 2>&1 || die "docker non trovato. Installa Docker e riprova."
docker compose version >/dev/null 2>&1 || die "Docker Compose non disponibile. Installa il plugin Docker Compose e riprova."
[ -f "$COMPOSE_FILE" ] || die "File Compose non trovato: $COMPOSE_FILE"

services="$("${COMPOSE[@]}" config --services)" || die "Impossibile leggere i servizi dal file Compose."
has_whatsapp=0
while IFS= read -r service; do
    if [ "$service" = "whatsapp" ]; then
        has_whatsapp=1
        break
    fi
done <<< "$services"
if [ "$has_whatsapp" -ne 1 ]; then
    die "Il file Compose non definisce il servizio 'whatsapp'."
fi

if [ -n "$("${COMPOSE[@]}" ps -aq whatsapp)" ]; then
    info "Riavvio del solo servizio WAHA (whatsapp)..."
    "${COMPOSE[@]}" restart whatsapp
else
    info "Il servizio WAHA non esiste ancora: avvio del solo servizio whatsapp..."
    "${COMPOSE[@]}" up -d whatsapp
fi

if [ "$WAIT" -eq 0 ]; then
    info "Comando completato (--no-wait)."
    exit 0
fi

command -v curl >/dev/null 2>&1 || die "curl non trovato: impossibile attendere l'API. Riprova con --no-wait oppure installa curl."
[[ "$WAIT_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || die "WAHA_API_TIMEOUT_SECONDS deve essere un intero positivo."

API_KEY="${WAHA_API_KEY:-}"
if [ -z "$API_KEY" ] && [ -f "$PROJECT_DIR/.env" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        if [[ "$line" =~ ^[[:space:]]*WAHA_API_KEY= ]]; then
            API_KEY="${line#*=}"
            API_KEY="${API_KEY#"${API_KEY%%[![:space:]]*}"}"
            API_KEY="${API_KEY%"${API_KEY##*[![:space:]]}"}"
            if [[ "$API_KEY" =~ ^\"(.*)\"$ ]] || [[ "$API_KEY" =~ ^\'(.*)\'$ ]]; then
                API_KEY="${BASH_REMATCH[1]}"
            fi
            break
        fi
    done < "$PROJECT_DIR/.env"
fi

info "Attendo l'API WAHA su $API_URL/api/version (timeout: ${WAIT_TIMEOUT_SECONDS}s)..."
start_time=$SECONDS
while (( SECONDS - start_time < WAIT_TIMEOUT_SECONDS )); do
    if [ -n "$API_KEY" ]; then
        if curl --fail --silent --show-error --connect-timeout 2 --max-time 5 \
            -H "X-Api-Key: $API_KEY" "$API_URL/api/version" >/dev/null 2>&1; then
            echo "✅ WAHA pronta: $API_URL"
            exit 0
        fi
    elif curl --fail --silent --show-error --connect-timeout 2 --max-time 5 \
        "$API_URL/api/version" >/dev/null 2>&1; then
        echo "✅ WAHA pronta: $API_URL"
        exit 0
    fi
    sleep 2
done

die "Timeout: WAHA non risponde su $API_URL/api/version entro ${WAIT_TIMEOUT_SECONDS}s. Controlla: docker compose -f \"$COMPOSE_FILE\" logs whatsapp"
