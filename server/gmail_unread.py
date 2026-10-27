"""Periodic Gmail unread check for the UI badges (one daemon worker).

The other backends push their unread counts with every message event; Gmail
has no push channel, so the server polls the unread count among the 200
messages the list shows (:func:`server.gmail.fetch_unread_status`: a read-only
``SELECT`` and one ``SEARCH UNSEEN``) every ``GMAIL_UNREAD_POLL_S`` seconds
(default 60, ``0`` disables) and publishes a ``gmail_unread`` web-push event
when the count or ``UIDNEXT`` changes.  Actions that change the count (mark
read, delete) :func:`poke` the worker so the badge follows at once.

IMAP IDLE would notify instantly but needs a dedicated, always-open
connection with renewal and reconnect handling; for mail a minute of latency
is fine, so polling keeps this small and robust.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from typing import Any

from protocols import config
from server import gmail

logger = logging.getLogger(__name__)

DEFAULT_POLL_S = 60.0
EVENT_TYPE = "gmail_unread"

_current: GmailUnreadWatcher | None = None


def poll_interval_s() -> float:
    """``GMAIL_UNREAD_POLL_S`` (seconds); ``0`` or invalid-negative disables."""
    raw = os.environ.get("GMAIL_UNREAD_POLL_S", "").strip()
    if not raw:
        return DEFAULT_POLL_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_POLL_S


def poke() -> None:
    """Ask the running watcher (if any) to re-check now."""
    watcher = _current
    if watcher is not None:
        watcher.poke()


def latest() -> dict[str, Any] | None:
    """Last ``{"unread", "uidnext"}`` seen by the running watcher, if any."""
    watcher = _current
    return watcher.snapshot() if watcher is not None else None


class GmailUnreadWatcher:
    """Daemon worker polling the Gmail unread count; thread-free until start()."""

    def __init__(
        self,
        publish: Callable[[dict[str, Any]], None],
        *,
        interval_s: float | None = None,
        fetch: Callable[[], dict[str, Any]] | None = None,
        enabled: Callable[[], bool] | None = None,
    ) -> None:
        self._publish = publish
        self._interval_s = poll_interval_s() if interval_s is None else interval_s
        self._fetch = fetch or gmail.fetch_unread_status
        self._enabled = enabled or config.gmail_enabled
        self._wake = threading.Event()
        self._stop = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last: dict[str, Any] | None = None

    def start(self) -> None:
        """Start the worker once (idempotent); no-op when polling is disabled."""
        global _current
        if self._interval_s <= 0:
            logger.info("Gmail unread polling disabled (GMAIL_UNREAD_POLL_S=0)")
            return
        thread = self._thread
        if thread is not None and thread.is_alive():
            return
        self._stop = False
        thread = threading.Thread(target=self._loop, name="gmail-unread", daemon=True)
        self._thread = thread
        _current = self
        thread.start()

    def stop(self) -> None:
        global _current
        self._stop = True
        self._wake.set()
        if _current is self:
            _current = None

    def poke(self) -> None:
        self._wake.set()

    def snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._last) if self._last is not None else None

    def _loop(self) -> None:
        while not self._stop:
            self.check_once()
            self._wake.wait(timeout=self._interval_s)
            self._wake.clear()

    def check_once(self) -> None:
        """Fetch the status and publish it when it changed (never raises)."""
        if not self._enabled():
            return
        try:
            status = self._fetch()
        except Exception:
            logger.debug("Gmail unread check failed", exc_info=True)
            return
        unread = int(status.get("unread") or 0)
        uidnext = status.get("uidnext")
        with self._lock:
            previous = self._last
            if previous == {"unread": unread, "uidnext": uidnext}:
                return
            self._last = {"unread": unread, "uidnext": uidnext}
        new_mail = bool(
            previous is not None
            and uidnext is not None
            and previous.get("uidnext") is not None
            and uidnext > previous["uidnext"]
        )
        try:
            self._publish(
                {
                    "type": EVENT_TYPE,
                    "payload": {"unread": unread, "new_mail": new_mail},
                }
            )
        except Exception:
            logger.debug("Gmail unread publish failed", exc_info=True)
