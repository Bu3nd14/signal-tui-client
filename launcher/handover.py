"""Move the TUI session between this machine and the remote (Hetzner) server.

Ported from ``scripts/tui_handover.sh``. The shared account state (WAHA
session, signal-cli daemon) must never run on two machines at once, so these
commands centralize the clean handoff.

Configuration:
  HZ_HOST — remote server IP (default 167.233.140.207)
  HZ_USER — SSH user on the remote server (default root)
  Requires an SSH key already installed on the server (ssh-copy-id); the
  password is never used.

The IP can change (e.g. after a server snapshot/recreate): update
``~/.config/signal-tui-handover.conf`` (``HZ_HOST=<new-ip>``) or export
``HZ_HOST`` in the shell.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import server as server_mod
from . import whatsapp as whatsapp_mod
from .common import PROJECT_DIR, command_exists, die, info, ok

REMOTE_PROJECT_DIR = "/root/signal-tui-client"

CONF_FILE = Path(
    os.environ.get(
        "SIGNAL_TUI_HANDOVER_CONF",
        str(Path.home() / ".config" / "signal-tui-handover.conf"),
    )
)

REMOTE_STATUS = r"""
[ -f /tmp/signal-tui.lock ] && echo "  TUI: ATTIVA (lock pid $(cat /tmp/signal-tui.lock))" || echo "  TUI: spenta"
pgrep -f "signal-cli.*daemon" >/dev/null && echo "  daemon signal-cli: attivo" || echo "  daemon signal-cli: spento"
docker ps --format '{{.Names}}' 2>/dev/null | grep -q "signal-tui-whatsapp" && echo "  WAHA: attivo" || echo "  WAHA: spento"
"""

REMOTE_STOP = f"""
if [ -f /tmp/signal-tui.lock ]; then
    kill -INT "$(cat /tmp/signal-tui.lock)" 2>/dev/null || true
    for _ in $(seq 1 12); do [ ! -f /tmp/signal-tui.lock ] && break; sleep 0.5; done
fi
tmux kill-session -t tui 2>/dev/null || true
rm -f /tmp/signal-tui.lock
pkill -f "signal-cli.*daemon" 2>/dev/null || true
cd {REMOTE_PROJECT_DIR} && docker compose down >/dev/null 2>&1 || true
echo "server spento"
"""


def _remote_start_script(*, docker_limits: bool = True) -> str:
    limits_flag = "" if docker_limits else " --no-docker-limits"
    return f"""
cd {REMOTE_PROJECT_DIR}
python3 launcher.py whatsapp start --no-wait{limits_flag} >/dev/null 2>&1 || true
tmux new-session -d -s tui "cd {REMOTE_PROJECT_DIR} && .venv/bin/python -m signal_tui --web --web-port 4242 --web-host 0.0.0.0"
sleep 3
if tmux list-sessions 2>/dev/null | grep -q "^tui:"; then
    echo "OK_TUI_SERVER"
else
    echo "FAIL_TUI_SERVER"
fi
"""


def _load_conf() -> dict[str, str]:
    values: dict[str, str] = {}
    if CONF_FILE.is_file():
        for line in CONF_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _hz_host() -> str:
    return os.environ.get("HZ_HOST") or _load_conf().get("HZ_HOST", "167.233.140.207")


def _hz_user() -> str:
    return os.environ.get("HZ_USER") or _load_conf().get("HZ_USER", "root")


def _ssh_base() -> list[str]:
    return [
        "ssh",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"{_hz_user()}@{_hz_host()}",
    ]


def _run_remote(script: str, *, capture: bool) -> subprocess.CompletedProcess:
    if not command_exists("ssh"):
        die("ssh non trovato. Installalo per usare l'handover verso il server remoto.")
    return subprocess.run(
        _ssh_base() + ["bash", "-s"],
        input=script,
        text=True,
        capture_output=capture,
        check=False,
    )


def status() -> int:
    print(f"=== LOCALE ({socket.gethostname()}) ===")
    if server_mod.LOCK_FILE.exists():
        print(f"  TUI: ATTIVA (lock pid {server_mod.LOCK_FILE.read_text().strip()})")
    else:
        print("  TUI: spenta")
    print(
        "  daemon signal-cli: attivo"
        if server_mod._pgrep("signal-cli.*daemon")
        else "  daemon signal-cli: spento"
    )
    print(
        "  WAHA: attivo"
        if server_mod._docker_container_running("signal-tui-whatsapp")
        else "  WAHA: spento"
    )
    print(f"=== REMOTO ({_hz_user()}@{_hz_host()}) ===")
    result = _run_remote(REMOTE_STATUS, capture=False)
    return result.returncode


def to_server(*, docker_limits: bool = True) -> int:
    info("Fermo il client LOCALE...")
    server_mod._stop_tui()
    subprocess.run(
        ["pkill", "-f", "signal-cli.*daemon"], capture_output=True, check=False
    )
    subprocess.run(
        ["docker", "compose", "-f", str(whatsapp_mod.COMPOSE_FILE), "down"],
        capture_output=True,
        check=False,
    )

    info(f"Accendo il client sul SERVER ({_hz_host()})...")
    result = _run_remote(
        _remote_start_script(docker_limits=docker_limits), capture=True
    )
    print(result.stdout, end="")
    if "OK_TUI_SERVER" not in result.stdout:
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        die("Avvio della TUI sul server non riuscito.")
        return 1

    ok("Handover verso il server completato.")
    print()
    print(f"Web UI:  http://{_hz_host()}:4242  (o via tunnel Cloudflare)")
    print(
        "Token:   vedi config.json del server → "
        "python3 -c 'import json;print(json.load(open("
        f'"{REMOTE_PROJECT_DIR}/config.json"))["web"]["token"])\''
    )
    print("Log:     tail -f /tmp/signal-tui.log (sul server)")
    return 0


def to_local(*, docker_limits: bool = True) -> int:
    info(f"Fermo il client sul SERVER ({_hz_host()})...")
    _run_remote(REMOTE_STOP, capture=False)

    info("Accendo il client LOCALE...")
    if whatsapp_mod.start(no_wait=False, docker_limits=docker_limits) == 0:
        ok("WAHA avviato e pronto")
    else:
        info("avvio WAHA non riuscito o timeout; provo comunque a far partire la TUI")
        whatsapp_mod.start(no_wait=True, docker_limits=docker_limits)

    subprocess.run(
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            "tui",
            (
                f"cd '{PROJECT_DIR}' && .venv/bin/python -m signal_tui "
                "--web --web-port 4242 --web-host 0.0.0.0"
            ),
        ],
        check=False,
    )
    time.sleep(2)
    sessions = subprocess.run(
        ["tmux", "list-sessions"], capture_output=True, text=True, check=False
    ).stdout
    if "tui:" not in sessions:
        die(f"avvio TUI fallito in {PROJECT_DIR}")
        return 1
    ok(f"TUI avviata (tmux 'tui') in {PROJECT_DIR}")

    ok("Handover verso il locale completato.")
    print()
    print("Web UI:  http://127.0.0.1:4242")
    print(
        "Token:   python3 -c 'import json;print(json.load(open("
        f'"{PROJECT_DIR}/config.json"))["web"]["token"])\''
    )
    print("Log:     tail -f /tmp/signal-tui.log")
    return 0
