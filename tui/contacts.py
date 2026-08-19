"""Contact list: sorting, filtering, progressive render, selection."""

import logging

from textual.widgets import Label, ListItem, ListView

from contact_picker import contact_sort_key
from models import (
    PROTOCOL_WHATSAPP,
    ChatContact,
    protocol_emoji,
)
from ui_components import MessageTextArea

logger = logging.getLogger(__name__)


class ContactListMixin:
    @staticmethod
    def _contact_sort_key(c: ChatContact) -> tuple:
        """Key per ordinare i contatti: ultimi messaggi in alto.

        Delega a ``contact_picker.contact_sort_key`` (funzione condivisa con il
        picker della rubrica) per garantire la stessa semantica in entrambi i
        punti: (1) contatti con messaggi per ``last_message_ts`` desc; (2)
        senza messaggi ma con nome, alfabetici; (3) solo numero in coda.
        """
        return contact_sort_key(c)

    def _sort_contacts(self):
        """Sort contacts: contacts with messages first (most recent first),
        then those without messages (alphabetical), unnamed ones last."""
        self.contacts.sort(key=self._contact_sort_key)

    def _reorder_contact_list(self):
        """Re-sort in-memory contacts and refresh the visible list.

        Preserves the current selection (if still visible under the active
        filter) and does not touch the chat log.  Runs in the UI thread.
        """
        self._sort_contacts()
        self._render_contact_list(list(self.contacts))

    def _promote_contact_after_send(self, contact: ChatContact, timestamp: int) -> None:
        """Update and immediately render the sent contact's position in the list."""
        if timestamp <= contact.last_message_ts:
            return

        contact.last_message_ts = timestamp
        self._sort_contacts()
        if self._is_mounted:
            self._render_contact_list(list(self.contacts))

    def _sync_last_ts(self):
        """Recover ``last_message_ts`` for every contact from the local cache.

        Best-effort fallback for whichever backend did not already populate it
        (e.g. WhatsApp whose /chats payload may lack ``t``).  Uses ``_cache``
        (keyed by ``contact_cache_key``), where each message dict carries a
        ``timestamp``.  Keeps the highest timestamp found.
        """
        for c in self.contacts:
            cache_key = c.cache_key
            msgs = self._cache.get(cache_key) or []
            ts = c.last_message_ts
            for m in msgs:
                mts = int(m.get("timestamp") or 0)
                ts = max(ts, mts)
            c.last_message_ts = max(c.last_message_ts, ts)

    def _filtered_contacts(self) -> list[ChatContact]:
        """Return contacts matching the active protocol filter."""
        if self._protocol_filter in ("signal", "whatsapp", "telegram"):
            return [c for c in self.contacts if c.protocol == self._protocol_filter]
        return list(self.contacts)

    def _protocol_class(self, contact: ChatContact) -> str:
        """Return the CSS accent class for a contact's protocol."""
        return f"protocol-{contact.protocol}"

    def _filter_title_suffix(self) -> str:
        """Human-readable suffix describing the active filter state."""
        if self._protocol_filter == "signal":
            return " - Signal"
        if self._protocol_filter == "whatsapp":
            return " - WhatsApp"
        if self._protocol_filter == "telegram":
            return " - Telegram"
        if self._protocol_filter == "all":
            return " - All"
        return ""

    def _apply_contact_visibility(self) -> None:
        """Toggle ``display`` on ListItems based on the active protocol filter.

        Unlike ``_render_contact_list`` this *never* destroys widgets — it
        only sets ``display=True`` / ``display=False`` on the children that
        are already in the DOM.  Called on every Ctrl+W cycle instead of a
        full rebuild, keeping the UI responsive even with 600+ contacts.

        The DOM must always hold the FULL contact set (``_render_contact_list``
        renders ``self.contacts``); this method only decides what's visible.
        It keeps the highlight on the selected contact while it's still
        visible under the filter, falling back to the first visible row.
        """
        contact_list = self.query_one("#contact-list", ListView)
        visible = {c.cache_key for c in self._filtered_contacts()}

        first_visible: int | None = None
        selected_index: int | None = None
        for i, child in enumerate(contact_list.children):
            key: str | None = getattr(child, "_contact_id", None)
            show = key in visible
            child.display = show
            if show:
                if first_visible is None:
                    first_visible = i
                if (
                    self.selected_contact is not None
                    and key == self.selected_contact.cache_key
                ):
                    selected_index = i

        if selected_index is not None:
            # Keep the highlight on the still-visible selected contact.
            contact_list.index = selected_index
        elif first_visible is not None:
            contact_list.index = first_visible
        elif self.selected_contact is not None:
            # No contact visible under this filter — deselect.
            self.selected_contact = None

        # Re-sync the `-highlight` class to exactly one row.  After an in-place
        # reorder (move_child) the ListView's highlight can remain stuck on a
        # stale ListItem because `index` didn't change (so `watch_index` never
        # fired to clear it).  Forcing `highlighted` to match `index` guarantees
        # a single highlighted contact.
        self._sync_contact_highlight(contact_list, contact_list.index)

    def _sync_contact_highlight(self, contact_list, index) -> None:
        """Force the `-highlight` class to match `index` on exactly one row.

        Textual's `ListView` tracks the highlighted row via `ListItem.highlighted`
        (the `-highlight` CSS class), toggled in `watch_index`.  When rows are
        reordered with `move_child` the highlighted widget can stay highlighted
        while `index` still points to a different position; the next `index`
        change then highlights a *second* row without clearing the first.  This
        helper re-aligns every row to `index`, leaving a single highlight.
        O(N) with cheap equality short-circuits, so it's safe on the hot path.
        """
        for i, child in enumerate(contact_list.children):
            child.highlighted = index is not None and i == index

    def _start_progressive_render(self, contacts: list[ChatContact]) -> None:
        """Begin a progressive (chunked) contact-list rebuild.

        Cancels any in-progress render, clears the ListView, and schedules
        ``_render_next_chunk`` to append contacts 50-at-a-time.
        """
        if self._render_timer is not None:
            self._render_timer.stop()
            self._render_timer = None

        self._pending_contacts = list(contacts)
        self._render_chunk_index = 0

        contact_list = self.query_one("#contact-list", ListView)
        contact_list.clear()
        self._contact_widgets.clear()
        self._render_next_chunk()

    def _render_next_chunk(self) -> None:
        """Render the next *chunk_size* contacts (progressive startup).

        Called initially from ``_on_backend_ready`` and then
        self-schedules via ``set_timer`` until all pending contacts are
        rendered.  Each chunk yields control back to the Textual event
        loop so the UI never freezes.
        """
        contact_list = self.query_one("#contact-list", ListView)
        visible = {c.cache_key for c in self._filtered_contacts()}
        start = self._render_chunk_index
        end = min(start + self._render_chunk_size, len(self._pending_contacts))
        chunk = self._pending_contacts[start:end]

        for c in chunk:
            text = self._contact_label(c)
            item = ListItem(Label(text))
            item._contact_id = c.cache_key
            item._label_text = text
            item.add_class(self._protocol_class(c))
            item.display = c.cache_key in visible
            contact_list.append(item)
            self._contact_widgets[c.cache_key] = item

        self._render_chunk_index = end
        if end < len(self._pending_contacts):
            self._render_timer = self.set_timer(0.05, self._render_next_chunk)
        else:
            self._render_timer = None
            # All chunks rendered — re-apply the filter's visibility and restore
            # the selection highlight (or fall back to the first visible row).
            self._apply_contact_visibility()

    def _render_contact_list(self, filtered: list[ChatContact]) -> None:
        """Renderizza la lista contatti, aggiornandola *in-place* quando la
        composizione/ordine non cambia.

        ``filtered`` è SEMPRE la lista completa (``list(self.contacts)``): il
        DOM deve contenere tutti i contatti; il filtro protocollo viene poi
        applicato solo via ``_apply_contact_visibility`` (toggle ``display``).
        In questo modo Ctrl+W non perde mai i contatti degli altri protocolli.

        Textual, dopo un ``clear()``+ri-append, ricostruisce man mano gli item:
        con ~350 contatti e un refresh frequente (poll ~1s) questo smonta/rimonta
        continuamente i ``ListItem`` (la lista diventa momentaneamente VUOTA), e
        la sola costruzione dei ~350 ListItem sul main thread bloccava la UI
        (finestra di chat in seconda priorità).  Qui i 3 casi:
          - stesso ordine        -> aggiorna solo testo/classi in-place;
          - stesso INSIEME ma    -> RIORDINA i ListItem esistenti in-place
            ordine diverso         (nessun clear, nessuna nuova costruzione),
                                  per via di un nuovo messaggio che sposta un
                                  contatto in cima;
          - insieme diverso      -> rebuild completo (solo filtro/startup).
        """
        contact_list = self.query_one("#contact-list", ListView)
        existing = list(contact_list.children)
        cur_ids = [getattr(it, "_contact_id", None) for it in existing]
        want_ids = [c.cache_key for c in filtered]

        # Stop any in-flight progressive render so stale chunks
        # don't corrupt this render.
        if self._render_timer is not None:
            self._render_timer.stop()
            self._render_timer = None

        def _sync_item(item, c):
            """Aggiorna testo/classe di un ListItem esistente per il contatto c."""
            label = item.children[0] if item.children else None
            new_text = self._contact_label(c)
            if getattr(item, "_label_text", None) != new_text and label is not None:
                label.update(new_text)
                item._label_text = new_text
            if not item.has_class(self._protocol_class(c)):
                for cl in ("protocol-signal", "protocol-whatsapp", "protocol-telegram"):
                    item.remove_class(cl)
                item.add_class(self._protocol_class(c))

        if cur_ids == want_ids:
            # Fast path: composizione+ordine invariati -> aggiorna in-place.
            for item, c in zip(existing, filtered):
                _sync_item(item, c)
        elif set(cur_ids) == set(want_ids):
            # Stesso INSIEME di contatti ma ordine diverso (un nuovo messaggio
            # ha spostato un contatto in cima): riordina i ListItem ESISTENTI
            # in-place.  Niente clear(), niente costruzione di nuovi widget ->
            # la lista non diventa mai vuota e il main non si blocca.
            by_id = {getattr(it, "_contact_id", None): it for it in existing}
            reordered = [by_id[cid] for cid in want_ids if cid in by_id]
            for item, c in zip(reordered, filtered):
                _sync_item(item, c)
            if existing != reordered:
                # Move_child sposta i nodi esistenti nel DOM senza smontarli,
                # un elemento alla volta costruendo l'ordine voluto (idempotente:
                # un elemento già nella posizione giusta è un no-op effettivo).
                # La lista non risulta mai vuota in mezzo.
                for i in range(1, len(reordered)):
                    contact_list.move_child(reordered[i], after=reordered[i - 1])
        elif cur_ids and set(cur_ids) < set(want_ids):
            # Superset: nuovi contatti (es. WhatsApp dopo Signal).
            # Crea SOLO i ListItem mancanti, preserva quelli esistenti,
            # poi riordina tutto con move_child — nessun clear, nessun flash.
            by_id = {getattr(it, "_contact_id", None): it for it in existing}
            for c in filtered:
                text = self._contact_label(c)
                if c.cache_key not in by_id:
                    item = ListItem(Label(text))
                    item._contact_id = c.cache_key
                    item._label_text = text
                    item.add_class(self._protocol_class(c))
                    self._contact_widgets[c.cache_key] = item
                    by_id[c.cache_key] = item
                    contact_list.append(item)
                else:
                    _sync_item(by_id[c.cache_key], c)
            reordered = [by_id[cid] for cid in want_ids if cid in by_id]
            if len(reordered) > 1:
                try:
                    for i in range(1, len(reordered)):
                        contact_list.move_child(reordered[i], after=reordered[i - 1])
                except AttributeError:
                    pass  # mock ListView in tests
        else:
            # Composizione/insieme cambiato (filtro nuovo / backend aggiunto /
            # stato iniziale) -> rebuild progressivo (chunked, non blocca la UI).
            self._start_progressive_render(filtered)

        # Re-apply the active protocol filter so newly added items get the
        # right ``display`` and the highlight lands on the correct row.
        self._apply_contact_visibility()

    def _apply_contact_filter(self) -> None:
        """Re-apply the active protocol filter to the contact list view.

        Uses ``_apply_contact_visibility`` (display toggle) instead of a
        full ListItem rebuild — the DOM keeps all contacts, hidden ones
        are ``display=False``.  Only the CSS border / banner classes are
        synced here.
        """
        self._apply_contact_visibility()
        try:
            section_lbl = self.query_one("#ContactsTitle", Label)
        except Exception as _e:
            logger.debug("Contacts title not found", exc_info=True)
            section_lbl = None
        if section_lbl is not None:
            section_lbl.update(f"📇 Contacts{self._filter_title_suffix()}")

        # Sync the filter accent across the chat border, the contact list border
        # and the two section banners (📇 Contacts / 💬 Chat).
        cls_signal = "chat-filter-signal"
        cls_whats = "chat-filter-whatsapp"
        cls_telegram = "chat-filter-telegram"
        widgets = [self.chat_log]
        for selector in ("#contact-list", "#ContactsTitle", "#ChatTitle"):
            try:
                widgets.append(self.query_one(selector))
            except Exception as _e:
                logger.debug("Filter widget not found: %s", selector, exc_info=True)
        for node in widgets:
            node.remove_class(cls_signal, cls_whats, cls_telegram)
            if self._protocol_filter == "signal":
                node.add_class(cls_signal)
            elif self._protocol_filter == "whatsapp":
                node.add_class(cls_whats)
            elif self._protocol_filter == "telegram":
                node.add_class(cls_telegram)
                # filtro "all": nessuna classe -> default (giallo).

    def action_cycle_protocol_filter(self):
        """Ctrl+W: cycle the contact list filter ALL -> SIGNAL -> WHATSAPP -> TELEGRAM."""
        order = ["all", "signal", "whatsapp", "telegram"]
        idx = (
            order.index(self._protocol_filter) if self._protocol_filter in order else 0
        )
        self._protocol_filter = order[(idx + 1) % len(order)]
        self._apply_contact_filter()
        # NB: volutamente NON scriviamo niente nella chat qui: il ctrl+W aggiorna
        # solo il titolo della barra contatti e la lista visibile, senza inquinare
        # la cronologia della conversazione in corso.

    # ─── Contact selection ─────────────────────────────────────────────────

    def _ensure_contact_selectable(self, contact: ChatContact) -> ChatContact | None:
        """Return the canonical contact to open for *contact*.

        If a contact with the same ``cache_key`` is already in ``self.contacts``
        that object is returned (the comparison is by ``cache_key`` ONLY — the
        dataclass ``__eq__`` includes ``extras``, so two address-book copies of
        the same chat would otherwise be treated as different).  Otherwise the
        contact is OPEN-OR-CREATED as a "ghost": marked ``extras["ghost"]``,
        appended to ``self.contacts``, registered in its backend via
        ``register_contact`` and rendered in-place (superset branch).

        Returns ``None`` when the contact's protocol has no registered backend;
        the caller then shows ``❌ backend non disponibile`` and aborts.
        """
        for existing in self.contacts:
            if existing.cache_key == contact.cache_key:
                return existing

        backend = self.manager.get(contact.protocol)
        if backend is None:
            return None

        contact.extras["ghost"] = True
        self.contacts.append(contact)
        backend.register_contact(contact)
        self._sort_contacts()
        self._render_contact_list(list(self.contacts))
        return contact

    def _check_ghost_whatsapp_number(self, contact: ChatContact) -> None:
        """Worker thread: best-effort check che un numero WhatsApp ghost esista.

        Chiamato solo per contatti WhatsApp appena aperti via open-or-create
        (``extras["ghost"]``).  Un ``False`` esplicito da ``check_number_exists``
        produce un warning informativo (non bloccante) nello status bar;
        ``True``/``None`` (endpoint assente o errore) non fanno nulla.
        """
        backend = self.manager.get(PROTOCOL_WHATSAPP)
        if backend is None:
            return
        rest = getattr(backend, "_rest", None)
        check = getattr(rest, "check_number_exists", None)
        if check is None:
            return
        phone = contact.phone
        if not phone:
            return
        try:
            exists = check(phone)
        except Exception:
            logger.debug("WhatsApp number-exists check failed", exc_info=True)
            return
        if exists is False:
            self.call_from_thread(self._status, f"⚠️ {phone} non risulta su WhatsApp", 0)

    def _select_contact(self, contact: ChatContact) -> None:
        """Select a contact and show its chat.

        Shared by both the contact list (``on_list_view_selected``) and the
        contact picker (``_open_contact_picker``).  Sets ``selected_contact``,
        highlights the contact in the left list, loads the chat, marks all
        messages as read, updates unread badges, and returns focus to the
        message input.
        """
        contact = self._ensure_contact_selectable(contact)
        if contact is None:
            self._status("❌ backend non disponibile")
            return

        self.selected_contact = contact
        # Update the chat banner with the selected contact's name.
        chat_title = self.query_one("#ChatTitle", Label)
        chat_title.update(
            f"{protocol_emoji(contact.protocol)} Chat - {contact.display_name}"
        )
        self._seen_timestamps.clear()
        self._seen_message_ids.clear()
        # Per WhatsApp la ricezione è PUSH via webhook (handle_webhook): WAHA
        # notifica direttamente ogni messaggio della chat aperta, quindi non
        # serve più registrare la chat come "osservata" per un giro di polling.
        # Per le chat non aperte basta lo storico all'apertura + il webhook.
        # Cancel any pending reply so we don't reply to the wrong contact
        self._cancel_reply()
        self._clear_chat()
        self._add_message(
            f"[{protocol_emoji(contact.protocol)} {contact.protocol.title()}] Chat with: "
            f"{self.selected_contact.display_name}",
            is_info=True,
        )
        self._add_message(self.selected_contact.id, is_info=True)
        self._add_message("─" * 40, is_info=True)

        # Bump the reload token so any in-flight load worker from a previous
        # selection is invalidated (avoids mounting messages after _clear_chat).
        self._chat_reload_token += 1
        self.run_worker(
            self._load_messages_worker,
            exclusive=True,
            thread=True,
        )

        # Mark all messages from this contact as read
        cache_key = self.selected_contact.cache_key
        if cache_key in self._cache:
            for msg in self._cache[cache_key]:
                if not msg.get("read", True):
                    msg["read"] = True
        # Mark read via the backend (persists to SQLite).
        self.manager.get(self.selected_contact.protocol).mark_read_sync(
            self.selected_contact.id
        )
        self._unread_counts[cache_key] = 0

        # Highlight the contact in the left list and remove the *N badge.
        # The ListView keeps ALL contacts in the DOM after the Ctrl+W filter
        # (hidden ones have ``display=False``), so we must locate the row via
        # its ListItem (``_contact_widgets``) and index it by its real position
        # in ``contact_list.children`` — NOT by its position in the filtered
        # list (that only matches when the filter is "all").
        contact_list = self.query_one("#contact-list", ListView)
        item = self._contact_widgets.get(self.selected_contact.cache_key)
        if item is not None:
            # Refresh the row label (removes the *N unread badge for this contact).
            label = item.children[0] if item.children else None
            if label is not None:
                label.update(self._contact_label(self.selected_contact))
            # Only highlight when the row is currently visible (a contact picked
            # from the picker may be filtered out of the active list).
            if getattr(item, "display", True):
                try:
                    contact_list.index = contact_list.children.index(item)
                except ValueError:
                    # Item not currently mounted (e.g. mid progressive render).
                    pass

        # Re-sync the `-highlight` class so exactly one row is highlighted even
        # if a previous in-place reorder left a stale highlight behind.
        self._sync_contact_highlight(contact_list, contact_list.index)

        # Return focus to the message input so the user can start typing
        # immediately after selecting a contact.
        try:
            self.query_one("#message-input", MessageTextArea).focus()
        except Exception as _e:
            logger.debug("Failed to focus message input", exc_info=True)

        # Open-or-create: per un ghost WhatsApp appena aperto, verifica in
        # background (worker thread, NON sul thread UI) che il numero esista.
        # Un False esplicito produce un warning non bloccante; True/None no.
        if contact.protocol == PROTOCOL_WHATSAPP and contact.extras.get("ghost"):
            self.run_worker(
                lambda c=contact: self._check_ghost_whatsapp_number(c),
                exclusive=False,
                thread=True,
            )

    def on_list_view_selected(self, event: ListView.Selected):
        """When a contact is selected, show the chat."""
        # Resolve the contact directly from the clicked ListItem's _contact_id.
        # Using ListView.index + _filtered_contacts() fails when hidden
        # children (display=False) are in the ListView (Ctrl+W filter).
        item = event.item
        cache_key = getattr(item, "_contact_id", None)
        if cache_key is None:
            return
        # Find the contact by cache_key
        contact = None
        for c in self.contacts:
            if c.cache_key == cache_key:
                contact = c
                break
        if contact is None:
            return
        # Guard: skip if already selected (avoids double reload)
        if contact == self.selected_contact:
            return
        self._select_contact(contact)
