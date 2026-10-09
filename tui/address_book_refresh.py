"""Dynamic address-book refresh: single daemon worker + scheduler.

The worker covers the periodic trigger (catch-all) and the lazy trigger
(scoped by the protocol of an unresolved incoming message).  The picker/web
book use the force path directly (``list_address_book_sync(force=True)``).
"""

import logging
import threading
import time

from protocols.config import (
    get_dynamic_refresh_cooldown_s,
    get_dynamic_refresh_interval_s,
)

logger = logging.getLogger(__name__)


class DynamicAddressBookMixin:
    """Periodic + lazy address-book refresh on a single daemon worker.

    The state attributes (``_address_book_refresh_stop``, ``_address_book_refresh_wake``,
    ``_address_book_refresh_lock``, ``_address_book_last_periodic``,
    ``_address_book_last_lazy``, ``_address_book_pending_protocols`` and
    ``_address_book_refresh_thread``) are initialized by ``SignalTUI.__init__``
    so tests that patch ``on_mount`` stay thread-free.
    """

    def start_address_book_refresh(self) -> None:
        """Start the daemon refresh worker once (idempotent)."""
        thread = self._address_book_refresh_thread
        if thread is not None and thread.is_alive():
            return
        self._address_book_refresh_stop = False
        thread = threading.Thread(
            target=self._address_book_refresh_loop,
            name="address-book-refresh",
            daemon=True,
        )
        self._address_book_refresh_thread = thread
        thread.start()

    def _address_book_refresh_loop(self) -> None:
        interval = get_dynamic_refresh_interval_s()
        cooldown = get_dynamic_refresh_cooldown_s()
        self._address_book_last_periodic = time.monotonic()
        while not self._address_book_refresh_stop:
            now = time.monotonic()
            remaining = max(0.0, interval - (now - self._address_book_last_periodic))
            self._address_book_refresh_wake.wait(timeout=remaining)
            self._address_book_refresh_wake.clear()
            if self._address_book_refresh_stop:
                return
            now = time.monotonic()
            if now - self._address_book_last_periodic >= interval:
                # PERIODICO: incondizionato.
                self._address_book_last_periodic = now
                self._address_book_last_lazy = now
                self._run_refresh(protocols=None)
                continue
            # LAZY: soggetto a cooldown.
            with self._address_book_refresh_lock:
                if now - self._address_book_last_lazy < cooldown:
                    continue
                self._address_book_last_lazy = now
                protocols = set(self._address_book_pending_protocols)
                self._address_book_pending_protocols.clear()
            self._run_refresh(protocols=protocols or None)

    def schedule_address_book_refresh(self, protocol: str) -> None:
        """Request a scoped refresh after an unresolved message (throttled)."""
        with self._address_book_refresh_lock:
            self._address_book_pending_protocols.add(protocol)
        self._address_book_refresh_wake.set()

    def stop_address_book_refresh(self) -> None:
        """Signal the daemon worker to stop (no join: daemon thread)."""
        self._address_book_refresh_stop = True
        wake = getattr(self, "_address_book_refresh_wake", None)
        if wake is not None:
            wake.set()

    def _run_refresh(self, protocols: set[str] | None) -> None:
        try:
            results = self.manager.refresh_contacts_sync(
                protocols=protocols, force=True
            )
            for proto, result in results.items():
                if result.errors:
                    logger.warning(
                        "Dynamic refresh failed for %s: %s", proto, result.errors
                    )
        except Exception:
            logger.debug("Dynamic refresh worker error", exc_info=True)
