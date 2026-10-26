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
#: Seconds to wait for a signalled server to exit before giving up.
STOP_TIMEOUT_S = 30
#: Arguments of ``.venv/bin/python`` that start the detached server (web on
#: all interfaces; the Unix socket is on by default).  The remote handover
#: script is built from the same list.
SERVE_ARGS = [
    "megachat.py",
    "serve",
    "--web-port",
    str(SERVER_PORT),
    "--web-host",
    "0.0.0.0",
]


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
            [str(PROJECT_DIR / ".venv" / "bin" / "python"), *SERVE_ARGS],
            cwd=PROJECT_DIR,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def server_pid() -> int | None:
    """Pid in the instance lock if that process is alive, else ``None``.

    "Running" means the lock holder is alive: the lock belongs to the
    ``megachat serve`` process (or to a legacy/``--in-process`` TUI).
    """
    try:
        pid = int(LOCK_FILE.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if _pid_alive(pid) else None


def stop_server() -> bool:
    """Stop the instance holding the lock (SIGINT = clean stop) and any legacy
    tmux session; wait for the process to exit.  Attached TUIs just lose their
    connection.

    Returns ``False`` (lock left in place, so no second server can start next
    to a half-stopped one) when the process is still alive after the timeout.
    """
    pid = server_pid()
    if pid is not None:
        try:
            os.kill(pid, signal.SIGINT)
        except OSError:
            pass
        for _ in range(int(STOP_TIMEOUT_S / 0.5)):
            if not _pid_alive(pid):
                break
            time.sleep(0.5)
        if _pid_alive(pid):
            return False
    _kill_tmux_session()
    LOCK_FILE.unlink(missing_ok=True)
    return True


def spawn_and_wait() -> bool:
    """Spawn the detached server and wait until the web adapter listens.

    ``False`` when it exited early or never came up.  ``OSError`` from the
    spawn itself propagates.
    """
    proc = _spawn_server()
    for _ in range(READY_TIMEOUT_S):
        if proc.poll() is not None:
            return False
        if port_listening(SERVER_PORT):
            return True
        time.sleep(1)
    return False


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


def start_all(*, docker_limits: bool = True, print_token: bool = True) -> int:
    info("Avvio WAHA (WhatsApp HTTP API)...")
    if whatsapp_mod.start(no_wait=False, docker_limits=docker_limits) == 0:
        ok("WAHA avviato e pronto")
        whatsapp_mod.describe_session(whatsapp_mod.session_status())
    else:
        info("WAHA non partito (vedi docker compose logs whatsapp)")

    info(
        "Avvio il server headless (megachat serve) con Web UI su "
        f"0.0.0.0:{SERVER_PORT}..."
    )
    # Restart semantics: stop a running instance (lock pid, SIGINT) and any
    # legacy tmux 'tui' session before taking the lock again.
    if not stop_server():
        die(
            f"Il server (pid {server_pid()}) non si ferma: non ne avvio un altro. "
            "Controlla: tail -50 /tmp/signal-tui.log"
        )
        return 1
    try:
        up = spawn_and_wait()
    except OSError as exc:
        die(f"Impossibile avviare il server: {exc}")
        return 1
    if up:
        ok(f"Server attivo — Web UI su http://{_local_ip()}:{SERVER_PORT}")
        if print_token:
            print(f"   Token Web UI: {_read_web_token()}")
        print("   Log:          tail -f /tmp/signal-tui.log")
        print("   Stop:         python3 launcher.py server stop")
        print("   Interattivo:  python3 megachat.py  (si collega al server)")
        return 0
    die(
        f"Il server non risulta attivo sulla porta {SERVER_PORT}. Controlla: "
        f"tail -50 /tmp/signal-tui.log {SERVER_LOG}"
    )
    return 1


def stop_all() -> int:
    info("Spegnimento server (SIGINT pulito)...")
    if not stop_server():
        die(f"Il server (pid {server_pid()}) non si ferma; WAHA lasciato acceso.")
        return 1
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
    pid = server_pid()
    if pid is not None:
        print(f"Server: ATTIVO (pid {pid})")
    else:
        print("Server: spento")
    print(
        "daemon signal-cli: attivo"
        if _pgrep("signal-cli.*daemon")
        else "daemon signal-cli: spento"
    )
    waha_active = _docker_container_running("signal-tui-whatsapp")
    print("WAHA: attivo" if waha_active else "WAHA: spento")
    if waha_active:
        whatsapp_mod.describe_session(whatsapp_mod.session_status(timeout=2))
    print("Web UI: in ascolto su 4242" if port_listening(4242) else "Web UI: spenta")
    return 0
