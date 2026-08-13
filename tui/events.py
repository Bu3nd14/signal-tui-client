"""Incoming event dispatch: messages, receipts, typing."""

import logging
import time


from models import (
    ChatContact,
    ChatEvent,
    PROTOCOL_SIGNAL,
    contact_cache_key,
    protocol_emoji,
)
from ui_components import (
    MessageWidget,
)

logger = logging.getLogger(__name__)


class EventHandlingMixin:

    def _handle_event(self, event: ChatEvent) -> bool:
        """Dispatch a normalized ``ChatEvent`` from a backend poll worker.

        This is the single entry point for all incoming data.  It handles
        message ingestion (via the backend), typing indicators, and receipts,
        and applies only UI-side side effects (display, unread badges).

        Returns ``True`` if the event was handled.
        """
        if event.type == "typing":
            return self._handle_typing_event(event)
        if event.type == "receipt":
            return self._handle_receipt_event(event)
        if event.type == "message":
            return self._handle_message_event(event)
        return False

    def _handle_message_event(self, event: ChatEvent) -> bool:
        """Ingest and display a normalized incoming/outgoing message."""
        # Resolve the owning backend by protocol (Signal, WhatsApp, ...).
        backend = self.manager.get(event.protocol)
        if backend is None:
            return False

        contact = event.payload.get("contact")
        if contact is None:
            # Resolve via the backend's contact table, or fall back to a
            # placeholder built from the event's contact id.
            identify = getattr(backend, "_identify_contact", None)
            if identify is not None:
                contact = identify(event.contact_id)
            if contact is None:
                contact = ChatContact(
                    id=event.contact_id,
                    display_name=event.contact_id,
                    protocol=event.protocol,
                )
                # New contact discovered live — add to lists and trigger re-render
                existing = {c.cache_key for c in self.contacts}
                if contact.cache_key not in existing:
                    self.contacts.append(contact)
                    if hasattr(backend, 'contacts'):
                        backend.contacts.append(contact)
                    self._contact_list_dirty = True
                    self._dirty_contact_keys.add(contact.cache_key)
        cache_key = contact.cache_key
        ts = event.payload.get("timestamp", 0)
        is_mine = event.payload.get("is_mine", False)

        # Aggiorna il timestamp dell'ultimo messaggio del contatto così la
        # lista contatti (ordinata per "ultimo messaggio") risente subito del
        # nuovo arrivo.  Il re-sort/render è differito a FINE batch dal poll
        # worker (flag _contact_list_dirty), per non ricostruire 320 contatti
        # ad ogni singolo messaggio.
        if isinstance(ts, int) and ts > (contact.last_message_ts or 0):
            contact.last_message_ts = ts
            if cache_key != (self.selected_contact.cache_key if self.selected_contact else None):
                self._contact_list_dirty = True
                self._dirty_contact_keys.add(cache_key)

        # Save to DB + backend cache.  Returns True only if newly added;
        # if the identity already exists (e.g. an optimistic save is confirmed
        # by a sync sent-envelope), nothing is duplicated.
        ingest = getattr(backend, "ingest_message", None)
        added = ingest(contact.id, event.payload, ts) if ingest is not None else True

        # Mirror into the UI's protocol-aware cache only when actually new.
        if added:
            if cache_key not in self._cache:
                self._cache[cache_key] = []
            self._cache[cache_key].append({
                "text": event.payload["text"],
                "is_mine": is_mine,
                "sender": event.payload.get("sender", ""),
                "timestamp": ts,
                "quote_text": event.payload.get("quote_text"),
                "msg_type": event.payload.get("msg_type", "text"),
                "attachment_info": event.payload.get("attachment_info"),
                "attachment_id": event.payload.get("attachment_id"),
                "read": is_mine,
                "status": "sent" if is_mine else "read",
            })

        # When a real message arrives, the sender stopped typing: move to the
        # mumbling (💭) state if they were typing.
        if cache_key in self._typing_contacts or cache_key in self._typing_mumbling:
            self._typing_contacts.pop(cache_key, None)
            self._typing_mumbling[cache_key] = time.time() + self._TYPING_MUMBLING_DURATION

        # If it's the current contact, show it immediately; else bump unread.
        if self.selected_contact and self.selected_contact.cache_key == cache_key and added:
            # Gate di visualizzazione LIVE: usa l'identità (protocol, key, ts,
            # testo) come _refresh_chat, NON il solo timestamp.  Due messaggi
            # WhatsApp distinti nello stesso secondo (stesso ts) devono essere
            # mostrati entrambi; il timestamp da solo li renderebbe indistinguibili
            # e il secondo verrebbe scartato (poi ricompariva solo rientrando).
            identity = (
                event.protocol,
                cache_key,
                int(ts),
                event.payload.get("text", ""),
            )
            if ts and identity not in self._seen_message_ids:
                self._seen_timestamps.add((event.protocol, cache_key, ts))
                self._seen_message_ids.add(identity)
                self.call_from_thread(
                    self._add_message,
                    event.payload["text"],
                    is_mine=is_mine,
                    quote_text=event.payload.get("quote_text"),
                    msg_type=event.payload.get("msg_type", "text"),
                    attachment_info=event.payload.get("attachment_info"),
                    attachment_id=event.payload.get("attachment_id"),
                    timestamp=ts,
                    sender=event.payload.get("sender", ""),
                    status="sent" if is_mine else "read",
                )
        else:
            # Message for another contact: mark the list dirty; the unread
            # badge / reorder is refreshed ONCE per poll batch (not per event).
            self._contact_list_dirty = True
            self._dirty_contact_keys.add(cache_key)

        return True

    def _handle_receipt_event(self, event: ChatEvent) -> bool:
        """Process a receipt event (delivery / read receipts).

        The backend updates its own cache (and SQLite for Signal); the status
        changes are mirrored into the UI cache, and widgets are refreshed if
        the affected contact is currently selected.
        """
        backend = self.manager.get(event.protocol)
        if backend is None:
            return False
        process = getattr(backend, "process_receipt", None)
        if process is None:
            return False

        if event.protocol == PROTOCOL_SIGNAL:
            # Reconstruct the Signal receipt envelope the backend expects.
            receipt = event.payload.get("receipt", {})
            envelope = {
                "sourceNumber": event.contact_id,
                "source": event.contact_id,
                "receiptMessage": receipt,
            }
            updated = process(envelope)
        else:
            # Generic backend (WhatsApp) uses a message-ids receipt payload.
            updated = process({
                "message_ids": event.payload.get("message_ids", []),
                "is_read": event.payload.get("is_read", False),
            })
        if not updated:
            return False

        # Mirror the updated statuses into the UI's cache.
        ui_key = contact_cache_key(event.protocol, event.contact_id)
        ui_msgs = self._cache.get(ui_key)
        if ui_msgs is not None:
            by_ts = {m.get("timestamp"): m for m in ui_msgs}
            for msg in updated:
                target = by_ts.get(msg.get("timestamp"))
                if target is not None:
                    target["status"] = msg.get("status", target.get("status", "sent"))

        if self.selected_contact and self.selected_contact.id == event.contact_id:
            self.call_from_thread(self._update_message_widgets_status, updated)
        return True


    def _update_message_widgets_status(self, updated_messages: list[dict]) -> None:
        """Update the visual status of MessageWidget instances in the chat log.

        Parameters
        ----------
        updated_messages:
            List of message dicts that had their status changed.
        """
        chat_log = self.chat_log
        # Build timestamp→widget index once (O(M)) instead of scanning
        # children for every receipt (O(N×M)).
        by_ts: dict[int, MessageWidget] = {}
        for child in chat_log.children:
            if isinstance(child, MessageWidget):
                by_ts[child._msg_timestamp] = child

        for msg in updated_messages:
            ts = msg.get("timestamp", 0)
            new_status = msg.get("status", "sent")
            # Exact match O(1) — covers the common case.
            widget = by_ts.get(ts)
            if widget is not None:
                widget.set_status(new_status)
                continue
            # Fuzzy fallback: WAHA timestamps may differ by a few ms from
            # the optimistic-send timestamp.  Runs only on cache miss.
            for candidate_ts, w in by_ts.items():
                if abs(candidate_ts - ts) <= 2000:
                    w.set_status(new_status)
                    break

    # ─── Typing indicators ──────────────────────────────────────────────────

    def _handle_typing_event(self, event: ChatEvent) -> bool:
        """Process a typing-indicator event.

        Updates ``self._typing_contacts`` (contact cache_key → time of last
        STARTED) and refreshes the contact list label so the ``✍️`` icon
        appears/disappears next to the contact.

        Typing indicators are ephemeral: they are never saved to cache and
        never shown as messages in the chat log.
        """
        action = event.payload.get("action", "")
        if action not in ("STARTED", "STOPPED"):
            return False

        cache_key = contact_cache_key(event.protocol, event.contact_id)
        now = time.time()

        if action == "STARTED":
            self._typing_contacts[cache_key] = now
            # A new STARTED clears any mumbling state (they are actively
            # typing again).
            self._typing_mumbling.pop(cache_key, None)
        else:  # STOPPED
            self._typing_contacts.pop(cache_key, None)
            # The contact stopped typing without sending a message: keep the
            # 💭 icon visible for a while (the list is always alphabetical,
            # so this only affects the icon, not the order).
            self._typing_mumbling[cache_key] = now + self._TYPING_MUMBLING_DURATION

        # Refresh del label del contatto (in-place, solo la riga interessata:
        # niente rebuild dell'intera lista a ogni evento typing).
        self.call_from_thread(self._update_typing_label, cache_key)
        return True

    def _update_typing_label(self, cache_key: str) -> None:
        """Aggiorna SOLO la riga del contatto che ha cambiato stato di typing.

        Sostituisce ``_refresh_typing_indicator`` nel flusso di arrivo degli
        eventi typing, che ricostruiva l'intera lista (sort + render O(N) su
        350 righe) per via di un singolo cambio di icona ``✍️``/``💭``: su
        raffiche di eventi typing (WhatsApp manda STARTED/STOPPED per molte
        chat) questo saturava il main thread e rendeva la digitazione lenta.

        Il typing cambia solo il testo della riga (mai l'ordinamento), quindi
        qui ci limitiamo a ri-etichettare in-place il ``ListItem`` interessato,
        senza toccare l'albero né gli altri ~350 item.
        """
        if not self.contacts:
            return
        # Trova il contatto in memoria per calcolare il nuovo label (che legge
        # _typing_contacts/_typing_mumbling, già aggiornati dall'evento).
        contact = next((c for c in self.contacts if c.cache_key == cache_key), None)
        if contact is None:
            return
        new_text = self._contact_label(contact)
        item = self._contact_widgets.get(cache_key)
        if item is None:
            return
        label = item.children[0] if item.children else None
        if label is None:
            return
        if getattr(item, "_label_text", None) != new_text:
            label.update(new_text)
            item._label_text = new_text

    def _contact_label(self, contact: ChatContact) -> str:

        """Build the contact list label.

        Format: ``{emoji} {name}`` + (if unread) `` *{N}`` + (if typing) `` ✍️``.
        The emoji is chosen per protocol (📱 for Signal, 💬 for WhatsApp).
        The typing icon appears to the right of the unread badge when present,
        otherwise to the right of the name.
        """
        label = f"{protocol_emoji(contact.protocol)} {contact.display_name}"
        unread = self._unread_counts.get(contact.cache_key, 0)
        if unread > 0 and contact != self.selected_contact:
            label += f" *{unread}"
        if contact.cache_key in self._typing_contacts:
            label += " ✍️"
        elif contact.cache_key in self._typing_mumbling:
            label += " 💭"
        return label
