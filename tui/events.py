"""Incoming event dispatch: messages, receipts, typing."""

import logging
import time

from models import (
    PROTOCOL_SIGNAL,
    ChatContact,
    ChatEvent,
    contact_cache_key,
    protocol_emoji,
    protocol_name,
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
        if event.type == "message_edit":
            return self._handle_edit_event(event)
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
                    if hasattr(backend, "contacts"):
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
            if cache_key != (
                self.selected_contact.cache_key if self.selected_contact else None
            ):
                self._contact_list_dirty = True
                self._dirty_contact_keys.add(cache_key)

        # Save to DB + backend cache.  Returns True only if newly added;
        # if the identity already exists (e.g. an optimistic save is confirmed
        # by a sync sent-envelope), nothing is duplicated.
        ingest = getattr(backend, "ingest_message", None)
        added = ingest(contact.id, event.payload, ts) if ingest is not None else True

        # Mirror into the UI's protocol-aware cache only when actually new.
        if added:
            if getattr(self, "_web_enabled", False):
                from web.bridge import push_event

                push_event(
                    {
                        "type": "message",
                        "payload": {
                            "id": event.payload.get("id"),
                            "protocol": event.protocol,
                            "contact_id": contact.id,
                            "timestamp": ts,
                        },
                    }
                )
            if cache_key not in self._cache:
                self._cache[cache_key] = []
            self._cache[cache_key].append(
                {
                    "id": event.payload.get("id"),
                    "text": event.payload["text"],
                    "is_mine": is_mine,
                    "sender": event.payload.get("sender", ""),
                    "timestamp": ts,
                    "quote_text": event.payload.get("quote_text"),
                    "msg_type": event.payload.get("msg_type", "text"),
                    "attachment_info": event.payload.get("attachment_info"),
                    "attachment_id": event.payload.get("attachment_id"),
                    "content_type": event.payload.get("content_type"),
                    "quote_timestamp": event.payload.get("quote_timestamp"),
                    "quote_author": event.payload.get("quote_author"),
                    "reply_to_message_id": event.payload.get("reply_to_message_id"),
                    "quote_attachment_id": event.payload.get("quote_attachment_id"),
                    "quote_attachment_path": event.payload.get("quote_attachment_path"),
                    "quote_content_type": event.payload.get("quote_content_type"),
                    "read": is_mine,
                    "status": event.payload.get(
                        "status", "sent" if is_mine else "read"
                    ),
                }
            )

        # When a real message arrives, the sender stopped typing: move to the
        # mumbling (💭) state if they were typing.
        if cache_key in self._typing_contacts or cache_key in self._typing_mumbling:
            self._typing_contacts.pop(cache_key, None)
            self._typing_mumbling[cache_key] = (
                time.time() + self._TYPING_MUMBLING_DURATION
            )

        # If it's the current contact, show it immediately; else bump unread.
        if (
            self.selected_contact
            and self.selected_contact.cache_key == cache_key
            and added
        ):
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
                    content_type=event.payload.get("content_type"),
                    timestamp=ts,
                    sender=event.payload.get("sender", ""),
                    status=event.payload.get("status", "sent" if is_mine else "read"),
                    message_id=event.payload.get("id"),
                    quote_attachment_id=event.payload.get("quote_attachment_id"),
                    quote_attachment_path=event.payload.get("quote_attachment_path"),
                    quote_content_type=event.payload.get("quote_content_type"),
                )
        else:
            # Message for another contact: mark the list dirty; the unread
            # badge / reorder is refreshed ONCE per poll batch (not per event).
            self._contact_list_dirty = True
            self._dirty_contact_keys.add(cache_key)

        return True

    def _handle_edit_event(self, event: ChatEvent) -> bool:
        """Applica un edit ricevuto: backend cache + DB (via apply_edit),
        cache UI, identity sets e widget — senza mai creare una bolla nuova."""
        backend = self.manager.get(event.protocol)
        if backend is None:
            return False
        apply_edit = getattr(backend, "apply_edit", None)
        if apply_edit is None:
            return False
        payload = event.payload
        new_text = payload.get("text", "")
        edit_id = payload.get("edit_message_id")
        if edit_id is None or not new_text:
            return False

        contact = payload.get("contact")
        if contact is None:
            identify = getattr(backend, "_identify_contact", None)
            if identify is not None:
                contact = identify(event.contact_id)
        if contact is None:
            contact = ChatContact(
                id=event.contact_id,
                display_name=event.contact_id,
                protocol=event.protocol,
            )

        # Single mutation point: cache backend + SQLite.  Idempotente:
        # testo già nuovo / target ignoto / media → None → no-op.
        info = apply_edit(
            event.contact_id,
            str(edit_id),
            new_text,
            is_mine=payload.get("is_mine"),
            edit_timestamp=payload.get("edit_timestamp"),
        )
        if not info:
            return False

        # Mirror nella cache UI + chirurgia identità.
        cache_key = contact.cache_key
        ui_msgs = self._cache.get(cache_key) or []
        target = next(
            (
                m
                for m in ui_msgs
                if m.get("id") is not None and str(m["id"]) == str(info["message_id"])
            ),
            None,
        )
        if target is None:
            target = next(
                (
                    m
                    for m in ui_msgs
                    if int(m.get("timestamp") or 0) == int(info["timestamp"])
                    and bool(m.get("is_mine")) == bool(info["is_mine"])
                    and m.get("text") == info["old_text"]
                ),
                None,
            )
        if target is not None:
            self._rewrite_message_identity(
                event.protocol,
                cache_key,
                info["timestamp"],
                info["old_text"],
                new_text,
                target.get("id") or info["message_id"],
            )
            target["text"] = new_text
            target["edited"] = True

        # Widget, solo se la chat è aperta (altrimenti il prossimo render
        # leggerà il testo già aggiornato dalla cache).
        if self.selected_contact and self.selected_contact.cache_key == cache_key:
            self.call_from_thread(self._update_edited_widget, info, new_text)
        return True

    def _update_edited_widget(self, info: dict, new_text: str) -> None:
        """UI thread: riscrive la bolla esistente in place, mai una nuova."""
        try:
            for child in self.chat_log.children:
                if not isinstance(child, MessageWidget):
                    continue
                if child._message_id and str(child._message_id) == str(
                    info["message_id"]
                ):
                    child.update_text(new_text)
                    return
                if (
                    child._msg_timestamp == info["timestamp"]
                    and child._msg_text == info["old_text"]
                ):
                    child.update_text(new_text)
                    return
        except Exception:
            logger.debug("edited widget update failed", exc_info=True)

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
            # Generic backend (WhatsApp/Telegram) uses a message-ids receipt
            # payload scoped to the contact that generated the receipt.
            updated = process(
                {
                    "message_ids": event.payload.get("message_ids", []),
                    "is_read": event.payload.get("is_read", False),
                    "contact_id": event.contact_id,
                }
            )
        if not updated:
            return False

        # Mirror the updated statuses into the UI's cache.
        ui_key = contact_cache_key(event.protocol, event.contact_id)
        ui_msgs = self._cache.get(ui_key)
        if ui_msgs is not None:
            by_id = {str(m.get("id", "")): m for m in ui_msgs if m.get("id")}
            by_ts = {m.get("timestamp"): m for m in ui_msgs}
            for msg in updated:
                target = None
                mid = msg.get("id")
                if mid is not None:
                    target = by_id.get(str(mid))
                if target is None:
                    target = by_ts.get(msg.get("timestamp"))
                if target is None:
                    # Defensive fuzzy fallback: echo timestamp drift.  Bound the
                    # match to the text so an unrelated entry is never touched.
                    # On hit, self-heal the entry's identity with the real id.
                    text = msg.get("text", "")
                    ts = msg.get("timestamp", 0)
                    if text:
                        for m in ui_msgs:
                            if (
                                m.get("text", "") == text
                                and abs(int(m.get("timestamp") or 0) - ts) <= 2000
                            ):
                                target = m
                                break
                if target is not None:
                    target["status"] = msg.get("status", target.get("status", "sent"))
                    if mid is not None and not target.get("id"):
                        target["id"] = mid

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
        # Build indexes once (O(M)) instead of scanning children for every
        # receipt (O(N×M)).  A widget is addressable by its server message id
        # (``_message_id``), by exact (timestamp, text), or by a fuzzy
        # timestamp fallback.
        by_id: dict[str, MessageWidget] = {}
        by_identity: dict[tuple[int, str], MessageWidget] = {}
        for child in chat_log.children:
            if isinstance(child, MessageWidget):
                if child._message_id:
                    by_id[str(child._message_id)] = child
                by_identity[(child._msg_timestamp, child._msg_text)] = child

        for msg in updated_messages:
            ts = msg.get("timestamp", 0)
            new_status = msg.get("status", "sent")
            mid = msg.get("id")
            text = msg.get("text", "")
            # 1) Primary: exact server message id.
            widget = by_id.get(str(mid)) if mid is not None else None
            if widget is not None:
                widget.set_status(new_status)
                continue
            # 2) Exact (timestamp, text) match.
            widget = by_identity.get((ts, text))
            if widget is not None:
                widget.set_status(new_status)
                continue
            # 3) Fuzzy fallback (echo timestamp drift) BOUND TO THE TEXT so we
            #    never recolor an unrelated bubble.
            if text:
                for (candidate_ts, candidate_text), w in by_identity.items():
                    if candidate_text == text and abs(candidate_ts - ts) <= 2000:
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
        new_text = self._member_label(contact)
        item = self._contact_widgets.get(cache_key)
        if item is None:
            return
        label = item.children[0] if item.children else None
        if label is None:
            return
        if getattr(item, "_label_text", None) != new_text:
            label.update(new_text)
            item._label_text = new_text

    def _member_label(self, contact: ChatContact) -> str:
        """Build the member-row label for the grouped contact list.

        Format: ``{emoji} {protocol name}`` + (if unread) `` *{N}`` + (if typing) `` ✍️``.
        The contact name is NOT repeated here — it already appears in the group
        header (which can be repeated up to once per protocol).  The unread badge
        and the typing/mumbling icons keep the exact same semantics as the old
        ``_contact_label``.
        """
        label = f"{protocol_emoji(contact.protocol)} {protocol_name(contact.protocol)}"
        unread = self._unread_counts.get(contact.cache_key, 0)
        if unread > 0 and contact != self.selected_contact:
            label += f" *{unread}"
        if contact.cache_key in self._typing_contacts:
            label += " ✍️"
        elif contact.cache_key in self._typing_mumbling:
            label += " 💭"
        return label
