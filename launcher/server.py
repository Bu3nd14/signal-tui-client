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
from dataclasses import asdict, dataclass
from pathlib import Path

from . import whatsapp as whatsapp_mod
from .common import PROJECT_DIR, command_exists, die, info, ok, port_listening, warn

# The instance lock belongs to the server (or to a ``megachat --in-process``
# TUI); same location and override as ``server/lock.py``.
LOCK_FILE = Path(os.environ.get("MEGACHAT_LOCK_FILE") or "/tmp/signal-tui.lock")
SERVER_LOG = Path("/tmp/megachat-serve.log")
SERVER_PORT = 4242
SERVER_HOST = "0.0.0.0"
#: Seconds to wait for the web adapter to listen after spawning the server.
READY_TIMEOUT_S = 30
#: Seconds to wait for a signalled server to exit before giving up.
STOP_TIMEOUT_S = 30


@dataclass(frozen=True)
class ServeOptions:
    """Options of ``megachat serve`` that the launcher passes through.

    The defaults are the launcher's historical ones: web on all interfaces,
    port 4242, token required, Unix socket on its default path.
    """

    web_host: str = SERVER_HOST
    web_port: int = SERVER_PORT
    web_no_auth: bool = False
    no_web: bool = False
    no_socket: bool = False
    socket: str | None = None
    debug: bool = False

    def serve_args(self) -> list[str]:
        """Arguments of ``.venv/bin/python`` that start this server."""
        args = ["megachat.py", "serve"]
        if self.no_web:
            args.append("--no-web")
        else:
            args += ["--web-port", str(self.web_port), "--web-host", self.web_host]
            if self.web_no_auth:
                args.append("--web-no-auth")
        if self.no_socket:
            args.append("--no-socket")
        elif self.socket:
            args += ["--socket", self.socket]
        if self.debug:
            args.append("--debug")
        return args

    def web_hosts(self) -> list[str]:
        """Every bind address of ``web_host`` (comma-separated)."""
        hosts = [h.strip() for h in self.web_host.split(",") if h.strip()]
        return hosts or [""]

    def probe_host(self) -> str:
        """Address to connect to when checking that the web UI listens."""
        host = self.web_hosts()[0]
        if host in ("0.0.0.0", "::", "", "localhost"):
            return "127.0.0.1"
        return host


#: Arguments of the default detached server (web on all interfaces; the Unix
#: socket is on by default).  The remote handover script is built from it.
SERVE_ARGS = ServeOptions().serve_args()


def _state_file() -> Path:
    """Options of the running server, written by ``start`` next to the lock."""
    return LOCK_FILE.with_name(LOCK_FILE.name + ".json")


def _write_state(pid: int, opts: ServeOptions) -> None:
    try:
        _state_file().write_text(json.dumps({"pid": pid, **asdict(opts)}))
    except OSError:
        pass  # status falls back to the defaults


def _read_state(pid: int | None) -> ServeOptions:
    """Options the running server *pid* was started with, else the defaults."""
    try:
        data = json.loads(_state_file().read_text())
        if pid is not None and data.pop("pid", None) == pid:
            return ServeOptions(**data)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return ServeOptions()


def _kill_tmux_session(name: str = "tui") -> None:
    """Kill a legacy tmux session (older versions ran the TUI inside tmux)."""
    try:
        subprocess.run(
            ["tmux", "kill-session", "-t", name], capture_output=True, check=False
        )
    except OSError:
        pass  # tmux not installed: nothing legacy to clean up


def _python() -> str:
    return str(PROJECT_DIR / ".venv" / "bin" / "python")


def _spawn_server(opts: ServeOptions) -> subprocess.Popen:
    """Start ``megachat serve`` detached from this terminal and session."""
    # The child inherits its own descriptor; closing ours after Popen is safe.
    with open(SERVER_LOG, "ab") as log:
        return subprocess.Popen(
            [_python(), *opts.serve_args()],
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
    _state_file().unlink(missing_ok=True)
    return True


def _ready(proc: subprocess.Popen, opts: ServeOptions) -> bool:
    if opts.no_web:
        # No port to probe: the server is up once it holds the lock.
        return server_pid() == proc.pid
    return port_listening(opts.web_port, opts.probe_host())


def spawn_and_wait(opts: ServeOptions | None = None) -> bool:
    """Spawn the detached server and wait until it is up.

    "Up" is the web adapter listening, or (with ``no_web``) the server
    holding the lock.  ``False`` when it exited early or never came up.
    ``OSError`` from the spawn itself propagates.
    """
    opts = opts or ServeOptions()
    proc = _spawn_server(opts)
    for _ in range(READY_TIMEOUT_S):
        if proc.poll() is not None:
            return False
        if _ready(proc, opts):
            _write_state(proc.pid, opts)
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


def _web_url(opts: ServeOptions) -> str:
    # The address worth sharing: the first one other machines can reach.
    hosts = opts.web_hosts()
    loopback = ("127.0.0.1", "::1", "localhost")
    host = next((h for h in hosts if h not in loopback), hosts[0])
    if host in ("0.0.0.0", "::", ""):
        host = _local_ip()
    return f"http://{host}:{opts.web_port}"


def _describe(opts: ServeOptions) -> str:
    if opts.no_web:
        return "senza Web UI"
    auth = ", senza token" if opts.web_no_auth else ""
    return f"con Web UI su {opts.web_host}:{opts.web_port}{auth}"


def start_all(
    *,
    docker_limits: bool = True,
    print_token: bool = True,
    opts: ServeOptions | None = None,
    whatsapp: bool = True,
    foreground: bool = False,
) -> int:
    opts = opts or ServeOptions()
    if whatsapp:
        info("Avvio WAHA (WhatsApp HTTP API)...")
        if whatsapp_mod.start(no_wait=False, docker_limits=docker_limits) == 0:
            ok("WAHA avviato e pronto")
            whatsapp_mod.describe_session(whatsapp_mod.session_status())
        else:
            info("WAHA non partito (vedi docker compose logs whatsapp)")

    loopback = all(
        host in ("127.0.0.1", "::1", "localhost") for host in opts.web_hosts()
    )
    if opts.web_no_auth and not opts.no_web and not loopback:
        warn(
            "Web UI senza token su un indirizzo raggiungibile dalla rete: chiunque "
            f"raggiunga {opts.web_host}:{opts.web_port} legge e invia messaggi."
        )
    info(f"Avvio il server headless (megachat serve) {_describe(opts)}...")
    # Restart semantics: stop a running instance (lock pid, SIGINT) and any
    # legacy tmux 'tui' session before taking the lock again.
    if not stop_server():
        die(
            f"Il server (pid {server_pid()}) non si ferma: non ne avvio un altro. "
            "Controlla: tail -50 /tmp/signal-tui.log"
        )
        return 1
    if foreground:
        # Same process, this terminal: Ctrl+C stops it cleanly.
        os.chdir(PROJECT_DIR)
        _write_state(os.getpid(), opts)  # exec keeps the pid
        os.execv(_python(), [_python(), *opts.serve_args()])
    try:
        up = spawn_and_wait(opts)
    except OSError as exc:
        die(f"Impossibile avviare il server: {exc}")
        return 1
    if up:
        if opts.no_web:
            ok("Server attivo (senza Web UI)")
        else:
            ok(f"Server attivo — Web UI su {_web_url(opts)}")
            if opts.web_no_auth:
                print("   Token Web UI: disattivato (--web-no-auth)")
            elif print_token:
                print(f"   Token Web UI: {_read_web_token()}")
        print("   Log:          tail -f /tmp/signal-tui.log")
        print("   Stop:         python3 launcher.py server stop")
        print("   Interattivo:  python3 megachat.py  (si collega al server)")
        return 0
    where = "senza Web UI" if opts.no_web else f"sulla porta {opts.web_port}"
    die(
        f"Il server non risulta attivo {where}. Controlla: "
        f"tail -50 /tmp/signal-tui.log {SERVER_LOG}"
    )
    return 1


def stop_all(*, whatsapp: bool = True) -> int:
    info("Spegnimento server (SIGINT pulito)...")
    if not stop_server():
        die(f"Il server (pid {server_pid()}) non si ferma; WAHA lasciato acceso.")
        return 1
    ok("Server fermato")
    if not whatsapp:
        info("WAHA lasciato acceso (--keep-whatsapp)")
        return 0

    info("Spegnimento WAHA...")
    result = subprocess.run(
        ["docker", "compose", *whatsapp_mod.compose_args(False), "down"],
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
    opts = _read_state(pid)
    if opts.no_web:
        print("Web UI: disattivata (--no-web)")
    elif port_listening(opts.web_port, opts.probe_host()):
        auth = ", senza token" if opts.web_no_auth else ""
        print(f"Web UI: in ascolto su {opts.web_host}:{opts.web_port}{auth}")
    else:
        print("Web UI: spenta")
    return 0
