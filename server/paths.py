"""Where the server's Unix socket and runtime file live.

Shared by the socket adapter (which binds there) and the socket client (which
connects there), so both resolve the location identically.  Stdlib only.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["SOCKET_ENV", "default_socket_path", "runtime_file_path"]

SOCKET_ENV = "MEGACHAT_SOCKET"


def default_socket_path(env: dict[str, str] | None = None) -> Path:
    """Where the server listens: ``$MEGACHAT_SOCKET``, else
    ``$XDG_RUNTIME_DIR/megachat/server.sock``, else
    ``~/.local/state/megachat/server.sock``.  (An explicit ``--socket`` path
    overrides all of these at the call site.)"""
    env = os.environ if env is None else env
    override = env.get(SOCKET_ENV)
    if override:
        return Path(override).expanduser()
    runtime = env.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isabs(runtime):
        return Path(runtime) / "megachat" / "server.sock"
    return Path.home() / ".local" / "state" / "megachat" / "server.sock"


def runtime_file_path(socket_path: Path) -> Path:
    """The runtime file next to a socket (``server.sock`` -> ``server.json``)."""
    return Path(socket_path).with_suffix(".json")
