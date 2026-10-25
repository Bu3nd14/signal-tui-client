"""Per-protocol backend connection workers."""

import logging
import threading

from models import ChatContact, contact_cache_key
from server.api import ApiError
from server.bus import GAP, Event
from server.events_api import contact_from_wire

logger = logging.getLogger("signal_tui")

# Delivery status rank: used to decide whether a merge may overwrite the status
# of an already-present entry (never downgrade read → sent).
_STATUS_RANK = {"pending": 0, "failed": 0, "sent": 1, "delivered": 2, "read": 3}


def _status_rank(status: str | None) -> int:
    """Return the numeric rank of a delivery status (default 0)."""
    return _STATUS_RANK.get(status, 0)


def _dedup_key(m: dict) -> tuple | None:
    """Composite dedup key for the id-based merge: ``(id, attachment_id)``.

    Signal splits a multi-attachment message into one cached row per
    attachment: every row shares the same ``id`` (the message timestamp) but
    carries a distinct ``attachment_id``.  Keying only on ``id`` collapses
    those attachments into a single entry (the "one image lost after re-entry"
    bug).  Messages without an id (optimistic sends) have no key; messages
    with an id but no attachment_id (text, ack-echo, captions) keep the
    ``(id, None)`` identity.
    """
    mid = m.get("id")
    if not mid:
        return None
    return (mid, m.get("attachment_id"))


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
        if (
            not self._pending_backends
            and self.selected_contact is None
            and self.contacts
        ):
            self._select_contact(self.contacts[0])

    def _prefetch_backend_ready(self, proto: str) -> tuple[dict | None, list | None]:
        """Worker thread: fetch the ``backend_ready`` data off the UI thread.

        ``messages.snapshot`` validates and copies the whole protocol cache
        (~0.3 s for tens of thousands of rows): doing it here keeps the UI
        thread free.  A failed call yields ``None`` so the UI-thread merge
        logs it and degrades exactly as before.
        """
        snapshot: dict | None = None
        contacts: list | None = None
        try:
            snapshot = self._call_op("messages.snapshot", {"protocol": proto})
        except ApiError as exc:
            logger.warning("messages.snapshot failed for %s", proto, exc_info=True)
            self._report_client_error(exc, "caricamento messaggi")
            snapshot = {}
        try:
            wire = self._call_op("contacts.for_protocol", {"protocol": proto})
            contacts = [contact_from_wire(w) for w in wire]
        except ApiError as exc:
            logger.warning("contacts.for_protocol failed for %s", proto, exc_info=True)
            self._report_client_error(exc, "caricamento contatti")
            contacts = []
        return snapshot, contacts

    def _on_backend_ready(
        self,
        proto: str,
        snapshot: dict | None = None,
        contacts: list[ChatContact] | None = None,
    ) -> None:
        """UI thread: merge atomico di cache e contatti da UN backend.

        Chiamato via ``call_from_thread`` dopo che un backend ha completato
        la connessione.  Poiché Textual esegue le callback del UI thread in
        ordine FIFO, i merge di backend multipli sono naturalmente serializzati
        senza bisogno di lock espliciti.

        Contatti e cache messaggi arrivano dalle operazioni
        ``contacts.for_protocol`` e ``messages.snapshot`` (copie wire).
        """
        if (snapshot is None or contacts is None) and self.is_running:
            # Data not prefetched: fetch it off the UI thread, merge afterwards.
            def _prefetch() -> None:
                fetched_snapshot, fetched_contacts = self._prefetch_backend_ready(proto)
                self._ui(
                    self._on_backend_ready,
                    proto,
                    snapshot if snapshot is not None else fetched_snapshot,
                    contacts if contacts is not None else fetched_contacts,
                )

            self._offload(_prefetch)
            return
        self._merge_ready_backend_cache(proto, snapshot)
        if contacts is None:
            contacts = self._fetch_protocol_contacts(proto)
        self._merge_backend_contacts(contacts)

        self._sync_last_ts()
        self._sort_contacts()
        self._render_contact_list(list(self.contacts))
        self._update_unread_badges()
        self._refresh_backend_status_if_idle()

        # Report this backend as done (ready).  The startup auto-selection is
        # triggered by `_mark_backend_done` only once ALL expected backends have
        # reported (ready or failed), so the selection lands on the top contact
        # of the final, fully-merged list.
        self._mark_backend_done(proto)

        n = len(contacts)
        logger.info("Backend %s ready: %d contacts", proto, n)
        self._status(f"✅ {proto.title()}: {n} contacts loaded")

    def _fetch_protocol_contacts(self, proto: str) -> list[ChatContact]:
        """Contacts of *proto* from the core, as TUI-owned copies.

        Blocking: only reached without a running app (unit tests); a running
        app prefetches in a worker (see ``_on_backend_ready``).  A refusal
        leaves the list empty: the backend is still reported done.
        """
        try:
            wire = self._call_op("contacts.for_protocol", {"protocol": proto})
        except ApiError:
            logger.warning("contacts.for_protocol failed for %s", proto, exc_info=True)
            return []
        return [contact_from_wire(w) for w in wire]

    def _merge_backend_contacts(self, contacts: list[ChatContact]) -> None:
        """Aggiunge i contatti nuovi; aggiorna quelli già in lista dal backend.

        Un contatto già presente (re-ready: avvio + device-link Ctrl+L) viene
        allineato a ciò che il core ora conosce — nome ed extras — come quando
        la TUI condivideva l'oggetto del backend e ne vedeva le modifiche sul
        posto; ``last_message_ts`` non scende mai (può essere più recente
        lato TUI).  L'identità dell'oggetto TUI resta stabile.
        """
        by_key = {c.cache_key: c for c in self.contacts}
        for c in contacts:
            old = by_key.get(c.cache_key)
            if old is None:
                self.contacts.append(c)
                by_key[c.cache_key] = c
                continue
            newest = max(c.last_message_ts or 0, old.last_message_ts or 0)
            old.display_name = c.display_name
            old.extras.clear()
            old.extras.update(c.extras)
            if newest:
                old.last_message_ts = newest

    def _merge_ready_backend_cache(
        self, proto: str, snapshot: dict | None = None
    ) -> None:
        """Merge the core's message cache of *proto* into the UI cache.

        The rows come from the ``messages.snapshot`` operation (canonical cache
        rows, WhatsApp storage-key union already applied by the core).  The
        merge here is the view-side reconciliation with the UI cache, which
        also holds optimistic rows the core does not know yet.
        """
        if snapshot is None:
            try:
                snapshot = self._call_op("messages.snapshot", {"protocol": proto})
            except ApiError:
                logger.warning("messages.snapshot failed for %s", proto, exc_info=True)
                return

        # ── Merge cache (incrementale, no clear, idempotente) ──
        # Il core non attende più questo merge (evento asincrono) e l'operazione
        # restituisce una copia: nessun dict è condiviso col backend.
        # `_on_backend_ready` può girare più volte (avvio + device-link Ctrl+L,
        # che ri-connette tutti i backend).  Un dedup basato SOLO su `id`
        # ri-appenderebbe i messaggi senza id (Signal, optimistic send) ad ogni
        # merge → duplicati in UI per tutte le chat.  Dedup quindi anche per
        # identità esatta (is_mine, testo, timestamp).
        def _merge_messages(key: str, msgs: list[dict]) -> None:
            ui_msgs = self._cache.setdefault(key, [])
            by_key = {_dedup_key(m): m for m in ui_msgs if _dedup_key(m) is not None}
            by_id = {m.get("id"): m for m in ui_msgs if m.get("id")}
            by_identity = {
                (
                    bool(m.get("is_mine", False)),
                    m.get("text", ""),
                    int(m.get("timestamp") or 0),
                ): m
                for m in ui_msgs
            }
            for m in msgs:
                mid = m.get("id")
                identity = (
                    bool(m.get("is_mine", False)),
                    m.get("text", ""),
                    int(m.get("timestamp") or 0),
                )
                existing = None
                dk = _dedup_key(m)
                if dk is not None and dk in by_key:
                    existing = by_key[dk]
                elif (
                    mid
                    and mid in by_id
                    and (
                        not m.get("attachment_id")
                        or not by_id[mid].get("attachment_id")
                    )
                ):
                    # ack-echo / text-echo (#36): one of the two entries has no
                    # attachment_id (the echo shares the media's id but has no
                    # distinguishing attachment) → pure id-first identity.
                    existing = by_id[mid]
                elif identity in by_identity:
                    existing = by_identity[identity]
                if existing is not None:
                    # An entry reconciled without an id now learns its id
                    # (e.g. a Signal optimistic send upgraded by its echo).
                    if mid and not existing.get("id"):
                        existing["id"] = mid
                        by_id[mid] = existing
                        learned = _dedup_key(existing)
                        if learned is not None:
                            by_key[learned] = existing
                    # Already present: still upgrade the status if the incoming
                    # rank is higher (never downgrade read → sent).
                    if _status_rank(m.get("status")) > _status_rank(
                        existing.get("status")
                    ):
                        existing["status"] = m.get("status")
                    continue
                ui_msgs.append(m)
                if mid:
                    by_id[mid] = m
                    if dk is not None:
                        by_key[dk] = m
                by_identity[identity] = m
            ui_msgs.sort(key=lambda m: int(m.get("timestamp") or 0))

        for cid, msgs in snapshot.items():
            _merge_messages(contact_cache_key(proto, cid), msgs)

    def _reconnect_touched_backends(
        self, protocols: set[str], after: threading.Thread | None = None
    ) -> None:
        """Reconnect only the backends whose link flow was actually started.

        Asks the server (``link.reconnect``, in a worker); the connect workers
        run in the core and report back through events.
        """
        if not protocols:
            return
        self._client_call_background(
            "link.reconnect", {"protocols": sorted(protocols)}, after=after
        )

    def _dispatch_core_event(self, event: Event) -> None:
        """UI thread: applica un evento del core al TUI."""
        payload = event.payload
        if event.type == "backend_connecting":
            self._mark_backend_connecting(payload["protocol"])
        elif event.type == "backend_status":
            if payload.get("persistent"):
                self._status(payload["text"], 0)
            else:
                self._status(payload["text"])
        elif event.type == "backend_ready":
            self._on_backend_ready(payload["protocol"])
        elif event.type == "backend_done":
            self._mark_backend_done(payload["protocol"])
        elif event.type == GAP:
            logger.debug("Core event bus gap: %s", payload)
        else:
            logger.debug("Ignoring unknown core event %s", event.type)
