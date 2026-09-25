"""Full installer — signal-cli download, venv, Python deps, web config.

Ported from ``install.sh``.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from . import whatsapp as whatsapp_mod
from .aliases import install_aliases
from .common import (
    C_BOLD,
    C_GREEN,
    C_RESET,
    C_YELLOW,
    PROJECT_DIR,
    command_exists,
    die,
    http_get,
    info,
    ok,
    warn,
)

BIN_DIR = PROJECT_DIR / "bin"
REPO = "AsamK/signal-cli"
REQUIRED_PYTHON = (3, 10)
REQUIRED_JAVA_MAJOR = 25


def ensure_web_config() -> None:
    config_file = PROJECT_DIR / "config.json"
    existed = config_file.exists()
    if existed:
        try:
            config = json.loads(config_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            die(f"config.json non valido: {exc}")
            return
        if not isinstance(config, dict):
            die("config.json deve contenere un oggetto JSON")
            return
    else:
        config = {}

    web = config.get("web")
    if not isinstance(web, dict):
        web = {}
    web.setdefault("enabled", True)
    web.setdefault("host", "127.0.0.1")
    web.setdefault("port", 4242)
    if not str(web.get("token") or "").strip():
        web["token"] = secrets.token_urlsafe(32)
    config["web"] = web

    fd, tmp_name = tempfile.mkstemp(prefix=".config.json.", dir=config_file.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            json.dump(config, tmp, ensure_ascii=False, indent=2)
            tmp.write("\n")
        os.chmod(tmp_name, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp_name, config_file)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass

    if existed:
        ok(f"Configurazione Web verificata in {config_file}")
    else:
        ok(f"Configurazione Web creata in {config_file} (permessi 0600)")


def get_latest_version() -> str:
    url = f"https://api.github.com/repos/{REPO}/releases/latest"
    result = http_get(
        url, headers={"User-Agent": "signal-tui-client-launcher"}, timeout=15
    )
    if result is None or result[0] != 200:
        die("Impossibile determinare l'ultima versione di signal-cli.")
        return ""
    try:
        data = json.loads(result[1])
    except json.JSONDecodeError:
        die("Impossibile determinare l'ultima versione di signal-cli.")
        return ""
    tag = str(data.get("tag_name") or "")
    return tag.removeprefix("v")


def get_installed_version() -> str:
    if BIN_DIR.is_dir():
        for d in sorted(BIN_DIR.glob("signal-cli-*")):
            if d.is_dir():
                return d.name.removeprefix("signal-cli-")
    return ""


def check_python() -> None:
    major, minor = sys.version_info[:2]
    if (major, minor) < REQUIRED_PYTHON:
        die(
            f"Python {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]}+ richiesto, "
            f"trovato {major}.{minor}."
        )
        return
    ok(f"Python {major}.{minor} trovato.")


def check_java() -> bool:
    if not command_exists("java"):
        warn(
            "Java non trovato. La build JVM di signal-cli richiede "
            f"Java {REQUIRED_JAVA_MAJOR}+."
        )
        warn(
            f"Installa Java {REQUIRED_JAVA_MAJOR} "
            f"(es. su Debian/Ubuntu: 'sudo apt install openjdk-{REQUIRED_JAVA_MAJOR}-jre')."
        )
        return False
    try:
        result = subprocess.run(
            ["java", "-version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        first_line = (result.stdout or result.stderr).splitlines()[0]
    except (subprocess.SubprocessError, OSError, IndexError):
        warn("Impossibile determinare la versione di Java.")
        return False
    import re

    m = re.search(r'version "(\d+)', first_line)
    if not m:
        warn("Impossibile determinare la versione di Java.")
        return False
    ver = int(m.group(1))
    if ver < REQUIRED_JAVA_MAJOR:
        warn(f"Java {ver} trovato, ma signal-cli richiede Java {REQUIRED_JAVA_MAJOR}+.")
        warn(
            f"Aggiorna Java (es. su Debian/Ubuntu: "
            f"'sudo apt install openjdk-{REQUIRED_JAVA_MAJOR}-jre')."
        )
        return False
    ok(f"Java {ver} trovato.")
    return True


def download_signal_cli(version: str) -> None:
    url = f"https://github.com/{REPO}/releases/download/v{version}/signal-cli-{version}.tar.gz"
    tarball = Path(tempfile.gettempdir()) / f"signal-cli-{version}.tar.gz"

    info(f"Scaricamento signal-cli v{version} (build JVM completa)...")
    info(f"  {url}")
    try:
        urllib.request.urlretrieve(url, tarball)
    except (urllib.error.URLError, OSError) as exc:
        die(f"Download fallito. Verifica la versione '{version}'. ({exc})")
        return

    info(f"Estrazione in {BIN_DIR} ...")
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(tarball) as tar:
            tar.extractall(BIN_DIR, filter="data")  # type: ignore[call-arg]
    except TypeError:
        with tarfile.open(tarball) as tar:
            tar.extractall(BIN_DIR)
    tarball.unlink(missing_ok=True)

    exe = BIN_DIR / f"signal-cli-{version}" / "bin" / "signal-cli"
    if not exe.is_file():
        die(
            f"Struttura inattesa: '{exe}' non trovato. "
            "La build scaricata non è quella JVM completa."
        )
        return
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    ok(f"signal-cli v{version} installato in {BIN_DIR / f'signal-cli-{version}'}/")


def remove_old_versions(keep: str) -> None:
    if not BIN_DIR.is_dir():
        return
    import shutil

    for d in BIN_DIR.glob("signal-cli-*"):
        if d.is_dir() and d.name != f"signal-cli-{keep}":
            info(f"Rimozione vecchia versione: {d.name}")
            shutil.rmtree(d, ignore_errors=True)


def install_python_deps(do_venv: bool, do_web: bool) -> bool:
    if do_venv:
        venv_dir = PROJECT_DIR / ".venv"
        if not venv_dir.is_dir():
            info(f"Creazione virtualenv in {venv_dir} ...")
            result = subprocess.run(
                [sys.executable, "-m", "venv", str(venv_dir)], check=False
            )
            if result.returncode != 0:
                die("Creazione del virtualenv fallita.")
                return do_web
        else:
            info(f"Virtualenv già presente in {venv_dir}")
        python_cmd = str(venv_dir / "bin" / "python")
    else:
        python_cmd = sys.executable

    info("Installazione delle dipendenze Python (requirements.txt) ...")
    subprocess.run(
        [python_cmd, "-m", "pip", "install", "--upgrade", "pip"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    result = subprocess.run(
        [
            python_cmd,
            "-m",
            "pip",
            "install",
            "-r",
            str(PROJECT_DIR / "requirements.txt"),
        ],
        check=False,
    )
    if result.returncode != 0:
        die("Installazione delle dipendenze Python fallita.")
        return do_web

    if do_web:
        result = subprocess.run(
            [
                python_cmd,
                "-m",
                "pip",
                "install",
                "-r",
                str(PROJECT_DIR / "requirements-web.txt"),
            ],
            check=False,
        )
        if result.returncode != 0:
            warn(
                "Dipendenze Web UI non installate; la Web UI resterà disabilitata. "
                f"Riprovare: {python_cmd} -m pip install -r requirements-web.txt"
            )
            do_web = False
    else:
        info("Dipendenze Web UI saltate (--no-web).")

    if do_venv:
        ok("Dipendenze installate nel virtualenv. Attivalo con:")
        print(f"    source {PROJECT_DIR}/.venv/bin/activate")
    else:
        ok("Dipendenze installate nel Python di sistema.")
    return do_web


def run_aliases_only() -> int:
    info("Aggiunta alias web…")
    ensure_web_config()
    return 0 if install_aliases() else 1


def run(args) -> int:
    do_venv = not args.no_venv
    do_signal_cli = not args.skip_signal_cli
    do_web = not args.no_web
    do_whatsapp = args.whatsapp
    do_check_whatsapp = args.check_whatsapp
    do_docker_limits = not args.no_docker_limits
    specific_version = args.version

    if args.aliases:
        return run_aliases_only()

    print()
    print(f"{C_BOLD}=== Signal TUI Client — Installazione ==={C_RESET}")
    print()

    if not command_exists("tar"):
        die("Comando 'tar' non trovato. Installalo e riprova.")
        return 1

    if args.update:
        info("Modalità aggiornamento signal-cli ...")
        latest = get_latest_version()
        if not latest:
            die("Impossibile determinare l'ultima versione di signal-cli.")
            return 1
        installed = get_installed_version()
        if installed and installed == latest:
            ok(f"signal-cli è già all'ultima versione ({latest}).")
        else:
            info(f"Versione installata: {installed or 'nessuna'} → ultima: {latest}")
            download_signal_cli(latest)
            remove_old_versions(latest)
            ok(f"signal-cli aggiornato alla versione {latest}.")
        print()
        print(f"{C_GREEN}{C_BOLD}=== Aggiornamento completato ==={C_RESET}")
        return 0

    check_python()
    check_java()  # warning-only, never fatal (mirrors `check_java || true`)

    if do_signal_cli:
        version = specific_version
        if not version:
            version = get_latest_version()
            if not version:
                die("Impossibile determinare l'ultima versione di signal-cli.")
                return 1
            info(f"Ultima versione di signal-cli rilevata: {version}")
        installed = get_installed_version()
        if installed and installed == version:
            ok(f"signal-cli v{version} già installato. (usa --update per aggiornare)")
        else:
            download_signal_cli(version)
            remove_old_versions(version)
    else:
        info("Download di signal-cli saltato (--skip-signal-cli).")
        installed = get_installed_version()
        if not installed:
            warn(
                f"Nessuna versione di signal-cli trovata in {BIN_DIR}. Il client non funzionerà."
            )
        else:
            ok(f"signal-cli v{installed} trovato in {BIN_DIR}.")

    do_web = install_python_deps(do_venv, do_web)

    if do_web:
        ensure_web_config()

    if do_check_whatsapp:
        whatsapp_mod.setup(should_start=False, docker_limits=do_docker_limits)
    elif do_whatsapp:
        whatsapp_mod.setup(should_start=True, docker_limits=do_docker_limits)

    if not install_aliases():
        warn("Installazione degli alias shell non riuscita; l'installazione continua.")

    print()
    print(f"{C_GREEN}{C_BOLD}=== Installazione completata! ==={C_RESET}")
    print()
    print("Prossimi passi:")
    print("  1. Configura il tuo numero di telefono:")
    print('       export SIGNAL_USER_NUMBER="+1234567890"')
    print("     oppure crea config.json:")
    print("""       echo '{"user_number": "+1234567890"}' > config.json""")
    print("  2. Collega il tuo account Signal (QR code):")
    if do_venv:
        print("       source .venv/bin/activate")
    print("       python3 link_account.py")
    if do_whatsapp:
        print("  3. Collega WhatsApp (QR code):")
        if do_venv:
            print("       source .venv/bin/activate")
        print("       python3 link_whatsapp.py")
        print("  4. Avvia il client:")
    else:
        print("  3. Avvia il client:")
    if do_venv:
        print("       source .venv/bin/activate")
    print("       python3 signal_tui.py")
    if do_web:
        print("       Web UI: http://127.0.0.1:4242")
        print("       Token:  config.json → web.token")
    print()
    if not do_whatsapp and not do_check_whatsapp and command_exists("docker"):
        print(f"{C_YELLOW}Suggerimento:{C_RESET} Hai Docker installato.")
        print(
            f"  Per usare anche WhatsApp esegui:  "
            f"{C_BOLD}python3 launcher.py install --whatsapp{C_RESET}"
        )
        print(
            f"  Per solo verificare i prerequisiti: "
            f"{C_BOLD}python3 launcher.py install --check-whatsapp{C_RESET}"
        )
        print()
    return 0
