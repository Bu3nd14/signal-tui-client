"""Event consumer worker: applies core events (from the client) to the TUI."""

import logging
import time

from server.bus import GAP, Event

logger = logging.getLogger(__name__)


class PollingMixin:
    def _poll_worker(self):
        """Thread worker that consumes the core events through ``self.client``.

        The event pump itself lives in ``ChatCore``: it polls the backends,
        does all domain handling and publishes core events.  This worker is
        the single event consumer of the TUI: every cycle it waits up to 0.1s
        for an event, drains the rest of the batch and dispatches each event.
        ``backend_*`` events are applied on the UI thread; domain events go to
        the view handlers, which run on this worker thread and use
        ``call_from_thread`` for widget work.  Every cycle checks
        ``_polling_active`` so quitting is prompt.
        """
        sub = self._core_sub
        if sub is None:
            return
        skip_ids: dict[int, dict] = self.__dict__.setdefault("_compat_payload_ids", {})
        try:
            while self._polling_active:
                try:
                    first = sub.get(timeout=0.1)
                    if first is None and sub.closed:
                        break
                    events = [first] if first is not None else []
                    events.extend(sub.drain())
                    for event in events:
                        if not self._polling_active:
                            return
                        if id(event.payload) in skip_ids:
                            # Already applied by the test-only ``_handle_event``.
                            skip_ids.pop(id(event.payload), None)
                            continue
                        self._dispatch_bus_event(event)

                    self._expire_typing()
                    self._flush_contact_list()
                except Exception:
                    logger.debug(
                        "Poll worker iteration failed, continuing", exc_info=True
                    )
        finally:
            sub.close()

    def _dispatch_bus_event(self, event: Event) -> None:
        """Route one bus event; never raises (a bad event must not stop the loop)."""
        try:
            if event.type.startswith("backend_"):
                self.call_from_thread(self._dispatch_core_event, event)
            elif event.type == GAP:
                # Lost events: we cannot know what changed -> full flush.
                logger.debug("Core event bus gap: %s", event.payload)
                self._contact_list_dirty = True
            else:
                self._dispatch_view_event(event)
        except Exception:
            logger.debug("Core event dispatch failed", exc_info=True)

    def _expire_typing(self) -> None:
        # Typing timeout: a STARTED without a STOPPED within
        # _TYPING_TIMEOUT seconds moves the contact to mumbling (💭).
        if self._typing_contacts:
            now = time.time()
            expired = [
                key
                for key, started_at in self._typing_contacts.items()
                if now - started_at > self._TYPING_TIMEOUT
            ]
            if expired:
                for key in expired:
                    self._typing_contacts.pop(key, None)
                    self._typing_mumbling[key] = now + self._TYPING_MUMBLING_DURATION
                    self.call_from_thread(self._update_typing_label, key)

        # Mumbling expiry: once the mumbling window passes, remove it.
        if self._typing_mumbling:
            now = time.time()
            expired = [
                key
                for key, expires_at in self._typing_mumbling.items()
                if now >= expires_at
            ]
            if expired:
                for key in expired:
                    self._typing_mumbling.pop(key, None)
                    self.call_from_thread(self._update_typing_label, key)

    def _flush_contact_list(self) -> None:
        # Flush differito della lista contatti: se durante il batch è
        # arrivato qualcosa (messaggio/typing), esegue UN solo aggiornamento
        # unread + un solo re-sort/render della lista invece di uno per
        # evento.  Entrambi devono girare nel thread della UI.
        if not self._contact_list_dirty:
            return
        self._contact_list_dirty = False
        keys = tuple(self._dirty_contact_keys)
        self._dirty_contact_keys.clear()
        if keys and len(keys) <= self._CONTACT_UPDATE_BATCH_MAX:
            # Percorso incrementale: ricalcola l'unread SOLO nei dati
            # (_recompute_unread, nessun render) per i contatti del
            # batch (O(M) ciascuno), e fa poi UN SOLO render di lista.
            for k in keys:
                self.call_from_thread(self._recompute_unread, k)
        else:
            # Batch grande (> soglia) o senza key note: ricalcolo
            # completo dei dati (nessun render qui dentro).
            self.call_from_thread(self._recompute_unread)
        # UN solo sort+render a fine batch, in-place e non distruttivo.
        # Ciò lascia il main libero per la finestra di chat (prioritaria)
        # invece di rifare il giro completo due volte.
        self.call_from_thread(self._reorder_contact_list)
        self.call_from_thread(self._refresh_backend_status_if_idle)
