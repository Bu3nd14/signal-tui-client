"""Client plumbing of the TUI: workers, error mapping, connection state, resync.

The TUI talks to the server through a ``ChatClient``; with a ``SocketClient``
every call is a round trip that can be slow or fail.  Rules this module
supports:

- no client call on the UI thread: a call site runs in a worker (``_offload``)
  and applies its result on the UI thread (``_ui``);
- every call has an explicit timeout (``tui.client_timeouts``);
- transport errors (``ServerUnavailable``, ``OperationTimeout``,
  ``IncompatibleServer``) become a status message, never a crash or a flood
  (``_report_client_error``);
- after a ``gap`` or a restored connection the TUI resyncs from the server
  (``_resync_once``): contacts, message caches, the open chat, unread badges.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from contact_picker import configure_picker
from models import ChatContact
from server.api import ApiError
from server.events_api import contact_from_wire
from server.socket_client import (
    IncompatibleServer,
    OperationTimeout,
    ServerUnavailable,
)
from tui.client_timeouts import call_op

logger = logging.getLogger("signal_tui")

#: Status texts of the connection to the server (Italian, like the other
#: connection/address-book messages of the TUI).
MSG_UNAVAILABLE = "⚠️ Server non raggiungibile — riconnessione…"
MSG_RESTORED = "✅ Riconnesso al server — risincronizzazione…"
MSG_RESYNCED = "✅ Dati risincronizzati con il server"
MSG_INCOMPATIBLE = (
    "❌ Server incompatibile con questa versione di Megachat — "
    "aggiornare client e server"
)

#: Minimum seconds between two transient "server problem" messages.
_ERROR_STATUS_INTERVAL = 5.0
#: Minimum seconds between two resyncs started by timeouts.
_TIMEOUT_RESYNC_INTERVAL = 10.0
#: Own messages of the open chat reconciled after a resync.
_RECONCILE_OWN_MESSAGES = 50
#: A pending bubble is only declared lost when the server has no outgoing row
#: with the same text within this many ms of it.
_PENDING_MATCH_WINDOW_MS = 600_000


def is_transport_error(exc: BaseException) -> bool:
    """True for the client-side errors of a remote transport."""
    return isinstance(exc, (ServerUnavailable, OperationTimeout, IncompatibleServer))


def is_unknown_outcome(exc: BaseException) -> bool:
    """True when a mutation that raised *exc* may or may not have been applied."""
    return isinstance(exc, (ServerUnavailable, OperationTimeout))


class ClientCallsMixin:
    """Thread helpers, error reporting and resync for the ``ChatClient`` calls."""

    # Class-level defaults: plain flags need no ``__init__`` ordering.
    _server_lost: bool = False
    _incompatible_shown: bool = False
    _last_error_status: float = 0.0
    _last_timeout_resync: float = 0.0
    _resync_running: bool = False
    _resync_again: bool = False
    _open_contact_token: int = 0
    _owns_core: bool = True
    _thumbnail_limits_cache: tuple[int, int] | None = None

    def _init_client_state(self) -> None:
        """Per-instance state (called from ``SignalTUI.__init__``)."""
        self._client_state_lock = threading.Lock()

    # ─── Threads ───────────────────────────────────────────────────────────

    def _ui(self, fn: Callable[..., Any], *args: Any) -> None:
        """Run ``fn(*args)`` on the UI thread (inline when already there)."""
        if self._thread_id == threading.get_ident():
            fn(*args)
            return
        try:
            self.call_from_thread(fn, *args)
        except RuntimeError:
            logger.debug("UI call dropped: app not running", exc_info=True)

    def _offload(self, work: Callable[[], None]) -> None:
        """Run blocking *work* off the UI thread.

        Without a running app (plain unit tests: no UI thread, no worker loop)
        the work runs inline.
        """
        if self.is_running:
            self.run_worker(work, thread=True, exclusive=False)
        else:
            work()

    def _call_op(self, op: str, data: dict[str, Any] | None = None) -> Any:
        """``client.call`` with the configured timeout.  Worker threads only."""
        return call_op(self.client, op, data)

    # ─── Error mapping ─────────────────────────────────────────────────────

    def _report_client_error(self, exc: BaseException, what: str = "") -> bool:
        """Show a transport error in the status bar; ``True`` when it was one.

        Callable from any thread.  ``ServerUnavailable`` and timeouts show a
        transient message at most every few seconds (and nothing while the
        persistent "lost" message is on display); ``IncompatibleServer``
        shows a persistent one once.  Other errors return ``False``: the
        caller keeps its own ``ApiError`` handling.
        """
        if not is_transport_error(exc):
            return False
        self._ui(self._show_client_error, exc, what)
        return True

    def _show_client_error(self, exc: BaseException, what: str) -> None:
        """UI thread: the status message of one transport error."""
        if isinstance(exc, IncompatibleServer):
            if not self._incompatible_shown:
                self._incompatible_shown = True
                self._status(MSG_INCOMPATIBLE, 0)
            return
        if isinstance(exc, ServerUnavailable) and self._server_lost:
            return  # the persistent "lost" message is already on display
        now = time.monotonic()
        if now - self._last_error_status < _ERROR_STATUS_INTERVAL:
            return
        self._last_error_status = now
        if isinstance(exc, ServerUnavailable):
            self._status(MSG_UNAVAILABLE, _ERROR_STATUS_INTERVAL)
        else:
            suffix = f" ({what})" if what else ""
            self._status(f"⚠️ Il server non risponde{suffix}", _ERROR_STATUS_INTERVAL)

    def _note_unknown_outcome(self, exc: BaseException) -> None:
        """A mutation raised *exc*: its outcome is unknown, so resync.

        A timeout is not followed by any connection event, so it asks for a
        resync itself (rate limited); an unavailable server resyncs when the
        connection is restored.
        """
        if not isinstance(exc, OperationTimeout):
            return
        now = time.monotonic()
        if now - self._last_timeout_resync < _TIMEOUT_RESYNC_INTERVAL:
            return
        self._last_timeout_resync = now
        self._ui(self._schedule_resync, "timeout")

    # ─── Connection events ─────────────────────────────────────────────────

    def _on_client_connection(self, payload: dict[str, Any]) -> None:
        """UI thread: the client lost or restored its connection."""
        state = payload.get("state")
        if state == "lost":
            if not self._server_lost:
                self._server_lost = True
                logger.info("Server connection lost: %s", payload.get("reason"))
                self._status(MSG_UNAVAILABLE, 0)
        elif state == "restored":
            self._server_lost = False
            logger.info("Server connection restored")
            self._status(MSG_RESTORED)
            self._schedule_resync("restored")

    # ─── Server config ─────────────────────────────────────────────────────

    def _start_server_config_fetch(self) -> None:
        """Fetch ``server.config`` off the UI thread (at mount and on resync)."""
        self._offload(self._server_config_worker)

    def _server_config_worker(self) -> None:
        """Worker: fetch the settings and apply them on the UI thread."""
        try:
            config = self._call_op("server.config")
        except ApiError as exc:
            logger.debug("server.config unavailable; defaults stay", exc_info=True)
            self._report_client_error(exc, "configurazione")
            return
        except Exception:
            logger.debug("server.config unavailable; defaults stay", exc_info=True)
            return
        self._ui(self._apply_server_config, config)

    def _apply_server_config(self, config: dict[str, Any]) -> None:
        """UI thread: thumbnail limits and picker options from ``server.config``."""
        try:
            self._thumbnail_limits_cache = (
                int(config["thumbnail_max_lines"]),
                int(config["thumbnail_max_cols"]),
            )
            configure_picker(
                max_results=int(config["picker_max_results"]),
                preferred_backend=str(config["picker_preferred_backend"]),
            )
        except Exception:
            logger.debug("server.config malformed; defaults stay", exc_info=True)

    # ─── Resync ────────────────────────────────────────────────────────────

    def _schedule_resync(self, reason: str) -> None:
        """UI thread: resync from the server (one at a time, coalesced)."""
        with self._client_state_lock:
            if self._resync_running:
                self._resync_again = True
                return
            self._resync_running = True
        logger.info("Resync from the server (%s)", reason)
        if self.is_running:
            self.run_worker(
                self._resync_worker, thread=True, exclusive=False, group="resync"
            )
        else:
            self._resync_worker()

    def _resync_worker(self) -> None:
        """Worker: resync until no further request arrived meanwhile."""
        try:
            while True:
                ok = self._resync_once()
                with self._client_state_lock:
                    again = self._resync_again and ok
                    self._resync_again = False
                    if not again:
                        self._resync_running = False
                        return
        except BaseException:
            with self._client_state_lock:
                self._resync_running = False
                self._resync_again = False
            raise

    def _resync_once(self) -> bool:
        """Worker: fetch the server's state, apply it on the UI thread.

        Fetches the backend status, then contacts and message cache of every
        connected protocol, then the open chat's window.  A transport error
        aborts (reported, no retry: the next connection event resyncs again).
        """
        try:
            status = self._call_op("server.status")
            per_protocol: dict[str, tuple[dict | None, list[ChatContact]]] = {}
            for proto, connected in status.items():
                if not connected:
                    continue
                try:
                    snapshot = self._call_op("messages.snapshot", {"protocol": proto})
                    wire = self._call_op("contacts.for_protocol", {"protocol": proto})
                except ApiError as exc:
                    if is_transport_error(exc):
                        raise
                    logger.warning("Resync of %s failed: %s", proto, exc)
                    continue
                per_protocol[proto] = (
                    snapshot,
                    [contact_from_wire(w) for w in wire],
                )
            contact = self.selected_contact
            rows: list[dict] | None = None
            if contact is not None:
                try:
                    window = self._call_op(
                        "messages.window",
                        {"protocol": contact.protocol, "contact_id": contact.id},
                    )
                    rows = window["messages"]
                except ApiError as exc:
                    if is_transport_error(exc):
                        raise
                    logger.warning("Resync of the open chat failed: %s", exc)
            if self._thumbnail_limits_cache is None:
                self._server_config_worker()
        except ApiError as exc:
            logger.info("Resync aborted: %s", exc)
            self._report_client_error(exc, "risincronizzazione")
            return False
        self._ui(self._apply_resync, per_protocol, contact, rows)
        return True

    def _apply_resync(
        self,
        per_protocol: dict[str, tuple[dict | None, list[ChatContact]]],
        contact: ChatContact | None,
        rows: list[dict] | None,
    ) -> None:
        """UI thread: merge the fetched state, add-only and without duplicates.

        Contacts merge by ``cache_key``; message caches and the open chat's
        window go through the same dedup merges as ``backend_ready`` and the
        history refresh.  A change in the open chat re-renders its window
        (``_render_chat_window`` clears and remounts, so no bubble is mounted
        twice).
        """
        selected = self.selected_contact
        before = self._chat_signature(selected)
        for proto, (snapshot, contacts) in per_protocol.items():
            self._merge_ready_backend_cache(proto, snapshot)
            self._merge_backend_contacts(contacts)

        render = False
        if (
            rows is not None
            and contact is not None
            and selected is not None
            and selected.cache_key == contact.cache_key
        ):
            render = self._merge_window_rows(selected, rows)
            self._reconcile_pending_sends(selected, rows)
        # The snapshot merge above may already have added the news.
        render = render or self._chat_signature(selected) != before
        if selected is not None:
            self._mark_open_chat_read(selected)

        self._sync_last_ts()
        self._sort_contacts()
        self._render_contact_list(list(self.contacts))
        self._update_unread_badges()
        self._refresh_backend_status_if_idle()
        if render and selected is not None:
            self._rerender_open_chat(selected)
        elif selected is not None:
            self._refresh_own_statuses(selected)
        self._status(MSG_RESYNCED)

    def _chat_signature(self, contact: ChatContact | None) -> list[tuple]:
        """What a re-render of *contact*'s chat would show (identity and text)."""
        if contact is None:
            return []
        return [
            (m.get("id"), m.get("text"), m.get("timestamp"))
            for m in self._cache.get(contact.cache_key, [])
        ]

    def _mark_open_chat_read(self, contact: ChatContact) -> None:
        """Messages merged into the open chat are being viewed: not unread."""
        newly_read = False
        for msg in self._cache.get(contact.cache_key, []):
            if not msg.get("is_mine") and not msg.get("read", True):
                msg["read"] = True
                newly_read = True
        if newly_read:
            self._client_call_background(
                "chats.mark_read",
                {"protocol": contact.protocol, "contact_id": contact.id},
            )

    def _rerender_open_chat(self, contact: ChatContact) -> None:
        """UI thread: remount the open chat's window from the merged cache."""
        token = self._chat_reload_token

        def _is_stale() -> bool:
            return self._chat_reload_token != token or self.selected_contact != contact

        self._offload(lambda: self._render_chat_window(contact, _is_stale))

    def _refresh_own_statuses(self, contact: ChatContact) -> None:
        """UI thread: show the delivery status the cache now holds on the bubbles."""
        own = [
            {
                "timestamp": m.get("timestamp", 0),
                "text": m.get("text", ""),
                "status": m.get("status", "sent"),
                "id": m.get("id"),
            }
            for m in self._cache.get(contact.cache_key, [])
            if m.get("is_mine") and m.get("status")
        ][-_RECONCILE_OWN_MESSAGES:]
        if not own:
            return
        try:
            self._update_message_widgets_status(own)
        except Exception:
            logger.debug("Status refresh of the open chat failed", exc_info=True)

    def _reconcile_pending_sends(self, contact: ChatContact, rows: list[dict]) -> None:
        """Settle bubbles left ``pending`` by a send with an unknown outcome.

        The core seeds its row before it sends, so a pending bubble that the
        server knows (an outgoing row with the same text near its timestamp)
        is left alone: its status arrives with the merge or the events.  One
        the server has no trace of never reached it: mark it ``failed`` so the
        user can retry.
        """
        for msg in self._cache.get(contact.cache_key, []):
            if not msg.get("is_mine") or msg.get("status") != "pending":
                continue
            ts = int(msg.get("timestamp") or 0)
            text = msg.get("text", "")
            twins = [
                row
                for row in rows
                if row.get("is_mine")
                and row.get("text", "") == text
                and abs(int(row.get("timestamp") or 0) - ts) <= _PENDING_MATCH_WINDOW_MS
            ]
            if twins:
                # Known to the server.  The merge cannot move a bubble from
                # pending to failed (same rank): do it when the server says so.
                exact = [
                    r for r in twins if abs(int(r.get("timestamp") or 0) - ts) <= 5000
                ]
                statuses = {r.get("status") for r in exact}
                if "failed" not in statuses or statuses & {"sent", "delivered", "read"}:
                    continue
            msg["status"] = "failed"
            try:
                self._update_message_widgets_status(
                    [{"timestamp": ts, "text": msg.get("text", ""), "status": "failed"}]
                )
            except Exception:
                logger.debug("Failed-status update of a bubble failed", exc_info=True)
