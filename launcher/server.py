"""Start/stop/status the TUI directly on this machine (tmux + WAHA).

Ported from ``scripts/start_on_server.sh`` — a disaster-recovery utility:
if the local machine is unreachable, this can be run from a server console
without depending on the handover scripts that normally run from local.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

from . import whatsapp as whatsapp_mod
from .common import PROJECT_DIR, command_exists, die, info, ok, port_listening

LOCK_FILE = Path("/tmp/signal-tui.lock")


def _kill_tmux_session(name: str = "tui") -> None:
    subprocess.run(
        ["tmux", "kill-session", "-t", name], capture_output=True, check=False
    )


def _stop_tui() -> None:
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            os.kill(pid, signal.SIGINT)
        except (OSError, ValueError):
            pass
        for _ in range(12):
            if not LOCK_FILE.exists():
                break
            time.sleep(0.5)
    _kill_tmux_session()
    LOCK_FILE.unlink(missing_ok=True)


def _local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _read_web_token() -> str:
    config_file = PROJECT_DIR / "config.json"
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
        return str((data.get("web") or {}).get("token", ""))
    except (OSError, json.JSONDecodeError, AttributeError):
        return ""


def _pgrep(pattern: str) -> bool:
    if not command_exists("pgrep"):
        return False
    return (
        subprocess.run(
            ["pgrep", "-f", pattern], capture_output=True, check=False
        ).returncode
        == 0
    )


def _docker_container_running(name: str) -> bool:
    if not command_exists("docker"):
        return False
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return name in result.stdout.splitlines()


def start_all(*, docker_limits: bool = True) -> int:
    info("Avvio WAHA (WhatsApp HTTP API)...")
    if whatsapp_mod.start(no_wait=False, docker_limits=docker_limits) == 0:
        ok("WAHA avviato e pronto")
    else:
        info("WAHA non partito (vedi docker compose logs whatsapp)")

    info("Avvio la TUI in tmux (sessione 'tui') con Web UI su 0.0.0.0:4242...")
    _kill_tmux_session()
    LOCK_FILE.unlink(missing_ok=True)
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

    time.sleep(12)
    sessions = subprocess.run(
        ["tmux", "list-sessions"], capture_output=True, text=True, check=False
    ).stdout
    if "tui:" in sessions and port_listening(4242):
        ok(f"TUI attiva — Web UI su http://{_local_ip()}:4242")
        print(f"   Token Web UI: {_read_web_token()}")
        print("   Log:          tail -f /tmp/signal-tui.log")
        print("   Stop:         python3 launcher.py server stop")
        return 0
    die(
        "La TUI non risulta attiva sulla porta 4242. Controlla: tail -50 /tmp/signal-tui.log"
    )
    return 1


def stop_all() -> int:
    info("Spegnimento TUI (SIGINT pulito)...")
    _stop_tui()
    ok("TUI fermata")

    info("Spegnimento WAHA...")
    result = subprocess.run(
        ["docker", "compose", "-f", str(whatsapp_mod.COMPOSE_FILE), "down"],
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        ok("WAHA fermato")
    else:
        info("WAHA non era attivo")
    ok("Server spento. Per riaccendere: python3 launcher.py server start")
    return 0


def status() -> int:
    if LOCK_FILE.exists():
        print(f"TUI: ATTIVA (pid {LOCK_FILE.read_text().strip()})")
    else:
        print("TUI: spenta")
    print(
        "daemon signal-cli: attivo"
        if _pgrep("signal-cli.*daemon")
        else "daemon signal-cli: spento"
    )
    print(
        "WAHA: attivo"
        if _docker_container_running("signal-tui-whatsapp")
        else "WAHA: spento"
    )
    print("Web UI: in ascolto su 4242" if port_listening(4242) else "Web UI: spenta")
    return 0
