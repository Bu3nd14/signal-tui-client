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

Domain events, produced by :meth:`ChatCore.process` for every raw backend
``ChatEvent`` (the pump publishes them on the bus; payloads carry in-process
objects such as ``ChatContact`` until Phase 1):

- ``message`` ``{"protocol", "contact_id", "contact": ChatContact,
  "was_placeholder": bool, "message": <raw payload dict>, "timestamp": ts,
  "added": bool, "changed": bool}`` (always emitted, even when nothing new
  was stored: consumers use it for recency / list dirtiness)
- ``message_edit`` ``{"protocol", "contact_id", "contact", "info", "text"}``
- ``reaction_update`` ``{"protocol", "contact_id", "info"}``
- ``receipt`` ``{"protocol", "contact_id", "updated": list[dict]}``
- ``typing`` ``{"protocol", "contact_id", "action": "STARTED"|"STOPPED"}``
- ``contact_update`` ``{"protocol", "contact_id", **backend payload}``
  (``contact``, ``display_name``, ``phone``, WhatsApp ``merged_into``)
- ``sent_mirror`` ``{"protocol", "contact_id", **backend payload,
  "contact": ChatContact}`` (contact resolved/registered by the core)
- ``lid_warmup_done`` ``{"protocol", "contact_id", **payload}``

This module must not import Textual, FastAPI, ``tui`` or ``web``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from models import (
    PROTOCOL_SIGNAL,
    ChatContact,
    ChatEvent,
    contact_cache_key,
)
from protocols import (
    BackendManager,
    SignalBackend,
    TelegramBackend,
    WhatsAppBackend,
)
from protocols.config import signal_enabled, telegram_enabled, whatsapp_enabled
from protocols.webhook import WEBHOOK_PORT, ensure_webhook_server
from server.bus import EventBus
from server.domain import resolve_placeholder_name

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

        # Event pump (one daemon thread, see start()/stop()).
        self._pump_thread: threading.Thread | None = None
        self._pump_stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start the event pump and the connect workers (same gating as the
        old TUI ``on_mount``)."""
        self._start_pump()
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

    def stop(self) -> None:
        """Stop the event pump; the thread exits within one 0.1s slice."""
        self._pump_stop.set()
        thread = self._pump_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(1.0)

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

    # -- event pump --------------------------------------------------------

    def _start_pump(self) -> None:
        thread = self._pump_thread
        if thread is not None and thread.is_alive():
            return
        self._pump_stop.clear()
        self._pump_thread = threading.Thread(
            target=self._pump_loop, name="core-pump", daemon=True
        )
        self._pump_thread.start()

    def _pump_loop(self) -> None:
        """Poll every backend, process each raw event, publish the results.

        One round over all backends, then ~1s of sleep in 0.1s slices so
        :meth:`stop` takes effect promptly.  A failing event or round is
        logged at debug level and never kills the loop.
        """
        stop = self._pump_stop
        while not stop.is_set():
            try:
                for backend in self.manager.all():
                    if stop.is_set():
                        return
                    try:
                        events = backend.poll_once()
                    except AttributeError:
                        events = []
                    for event in events:
                        if stop.is_set():
                            return
                        self._pump_event(event)
            except Exception:
                logger.debug("Pump iteration failed, continuing", exc_info=True)
            for _ in range(10):
                if stop.is_set():
                    return
                time.sleep(0.1)

    def _pump_event(self, event: ChatEvent) -> None:
        try:
            results = self.process(event)
        except Exception:
            logger.debug("Event processing failed, continuing", exc_info=True)
            return
        for etype, payload in results:
            self.bus.publish(etype, payload)

    # -- domain processing -------------------------------------------------

    def _resolve_contact(self, backend, event: ChatEvent) -> tuple[ChatContact, bool]:
        """Payload contact -> backend lookup -> placeholder.

        Returns ``(contact, was_placeholder)``.
        """
        contact = event.payload.get("contact")
        if contact is not None:
            return contact, False
        identify = getattr(backend, "_identify_contact", None)
        if identify is not None:
            contact = identify(event.contact_id)
        if contact is not None:
            return contact, False
        return (
            ChatContact(
                id=event.contact_id,
                display_name=resolve_placeholder_name(backend, event.contact_id),
                protocol=event.protocol,
            ),
            True,
        )

    @staticmethod
    def _ensure_registered(backend, contact: ChatContact) -> None:
        """Make *contact* known to *backend* when the backend does not have it.

        The decision is based on the backend's own contact list (by
        ``cache_key``).  Before the split it was based on the TUI list; the
        two only differ for a contact the backend already knows but the TUI
        list lacks (old code re-registered it, a no-op) or the reverse (an
        object only the TUI list had, now registered too).
        """
        known = getattr(backend, "contacts", None)
        if known is not None and any(c.cache_key == contact.cache_key for c in known):
            return
        if hasattr(backend, "register_contact"):
            backend.register_contact(contact)
        elif known is not None:
            known.append(contact)

    def process(self, event: ChatEvent) -> list[tuple[str, dict[str, Any]]]:
        """Domain step for ONE raw backend event; returns events to publish.

        Mutates the backend (cache / SQLite / contact registration) but does
        not publish: the caller decides (the pump publishes on the bus).
        """
        handler = {
            "message": self._process_message,
            "message_edit": self._process_edit,
            "reaction_update": self._process_reaction,
            "receipt": self._process_receipt,
            "typing": self._process_typing,
            "contact_update": self._process_passthrough,
            "sent-mirror": self._process_sent_mirror,
            "lid_warmup_done": self._process_passthrough,
        }.get(event.type)
        if handler is None:
            return []
        return handler(event)

    @staticmethod
    def _passthrough_type(event: ChatEvent) -> str:
        return event.type.replace("-", "_")

    def _process_passthrough(self, event: ChatEvent) -> list[tuple[str, dict]]:
        return [
            (
                self._passthrough_type(event),
                {
                    **event.payload,
                    "protocol": event.protocol,
                    "contact_id": event.contact_id,
                },
            )
        ]

    def _process_sent_mirror(self, event: ChatEvent) -> list[tuple[str, dict]]:
        backend = self.manager.get(event.protocol)
        if backend is None:
            return []
        # Prefer the object the backend already holds (no lookup needed);
        # otherwise payload copy -> backend identify -> placeholder.
        key = contact_cache_key(event.protocol, event.contact_id)
        contact = next(
            (c for c in getattr(backend, "contacts", None) or [] if c.cache_key == key),
            None,
        )
        if contact is None:
            contact, _ = self._resolve_contact(backend, event)
        self._ensure_registered(backend, contact)
        return [
            (
                "sent_mirror",
                {
                    **event.payload,
                    "protocol": event.protocol,
                    "contact_id": event.contact_id,
                    "contact": contact,
                },
            )
        ]

    def _process_message(self, event: ChatEvent) -> list[tuple[str, dict]]:
        backend = self.manager.get(event.protocol)
        if backend is None:
            return []
        contact, was_placeholder = self._resolve_contact(backend, event)
        self._ensure_registered(backend, contact)
        ts = event.payload.get("timestamp", 0)
        # Save to DB + backend cache.  True only if newly added; "changed"
        # when an existing identity was upgraded (e.g. optimistic -> echo).
        ingest = getattr(backend, "ingest_message", None)
        result = ingest(contact.id, event.payload, ts) if ingest is not None else True
        added = result is True
        changed = result == "changed"
        logger.debug(
            "core: message added=%s changed=%s protocol=%s contact=%s id=%s",
            added,
            changed,
            event.protocol,
            contact.id,
            event.payload.get("id"),
        )
        return [
            (
                "message",
                {
                    "protocol": event.protocol,
                    "contact_id": contact.id,
                    "contact": contact,
                    "was_placeholder": was_placeholder,
                    "message": event.payload,
                    "timestamp": ts,
                    "added": added,
                    "changed": changed,
                },
            )
        ]

    def _process_edit(self, event: ChatEvent) -> list[tuple[str, dict]]:
        backend = self.manager.get(event.protocol)
        if backend is None:
            return []
        apply_edit = getattr(backend, "apply_edit", None)
        if apply_edit is None:
            return []
        payload = event.payload
        new_text = payload.get("text", "")
        edit_id = payload.get("edit_message_id")
        if edit_id is None or not new_text:
            return []
        contact, _ = self._resolve_contact(backend, event)
        # Single mutation point: backend cache + SQLite.  Idempotent: text
        # already new / unknown target / media -> None -> no-op.
        info = apply_edit(
            event.contact_id,
            str(edit_id),
            new_text,
            is_mine=payload.get("is_mine"),
            edit_timestamp=payload.get("edit_timestamp"),
        )
        if not info:
            return []
        return [
            (
                "message_edit",
                {
                    "protocol": event.protocol,
                    "contact_id": event.contact_id,
                    "contact": contact,
                    "info": info,
                    "text": new_text,
                },
            )
        ]

    def _process_reaction(self, event: ChatEvent) -> list[tuple[str, dict]]:
        backend = self.manager.get(event.protocol)
        if backend is None:
            return []
        apply_reaction = getattr(backend, "apply_reaction", None)
        if apply_reaction is None:
            return []
        info = apply_reaction(event.contact_id, event.payload)
        if not info:
            return []
        return [
            (
                "reaction_update",
                {
                    "protocol": event.protocol,
                    "contact_id": event.contact_id,
                    "info": info,
                },
            )
        ]

    def _process_receipt(self, event: ChatEvent) -> list[tuple[str, dict]]:
        backend = self.manager.get(event.protocol)
        if backend is None:
            return []
        process = getattr(backend, "process_receipt", None)
        if process is None:
            return []
        if event.protocol == PROTOCOL_SIGNAL:
            # Reconstruct the Signal receipt envelope the backend expects.
            envelope = {
                "sourceNumber": event.contact_id,
                "source": event.contact_id,
                "receiptMessage": event.payload.get("receipt", {}),
            }
            updated = process(envelope)
        else:
            # Generic backend (WhatsApp/Telegram): message-ids receipt payload
            # scoped to the contact that generated the receipt.
            updated = process(
                {
                    "message_ids": event.payload.get("message_ids", []),
                    "is_read": event.payload.get("is_read", False),
                    "contact_id": event.contact_id,
                }
            )
        if not updated:
            return []
        return [
            (
                "receipt",
                {
                    "protocol": event.protocol,
                    "contact_id": event.contact_id,
                    "updated": updated,
                },
            )
        ]

    def _process_typing(self, event: ChatEvent) -> list[tuple[str, dict]]:
        action = event.payload.get("action", "")
        if action not in ("STARTED", "STOPPED"):
            return []
        return [
            (
                "typing",
                {
                    "protocol": event.protocol,
                    "contact_id": event.contact_id,
                    "action": action,
                },
            )
        ]
