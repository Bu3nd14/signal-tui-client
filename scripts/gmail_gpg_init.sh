#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/signal-tui"
PLAIN="$CONFIG_DIR/gmail-app-password"
ENC="$CONFIG_DIR/gmail-app-password.gpg"
ENV_FILE="$PROJECT_DIR/.env"
CACHE_TTL=604800
CONFIGURE_AGENT_CACHE=1

usage() {
    echo "Uso: $(basename "$0") [--cache-ttl SECONDI] [--no-agent-cache]"
    echo
    echo "Cifra la Gmail App Password con GPG (AES-256) e configura la cache di gpg-agent."
    echo
    echo "  --cache-ttl SECONDI   Durata della cache gpg-agent (default: 604800 = 7 giorni)"
    echo "  --no-agent-cache      Non modificare gpg-agent.conf"
    echo "  -h, --help            Mostra questo aiuto"
}

setup_agent_cache() {
    local gnupg_home="${GNUPGHOME:-$HOME/.gnupg}"
    local conf tmp mode display days

    GNUPGHOME="$gnupg_home"
    export GNUPGHOME
    mkdir -p "$GNUPGHOME"
    chmod 700 "$GNUPGHOME"

    conf="$GNUPGHOME/gpg-agent.conf"
    tmp="$(mktemp "$GNUPGHOME/.gpg-agent.conf.XXXXXX")"

    if [[ -f "$conf" ]]; then
        grep -v -E '^[[:space:]]*(default-cache-ttl|max-cache-ttl)([[:space:]]|$)' "$conf" > "$tmp" || true
    fi

    if [[ -s "$tmp" ]] && [[ -n "$(tail -c 1 "$tmp" 2>/dev/null)" ]]; then
        printf '\n' >> "$tmp"
    fi

    printf 'default-cache-ttl %s\nmax-cache-ttl %s\n' "$CACHE_TTL" "$CACHE_TTL" >> "$tmp"

    if [[ -f "$conf" ]]; then
        if mode="$(stat -c '%a' "$conf" 2>/dev/null)"; then
            :
        else
            mode="$(stat -f '%Lp' "$conf" 2>/dev/null || echo 600)"
        fi
        chmod "$mode" "$tmp" 2>/dev/null || chmod 600 "$tmp"
    else
        chmod 600 "$tmp"
    fi

    mv "$tmp" "$conf"
    gpgconf --kill gpg-agent 2>/dev/null || true

    days="$(awk -v t="$CACHE_TTL" 'BEGIN { d = t / 86400; if (d == int(d)) printf "%d", d; else printf "%.2f", d }')"
    display="$conf"
    case "$display" in
        "$HOME"/*) display="~/${display#"$HOME"/}" ;;
    esac
    echo "Cache gpg-agent: $days giorni ($CACHE_TTL s) — $display"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cache-ttl)
            if [[ $# -lt 2 ]]; then
                echo "Errore: --cache-ttl richiede un valore in secondi." >&2
                exit 1
            fi
            CACHE_TTL="$2"
            shift 2
            ;;
        --cache-ttl=*)
            CACHE_TTL="${1#*=}"
            shift
            ;;
        --no-agent-cache)
            CONFIGURE_AGENT_CACHE=0
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Errore: opzione sconosciuta: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [[ ! "$CACHE_TTL" =~ ^[0-9]+$ ]] || [[ "$CACHE_TTL" =~ ^0+$ ]]; then
    echo "Errore: --cache-ttl deve essere un numero positivo di secondi." >&2
    exit 1
fi

if ! command -v gpg >/dev/null 2>&1; then
    echo "Errore: gpg non trovato nel PATH. Installa GnuPG (su macOS: brew install gnupg)." >&2
    exit 1
fi

if [[ ! -s "$PLAIN" ]]; then
    echo "File in chiaro non trovato: $PLAIN" >&2
    echo "Crea prima il file con la App Password (permessi 600)." >&2
    exit 1
fi

if [[ -e "$ENC" ]]; then
    read -r -p "Esiste già $ENC. Sovrascrivere? [y/N] " reply
    [[ "$reply" =~ ^[yY]$ ]] || exit 1
fi

if [[ "$CONFIGURE_AGENT_CACHE" -eq 1 ]]; then
    if command -v gpgconf >/dev/null 2>&1; then
        setup_agent_cache
    else
        echo "Warning: gpgconf non trovato; salto la configurazione della cache di gpg-agent." >&2
    fi
fi

GPG_TTY="$(tty 2>/dev/null || true)"
export GPG_TTY

umask 077
gpg --symmetric --cipher-algo AES256 --output "$ENC" "$PLAIN"
chmod 600 "$ENC"

if command -v shred >/dev/null 2>&1; then
    shred -u "$PLAIN" || rm -f "$PLAIN"
    echo "Plaintext eliminato con shred (sovrascritto)."
else
    rm -f "$PLAIN"
    echo "Plaintext rimosso con rm (su macOS shred non è disponibile: nessuna sovrascrittura)."
fi

python3 - "$ENV_FILE" "$ENC" <<'PY'
import shlex
import sys
from pathlib import Path

env_path = Path(sys.argv[1])
enc_path = Path(sys.argv[2]).expanduser()
lines = env_path.read_text().splitlines()
out = [
    line
    for line in lines
    if not line.startswith(
        (
            "GMAIL_APP_PASSWORD_FILE=",
            "GMAIL_APP_PASSWORD=",
            "GMAIL_APP_PASSWORD_CMD=",
        )
    )
]
command = "gpg --quiet --decrypt " + shlex.quote(str(enc_path))
out.append(f"GMAIL_APP_PASSWORD_CMD={command}")
env_path.write_text("\n".join(out) + "\n")
env_path.chmod(0o600)
PY

warm_ok=1
gpg --quiet --decrypt "$ENC" >/dev/null || warm_ok=0

if [[ "$warm_ok" -eq 1 ]]; then
    if ! gpg --batch --quiet --decrypt "$ENC" >/dev/null 2>&1; then
        echo
        echo "Warning: la verifica non interattiva della cache gpg-agent è fallita." >&2
        echo "Esegui manualmente in un terminale:" >&2
        echo "  gpg --quiet --decrypt \"$ENC\" >/dev/null" >&2
    fi
else
    echo
    echo "Warning: impossibile scaldare la cache gpg-agent (passphrase non disponibile)." >&2
    echo "Esegui manualmente in un terminale:" >&2
    echo "  gpg --quiet --decrypt \"$ENC\" >/dev/null" >&2
fi

echo
echo "Fatto: segreto cifrato in $ENC"
echo "Il plaintext è stato eliminato e .env ora usa GMAIL_APP_PASSWORD_CMD."
echo "Su macOS non sono disponibili TPM2 né systemd: resta valida solo la cifratura GPG."
echo "Dopo un reboot o alla scadenza della cache gpg-agent esegui:"
echo "  gpg --quiet --decrypt \"$ENC\" >/dev/null"
echo "Su Linux restano disponibili scripts/gmail_tpm_init.sh e systemd-creds."
