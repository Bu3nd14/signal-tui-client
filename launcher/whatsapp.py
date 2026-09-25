"""WAHA (WhatsApp HTTP API) lifecycle — Docker Compose orchestration.

Ported from ``scripts/start_whatsapp.sh`` (``start``/``stop``) and the
WhatsApp prerequisite-check/start flow of ``install.sh`` (``setup``).
"""

from __future__ import annotations

import os
import platform
import re
import secrets
import stat
import subprocess
import time

from .common import (
    C_BLUE,
    C_BOLD,
    C_RESET,
    PROJECT_DIR,
    command_exists,
    err,
    http_get,
    info,
    ok,
    port_listening,
    warn,
)

WA_PORT = int(os.environ.get("WHATSAPP_API_PORT", "3005") or "3005")
WEBHOOK_PORT = int(os.environ.get("CLIENT_WEBHOOK_PORT", "8088") or "8088")
COMPOSE_FILE = PROJECT_DIR / "docker-compose.yml"


def read_waha_api_key() -> str:
    """Read WAHA_API_KEY from the environment, else the project's ``.env``."""
    key = os.environ.get("WAHA_API_KEY", "")
    if key:
        return key
    env_file = PROJECT_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("WAHA_API_KEY="):
                value = line.split("=", 1)[1].strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                return value
    return ""


def _listening_port_owner(port: int) -> tuple[str | None, str | None]:
    """Best-effort ``(pid, process_name)`` for *port* via ``ss -tlnp``."""
    if not command_exists("ss"):
        return None, None
    try:
        out = subprocess.run(
            ["ss", "-tlnp"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return None, None
    for line in out.splitlines():
        if re.search(rf":{port}\s", line):
            pid_m = re.search(r"pid=(\d+)", line)
            name_m = re.search(r'users:\(\("([^"]+)"', line)
            return (
                pid_m.group(1) if pid_m else None,
                name_m.group(1) if name_m else None,
            )
    return None, None


def check_port(port: int, label: str) -> bool:
    info(f"Controllo porta {label} ({port})...")
    pid, pname = _listening_port_owner(port)
    in_use = pid is not None or pname is not None or port_listening(port)
    if not in_use:
        ok(f"  Porta {port} disponibile")
        return True
    if port == WEBHOOK_PORT and pname == "python":
        ok(f"  Porta {port} gia in uso (pid {pid}, webhook server) - OK")
        return True
    if pid:
        warn(f"  Porta {port} gia in uso da pid {pid} ({pname or 'sconosciuto'}).")
    else:
        warn(f"  Porta {port} gia in uso (Docker o demone).")
    warn("  Cambia porta o ferma il processo se necessario.")
    return False


def check_firewall(port: int, label: str) -> None:
    info(f"Controllo firewall per {label} ({port})...")
    if command_exists("ufw"):
        status = subprocess.run(
            ["ufw", "status"], capture_output=True, text=True, check=False
        ).stdout
        if "Status: active" in status:
            if re.search(rf"^{port}\s.*ALLOW", status, re.MULTILINE):
                ok(f"  ufw: porta {port} consentita")
            else:
                warn(f"  ufw attivo: apri la porta {port} (ufw allow {port})")
            return
    if command_exists("iptables"):
        out = subprocess.run(
            ["iptables", "-L", "INPUT", "-n"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        if f"dpt:{port}" in out:
            ok(f"  iptables: porta {port} consentita")
            return
        if re.search(r"^DROP", out, re.MULTILINE):
            warn(f"  iptables policy DROP: apri la porta {port}")
            return
    ok("  Nessun firewall restrittivo rilevato")


def ensure_waha_env() -> None:
    """Generate/preserve WAHA credentials in ``.env`` (same layout as bash)."""
    env_file = PROJECT_DIR / ".env"
    env_example = PROJECT_DIR / ".env.example"
    existed = env_file.exists()
    if env_file.exists():
        lines = env_file.read_text(encoding="utf-8").splitlines()
    elif env_example.exists():
        lines = env_example.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    architecture = platform.machine().lower()
    image = (
        "devlikeapro/waha:arm"
        if architecture in ("arm64", "aarch64")
        else "devlikeapro/waha:latest"
    )
    credentials = [
        ("WAHA_API_KEY", lambda: secrets.token_urlsafe(32)),
        ("WAHA_DASHBOARD_USERNAME", lambda: "admin"),
        ("WAHA_DASHBOARD_PASSWORD", lambda: secrets.token_urlsafe(32)),
        ("WHATSAPP_SWAGGER_USERNAME", lambda: "admin"),
        ("WHATSAPP_SWAGGER_PASSWORD", lambda: secrets.token_urlsafe(32)),
    ]
    values = [("WAHA_IMAGE", image)]
    for key, generate in credentials:
        prefix = f"{key}="
        existing = ""
        for line in reversed(lines):
            if line.startswith(prefix) and line[len(prefix) :].strip():
                existing = line[len(prefix) :].strip()
                break
        values.append((key, existing or generate()))

    managed = tuple(f"{key}=" for key, _ in values)
    lines = [line for line in lines if not line.startswith(managed)]
    if lines and lines[-1]:
        lines.append("")
    lines.extend(f"{key}={value}" for key, value in values)
    content = "\n".join(lines) + "\n"
    if not env_file.exists() or env_file.read_text(encoding="utf-8") != content:
        env_file.write_text(content, encoding="utf-8")
    env_file.chmod(stat.S_IRUSR | stat.S_IWUSR)

    if existed:
        ok(f"Credenziali WAHA verificate in {env_file}")
    else:
        ok(f"Credenziali WAHA generate in {env_file} (permessi 0600)")


def setup(should_start: bool) -> bool:
    """Prerequisite check (``--check-whatsapp``) or full start (``--whatsapp``).

    Used by the ``install`` command.
    """
    print()
    print(
        f"{C_BLUE}{C_BOLD}-- WhatsApp (WAHA) "
        f"------------------------------------------------------------{C_RESET}"
    )
    print()
    if not command_exists("docker"):
        err("Docker non trovato. Installa Docker per usare WhatsApp.")
        return False
    version = subprocess.run(
        ["docker", "--version"], capture_output=True, text=True, check=False
    ).stdout.strip()
    ok(f"Docker trovato: {version}")
    if (
        subprocess.run(
            ["docker", "compose", "version"], capture_output=True, check=False
        ).returncode
        != 0
    ):
        err("Docker Compose non trovato.")
        return False
    ok("Docker Compose trovato")
    check_port(WA_PORT, "WAHA API")
    check_port(WEBHOOK_PORT, "webhook")
    check_firewall(WA_PORT, "WAHA API")
    check_firewall(WEBHOOK_PORT, "webhook")
    if should_start:
        ensure_waha_env()
        print()
        info("Avvio WAHA via Docker Compose...")
        result = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d"], check=False
        )
        if result.returncode != 0:
            err("Avvio di WAHA fallito.")
            return False
        ok(f"WAHA avviato. API: http://127.0.0.1:{WA_PORT}")
        print()
        info("In attesa che WAHA sia pronto...")
        api_key = read_waha_api_key()
        ready = False
        for i in range(1, 31):
            result_get = http_get(
                f"http://127.0.0.1:{WA_PORT}/api/sessions",
                headers={"X-Api-Key": api_key} if api_key else {},
                timeout=3,
            )
            if result_get is not None and 200 <= result_get[0] < 300:
                ok(f"WAHA pronto! (dopo {i}s)")
                ready = True
                break
            time.sleep(1)
        if not ready:
            warn(
                "WAHA avviato ma non pronto dopo 30s; "
                "controlla: docker compose logs whatsapp"
            )
    print()
    return True


def start(no_wait: bool) -> int:
    """Standalone start (formerly ``scripts/start_whatsapp.sh``)."""
    if not command_exists("docker"):
        err("docker non trovato. Installa Docker e riprova.")
        return 1
    print("🟢 Avvio WhatsApp HTTP API (WAHA) via Docker Compose...")
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d"], check=False
    )
    if result.returncode != 0:
        return result.returncode
    if no_wait:
        print("▶️  Comando di avvio inviato (--no-wait).")
        return 0

    api_key = read_waha_api_key()
    api_url = os.environ.get("WHATSAPP_API_URL", f"http://127.0.0.1:{WA_PORT}").rstrip(
        "/"
    )
    print(f"⏳ Attendo che l'API risponda su {api_url} ...")
    for _ in range(60):
        r = http_get(
            f"{api_url}/api/version",
            headers={"X-Api-Key": api_key} if api_key else {},
            timeout=3,
        )
        if r is not None and 200 <= r[0] < 300:
            print(f"✅ WhatsApp HTTP API pronta: {api_url}")
            return 0
        time.sleep(2)
    err(f"Timeout: l'API non risponde su {api_url} dopo ~120s.")
    print(f'   Controlla: docker compose -f "{COMPOSE_FILE}" logs whatsapp')
    return 1


def stop() -> int:
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "down"], check=False
    )
    if result.returncode == 0:
        print("WAHA stopped.")
    return result.returncode
