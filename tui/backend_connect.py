"""Per-protocol backend connection workers."""

import logging
import time


from models import (
    contact_cache_key,
)
from backends import (
    ChatBackend,
)
from backend import (
    ensure_webhook_server,
    WEBHOOK_PORT,
)

logger = logging.getLogger("signal_tui")


class BackendConnectMixin:

    def _mark_backend_connecting(self, proto: str) -> None:
        """UI thread: un backend ha avviato la connessione (in attesa di report)."""
        self._pending_backends.add(proto)

    def _mark_backend_done(self, proto: str) -> None:
        """UI thread: un backend ha terminato (ready o fallito).

        Quando TUTTI i backend attesi hanno riportato un esito, se non c'è
        ancora una selezione e ci sono contatti, seleziona il primo (il più
        recente).  Così l'auto-selezione attende l'ULTIMO backend — non il
        primo — e finisce sul contatto in cima alla lista finale.
        """
        self._pending_backends.discard(proto)
        if not self._pending_backends and self.selected_contact is None and self.contacts:
            self._select_contact(self.contacts[0])

    def _on_backend_ready(self, backend: ChatBackend) -> None:
        """UI thread: merge atomico di cache e contatti da UN backend.

        Chiamato via ``call_from_thread`` dopo che un backend ha completato
        la connessione.  Poiché Textual esegue le callback del UI thread in
        ordine FIFO, i merge di backend multipli sono naturalmente serializzati
        senza bisogno di lock espliciti.
        """
        proto = backend.protocol

        # ── Merge cache (incrementale, no clear, idempotente) ──
        # `_on_backend_ready` può girare più volte (avvio + device-link Ctrl+L,
        # che ri-connette tutti i backend).  Un dedup basato SOLO su `id`
        # ri-appenderebbe i messaggi senza id (Signal, optimistic send) ad ogni
        # merge → duplicati in UI per tutte le chat.  Dedup quindi anche per
        # identità esatta (is_mine, testo, timestamp).
        for cid, msgs in backend.cache.items():
            key = contact_cache_key(proto, cid)
            ui_msgs = self._cache.setdefault(key, [])
            seen_ids = {m.get("id") for m in ui_msgs if m.get("id")}
            seen_identities = {
                (
                    bool(m.get("is_mine", False)),
                    m.get("text", ""),
                    int(m.get("timestamp") or 0),
                )
                for m in ui_msgs
            }
            for m in msgs:
                mid = m.get("id")
                if mid and mid in seen_ids:
                    continue
                identity = (
                    bool(m.get("is_mine", False)),
                    m.get("text", ""),
                    int(m.get("timestamp") or 0),
                )
                if identity in seen_identities:
                    continue
                ui_msgs.append(m)
                if mid:
                    seen_ids.add(mid)
                seen_identities.add(identity)
            ui_msgs.sort(key=lambda m: int(m.get("timestamp") or 0))

        # ── Merge contatti (aggiunge nuovi, aggiorna last_message_ts) ──
        existing_ids = {c.cache_key for c in self.contacts}
        for c in backend.contacts:
            if c.cache_key in existing_ids:
                for old in self.contacts:
                    if old.cache_key == c.cache_key:
                        if (c.last_message_ts or 0) > (old.last_message_ts or 0):
                            old.last_message_ts = c.last_message_ts
                        break
            else:
                self.contacts.append(c)

        self._sync_last_ts()
        self._sort_contacts()
        self._render_contact_list(list(self.contacts))
        self._update_unread_badges()

        # Report this backend as done (ready).  The startup auto-selection is
        # triggered by `_mark_backend_done` only once ALL expected backends have
        # reported (ready or failed), so the selection lands on the top contact
        # of the final, fully-merged list.
        self._mark_backend_done(proto)

        n = len(backend.contacts)
        logger.info("Backend %s ready: %d contacts", proto, n)
        self._status(f"✅ {proto.title()}: {n} contacts loaded")

    def _reconnect_touched_backends(self, protocols: set[str]) -> None:
        """Reconnect only the backends whose link flow was actually started.

        Called after the device-link screen dismisses.  A plain ``Ctrl+L`` →
        ``Esc`` touches nothing and reconnects nothing; starting a QR flow for
        a protocol marks it and, on dismiss, only that backend is reconnected
        (to restore the connection the QR flow disturbed or to activate a just
        linked account).
        """
        if self.signal_backend and "signal" in protocols:
            self.run_worker(self._connect_signal, exclusive=False, thread=True)
        if self.whatsapp_backend and "whatsapp" in protocols:
            self.run_worker(self._connect_whatsapp, exclusive=False, thread=True)
        if self.telegram_backend and "telegram" in protocols:
            self.run_worker(self._connect_telegram, exclusive=False, thread=True)

    def _connect_signal(self) -> None:
        """Worker thread: avvia Signal, poi merge nel UI thread."""
        self.call_from_thread(
            self._mark_backend_connecting, self.signal_backend.protocol
        )
        try:
            self.call_from_thread(
                self._status, "⏳ Signal: avvio daemon...", 0
            )
            sb = self.signal_backend
            logger.info("LINK-SIG: start, daemon_proc=%s", sb.daemon_proc is not None)
            sb._connect_sync()
            logger.info("LINK-SIG: connect_sync done, use_daemon=%s", sb._use_daemon)
            self.call_from_thread(self._on_backend_ready, sb)
            self.call_from_thread(
                self._status, "💡 Select a contact to view chat"
            )
            if sb._use_daemon:
                self.call_from_thread(
                    self._status, "✅ Signal: daemon attivo"
                )
            else:
                self.call_from_thread(
                    self._status, "⚠️ Signal: daemon non disponibile (subprocess)", 0
                )
        except Exception as e:
            logger.exception("LINK-SIG: failed: %s", e)
            self.call_from_thread(
                self._status, f"❌ Signal: errore — {e}", 0
            )
            self.call_from_thread(
                self._mark_backend_done, self.signal_backend.protocol
            )

    def _connect_whatsapp(self) -> None:
        """Worker thread: avvia WhatsApp, mostra contatti subito, sync cronologia dopo."""
        if self._wa_connecting:
            logger.info("LINK-WA: already connecting, skipping duplicate worker")
            return
        self._wa_connecting = True
        self.call_from_thread(
            self._mark_backend_connecting, self.whatsapp_backend.protocol
        )
        try:
            logger.info("LINK-WA: start")
            if self.whatsapp_backend.needs_pairing:
                self.call_from_thread(
                    self._status, "⏳ WAHA: attesa pairing...", 0
                )
            self.whatsapp_backend.connect_sync()
            n = len(self.whatsapp_backend.contacts)
            logger.info("LINK-WA: connect_sync done, wa_contacts=%d", n)
            try:
                ensure_webhook_server(self.whatsapp_backend)
            except Exception:
                pass
            if n > 0:
                self.call_from_thread(
                    self._status,
                    f"✅ WAHA: {n} contatti (webhook :{WEBHOOK_PORT})",
                )
                self.call_from_thread(self._on_backend_ready, self.whatsapp_backend)
                self._resync_wa_history()
                self._wa_connecting = False
            else:
                # WAHA is working but server-side contacts sync still in
                # progress.  Launch a background poller worker — sleeps in
                # a thread, never blocks the UI.
                self.run_worker(self._poll_wa_contacts, thread=True)
        except Exception as exc:
            logger.exception("LINK-WA: failed: %s", exc)
            self.call_from_thread(
                self._status, f"❌ WAHA: non disponibile — {exc}", 0
            )
            self.call_from_thread(
                self._mark_backend_done, self.whatsapp_backend.protocol
            )
            self._wa_connecting = False

    def _poll_wa_contacts(self) -> None:
        """Worker thread: attende sync contatti WAHA, mostra subito, poi cronologia."""
        poll_count = 0
        deadline = time.monotonic() + 120.0
        self.call_from_thread(self._status, "🔄 WAHA: sync contatti...", 0)
        while time.monotonic() < deadline:
            time.sleep(2.0)
            poll_count += 1
            self.whatsapp_backend._load_contacts()
            n = len(self.whatsapp_backend.contacts)
            if n > 0:
                logger.info(
                    "LINK-WA: contacts synced after %d polls, wa_contacts=%d",
                    poll_count, n,
                )
                self.call_from_thread(
                    self._status, f"📥 WAHA: {n} contatti caricati",
                )
                break
            logger.info("LINK-WA: waiting (poll=%d)", poll_count)
        else:
            logger.warning("LINK-WA: timeout after 2 min")
            self.call_from_thread(
                self._status, "⚠️ WAHA: timeout contatti", 0,
            )
            self.call_from_thread(
                self._mark_backend_done, self.whatsapp_backend.protocol
            )
            self._wa_connecting = False
            return

        # Mostra i contatti SUBITO, poi sync cronologia in background
        self.call_from_thread(self._on_backend_ready, self.whatsapp_backend)
        self.call_from_thread(self._status, "⏳ WAHA: sync cronologia...", 0)
        self._resync_wa_history()
        self.call_from_thread(self._status, "")
        logger.info("LINK-WA: done, total_contacts=%d", len(self.contacts))
        self._wa_connecting = False


    def _connect_telegram(self) -> None:
        """Worker thread: connette Telegram, poi merge nel UI thread."""
        if self._tg_connecting:
            logger.info("LINK-TG: already connecting, skipping duplicate worker")
            return
        self._tg_connecting = True
        try:
            self.call_from_thread(
                self._mark_backend_connecting, self.telegram_backend.protocol
            )
            logger.info("LINK-TG: start, needs_pairing=%s", self.telegram_backend.needs_pairing)
            self.call_from_thread(self._status, "⏳ Telegram: connecting...", 0)
            self.telegram_backend._connect_sync()
            n = len(self.telegram_backend.contacts)
            logger.info("LINK-TG: connect_sync done, contacts=%d, connected=%s", n, self.telegram_backend._connected)
            if n > 0:
                self.call_from_thread(self._status, "⏳ Telegram: sync cronologia...", 0)
                fetched = self.telegram_backend.fetch_recent_history(limit=20)
                logger.info("LINK-TG: history synced, %d messages", fetched)
            self.call_from_thread(self._on_backend_ready, self.telegram_backend)
        except Exception as e:
            logger.exception("Telegram connect failed: %s", e)
            self.call_from_thread(self._status, f"❌ Telegram: {e}", 0)
            self.call_from_thread(
                self._mark_backend_done, self.telegram_backend.protocol
            )
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
        except Exception:
            return 0
        if resync is None:
            return 0
        try:
            n = resync()
        except Exception:
            return 0
        if n:
            try:
                self.call_from_thread(
                    self._status,
                    f"✅ WAHA: cronologia sincronizzata per {n} chat"
                )
            except Exception:
                pass  # il report è solo informativo
        return n
