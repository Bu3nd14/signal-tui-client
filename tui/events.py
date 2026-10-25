"""View handling of core events: messages, receipts, typing.

The domain work (ingest, edits, reactions, receipts, contact registration) is
done by :meth:`server.core.ChatCore.process`; the handlers here only update
the TUI (contact list, cache mirror, widgets) from the resulting core events.
"""

import logging
import time
from typing import Any

from models import (
    PROTOCOL_SIGNAL,
    PROTOCOL_WHATSAPP,
    ChatContact,
    ChatEvent,
    contact_cache_key,
    protocol_emoji,
    protocol_name,
)
from server.bus import Event
from server.domain import looks_unresolved, resolve_placeholder_name
from server.events_api import contact_from_wire
from ui_components import (
    MessageWidget,
)

logger = logging.getLogger(__name__)

# Re-exported for callers/tests that imported the helpers from here.
_resolve_placeholder_name = resolve_placeholder_name
_looks_unresolved = looks_unresolved


def _signal_stable_key(c: ChatContact) -> str:
    """Identità stabile Signal per il dedup: ``aci`` (UUID) o numero normalizzato."""
    aci = c.extras.get("aci")
    if aci:
        return f"aci:{aci}"
    number = c.extras.get("number") or c.id
    return f"phone:{''.join(ch for ch in str(number) if ch.isdigit())}"


def _cache_find_existing(msgs: list[dict], m: dict) -> dict | None:
    """Trova la voce equivalente a *m* in *msgs* (identità di ``_merge_backend_cache``)."""
    is_mine = bool(m.get("is_mine", False))
    text = m.get("text", "")
    ts = int(m.get("timestamp") or 0)
    mid = m.get("id")
    inc_att = m.get("attachment_id")
    for existing in msgs:
        if bool(existing.get("is_mine", False)) != is_mine:
            continue
        if mid and existing.get("id") and existing.get("id") == mid:
            ex_att = existing.get("attachment_id")
            if not (inc_att and ex_att and inc_att != ex_att):
                return existing
        if existing.get("text", "") != text:
            continue
        existing_ts = int(existing.get("timestamp") or 0)
        if not is_mine:
            if abs(existing_ts - ts) <= 5000:
                return existing
        elif mid:
            if not existing.get("id") and abs(existing_ts - ts) <= 600000:
                return existing
        elif abs(existing_ts - ts) <= 5000:
            return existing
    return None


def _merge_cached_messages(dest: list[dict], src: list[dict]) -> None:
    """Merge add-only (dedup per identità) di *src* in *dest*, poi ordina per ts."""
    for m in src:
        if _cache_find_existing(dest, m) is None:
            dest.append(m)
    dest.sort(key=lambda m: int(m.get("timestamp") or 0))


class EventHandlingMixin:
    def _handle_event(self, raw: ChatEvent) -> bool:
        """TEST-ONLY compatibility entry point for a raw backend ``ChatEvent``.

        Production code must NOT call this: the core pump (``ChatCore``)
        processes raw events and the poll worker consumes the resulting core
        events from the bus.  This wrapper runs ``core.process(raw)``,
        publishes each result on ``self.bus`` (so bus/web consumers observe
        it) and applies the view handler directly, returning ``True`` when
        the event was handled (i.e. the core produced events).

        The published events are marked so the poll worker does not apply
        their view handlers a second time.
        """
        results = self.core.process(raw)
        handled = False
        skip: dict[int, dict] = self.__dict__.setdefault("_compat_payload_ids", {})
        for etype, payload in results:
            # Before publish (the worker may be racing); the payload itself is
            # kept as the value so its id cannot be reused while marked.
            skip[id(payload)] = payload
            event = self.bus.publish(etype, payload)
            if self._dispatch_view_event(event):
                handled = True
        return handled

    def _dispatch_view_event(self, event: Event) -> bool:
        """Apply the view-side effects of a domain core event."""
        handler = {
            "message": self._view_message,
            "message_edit": self._view_edit,
            "reaction_update": self._view_reaction,
            "receipt": self._view_receipt,
            "typing": self._view_typing,
            "contact_update": self._view_contact_update,
            "sent_mirror": self._view_sent_mirror,
        }.get(event.type)
        if handler is None:
            return False
        return handler(event)

    def _view_contact_update(self, ev: Event) -> bool:
        """Applica/appende in UI un contatto risolto dinamicamente.

        - Aggiorna ``display_name``/``phone`` sull'oggetto esistente della lista
          TUI, oppure APPENDE il nuovo contatto (la lista principale non viene
          ricostruita).  Mai riscrittura dello storico: tocca solo ``self.contacts``.
        - R1: per Signal deduplica il cambio di id uuid↔number via
          ``_signal_stable_key`` (rimuove la voce stale, evita righe duplicate).
        - ``payload["merged_into"]`` (WhatsApp): il ghost loser è stato fuso in
          un winner già in lista; lo rimuove (lista, widget, cache) e ri-ancora
          la selezione, senza appendere il contatto.
        """
        p = ev.payload
        protocol = p["protocol"]
        merged_into = p.get("merged_into")
        if merged_into and protocol == PROTOCOL_WHATSAPP:
            return self._view_contact_merged(ev, str(merged_into))

        cache_key = contact_cache_key(protocol, p["contact_id"])
        wire_contact = p.get("contact")
        new_name = p.get("display_name")
        phone = p.get("phone")

        if wire_contact is not None and protocol == PROTOCOL_SIGNAL:
            stable = _signal_stable_key(contact_from_wire(wire_contact))
            if stable:
                for stale in list(self.contacts):
                    if (
                        stale.cache_key != cache_key
                        and stale.protocol == PROTOCOL_SIGNAL
                        and _signal_stable_key(stale) == stable
                    ):
                        self.contacts.remove(stale)
                        widgets = getattr(self, "_contact_widgets", None)
                        if widgets is not None:
                            widgets.pop(stale.cache_key, None)
                        self._dirty_contact_keys.add(stale.cache_key)
                        break

        target = next((c for c in self.contacts if c.cache_key == cache_key), None)
        if target is None and wire_contact is not None:
            # NUOVO contatto → in lista (copia dal wire, non l'oggetto del backend)
            target = contact_from_wire(wire_contact)
            self.contacts.append(target)
        elif target is not None:
            if new_name and target.display_name != new_name:
                target.display_name = new_name
            if phone and not target.extras.get("phone"):
                target.extras["phone"] = phone

        self._contact_list_dirty = True
        self._dirty_contact_keys.add(cache_key)
        return True

    def _view_contact_merged(self, ev: Event, merged_into: str) -> bool:
        """Rimuove dalla UI il ghost loser fuso in *merged_into* (WhatsApp).

        Non appende ``payload["contact"]``: il winner è già nella lista.  Unisce
        la cache del loser nel winner (add-only) e ripunta la selezione.
        """
        protocol = ev.payload["protocol"]
        loser_key = contact_cache_key(protocol, ev.payload["contact_id"])
        winner_key = contact_cache_key(protocol, merged_into)
        self.contacts = [c for c in self.contacts if c.cache_key != loser_key]
        widgets = getattr(self, "_contact_widgets", None)
        if widgets is not None:
            widgets.pop(loser_key, None)
        dirty = getattr(self, "_dirty_contact_keys", None)
        if dirty is not None:
            dirty.add(loser_key)
        loser_msgs = self._cache.get(loser_key)
        if loser_msgs:
            _merge_cached_messages(self._cache.setdefault(winner_key, []), loser_msgs)
        self._cache.pop(loser_key, None)
        if (
            self.selected_contact is not None
            and self.selected_contact.cache_key == loser_key
        ):
            winner = next((c for c in self.contacts if c.cache_key == winner_key), None)
            if winner is not None:
                self.selected_contact = winner
        self._contact_list_dirty = True
        return True

    def _view_sent_mirror(self, ev: Event) -> bool:
        """Handle the lightweight ``sent_mirror`` notification (batch sends).

        The backend send barrier has already materialized the mirror rows
        in cache/DB and ``web/api.py`` already pushed the generic web
        refresh, so this handler only promotes the contact in the TUI
        list: the core never ingests it (no double DB write).
        """
        p = ev.payload
        # Prefer the copy already in ``self.contacts`` (it may differ from the
        # core/backend copy after a contact-list rebuild); the core resolved
        # (and registered with the backend) the fallback in ``p["contact"]``.
        target_key = contact_cache_key(p["protocol"], p["contact_id"])
        contact = next(
            (
                candidate
                for candidate in self.contacts
                if candidate.cache_key == target_key
            ),
            None,
        )
        if contact is None:
            contact = contact_from_wire(p["contact"])
        # Ensure the contact is in the TUI list: a contact materialized by the
        # dynamic lid resolver must still be added.
        existing = {c.cache_key for c in self.contacts}
        if contact.cache_key not in existing:
            self.contacts.append(contact)
            self._contact_list_dirty = True
            self._dirty_contact_keys.add(contact.cache_key)

        # Promote the contact in the "most recent first" ordering; the
        # re-sort/render is deferred to the end of the batch by the poll
        # worker (same pattern as _view_message).
        ts = p.get("timestamp", 0)
        # The core may already have advanced a contact object shared with the
        # backend, so the dirty flag cannot depend on the timestamp changing.
        if isinstance(ts, int):
            if ts > (contact.last_message_ts or 0):
                contact.last_message_ts = ts
            if contact.cache_key != (
                self.selected_contact.cache_key if self.selected_contact else None
            ):
                self._contact_list_dirty = True
                self._dirty_contact_keys.add(contact.cache_key)
        return True

    def _view_message(self, ev: Event) -> bool:
        """Display an ingested incoming/outgoing message (ingest done by core)."""
        p = ev.payload
        protocol = p["protocol"]
        wire_contact: dict[str, Any] = p["contact"]
        msg: dict[str, Any] = p["message"]
        added: bool = p["added"]
        # RIANCORAGGIO: preferisci l'oggetto già nella lista TUI (identità
        # stabile) così gli aggiornamenti a display_name/last_message_ts si
        # riflettono su sort/render.  Se assente, costruiscilo dallo snapshot
        # wire e appendilo (un contatto materializzato dal resolver lid deve
        # comunque entrare in lista).
        tui_contact = next(
            (c for c in self.contacts if c.cache_key == wire_contact["cache_key"]),
            None,
        )
        if tui_contact is not None:
            contact = tui_contact
        else:
            contact = contact_from_wire(wire_contact)
            self.contacts.append(contact)
            self._contact_list_dirty = True
            self._dirty_contact_keys.add(contact.cache_key)
        cache_key = contact.cache_key
        ts = p["timestamp"]
        is_mine = msg.get("is_mine", False)

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

        # Mirror into the UI's protocol-aware cache only when actually new.
        if added:
            if cache_key not in self._cache:
                self._cache[cache_key] = []
            self._cache[cache_key].append(
                {
                    "id": msg.get("id"),
                    "text": msg["text"],
                    "is_mine": is_mine,
                    "sender": msg.get("sender", ""),
                    "timestamp": ts,
                    "quote_text": msg.get("quote_text"),
                    "msg_type": msg.get("msg_type", "text"),
                    "attachment_info": msg.get("attachment_info"),
                    "attachment_id": msg.get("attachment_id"),
                    "content_type": msg.get("content_type"),
                    "quote_timestamp": msg.get("quote_timestamp"),
                    "quote_author": msg.get("quote_author"),
                    "reply_to_message_id": msg.get("reply_to_message_id"),
                    "quote_attachment_id": msg.get("quote_attachment_id"),
                    "quote_attachment_path": msg.get("quote_attachment_path"),
                    "quote_content_type": msg.get("quote_content_type"),
                    "read": is_mine,
                    "status": msg.get("status", "sent" if is_mine else "read"),
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
                protocol,
                cache_key,
                int(ts),
                msg.get("text", ""),
            )
            if ts and identity not in self._seen_message_ids:
                self._seen_timestamps.add((protocol, cache_key, ts))
                self._seen_message_ids.add(identity)
                self.call_from_thread(
                    self._add_message,
                    msg["text"],
                    is_mine=is_mine,
                    quote_text=msg.get("quote_text"),
                    msg_type=msg.get("msg_type", "text"),
                    attachment_info=msg.get("attachment_info"),
                    attachment_id=msg.get("attachment_id"),
                    content_type=msg.get("content_type"),
                    timestamp=ts,
                    sender=msg.get("sender", ""),
                    status=msg.get("status", "sent" if is_mine else "read"),
                    message_id=msg.get("id"),
                    quote_attachment_id=msg.get("quote_attachment_id"),
                    quote_attachment_path=msg.get("quote_attachment_path"),
                    quote_content_type=msg.get("quote_content_type"),
                )
        else:
            # Message for another contact: mark the list dirty; the unread
            # badge / reorder is refreshed ONCE per poll batch (not per event).
            self._contact_list_dirty = True
            self._dirty_contact_keys.add(cache_key)

        return True

    def _view_edit(self, ev: Event) -> bool:
        """Riflette un edit già applicato dal core: cache UI, identity sets e
        widget — senza mai creare una bolla nuova."""
        p = ev.payload
        info = p["info"]
        new_text = p["text"]

        # Mirror nella cache UI + chirurgia identità.
        cache_key = p["contact"]["cache_key"]
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
                p["protocol"],
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

    def _view_reaction(self, ev: Event) -> bool:
        """Reaction già applicata dal core: nessun effetto di vista (la bolla
        si aggiorna al prossimo render dalla cache del backend)."""
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

    def _view_receipt(self, ev: Event) -> bool:
        """Mirror the statuses updated by the core into the UI cache and
        refresh widgets if the affected contact is currently selected."""
        p = ev.payload
        updated = p["updated"]
        contact_id = p["contact_id"]

        # Mirror the updated statuses into the UI's cache.
        ui_key = contact_cache_key(p["protocol"], contact_id)
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

        if self.selected_contact and self.selected_contact.id == contact_id:
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

    # ─── Typing indicators ──────────────────────────────────────────────────

    def _view_typing(self, ev: Event) -> bool:
        """Apply a (core-validated) typing-indicator event.

        Updates ``self._typing_contacts`` (contact cache_key → time of last
        STARTED) and refreshes the contact list label so the ``✍️`` icon
        appears/disappears next to the contact.

        Typing indicators are ephemeral: they are never saved to cache and
        never shown as messages in the chat log.
        """
        action = ev.payload["action"]
        cache_key = contact_cache_key(ev.payload["protocol"], ev.payload["contact_id"])
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
