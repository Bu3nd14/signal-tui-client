"""Background poll worker that drains backend events."""

import logging
import time

logger = logging.getLogger(__name__)


class PollingMixin:

    def _poll_worker(self):
        """Thread worker that polls the backend receive loop.

        Runs as a plain (non-async) thread loop exactly like the original, so
        quitting is prompt: every cycle it checks ``_polling_active`` and
        sleeps briefly.  Each round pulls a batch of events via
        ``backend.poll_once()`` and dispatches them through ``_handle_event``.
        """
        while self._polling_active:
            try:
                # Drain events from every registered backend (Signal, WhatsApp, ...).
                for backend in self.manager.all():
                    if not self._polling_active:
                        return
                    try:
                        events = backend.poll_once()
                    except AttributeError:
                        events = []
                    for event in events:
                        if not self._polling_active:
                            return
                        self._handle_event(event)

                        # Typing timeout: a STARTED without a STOPPED within
                        # _TYPING_TIMEOUT seconds moves the contact to mumbling (💭).
                        if self._typing_contacts:
                            now = time.time()
                            expired = [
                                key for key, started_at in self._typing_contacts.items()
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
                                key for key, expires_at in self._typing_mumbling.items()
                                if now >= expires_at
                            ]
                            if expired:
                                for key in expired:
                                    self._typing_mumbling.pop(key, None)
                                    self.call_from_thread(self._update_typing_label, key)

                # Flush differito della lista contatti: se durante il batch è
                # arrivato qualcosa (messaggio/typing), esegue UN solo aggiornamento
                # unread + un solo re-sort/render della lista invece di uno per
                # evento.  Entrambi devono girare nel thread della UI.
                if self._contact_list_dirty:
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


                # Prompt-exit inner sleep.  This runs every cycle (even when no
                # messages arrived) so the worker exits as soon as the user quits.
                for _ in range(10):
                    if not self._polling_active:
                        return
                    time.sleep(0.1)
            except Exception as _e:
                logger.debug("Poll worker iteration failed, continuing", exc_info=True)
            # Re-check before the next poll so an empty round still exits.
            if not self._polling_active:
                return


