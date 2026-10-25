"""Client-side startup of the TUI: find a server, or spawn one (design §5).

Pure logic, no UI: callers (``signal_tui.main``) print :class:`AttachError`
messages.  Must not import Textual, ``tui``, ``web`` or ``protocols``.

Decision tree of :func:`attach_or_spawn`:

1. ``connect_path`` given (``--connect PATH``): connect there; if nothing
   answers raise :class:`AttachError`.  Never spawns.
2. A live server answers on the default socket path (``$MEGACHAT_SOCKET`` /
   XDG / ``~/.local/state``): attach (``spawned=False``).  A server that
   answers with another wire version is an error, not a reason to spawn.
3. Otherwise spawn ``megachat serve`` detached and connect to it as soon as
   its socket answers (``spawned=True``).  If the spawned process exits first
   and the instance lock is held by someone else, another TUI won the spawn
   race: keep waiting and attach to that server (``spawned=False``).  If it
   exits and nobody holds the lock, raise :class:`AttachError` with the tail of
   its output.  On timeout or Ctrl+C the process we spawned is stopped.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from server import lock
from server.paths import default_socket_path, runtime_file_path
from server.socket_client import IncompatibleServer, ServerUnavailable, SocketClient

logger = logging.getLogger("signal_tui")

__all__ = [
    "SERVE_LOG",
    "AttachError",
    "Attached",
    "attach_or_spawn",
    "serve_args_for_web",
    "spawn_server",
]

#: stdout/stderr of a spawned ``megachat serve`` (the server's own log stays
#: ``/tmp/signal-tui.log``).
SERVE_LOG = Path("/tmp/megachat-serve.log")
#: Seconds to wait for a spawned server's socket.
SPAWN_TIMEOUT_S = 30.0
#: Seconds to wait for a live server to answer the first handshake.
PROBE_TIMEOUT_S = 2.0
_REPO_DIR = Path(__file__).resolve().parent.parent
_TAIL_LINES = 15


class AttachError(Exception):
    """No server could be reached or started; the message is user facing."""


@dataclass
class Attached:
    """A connected client and how we got it."""

    client: SocketClient
    #: ``True`` when this call spawned the server (the exit dialog then
    #: defaults to Quit), ``False`` when it attached to one already running.
    spawned: bool
    path: Path


def serve_args_for_web(
    *,
    web: bool,
    web_port: int | None = None,
    web_host: str | None = None,
    web_no_auth: bool = False,
) -> list[str]:
    """``megachat serve`` web flags for the TUI's: the server serves the web UI
    only when *web* is wanted, otherwise ``--no-web``."""
    if not web:
        return ["--no-web"]
    args: list[str] = []
    if web_port is not None:
        args += ["--web-port", str(web_port)]
    if web_host is not None:
        args += ["--web-host", web_host]
    if web_no_auth:
        args.append("--web-no-auth")
    return args


def _tail(path: Path, offset: int) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(text.strip().splitlines()[-_TAIL_LINES:])


def spawn_server(
    serve_args: list[str], socket_path: Path, log_path: Path = SERVE_LOG
) -> tuple[subprocess.Popen, int]:
    """Start ``megachat serve`` detached; return ``(process, log_offset)``.

    Same interpreter and checkout as this process, a new session (it survives
    the TUI and its terminal, Ctrl+C in the TUI does not reach it), stdin from
    ``/dev/null``, stdout/stderr appended to *log_path* (*log_offset* is where
    this run's output starts).  A daemon thread reaps the child so it never
    lingers as a zombie while the TUI runs.
    """
    with open(log_path, "ab") as log:
        offset = log.tell()
        proc = subprocess.Popen(
            [
                sys.executable,
                str(_REPO_DIR / "megachat.py"),
                "serve",
                "--socket",
                str(socket_path),
                *serve_args,
            ],
            cwd=_REPO_DIR,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        )
    threading.Thread(
        target=proc.wait, name="megachat-serve-reaper", daemon=True
    ).start()
    return proc, offset


def _stop_spawned(proc: subprocess.Popen) -> None:
    """Ask the server we spawned to stop (clean SIGTERM, then SIGKILL)."""
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    except OSError:
        pass


def _probe(path: Path, timeout: float) -> SocketClient | None:
    """A connected client if a live server answers at *path*, else ``None``.
    :class:`IncompatibleServer` propagates."""
    try:
        return SocketClient.connect(path, timeout=timeout)
    except ServerUnavailable:
        return None


def attach_or_spawn(
    *,
    connect_path: str | Path | None = None,
    serve_args: list[str] | None = None,
    spawn_timeout: float = SPAWN_TIMEOUT_S,
    probe_timeout: float = PROBE_TIMEOUT_S,
    log_path: Path | None = None,
    spawn: Callable[..., tuple[subprocess.Popen, int]] | None = None,
    notify: Callable[[str], None] | None = None,
) -> Attached:
    """Run the decision tree (module docstring); raises :class:`AttachError`."""
    try:
        return _attach_or_spawn(
            connect_path,
            serve_args or [],
            spawn_timeout,
            probe_timeout,
            SERVE_LOG if log_path is None else log_path,
            spawn_server if spawn is None else spawn,
            notify or (lambda _msg: None),
        )
    except IncompatibleServer as exc:
        raise AttachError(
            f"The server is incompatible with this client: {exc.message}"
        ) from exc


def _attach_or_spawn(
    connect_path, serve_args, spawn_timeout, probe_timeout, log_path, spawn, notify
) -> Attached:
    if connect_path is not None:
        path = Path(connect_path).expanduser()
        client = _probe(path, probe_timeout)
        if client is None:
            raise AttachError(f"No Megachat server answers on {path} (--connect).")
        return Attached(client, False, path)

    path = default_socket_path()
    client = _probe(path, probe_timeout)
    if client is not None:
        return Attached(client, False, path)

    notify("Starting the Megachat server (megachat serve)...")
    proc, offset = spawn(serve_args, path, log_path)
    logger.info("Spawned megachat serve (pid %s) for %s", proc.pid, path)
    deadline = time.monotonic() + spawn_timeout
    try:
        while True:
            exited = proc.poll() is not None
            client = _probe(path, 0.0 if not exited else probe_timeout)
            if client is not None:
                # We spawned a process, but if it lost the lock race the server
                # that answers belongs to another TUI.
                return Attached(client, _is_ours(path, proc, exited), path)
            if exited:
                holder = lock.lock_holder()
                if holder is None or holder == os.getpid():
                    raise AttachError(_early_exit_message(proc, log_path, offset))
                logger.info("Lost the spawn race to pid %s: waiting for it", holder)
            if time.monotonic() >= deadline:
                _stop_spawned(proc)
                raise AttachError(
                    f"The server did not answer on {path} within "
                    f"{spawn_timeout:.0f}s." + _log_hint(log_path, offset)
                )
            time.sleep(0.1)
    except BaseException:
        _stop_spawned(proc)
        raise


def _is_ours(path: Path, proc: subprocess.Popen, exited: bool) -> bool:
    """Whether the server answering on *path* is the process we spawned
    (runtime file pid; without it, whether our process is still alive)."""
    try:
        info = json.loads(runtime_file_path(path).read_text(encoding="utf-8"))
        return info.get("pid") == proc.pid
    except (OSError, ValueError, AttributeError):
        return not exited


def _log_hint(log_path: Path, offset: int) -> str:
    tail = _tail(log_path, offset)
    hint = f"\nServer log: /tmp/signal-tui.log, output: {log_path}"
    return hint + (f"\n{tail}" if tail else "")


def _early_exit_message(proc: subprocess.Popen, log_path: Path, offset: int) -> str:
    return (
        f"The server (megachat serve) exited with code {proc.returncode} "
        "before accepting connections." + _log_hint(log_path, offset)
    )
