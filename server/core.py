"""Headless chat core: owns the protocol backends and their connect workers.

The core never touches a UI.  Progress of the connection workers is published
on the :class:`~server.bus.EventBus`; frontends (TUI, web, ...) subscribe and
react.  Event types and payloads:

- ``backend_connecting`` ``{"protocol": p}``
- ``backend_status`` ``{"protocol": p, "text": str, "persistent": bool}``
  (``persistent`` is True where the status must stay until replaced)
- ``backend_ready`` ``{"protocol": p, "contacts": int}``
- ``backend_done`` ``{"protocol": p, "ok": False}`` for failures, skips and
  timeouts (a ready backend implies done on the consumer side).

This module must not import Textual, FastAPI, ``tui`` or ``web``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from protocols import (
    BackendManager,
    SignalBackend,
    TelegramBackend,
    WhatsAppBackend,
)
from protocols.config import signal_enabled, telegram_enabled, whatsapp_enabled
from protocols.webhook import WEBHOOK_PORT, ensure_webhook_server
from server.bus import EventBus

logger = logging.getLogger("signal_tui")

#: How long (seconds) the boot worker waits for WAHA to report WORKING before
#: giving up, and how often it polls.  Covers a WAHA session that is still
#: syncing when the app starts (e.g. right after a container restart/device
#: link) so the UI no longer stays empty until a manual restart.
WA_BOOT_READY_TIMEOUT_S = 300.0
WA_BOOT_POLL_INTERVAL_S = 2.5


def _run_startup_maintenance() -> None:
    """Run the idempotent DB maintenance before backend caches load.

    Each connect worker calls this; ``run_startup_maintenance`` is guarded per
    DB path (R3), so the heavy dedup runs at most once per process.  Never
    raises: a maintenance failure must not abort a backend connection.
    """
    try:
        from protocols.db import run_startup_maintenance

        run_startup_maintenance()
    except Exception:
        logger.debug("Startup DB maintenance failed", exc_info=True)


class ChatCore:
    """Owns the backends and runs their connection workers on daemon threads."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus

        # Multi-protocol backend manager + the always-registered Signal backend.
        self.manager = BackendManager()
        self.signal_backend = SignalBackend()
        self.manager.register(self.signal_backend)

        # WhatsApp backend is registered only when configured (env/config.json);
        # otherwise it's skipped gracefully and the Signal TUI keeps working.
        self.whatsapp_backend: WhatsAppBackend | None = None
        if whatsapp_enabled():
            self.whatsapp_backend = WhatsAppBackend()
            self.manager.register(self.whatsapp_backend)

        # Telegram backend is registered only when credentials are configured;
        # otherwise it's skipped gracefully.
        self.telegram_backend: TelegramBackend | None = None
        if telegram_enabled():
            self.telegram_backend = TelegramBackend()
            self.manager.register(self.telegram_backend)

        # Guards against duplicate concurrent connect workers.
        self._wa_connecting = False
        self._tg_connecting = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start the connect workers (same gating as the old TUI ``on_mount``)."""
        # Only connect backends that are already linked (skip slow daemon
        # startup for unlinked accounts — they connect after Ctrl+L link).
        if signal_enabled():
            self._spawn(self.connect_signal, "core-connect-signal")
        else:
            logger.info("Signal auto-connect disabled (signal_enabled() is False)")
            self._done(self.signal_backend.protocol)
        # WhatsApp: WAHA may still be syncing at boot.  The boot worker waits
        # (bounded) for the session to become WORKING, then connects; the old
        # strict ``is_working`` gate skipped the connect with no retry, leaving
        # the UI empty until a manual restart.
        if self.whatsapp_backend is not None:
            self._spawn(self.connect_whatsapp_boot, "core-connect-whatsapp-boot")
        if (
            self.telegram_backend is not None
            and not self.telegram_backend.needs_pairing
        ):
            self._spawn(self.connect_telegram, "core-connect-telegram")

    def reconnect(self, protocols: set[str]) -> None:
        """Reconnect only the backends whose link flow was actually started.

        Called after the device-link screen dismisses.  A plain ``Ctrl+L`` →
        ``Esc`` touches nothing and reconnects nothing; starting a QR flow for
        a protocol marks it and, on dismiss, only that backend is reconnected
        (to restore the connection the QR flow disturbed or to activate a just
        linked account).
        """
        if self.signal_backend and "signal" in protocols:
            self._spawn(self.connect_signal, "core-connect-signal")
        if self.whatsapp_backend and "whatsapp" in protocols:
            self._spawn(self.connect_whatsapp, "core-connect-whatsapp")
        if self.telegram_backend and "telegram" in protocols:
            self._spawn(self.connect_telegram, "core-connect-telegram")

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _spawn(target: Callable[[], None], name: str) -> threading.Thread:
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        return t

    def _connecting(self, proto: str) -> None:
        self.bus.publish("backend_connecting", {"protocol": proto})

    def _status(self, proto: str, text: str, persistent: bool = False) -> None:
        self.bus.publish(
            "backend_status",
            {"protocol": proto, "text": text, "persistent": persistent},
        )

    def _ready(self, backend) -> None:
        self.bus.publish(
            "backend_ready",
            {"protocol": backend.protocol, "contacts": len(backend.contacts)},
        )

    def _done(self, proto: str) -> None:
        self.bus.publish("backend_done", {"protocol": proto, "ok": False})

    # -- connect workers ---------------------------------------------------

    def connect_signal(self) -> None:
        """Worker thread: avvia Signal e pubblica gli eventi di stato."""
        _run_startup_maintenance()
        proto = self.signal_backend.protocol
        self._connecting(proto)
        try:
            self._status(proto, "⏳ Signal: avvio daemon...", True)
            sb = self.signal_backend
            logger.info("LINK-SIG: start, daemon_proc=%s", sb.daemon_proc is not None)
            sb._connect_sync()
            logger.info("LINK-SIG: connect_sync done, use_daemon=%s", sb._use_daemon)
            self._ready(sb)
            self._status(proto, "💡 Select a contact to view chat")
            if sb._use_daemon:
                self._status(proto, "✅ Signal: daemon attivo")
            else:
                self._status(
                    proto, "⚠️ Signal: daemon non disponibile (subprocess)", True
                )
        except Exception as e:
            logger.exception("LINK-SIG: failed")
            self._status(proto, f"❌ Signal: errore — {e}", True)
            self._done(proto)

    def connect_whatsapp_boot(self) -> None:
        """Worker thread: connect WhatsApp once WAHA reports WORKING.

        WAHA may still be syncing when the app starts (``status != WORKING``,
        e.g. right after a container restart).  The previous strict
        ``is_working`` gate skipped the connect *before* scheduling anything,
        with no retry, so the UI stayed empty until a manual restart.  Poll
        (bounded) until the session is WORKING, then delegate to the normal
        ``connect_whatsapp`` path which waits for readiness and polls contacts.
        If the session asks for pairing in the meantime, stop: the user links it
        from Ctrl+L.
        """
        backend = self.whatsapp_backend
        if backend is None:
            return
        _run_startup_maintenance()
        deadline = time.monotonic() + WA_BOOT_READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if backend.needs_pairing:
                logger.info("LINK-WA: pairing richiesto, skip auto-connect")
                self._done(backend.protocol)
                return
            if backend.is_working:
                logger.info("LINK-WA: WAHA WORKING, avvio connessione")
                self.connect_whatsapp()
                return
            time.sleep(WA_BOOT_POLL_INTERVAL_S)
        logger.warning(
            "LINK-WA: WAHA non WORKING dopo %.0fs, auto-connect annullato",
            WA_BOOT_READY_TIMEOUT_S,
        )
        self._done(backend.protocol)

    def connect_whatsapp(self) -> None:
        """Worker thread: avvia WhatsApp, mostra contatti subito, sync cronologia dopo."""
        if self._wa_connecting:
            logger.info("LINK-WA: already connecting, skipping duplicate worker")
            return
        self._wa_connecting = True
        wa = self.whatsapp_backend
        assert wa is not None
        proto = wa.protocol
        self._connecting(proto)
        try:
            logger.info("LINK-WA: start")
            if wa.needs_pairing:
                self._status(proto, "⏳ WAHA: attesa pairing...", True)
            wa.connect_sync()
            n = len(wa.contacts)
            logger.info("LINK-WA: connect_sync done, wa_contacts=%d", n)
            try:
                ensure_webhook_server(wa)
            except Exception as _e:
                logger.debug("Webhook server startup failed", exc_info=True)
            if n > 0:
                self._status(proto, f"✅ WAHA: {n} contatti (webhook :{WEBHOOK_PORT})")
                self._ready(wa)
                self._resync_wa_history()
                self._wa_connecting = False
            else:
                # WAHA is working but server-side contacts sync still in
                # progress.  Launch a background poller thread — sleeps in
                # a thread, never blocks the UI.
                self._spawn(self._poll_wa_contacts, "core-poll-wa-contacts")
        except Exception as exc:
            logger.exception("LINK-WA: failed")
            self._status(proto, f"❌ WAHA: non disponibile — {exc}", True)
            self._done(proto)
            self._wa_connecting = False

    def _poll_wa_contacts(self) -> None:
        """Worker thread: attende sync contatti WAHA, mostra subito, poi cronologia."""
        wa = self.whatsapp_backend
        assert wa is not None
        proto = wa.protocol
        poll_count = 0
        deadline = time.monotonic() + 120.0
        self._status(proto, "🔄 WAHA: sync contatti...", True)
        while time.monotonic() < deadline:
            time.sleep(2.0)
            poll_count += 1
            wa._load_contacts()
            n = len(wa.contacts)
            if n > 0:
                logger.info(
                    "LINK-WA: contacts synced after %d polls, wa_contacts=%d",
                    poll_count,
                    n,
                )
                self._status(proto, f"📥 WAHA: {n} contatti caricati")
                break
            logger.info("LINK-WA: waiting (poll=%d)", poll_count)
        else:
            logger.warning("LINK-WA: timeout after 2 min")
            self._status(proto, "⚠️ WAHA: timeout contatti", True)
            self._done(proto)
            self._wa_connecting = False
            return

        # Mostra i contatti SUBITO, poi sync cronologia in background
        self._ready(wa)
        self._status(proto, "⏳ WAHA: sync cronologia...", True)
        self._resync_wa_history()
        self._status(proto, "")
        logger.info("LINK-WA: done, total_contacts=%d", len(wa.contacts))
        self._wa_connecting = False

    def connect_telegram(self) -> None:
        """Worker thread: connette Telegram e pubblica gli eventi di stato."""
        if self._tg_connecting:
            logger.info("LINK-TG: already connecting, skipping duplicate worker")
            return
        self._tg_connecting = True
        _run_startup_maintenance()
        tg = self.telegram_backend
        assert tg is not None
        proto = tg.protocol
        try:
            self._connecting(proto)
            logger.info("LINK-TG: start, needs_pairing=%s", tg.needs_pairing)
            self._status(proto, "⏳ Telegram: connecting...", True)
            tg._connect_sync()
            n = len(tg.contacts)
            logger.info(
                "LINK-TG: connect_sync done, contacts=%d, connected=%s",
                n,
                tg._connected,
            )
            if n > 0:
                self._status(proto, "⏳ Telegram: sync cronologia...", True)
                fetched = tg.fetch_recent_history(limit=20)
                logger.info("LINK-TG: history synced, %d messages", fetched)
            self._ready(tg)
        except Exception as e:
            logger.exception("Telegram connect failed")
            self._status(proto, f"❌ Telegram: {e}", True)
            self._done(proto)
        finally:
            self._tg_connecting = False

    def _resync_wa_history(self) -> int:
        """Re-sync best-effort dello storico WhatsApp all'avvio.

        Delega al backend ``resync_history`` (unread ∪ chat con messaggi nel DB)
        e riporta un info-message se ha processato qualche chat.  Non solleva
        mai eccezioni: all'avvio l'UI non deve fallire né per un errore remoto
        né per il reporting.  Ritorna il numero di chat ri-sincronizzate
        (0 se non applicabile).
        """
        if self.whatsapp_backend is None or not getattr(
            self.whatsapp_backend, "_connected", False
        ):
            return 0
        try:
            resync = getattr(self.whatsapp_backend, "resync_history", None)
        except Exception as _e:
            logger.debug("Failed to resolve resync_history", exc_info=True)
            return 0
        if resync is None:
            return 0
        try:
            n = resync()
        except Exception as _e:
            logger.debug("WhatsApp history resync failed", exc_info=True)
            return 0
        if n:
            try:
                self._status(
                    self.whatsapp_backend.protocol,
                    f"✅ WAHA: cronologia sincronizzata per {n} chat",
                )
            except Exception as _e:
                logger.debug(
                    "Status report failed", exc_info=True
                )  # il report è solo informativo
        return n
