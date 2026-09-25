"""Restart the signal-cli daemon (for the *configured* account only) and WAHA.

Ported from ``scripts/restart_backend.sh``.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from . import whatsapp as whatsapp_mod
from .common import PROJECT_DIR, command_exists, die, http_post, info, port_listening

SIGNAL_RPC_URL = "http://127.0.0.1:8080/api/v1/rpc"


def _signal_number() -> str:
    env_val = os.environ.get("SIGNAL_USER_NUMBER", "")
    if env_val:
        return env_val
    config_file = PROJECT_DIR / "config.json"
    if not config_file.exists():
        return ""
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
        return str(data.get("user_number") or "")
    except (OSError, json.JSONDecodeError):
        return ""


def _is_configured_signal_daemon(pid: int, signal_number: str) -> bool:
    proc_dir = Path(f"/proc/{pid}")
    try:
        if proc_dir.stat().st_uid != os.getuid():
            return False
        raw = (proc_dir / "cmdline").read_bytes()
    except OSError:
        return False
    args = [a.decode("utf-8", "replace") for a in raw.split(b"\x00") if a]
    if not args:
        return False
    has_daemon = "daemon" in args
    has_signal_cli = any("signal-cli" in a or "org.asamk.signal" in a for a in args)
    has_account = False
    next_is_account = False
    for arg in args:
        if next_is_account and arg == signal_number:
            has_account = True
        next_is_account = arg in ("-u", "--account")
    return has_daemon and has_account and has_signal_cli


def _configured_daemon_pids(signal_number: str) -> list[int]:
    pids = []
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            pid = int(entry.name)
        except ValueError:
            continue
        if _is_configured_signal_daemon(pid, signal_number):
            pids.append(pid)
    return pids


def _stop_configured_signal_daemon(signal_number: str) -> None:
    pids = _configured_daemon_pids(signal_number)
    if not pids:
        info("Nessun daemon signal-cli dell'account configurato da fermare.")
        return
    info("Arresto del daemon signal-cli dell'account configurato...")
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not port_listening(8080):
            return
        time.sleep(1)

    for pid in pids:
        if _is_configured_signal_daemon(pid, signal_number):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _find_signal_cli() -> str | None:
    for candidate in sorted((PROJECT_DIR / "bin").glob("signal-cli-*/bin/signal-cli")):
        if os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _restart_signal_daemon(wait: bool, timeout_s: int) -> None:
    signal_number = _signal_number()
    if not signal_number:
        info(
            "Backend Signal non configurato (SIGNAL_USER_NUMBER o "
            "config.json[user_number]): salto signal-cli."
        )
        return

    signal_cli = _find_signal_cli()
    if signal_cli is None:
        info(
            "Backend Signal non disponibile (binario signal-cli non trovato "
            "sotto bin/): salto signal-cli."
        )
        return

    _stop_configured_signal_daemon(signal_number)

    deadline = time.monotonic() + timeout_s
    while port_listening(8080):
        if time.monotonic() >= deadline:
            die(
                "La porta 8080 non si è liberata: non avvio signal-cli per non toccare altri servizi."
            )
            return
        time.sleep(1)

    info("Avvio del daemon signal-cli...")
    subprocess.Popen(
        [
            signal_cli,
            "-u",
            signal_number,
            "daemon",
            "--http",
            "127.0.0.1:8080",
            "--receive-mode",
            "on-connection",
            "--no-receive-stdout",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )

    if not wait:
        info("Daemon signal-cli avviato (--no-wait).")
        return

    info(
        f"Attendo il JSON-RPC signal-cli su {SIGNAL_RPC_URL} (timeout: {timeout_s}s)..."
    )
    deadline = time.monotonic() + timeout_s
    payload = b'{"jsonrpc":"2.0","method":"listContacts","id":"restart-healthcheck"}'
    while time.monotonic() < deadline:
        result = http_post(
            SIGNAL_RPC_URL,
            payload,
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        if result is not None:
            status, body = result
            text = body.decode("utf-8", "replace")
            if status == 200 and '"jsonrpc"' in text and '"result"' in text:
                print(f"✅ signal-cli pronto: {SIGNAL_RPC_URL}")
                return
        time.sleep(1)
    die(f"Timeout: signal-cli non risponde via JSON-RPC entro {timeout_s}s.")


def _restart_waha(wait: bool, api_timeout_s: int, *, docker_limits: bool = True) -> None:
    if not command_exists("docker"):
        die("docker non trovato. Installa Docker e riprova.")
        return
    if (
        subprocess.run(
            ["docker", "compose", "version"], capture_output=True, check=False
        ).returncode
        != 0
    ):
        die(
            "Docker Compose non disponibile. Installa il plugin Docker Compose e riprova."
        )
        return
    compose_file = whatsapp_mod.COMPOSE_FILE
    if not compose_file.exists():
        die(f"File Compose non trovato: {compose_file}")
        return

    compose = ["docker", "compose", *whatsapp_mod.compose_args(docker_limits)]
    result = subprocess.run(
        compose + ["config", "--services"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        die("Impossibile leggere i servizi dal file Compose.")
        return
    if "whatsapp" not in result.stdout.splitlines():
        die("Il file Compose non definisce il servizio 'whatsapp'.")
        return

    existing = subprocess.run(
        compose + ["ps", "-aq", "whatsapp"], capture_output=True, text=True, check=False
    ).stdout.strip()
    if existing:
        info("Riavvio del solo servizio WAHA (whatsapp)...")
        result = subprocess.run(compose + ["restart", "whatsapp"], check=False)
    else:
        info("Il servizio WAHA non esiste ancora: avvio del solo servizio whatsapp...")
        result = subprocess.run(compose + ["up", "-d", "whatsapp"], check=False)
    if result.returncode != 0:
        die("Avvio/riavvio del servizio whatsapp fallito.")
        return

    if not wait:
        info("Comando completato (--no-wait).")
        return

    api_url = os.environ.get(
        "WHATSAPP_API_URL", f"http://127.0.0.1:{whatsapp_mod.WA_PORT}"
    ).rstrip("/")
    api_key = whatsapp_mod.read_waha_api_key()
    info(f"Attendo l'API WAHA su {api_url}/api/version (timeout: {api_timeout_s}s)...")
    deadline = time.monotonic() + api_timeout_s
    from .common import http_get

    while time.monotonic() < deadline:
        result_get = http_get(
            f"{api_url}/api/version",
            headers={"X-Api-Key": api_key} if api_key else {},
            timeout=5,
        )
        if result_get is not None and 200 <= result_get[0] < 300:
            print(f"✅ WAHA pronta: {api_url}")
            return
        time.sleep(2)
    die(
        f"Timeout: WAHA non risponde su {api_url}/api/version entro {api_timeout_s}s. "
        f'Controlla: docker compose -f "{compose_file}" logs whatsapp'
    )


def run(no_wait: bool, *, docker_limits: bool = True) -> int:
    wait = not no_wait
    signal_timeout = int(os.environ.get("SIGNAL_DAEMON_TIMEOUT_SECONDS", "30") or "30")
    api_timeout = int(os.environ.get("WAHA_API_TIMEOUT_SECONDS", "120") or "120")
    _restart_signal_daemon(wait, signal_timeout)
    _restart_waha(wait, api_timeout, docker_limits=docker_limits)
    return 0
