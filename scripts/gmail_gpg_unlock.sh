#!/usr/bin/env bash
# Warm gpg-agent's passphrase cache for the encrypted Gmail App Password.
# Run after a reboot (or when the cache expires): gpg prompts for the
# passphrase once and the TUI can then decrypt the secret on its own.
set -euo pipefail

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/signal-tui"
ENC="$CONFIG_DIR/gmail-app-password.gpg"

if [[ ! -s "$ENC" ]]; then
    echo "File cifrato non trovato: $ENC" >&2
    exit 1
fi

GPG_TTY="$(tty 2>/dev/null || true)"
export GPG_TTY
gpg --quiet --decrypt "$ENC" >/dev/null
echo "Cache gpg-agent aggiornata."
