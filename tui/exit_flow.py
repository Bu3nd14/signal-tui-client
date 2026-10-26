"""What quitting does for a TUI that is a client of a server (design §5).

======================  ===================================================
Where it comes from      Result
======================  ===================================================
``--in-process``         quit as before (the TUI owns the core): no dialog
Ctrl+Q, server reachable dialog: Detach / Quit / Cancel
Ctrl+Q, server lost      no dialog, exit (nothing to detach from)
Ctrl+C / SIGINT          Detach, no dialog (never stops a shared server)
======================  ===================================================

Detach closes the client; Quit asks the server to stop (``server.shutdown``),
waits for it to be gone, closes the client and exits.  A Quit that cannot reach
the server exits anyway; any other error is shown and the TUI stays.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from server.api import ApiError
from server.attach import server_pid, wait_server_stopped
from server.socket_client import ServerUnavailable
from tui.client_timeouts import call_op
from tui.exit_dialog import ExitDialogScreen

logger = logging.getLogger("signal_tui")

MSG_STOPPING = "Arresto del server in corso…"
MSG_STOP_FAILED = "⚠️ Arresto del server non riuscito: {error}"
MSG_STILL_RUNNING = "⚠️ Il server non si e' ancora fermato"

#: Seconds to wait for the server process to exit after it acknowledged.
STOP_WAIT_S = 30.0


class ExitFlowMixin:
    """``SignalTUI``'s quit routing, dialog and Detach/Quit actions."""

    _owns_core: bool = True
    _exit_pending: bool = False
    #: Set when the app is leaving, to cut a Quit's wait for the server short.
    _exit_cancel: threading.Event | None = None

    def _init_exit_state(self) -> None:
        self._exit_pending = False
        self._exit_cancel = threading.Event()

    # -- entry points ---------------------------------------------------------

    def _cancel_exit_wait(self) -> None:
        event = self._exit_cancel
        if event is not None:
            event.set()

    def _request_exit(self) -> None:
        """Ctrl+Q.  UI thread."""
        if self._owns_core:
            self._finish_exit()  # in-process: the TUI owns the core
            return
        if self._exit_pending:
            return
        if not getattr(self.client, "connected", True):
            self._finish_exit()  # server lost: nothing to detach from
            return
        self._exit_pending = True
        self._offload(self._exit_probe_worker)

    def _finish_exit(self) -> None:
        """Stop polling and leave; ``on_exit_app`` closes the client."""
        self._polling_active = False
        self.exit()

    # -- dialog ---------------------------------------------------------------

    def _exit_probe_worker(self) -> None:
        """Worker: how many other clients are there?  Then ask the user."""
        others = 0
        try:
            data = call_op(self.client, "server.clients")
            others = _other_clients(data)
        except ServerUnavailable:
            self._exit_pending = False
            self._ui(self._finish_exit)
            return
        except Exception:
            logger.debug("server.clients failed; asking without a count", exc_info=True)
        self._ui(self._show_exit_dialog, others)

    def _show_exit_dialog(self, others: int) -> None:
        screen = ExitDialogScreen(
            default_quit=self.server_spawned is True,
            other_clients=others,
        )
        self.push_screen(screen, self._on_exit_choice)

    def _on_exit_choice(self, choice: str | None) -> None:
        if choice == "detach":
            self._finish_exit()
        elif choice == "quit":
            self._status(MSG_STOPPING, 0)
            self._offload(self._quit_server_worker)
        else:
            self._exit_pending = False

    # -- Quit -----------------------------------------------------------------

    def _quit_server_worker(self) -> None:
        """Worker: ``server.shutdown``, wait for the server to be gone, exit."""
        path = getattr(self.client, "path", None)
        pid = server_pid(path) if path is not None else None
        try:
            call_op(self.client, "server.shutdown")
        except ServerUnavailable:
            self._ui(self._finish_exit)
            return
        except ApiError as exc:  # also timeouts and an incompatible server
            logger.warning("server.shutdown failed: %s", exc)
            self._exit_pending = False
            message = getattr(exc, "message", None) or str(exc)
            self._ui(self._status, MSG_STOP_FAILED.format(error=message), 8.0)
            return
        if path is not None and not wait_server_stopped(
            path,
            pid,
            STOP_WAIT_S,
            cancelled=lambda: bool(self._exit_cancel and self._exit_cancel.is_set()),
        ):
            logger.warning("The server did not stop within %ss", STOP_WAIT_S)
        self._ui(self._finish_exit)


def _other_clients(data: Any) -> int:
    """Clients other than this one in a ``server.clients`` answer."""
    connections = int(data.get("connections", 0))
    if data.get("self_included"):
        connections -= 1
    return max(0, connections) + max(0, int(data.get("web", 0)))
