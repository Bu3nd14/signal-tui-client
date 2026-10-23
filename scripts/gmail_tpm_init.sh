#!/usr/bin/env bash
# Migrate the Gmail App Password from the GPG-encrypted file to a TPM2-sealed
# credential, then point GMAIL_APP_PASSWORD_CMD at systemd-creds.
#
# Requirements:
#   - access to /dev/tpmrm0 (user in the "tss" group)
#   - the existing GPG file and a warm gpg-agent cache (or a TTY for pinentry)
#
# Run from a terminal: bash scripts/gmail_tpm_init.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/signal-tui"
GPG_FILE="$CONFIG_DIR/gmail-app-password.gpg"
CRED_FILE="$CONFIG_DIR/gmail-app-password.cred"
ENV_FILE="$PROJECT_DIR/.env"

if [[ ! -r /dev/tpmrm0 ]]; then
    echo "TPM non accessibile (/dev/tpmrm0)." >&2
    echo "Esegui una volta: sudo usermod -aG tss $USER" >&2
    echo "Poi rifai il login (o usa: sg tss -c 'bash scripts/gmail_tpm_init.sh')." >&2
    exit 1
fi

if ! systemd-creds has-tpm2 >/dev/null; then
    echo "systemd-creds non rileva un TPM2 utilizzabile." >&2
    exit 1
fi

if [[ ! -s "$GPG_FILE" ]]; then
    echo "File GPG non trovato: $GPG_FILE" >&2
    exit 1
fi

umask 077

# Decrypt GPG straight into systemd-creds: no plaintext file on disk.
if ! gpg --quiet --decrypt "$GPG_FILE" \
    | systemd-creds encrypt --with-key=tpm2 --name=gmail-app-password - "$CRED_FILE"; then
    echo "Cifratura TPM fallita." >&2
    rm -f "$CRED_FILE"
    exit 1
fi

chmod 600 "$CRED_FILE"

secret_len="$(
    systemd-creds decrypt --name=gmail-app-password "$CRED_FILE" - 2>/dev/null \
        | tr -d '\n' \
        | wc -c
)"
if [[ "$secret_len" -lt 8 ]]; then
    echo "Verifica TPM fallita: il segreto decifrato è troppo corto." >&2
    exit 1
fi

python3 - "$ENV_FILE" "$CRED_FILE" <<'PY'
import shlex
import sys
from pathlib import Path

env_path = Path(sys.argv[1])
cred_path = Path(sys.argv[2]).expanduser()
lines = env_path.read_text().splitlines()
out = [
    line
    for line in lines
    if not line.startswith(("GMAIL_APP_PASSWORD_FILE=", "GMAIL_APP_PASSWORD_CMD="))
]
command = (
        "systemd-creds decrypt --name=gmail-app-password "
        + shlex.quote(str(cred_path))
        + " -"
    )
out.append(f"GMAIL_APP_PASSWORD_CMD={command}")
env_path.write_text("\n".join(out) + "\n")
env_path.chmod(0o600)
PY

echo "Fatto: credenziale TPM in $CRED_FILE"
echo "Il file GPG è rimasto come backup: $GPG_FILE"
echo ".env ora usa GMAIL_APP_PASSWORD_CMD con systemd-creds."
echo "Riavvia la TUI (in una sessione con il gruppo tss) per applicare il cambio."
