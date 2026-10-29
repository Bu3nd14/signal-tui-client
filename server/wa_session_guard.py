"""WAHA session guard (issue #311): detect a FAILED session and recover it.

WhatsApp runs through WAHA (Docker, engine WEBJS).  When its headless page
hangs the session goes ``FAILED``; before this guard the app polled the
session only at startup (``ChatCore.connect_whatsapp_boot``) and never noticed
later.  The guard is a daemon thread owned by :class:`~server.core.ChatCore`
(mixed in) that polls the session status, restarts a FAILED session with a
bounded backoff, never restarts during QR/link/stopped states, alerts when WAHA
is unreachable and reconnects the backend once the session is WORKING again.

Client-side only: restart uses ``POST /api/sessions/{name}/restart`` (keeps
auth); logout/reset are never called.  Residual design: polling only, no
``session.status`` webhook (registering it means a session PUT, which while
WORKING risks a logout).

The decision logic is a pure step (:meth:`wa_session_guard_tick`) with an
injectable clock, so tests drive it without threads or sleeps; the thread is a
thin loop around it.  This module must not import Textual/FastAPI/tui/web.
"""

import logging
import threading
import time
from collections.abc import Callable

from protocols.config import (
    get_wa_session_backoff_s,
    get_wa_session_failed_grace_s,
    get_wa_session_guard,
    get_wa_session_max_restarts_per_hour,
    get_wa_session_poll_s,
    get_wa_session_restart_timeout_s,
    get_wa_session_stable_reset_s,
    get_wa_session_unreachable_after,
)

logger = logging.getLogger(__name__)

# Guard states.
UNKNOWN = "unknown"
HEALTHY = "healthy"
SUSPECT = "suspect"
RECOVERING = "recovering"
NEEDS_PAIRING = "needs_pairing"
STOPPED = "stopped"
UNREACHABLE = "unreachable"
GAVE_UP = "gave_up"

#: Poll interval (s) while a restart is in flight.
RECOVERY_POLL_S = 5.0
#: Pause (s) of automatic restarts after a manual link/reconnect.
MANUAL_COOLDOWN_S = 60.0
#: Sliding window (s) of the restarts-per-hour cap.
RESTART_WINDOW_S = 3600.0


class WhatsAppSessionGuardMixin:
    """Poll the WAHA session and auto-restart it when FAILED.

    The state attributes are initialised by :meth:`_init_wa_session_guard`
    (called from ``ChatCore.__init__``) so constructing a core stays
    thread-free until ``start()``.  Relies on ``whatsapp_backend``, ``api``,
    ``_status`` and ``_spawn`` / ``connect_whatsapp`` of the host class.
    """

    def _init_wa_session_guard(self) -> None:
        self._wa_guard_clock: Callable[[], float] = time.monotonic
        self._wa_guard_thread: threading.Thread | None = None
        self._wa_guard_stop = threading.Event()
        self._wa_guard_lock = threading.Lock()  # single-flight for the tick
        self._wa_guard_state = UNKNOWN
        self._wa_guard_failed_since: float | None = None
        self._wa_guard_unreachable = 0  # consecutive unreachable polls
        self._wa_guard_attempts = 0  # restarts since last stable reset
        self._wa_guard_restarts: list[float] = []  # restart call timestamps
        self._wa_guard_next_attempt_at = 0.0
        self._wa_guard_restart_started: float | None = None
        self._wa_guard_healthy_since: float | None = None
        self._wa_guard_restarting = False

    # -- lifecycle ---------------------------------------------------------

    def start_wa_session_guard(self) -> None:
        """Start the guard daemon thread (idempotent; no-op if disabled)."""
        if self.whatsapp_backend is None or not get_wa_session_guard():
            logger.info("WA session guard not started (no backend or disabled)")
            return
        thread = self._wa_guard_thread
        if thread is not None and thread.is_alive():
            return
        self._wa_guard_stop.clear()
        thread = threading.Thread(
            target=self._wa_session_guard_loop,
            name="wa-session-guard",
            daemon=True,
        )
        self._wa_guard_thread = thread
        thread.start()

    def stop_wa_session_guard(self) -> None:
        """Signal the guard thread to stop (no join: daemon thread)."""
        self._wa_guard_stop.set()

    def _wa_session_guard_loop(self) -> None:
        while not self._wa_guard_stop.is_set():
            try:
                delay = self.wa_session_guard_tick()
            except Exception:
                logger.debug("WA session guard tick failed", exc_info=True)
                delay = float(get_wa_session_poll_s())
            self._wa_guard_stop.wait(delay)

    def note_wa_session_manual(self) -> None:
        """A user link/reconnect happened: pause restarts, clear sticky states."""
        with self._wa_guard_lock:
            self._wa_guard_next_attempt_at = self._wa_guard_clock() + MANUAL_COOLDOWN_S
            if self._wa_guard_state in (GAVE_UP, NEEDS_PAIRING):
                self._wa_guard_state = UNKNOWN
                self._wa_guard_failed_since = None

    # -- decision step -----------------------------------------------------

    def wa_session_guard_tick(self) -> float:
        """One observation + decision; returns seconds until the next tick."""
        poll = float(get_wa_session_poll_s())
        if not self._wa_guard_lock.acquire(blocking=False):
            return poll  # single-flight
        try:
            return self._wa_guard_step(poll)
        finally:
            self._wa_guard_lock.release()

    def _wa_guard_poll(self, poll: float) -> float:
        return RECOVERY_POLL_S if self._wa_guard_state == RECOVERING else poll

    def _wa_guard_step(self, poll: float) -> float:
        backend = self.whatsapp_backend
        if backend is None:
            return poll
        proto = backend.protocol
        now = self._wa_guard_clock()
        info = backend.session_info()
        if info is None:
            return self._wa_guard_on_unreachable(proto, now, poll)
        self._wa_guard_unreachable = 0
        status = str(info.get("status") or "").upper()
        me = info.get("me")
        try:
            linking = bool(self.api.link.is_active(proto))
        except Exception:  # noqa: BLE001 - best effort, never block the guard
            linking = False
        if linking:
            return poll  # the link flow owns the session: hands off
        if status == "WORKING":
            return self._wa_guard_on_working(proto, now, poll, bool(me))
        self._wa_guard_healthy_since = None
        if status in ("SCAN_QR_CODE", "SCAN_QR"):
            return self._wa_guard_on_qr(proto, poll)
        if status == "STOPPED":
            return self._wa_guard_on_stopped(proto, poll)
        if self._wa_guard_state == RECOVERING and status in (
            "STARTING",
            "PENDING",
            "FAILED",
        ):
            return self._wa_guard_on_recovering(proto, now, status)
        if status == "FAILED":
            return self._wa_guard_on_failed(backend, proto, now, poll)
        # STARTING / PENDING outside a recovery, or unknown status: no action.
        return poll

    def _wa_guard_on_unreachable(self, proto: str, now: float, poll: float) -> float:
        self._wa_guard_unreachable += 1
        if (
            self._wa_guard_unreachable >= get_wa_session_unreachable_after()
            and self._wa_guard_state != UNREACHABLE
        ):
            was_recovering = self._wa_guard_state == RECOVERING
            self._wa_guard_state = UNREACHABLE
            self._wa_guard_restarting = False
            logger.warning("WA session guard: WAHA unreachable")
            self._status(proto, "⚠️ WAHA: non raggiungibile", True)
            if was_recovering:
                return poll
        elif self._wa_guard_state == RECOVERING:
            started = self._wa_guard_restart_started
            if started is not None and now - started >= (
                get_wa_session_restart_timeout_s()
            ):
                return self._wa_guard_restart_failed(now, poll)
        return self._wa_guard_poll(poll)

    def _wa_guard_on_working(
        self, proto: str, now: float, poll: float, has_me: bool
    ) -> float:
        if not has_me:
            return poll  # transient: WORKING but no account yet
        previous = self._wa_guard_state
        self._wa_guard_state = HEALTHY
        self._wa_guard_failed_since = None
        self._wa_guard_restarting = False
        self._wa_guard_restart_started = None
        if previous not in (UNKNOWN, HEALTHY):
            logger.info("WA session guard: session WORKING again (was %s)", previous)
            self._status(proto, "✅ WAHA: sessione ripristinata")
            self._spawn(self.connect_whatsapp, "core-connect-whatsapp")
        if self._wa_guard_healthy_since is None:
            self._wa_guard_healthy_since = now
        if now - self._wa_guard_healthy_since >= get_wa_session_stable_reset_s():
            self._wa_guard_attempts = 0
        return poll

    def _wa_guard_on_qr(self, proto: str, poll: float) -> float:
        self._wa_guard_restarting = False
        self._wa_guard_failed_since = None
        if self._wa_guard_state != NEEDS_PAIRING:
            self._wa_guard_state = NEEDS_PAIRING
            logger.warning("WA session guard: QR pairing required, no restart")
            self._status(
                proto, "📱 WhatsApp: ricollegamento necessario (QR) — Ctrl+L", True
            )
        return poll

    def _wa_guard_on_stopped(self, proto: str, poll: float) -> float:
        self._wa_guard_restarting = False
        self._wa_guard_failed_since = None
        if self._wa_guard_state != STOPPED:
            self._wa_guard_state = STOPPED
            logger.warning("WA session guard: session STOPPED, never auto-started")
            self._status(proto, "⏹️ WAHA: sessione ferma", True)
        return poll

    def _wa_guard_on_recovering(self, proto: str, now: float, status: str) -> float:
        """A restart is in flight and the session is STARTING/PENDING/FAILED."""
        started = self._wa_guard_restart_started
        poll = float(get_wa_session_poll_s())
        if started is None:
            started = now
        elapsed = now - started
        if elapsed >= get_wa_session_restart_timeout_s() or (
            status == "FAILED" and elapsed >= RECOVERY_POLL_S
        ):
            return self._wa_guard_restart_failed(now, poll)
        return RECOVERY_POLL_S

    def _wa_guard_backoff(self) -> float:
        backoff = get_wa_session_backoff_s()
        idx = min(max(self._wa_guard_attempts - 1, 0), len(backoff) - 1)
        return float(backoff[idx])

    def _wa_guard_restart_failed(self, now: float, poll: float) -> float:
        """The restart did not bring the session back: back off and re-arm."""
        self._wa_guard_restarting = False
        self._wa_guard_restart_started = None
        self._wa_guard_state = SUSPECT
        # Next FAILED counts as already confirmed (grace elapsed).
        self._wa_guard_failed_since = now - float(get_wa_session_failed_grace_s())
        self._wa_guard_next_attempt_at = now + self._wa_guard_backoff()
        logger.warning(
            "WA session guard: restart failed (attempt %d), retry in %.0fs",
            self._wa_guard_attempts,
            self._wa_guard_next_attempt_at - now,
        )
        return min(poll, max(1.0, self._wa_guard_next_attempt_at - now))

    def _wa_guard_on_failed(
        self, backend, proto: str, now: float, poll: float
    ) -> float:
        if self._wa_guard_state == GAVE_UP:
            return poll  # sticky until WORKING or a manual action
        grace = float(get_wa_session_failed_grace_s())
        if self._wa_guard_failed_since is None:
            self._wa_guard_failed_since = now
            self._wa_guard_state = SUSPECT
            logger.warning("WA session guard: session FAILED, confirming")
            if grace > 0:
                return max(grace, 1.0)
        remaining = grace - (now - self._wa_guard_failed_since)
        if remaining > 0:
            return max(remaining, 1.0)
        if now < self._wa_guard_next_attempt_at:
            return min(poll, max(1.0, self._wa_guard_next_attempt_at - now))
        cap = get_wa_session_max_restarts_per_hour()
        self._wa_guard_restarts = [
            t for t in self._wa_guard_restarts if now - t < RESTART_WINDOW_S
        ]
        if len(self._wa_guard_restarts) >= cap:
            self._wa_guard_state = GAVE_UP
            logger.warning("WA session guard: restart cap reached (%d/h)", cap)
            self._status(
                proto,
                "❌ WAHA: sessione FAILED, riavvio automatico sospeso (limite raggiunto)",
                True,
            )
            return poll
        if self._wa_guard_restarting:
            return RECOVERY_POLL_S
        return self._wa_guard_restart(backend, proto, now, poll, cap)

    def _wa_guard_restart(
        self, backend, proto: str, now: float, poll: float, cap: int
    ) -> float:
        self._wa_guard_restarting = True
        self._wa_guard_attempts += 1
        self._wa_guard_restarts.append(now)
        self._wa_guard_restart_started = now
        self._wa_guard_state = RECOVERING
        self._wa_guard_next_attempt_at = now + self._wa_guard_backoff()
        n = len(self._wa_guard_restarts)
        logger.warning("WA session guard: restarting FAILED session (%d/%d)", n, cap)
        self._status(proto, f"🔄 WAHA: sessione FAILED, riavvio ({n}/{cap})…", True)
        try:
            accepted = backend.restart_session()
        except Exception:
            logger.warning("WA session guard: restart call raised", exc_info=True)
            accepted = False
        if not accepted:
            return self._wa_guard_restart_failed(now, poll)
        return RECOVERY_POLL_S
