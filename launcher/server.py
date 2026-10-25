"""Start/stop/status the headless server directly on this machine (WAHA +
``megachat serve`` as a detached background process).

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

# The instance lock belongs to the server (or to a ``megachat --in-process``
# TUI); same location and override as ``server/lock.py``.
LOCK_FILE = Path(os.environ.get("MEGACHAT_LOCK_FILE") or "/tmp/signal-tui.lock")
SERVER_LOG = Path("/tmp/megachat-serve.log")
SERVER_PORT = 4242
#: Seconds to wait for the web adapter to listen after spawning the server.
READY_TIMEOUT_S = 30


def _kill_tmux_session(name: str = "tui") -> None:
    """Kill a legacy tmux session (older versions ran the TUI inside tmux)."""
    try:
        subprocess.run(
            ["tmux", "kill-session", "-t", name], capture_output=True, check=False
        )
    except OSError:
        pass  # tmux not installed: nothing legacy to clean up


def _spawn_server() -> subprocess.Popen:
    """Start ``megachat serve`` detached from this terminal and session."""
    # The child inherits its own descriptor; closing ours after Popen is safe.
    with open(SERVER_LOG, "ab") as log:
        return subprocess.Popen(
            [
                str(PROJECT_DIR / ".venv" / "bin" / "python"),
                "megachat.py",
                "serve",
                "--web-port",
                str(SERVER_PORT),
                "--web-host",
                "0.0.0.0",
            ],
            cwd=PROJECT_DIR,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        )


def _stop_tui() -> None:
    """Stop the instance holding the lock (the server; SIGINT = clean stop)
    and any legacy tmux session.  Attached TUIs just lose their connection."""
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

    info(
        "Avvio il server headless (megachat serve) con Web UI su "
        f"0.0.0.0:{SERVER_PORT}..."
    )
    # Restart semantics: stop a running instance (lock pid, SIGINT) and any
    # legacy tmux 'tui' session before taking the lock again.
    _stop_tui()
    try:
        proc = _spawn_server()
    except OSError as exc:
        die(f"Impossibile avviare il server: {exc}")
        return 1

    for _ in range(READY_TIMEOUT_S):
        if proc.poll() is not None:
            break
        if port_listening(SERVER_PORT):
            ok(f"Server attivo — Web UI su http://{_local_ip()}:{SERVER_PORT}")
            print(f"   Token Web UI: {_read_web_token()}")
            print("   Log:          tail -f /tmp/signal-tui.log")
            print("   Stop:         python3 launcher.py server stop")
            return 0
        time.sleep(1)
    die(
        f"Il server non risulta attivo sulla porta {SERVER_PORT}. Controlla: "
        f"tail -50 /tmp/signal-tui.log {SERVER_LOG}"
    )
    return 1


def stop_all() -> int:
    info("Spegnimento server (SIGINT pulito)...")
    _stop_tui()
    ok("Server fermato")

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
        print(f"Server: ATTIVO (pid {LOCK_FILE.read_text().strip()})")
    else:
        print("Server: spento")
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
