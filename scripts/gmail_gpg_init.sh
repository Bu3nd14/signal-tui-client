#!/usr/bin/env bash
# Encrypt the Gmail App Password with GPG (symmetric, AES-256) and point
# GMAIL_APP_PASSWORD_CMD at the encrypted file.
#
# Run it from a terminal: gpg prompts for the passphrase (twice).
# The passphrase is never written to disk by this script.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/signal-tui"
PLAIN="$CONFIG_DIR/gmail-app-password"
ENC="$CONFIG_DIR/gmail-app-password.gpg"
ENV_FILE="$PROJECT_DIR/.env"

if [[ ! -s "$PLAIN" ]]; then
    echo "File in chiaro non trovato: $PLAIN" >&2
    echo "Crea prima il file con la App Password (permessi 600)." >&2
    exit 1
fi

if [[ -e "$ENC" ]]; then
    read -r -p "Esiste già $ENC. Sovrascrivere? [y/N] " reply
    [[ "$reply" =~ ^[yY]$ ]] || exit 1
fi

umask 077
gpg --symmetric --cipher-algo AES256 --output "$ENC" "$PLAIN"
chmod 600 "$ENC"
shred -u "$PLAIN" || rm -f "$PLAIN"

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
    if not line.startswith(("GMAIL_APP_PASSWORD_FILE=", "GMAIL_APP_PASSWORD_CMD="))
]
command = "gpg --quiet --decrypt " + shlex.quote(str(enc_path))
out.append(f"GMAIL_APP_PASSWORD_CMD={command}")
env_path.write_text("\n".join(out) + "\n")
env_path.chmod(0o600)
PY

# Warm gpg-agent's passphrase cache so the TUI can decrypt without a prompt.
gpg --quiet --decrypt "$ENC" >/dev/null

echo
echo "Fatto: segreto cifrato in $ENC"
echo "Il plaintext è stato eliminato e .env ora usa GMAIL_APP_PASSWORD_CMD."
echo "Dopo un reboot (o alla scadenza della cache gpg-agent) esegui:"
echo "  gpg --quiet --decrypt \"$ENC\" >/dev/null"
