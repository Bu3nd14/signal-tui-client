"""Web reader shell aliases — install/refresh into the user's shell rc file.

Ported from ``install.sh``'s ``install_aliases()``; see docs/ALIASES.md.
"""

from __future__ import annotations

import os
from pathlib import Path

from .common import PROJECT_DIR, ok, warn

BEGIN_MARKER = "# ── BEGIN signal-tui aliases ──"
END_MARKER = "# ── END signal-tui aliases ──"

# Kept byte-for-byte identical to the block install.sh used to write, so an
# existing installation is unaffected by the bash → Python switch.
_ALIASES_TEMPLATE = r"""# ─── Signal TUI Client: web reader + background via tmux ─────────────────
# Web su 0.0.0.0:4242. Token Bearer: config.json (web.token) o SIGNAL_TUI_WEB_TOKEN.
# web-signal-tui-bg stampa ed esporta il token (fast cycle: curl + login Web UI).
SIGNAL_TUI_DIR="__PROJECT_DIR__"
alias web-signal-tui='( cd "$SIGNAL_TUI_DIR" && .venv/bin/python -m signal_tui --web --web-port 4242 --web-host 0.0.0.0 )'
_signal_tui_web_bg() {
    if ! tmux has-session -t tui 2>/dev/null; then
        tmux new-session -d -s tui "cd \"$SIGNAL_TUI_DIR\" && exec .venv/bin/python -m signal_tui" || return 1
    fi
    SIGNAL_TUI_WEB_TOKEN="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["web"]["token"])' "$SIGNAL_TUI_DIR/config.json")" || return 1
    export SIGNAL_TUI_WEB_TOKEN
    echo "TUI bg attiva — token: $SIGNAL_TUI_WEB_TOKEN"
}
_signal_tui_web_stop() {
    if [ -f /tmp/signal-tui.lock ]; then
        kill -INT "$(cat /tmp/signal-tui.lock)" 2>/dev/null || true
    fi
    for i in $(seq 1 12); do
        [ ! -f /tmp/signal-tui.lock ] && break
        sleep 0.5
    done
    tmux kill-session -t tui 2>/dev/null || true
    rm -f /tmp/signal-tui.lock
    echo "TUI bg fermata."
}
alias web-signal-tui-bg='_signal_tui_web_bg'
alias web-signal-tui-stop='_signal_tui_web_stop'
alias signal-tui-bg='_signal_tui_web_bg'
alias signal-tui-stop='_signal_tui_web_stop'
"""


def _rc_file_for_shell() -> Path | None:
    shell_name = Path(os.environ.get("SHELL", "")).name
    # Order matters: "ash" is a substring of "bash", so bash must be checked
    # first (mirrors the bash `case` statement's arm order, which relied on
    # first-match-wins for the same reason).
    if "zsh" in shell_name:
        return Path.home() / ".zshrc"
    if "bash" in shell_name or shell_name == "":
        return Path.home() / ".bashrc"
    if "ash" in shell_name:
        return Path.home() / ".ashrc"
    warn(
        f"Shell '{shell_name}' non supportata: gli alias richiedono bash/zsh/ash; "
        "fish richiederebbe funzioni (vedi docs/ALIASES.md)."
    )
    return None


def install_aliases() -> bool:
    """Idempotently (re)write the aliases block into the shell rc file.

    Returns ``True`` on success or on a graceful "unsupported shell" skip
    (matching bash's ``return 0``); ``False`` only on a real write failure.
    """
    rc_file = _rc_file_for_shell()
    if rc_file is None:
        return True

    if rc_file.name == ".ashrc" and os.environ.get("ENV", "") != str(rc_file):
        warn(f"ash legge gli alias da $ENV, non automaticamente da {rc_file}.")
        warn(
            f"Assicurati che $ENV punti a {rc_file} "
            "(tipicamente esportato da /etc/profile su Alpine)."
        )

    project_dir_str = str(PROJECT_DIR)
    if any(ch.isspace() for ch in project_dir_str):
        warn(
            "Il path del progetto contiene spazi; verifica il quoting degli "
            f"alias dopo l'installazione: {project_dir_str}"
        )

    block_lines = (
        _ALIASES_TEMPLATE.replace("__PROJECT_DIR__", project_dir_str)
        .rstrip("\n")
        .splitlines()
    )

    if not rc_file.exists():
        rc_file.touch()

    existing_lines = rc_file.read_text(encoding="utf-8").splitlines()
    out_lines: list[str] = []
    in_block = False
    replaced = False
    for line in existing_lines:
        if line == BEGIN_MARKER:
            if not replaced:
                out_lines.append(BEGIN_MARKER)
                out_lines.extend(block_lines)
                out_lines.append(END_MARKER)
                replaced = True
            in_block = True
        elif line == END_MARKER and in_block:
            in_block = False
        elif not in_block:
            out_lines.append(line)

    if in_block:
        warn(f"Marcatore END mancante in {rc_file}; file lasciato invariato.")
        return False

    if not replaced:
        # Bash always inserted a leading blank line here (printf '\n%s...'),
        # even for a brand-new empty rc file — kept for byte-identical output.
        out_lines.append("")
        out_lines.append(BEGIN_MARKER)
        out_lines.extend(block_lines)
        out_lines.append(END_MARKER)

    try:
        rc_file.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    except OSError as exc:
        warn(f"Impossibile scrivere {rc_file}: {exc}")
        return False

    ok(f"Alias shell della Web UI installati in {rc_file}")
    return True
