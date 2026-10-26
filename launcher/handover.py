"""Move the server (`megachat serve`) between this machine and the remote (Hetzner) server.

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
import shlex
import socket
import subprocess
import sys
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

# Remote lock path: fixed (the ssh session does not inherit local overrides).
_LOCK = "/tmp/signal-tui.lock"

# Bash helpers shared by the remote scripts; mirror scripts/tui_handover.sh.
# stop_serve: SIGINT the lock holder and wait for it to exit (30 s); a legacy
# tmux 'tui' session is killed too.  Returns 1, lock left in place, when the
# process survives (never leave two servers).  Only the lock path is rm'd.
_REMOTE_FUNCS = f"""
LOCK={shlex.quote(_LOCK)}
lock_pid() {{
    local pid=""
    [ -f "$LOCK" ] && pid="$(cat "$LOCK" 2>/dev/null || true)"
    case "$pid" in ''|*[!0-9]*) return 1 ;; esac
    kill -0 "$pid" 2>/dev/null || return 1
    echo "$pid"
}}
stop_serve() {{
    local pid
    if pid="$(lock_pid)"; then
        kill -INT "$pid" 2>/dev/null || true
        for _ in $(seq 1 {int(server_mod.STOP_TIMEOUT_S * 2)}); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.5
        done
        kill -0 "$pid" 2>/dev/null && return 1
    fi
    tmux kill-session -t tui 2>/dev/null || true
    rm -f "$LOCK"
    return 0
}}
"""

REMOTE_STATUS = r"""
LOCK=/tmp/signal-tui.lock
pid="$(cat "$LOCK" 2>/dev/null || true)"
case "$pid" in ''|*[!0-9]*) pid="" ;; esac
if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then echo "  Server: ATTIVO (lock pid $pid)"; else echo "  Server: spento"; fi
pgrep -f "signal-cli.*daemon" >/dev/null && echo "  daemon signal-cli: attivo" || echo "  daemon signal-cli: spento"
docker ps --format '{{.Names}}' 2>/dev/null | grep -q "signal-tui-whatsapp" && echo "  WAHA: attivo" || echo "  WAHA: spento"
"""

#: Exit code of REMOTE_STOP when the remote server refuses to stop.
REMOTE_STOP_STUCK = 3

REMOTE_STOP = (
    _REMOTE_FUNCS
    + f"""
stop_serve || {{ echo "server remoto non si ferma (pid $(lock_pid))" >&2; exit {REMOTE_STOP_STUCK}; }}
pkill -f "signal-cli.*daemon" 2>/dev/null || true
cd {shlex.quote(REMOTE_PROJECT_DIR)} && docker compose down >/dev/null 2>&1 || true
echo "server spento"
"""
)


def _remote_start_script(*, docker_limits: bool = True) -> str:
    limits_flag = "" if docker_limits else " --no-docker-limits"
    serve = shlex.join([".venv/bin/python", *server_mod.SERVE_ARGS])
    log = shlex.quote(str(server_mod.SERVER_LOG))
    return (
        _REMOTE_FUNCS
        + f"""
cd {shlex.quote(REMOTE_PROJECT_DIR)} || {{ echo FAIL_TUI_SERVER; exit 0; }}
python3 launcher.py whatsapp start --no-wait{limits_flag} >/dev/null 2>&1 || true
stop_serve || {{ echo FAIL_TUI_SERVER; exit 0; }}
( nohup {serve} </dev/null >>{log} 2>&1 & )
for _ in $(seq 1 {server_mod.READY_TIMEOUT_S}); do
    if lock_pid >/dev/null && (exec 3<>/dev/tcp/127.0.0.1/{server_mod.SERVER_PORT}) 2>/dev/null; then
        echo "OK_TUI_SERVER"
        exit 0
    fi
    sleep 1
done
echo "FAIL_TUI_SERVER"
"""
    )


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
    pid = server_mod.server_pid()
    if pid is not None:
        print(f"  Server: ATTIVO (lock pid {pid})")
    else:
        print("  Server: spento")
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
    if not server_mod.stop_server():
        die(
            f"Il server locale (pid {server_mod.server_pid()}) non si ferma: "
            "handover interrotto, nulla è stato avviato sul server."
        )
        return 1
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
        die("Avvio del server sul server remoto non riuscito.")
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
    print(
        "TUI:     sul server, da /root/signal-tui-client, lancia "
        "`.venv/bin/python megachat.py` (si collega al server in esecuzione)"
    )
    return 0


def to_local(*, docker_limits: bool = True) -> int:
    info(f"Fermo il client sul SERVER ({_hz_host()})...")
    stop = _run_remote(REMOTE_STOP, capture=False)
    if stop.returncode != 0:
        die(
            "Impossibile fermare il server remoto "
            f"(ssh/stop codice {stop.returncode}): handover interrotto per non "
            "avere due server attivi. Verifica con: python3 launcher.py handover status"
        )
        return 1

    info("Accendo il client LOCALE...")
    if whatsapp_mod.start(no_wait=False, docker_limits=docker_limits) == 0:
        ok("WAHA avviato e pronto")
    else:
        info(
            "avvio WAHA non riuscito o timeout; provo comunque a far partire il server"
        )
        whatsapp_mod.start(no_wait=True, docker_limits=docker_limits)

    if not server_mod.stop_server():
        die(f"Il server locale (pid {server_mod.server_pid()}) non si ferma.")
        return 1
    try:
        up = server_mod.spawn_and_wait()
    except OSError as exc:
        die(f"avvio server fallito in {PROJECT_DIR}: {exc}")
        return 1
    if not up:
        die(
            f"avvio server fallito in {PROJECT_DIR}: controlla "
            f"tail -50 /tmp/signal-tui.log {server_mod.SERVER_LOG}"
        )
        return 1
    ok(f"Server avviato (megachat serve) in {PROJECT_DIR}")

    ok("Handover verso il locale completato.")
    print()
    print("Web UI:  http://127.0.0.1:4242")
    print(
        "Token:   python3 -c 'import json;print(json.load(open("
        f'"{PROJECT_DIR}/config.json"))["web"]["token"])\''
    )
    print("Log:     tail -f /tmp/signal-tui.log")
    print("TUI:     python3 megachat.py  (si collega al server in esecuzione)")
    return 0
