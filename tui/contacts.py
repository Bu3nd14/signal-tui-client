"""Contact list: sorting, filtering, progressive render, selection."""

import logging
from dataclasses import dataclass

from textual.widgets import Label, ListItem, ListView

from contact_picker import (
    PickerEntry,
    _protocol_priority,
    contact_sort_key,
    entry_default_contact,
    group_by_person,
)
from models import (
    PROTOCOL_WHATSAPP,
    ChatContact,
    protocol_emoji,
)
from ui_components import MessageTextArea

logger = logging.getLogger(__name__)


@dataclass
class _Row:
    """Flat descriptor for one contact-list row (group header or member).

    ``kind`` is ``"group"`` (header) or ``"member"``; ``key`` is the row's
    ``_contact_id`` (``person:<group_key>`` for headers, ``cache_key`` for
    members); ``group_key`` is the owning ``entry.key`` (on headers it equals
    the entry's own key).
    """

    kind: str
    key: str
    group_key: str | None
    contact: ChatContact | None = None
    entry: PickerEntry | None = None


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
        """Return contacts matching the active protocol + unread filters."""
        if self._protocol_filter in ("signal", "whatsapp", "telegram"):
            contacts = [c for c in self.contacts if c.protocol == self._protocol_filter]
        else:
            contacts = list(self.contacts)
        if self._unread_only:
            contacts = [
                c for c in contacts if self._unread_counts.get(c.cache_key, 0) > 0
            ]
        return contacts

    def _visible_keys(self) -> set[str]:
        """Return the cache_keys that should be visible under the active filter.

        The base set is the pure filtered set (``_filtered_contacts``).  When the
        unread-only filter is active, the selected contact is *pinned*: it stays
        visible even after being marked read (unread=0) so it doesn't vanish from
        the list while its chat is open.  The pin only applies while the selected
        contact is inside the current protocol filter's scope (a selected contact
        of another protocol never appears when a single protocol is filtered).
        """
        visible = {c.cache_key for c in self._filtered_contacts()}
        if (
            self._unread_only
            and self.selected_contact is not None
            and (
                self._protocol_filter == "all"
                or self.selected_contact.protocol == self._protocol_filter
            )
        ):
            visible.add(self.selected_contact.cache_key)
        return visible

    def _protocol_class(self, contact: ChatContact) -> str:
        """Return the CSS accent class for a contact's protocol."""
        return f"protocol-{contact.protocol}"

    def _filter_title_suffix(self) -> str:
        """Human-readable suffix describing the active filter state."""
        if self._protocol_filter == "signal":
            suffix = " - Signal"
        elif self._protocol_filter == "whatsapp":
            suffix = " - WhatsApp"
        elif self._protocol_filter == "telegram":
            suffix = " - Telegram"
        elif self._protocol_filter == "all":
            suffix = " - All"
        else:
            suffix = ""
        if self._unread_only:
            suffix += " · Unread"
        return suffix

    def _visible_rows(self) -> list[_Row]:
        """Project ``self.contacts`` into flat, sorted rows (header + members).

        Reuses the picker's grouping machine in read-only mode: contacts are
        grouped per person via ``group_by_person`` and members are emitted in a
        FIXED protocol order (Signal → WhatsApp → Telegram), never by recency.

        Group ordering is filter-aware: under a single-protocol filter each group
        is ordered by the recency of THAT protocol's member (``contact_sort_key``),
        so a contact that is recent on Telegram but stale on Signal no longer
        surfaces at the top of the Signal view.  In "all" mode (and "all+unread")
        the group is ordered by the recency of its default contact
        (``entry_default_contact`` + ``contact_sort_key``) — the global recency.
        Every visible group under a single-protocol filter has exactly one member
        of that protocol, so the fallback to ``entry_default_contact`` is only a
        safety net.

        Every contact — including single-member entries ("Mamma Vod") — gets a
        header.  The projection does NOT depend on collapse state; the set of
        ``want_ids`` stays stable while collapse/filter only toggle ``display``
        (the filter may, however, reorder groups).  Side-effect: refreshes
        ``_group_members``.
        """
        entries = group_by_person(self.contacts)

        def _sort_key(entry):
            if self._protocol_filter in ("signal", "whatsapp", "telegram"):
                for m in entry.members.values():
                    if m.protocol == self._protocol_filter:
                        return contact_sort_key(m)
            return contact_sort_key(entry_default_contact(entry))

        entries.sort(key=_sort_key)
        self._group_members = {}
        rows: list[_Row] = []
        for entry in entries:
            members = sorted(
                entry.members.values(),
                key=lambda c: _protocol_priority(c.protocol),
            )
            self._group_members[entry.key] = [m.cache_key for m in members]
            rows.append(_Row("group", f"person:{entry.key}", entry.key, entry=entry))
            for member in members:
                rows.append(_Row("member", member.cache_key, entry.key, contact=member))
        return rows

    def _group_label(self, entry: PickerEntry) -> str:
        """Build the neutral group-header label (no typing icons).

        The chevron ▸/▾ conveys the collapse state (▸ collapsed, ▾ expanded)
        in "all" mode; in single-protocol filter mode the header is the only
        row shown (collapse is meaningless), so a bullet ``•`` is used instead.
        In "all" mode the aggregate unread badge is suppressed entirely when
        the selected contact is one of the group's members (decision 5).  In
        filter mode the badge is the unread of the filtered protocol's member
        ONLY (bare `` *N``, no emoji): the per-backend breakdown is gone — the
        per-backend status bar is now the informative mechanism.  After reading
        that member its unread is 0, so the badge disappears naturally (no
        selected-member exclusion).
        """
        if self._protocol_filter in ("signal", "whatsapp", "telegram"):
            # Filter mode masks members: the header is the only row shown, so
            # the collapse chevron is meaningless and a bullet is used instead.
            chevron = "•"
        else:
            chevron = "▸" if entry.key not in self._expanded_groups else "▾"
        label = f"{chevron} {entry.display_name}" if chevron else entry.display_name

        if self._protocol_filter in ("signal", "whatsapp", "telegram"):
            # Filter mode: only the unread of the filtered protocol's member.
            unread = sum(
                self._unread_counts.get(m.cache_key, 0)
                for m in entry.members.values()
                if m.protocol == self._protocol_filter
            )
            if unread:
                label += f" *{unread}"
            return label

        # Decision 5 (only in "all" mode): when the selected contact belongs to
        # this group, no aggregate unread badge at all.
        if self.selected_contact is not None and self.selected_contact.cache_key in {
            m.cache_key for m in entry.members.values()
        }:
            return label

        # "all": aggregate sum, no emoji (unchanged).
        unread = sum(
            self._unread_counts.get(m.cache_key, 0) for m in entry.members.values()
        )
        if unread:
            label += f" *{unread}"
        return label

    def _row_visible(self, row: _Row, visible_keys: set[str]) -> bool:
        """Whether *row* should be displayed given *visible_keys* (filtered set).

        Members are shown only when they pass the filter AND their group is
        expanded (default collapsed).  Group headers are shown when at least one
        member passes the filter (decision 7).  A member with no ``group_key``
        (legacy/test rows) behaves like the old member-only list.
        """
        if row.kind == "member":
            if self._protocol_filter in ("signal", "whatsapp", "telegram"):
                # Filter mode (single protocol) masks every member row: only
                # group headers are shown, never members nor chevrons.
                return False
            if row.key not in visible_keys:
                return False
            return row.group_key is None or row.group_key in self._expanded_groups
        members = self._group_members.get(row.group_key)
        if members is None and row.entry is not None:
            members = [m.cache_key for m in row.entry.members.values()]
        if not members:
            return False
        return any(key in visible_keys for key in members)

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
        visible = self._visible_keys()

        first_visible: int | None = None
        selected_index: int | None = None
        for i, child in enumerate(contact_list.children):
            kind: str = getattr(child, "_row_kind", "member")
            key: str | None = getattr(child, "_contact_id", None)
            group_key: str | None = getattr(child, "_group_key", None)
            row = _Row(kind=kind, key=key, group_key=group_key)
            show = self._row_visible(row, visible)
            child.display = show
            if show:
                if first_visible is None:
                    first_visible = i
                if (
                    self.selected_contact is not None
                    and kind == "member"
                    and key == self.selected_contact.cache_key
                ):
                    selected_index = i

        # Fallback highlight: when the selected contact's member row is not
        # visible (collapsed group or filtered member), highlight its group
        # header instead (no auto-expansion).
        header_index: int | None = None
        if selected_index is None and self.selected_contact is not None:
            group_key = self._member_to_group.get(self.selected_contact.cache_key)
            if group_key is not None:
                header = self._group_widgets.get(group_key)
                if header is not None and getattr(header, "display", True):
                    try:
                        header_index = contact_list.children.index(header)
                    except ValueError:
                        header_index = None

        if selected_index is not None:
            # Keep the highlight on the still-visible selected contact.
            contact_list.index = selected_index
        elif header_index is not None:
            # Collapsed group / filtered member: highlight the group header.
            contact_list.index = header_index
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

    def _build_row_item(self, row: _Row) -> ListItem:
        """Build a fresh ListItem for *row* (shared by superset + chunk paths)."""
        if row.kind == "member":
            text = self._member_label(row.contact)
            item = ListItem(Label(text))
            item.add_class(self._protocol_class(row.contact))
            item.add_class("contact-member")
        else:
            text = self._group_label(row.entry)
            item = ListItem(Label(text))
            item.add_class("contact-group")
            item._entry = row.entry
        item._contact_id = row.key
        item._label_text = text
        item._row_kind = row.kind
        item._group_key = row.group_key
        return item

    def _register_row_widget(self, row: _Row, item: ListItem) -> None:
        """Record *item* in the O(1) lookup maps for later label/highlight syncs."""
        if row.kind == "member":
            self._contact_widgets[row.key] = item
            self._member_to_group[row.key] = row.group_key
        else:
            self._group_widgets[row.group_key] = item

    def _start_progressive_render(self, rows: list[_Row]) -> None:
        """Begin a progressive (chunked) contact-list rebuild.

        Cancels any in-progress render, clears the ListView, and schedules
        ``_render_next_chunk`` to append rows 50-at-a-time.
        """
        if self._render_timer is not None:
            self._render_timer.stop()
            self._render_timer = None

        self._pending_rows = list(rows)
        self._render_chunk_index = 0

        contact_list = self.query_one("#contact-list", ListView)
        contact_list.clear()
        self._contact_widgets.clear()
        self._group_widgets.clear()
        self._member_to_group.clear()
        self._render_next_chunk()

    def _render_next_chunk(self) -> None:
        """Render the next *chunk_size* rows (progressive startup).

        Called initially from ``_on_backend_ready`` and then
        self-schedules via ``set_timer`` until all pending rows are
        rendered.  Each chunk yields control back to the Textual event
        loop so the UI never freezes.
        """
        contact_list = self.query_one("#contact-list", ListView)
        visible = self._visible_keys()
        start = self._render_chunk_index
        end = min(start + self._render_chunk_size, len(self._pending_rows))
        chunk = self._pending_rows[start:end]

        for row in chunk:
            item = self._build_row_item(row)
            item.display = self._row_visible(row, visible)
            contact_list.append(item)
            self._register_row_widget(row, item)

        self._render_chunk_index = end
        if end < len(self._pending_rows):
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
        rows = self._visible_rows()
        want_ids = [row.key for row in rows]

        # Stop any in-flight progressive render so stale chunks
        # don't corrupt this render.
        if self._render_timer is not None:
            self._render_timer.stop()
            self._render_timer = None

        def _sync_row(item, row):
            """Aggiorna testo/classi di un ListItem esistente per la riga row."""
            if row.kind == "member":
                new_text = self._member_label(row.contact)
            else:
                new_text = self._group_label(row.entry)
            if getattr(item, "_label_text", None) != new_text:
                item._label_text = new_text
                label = item.children[0] if item.children else None
                if label is not None:
                    label.update(new_text)
            if row.kind == "member":
                if not item.has_class(self._protocol_class(row.contact)):
                    for cl in (
                        "protocol-signal",
                        "protocol-whatsapp",
                        "protocol-telegram",
                    ):
                        item.remove_class(cl)
                    item.add_class(self._protocol_class(row.contact))
                item.add_class("contact-member")
            else:
                for cl in ("protocol-signal", "protocol-whatsapp", "protocol-telegram"):
                    item.remove_class(cl)
                item.add_class("contact-group")
                item._entry = row.entry
            item._row_kind = row.kind
            item._group_key = row.group_key

        if cur_ids == want_ids:
            # Fast path: composizione+ordine invariati -> aggiorna in-place.
            for item, row in zip(existing, rows):
                _sync_row(item, row)
        elif set(cur_ids) == set(want_ids):
            # Stesso INSIEME di righe ma ordine diverso (un nuovo messaggio
            # ha spostato un gruppo in cima): riordina i ListItem ESISTENTI
            # in-place.  Niente clear(), niente costruzione di nuovi widget ->
            # la lista non diventa mai vuota e il main non si blocca.
            by_id = {getattr(it, "_contact_id", None): it for it in existing}
            reordered = [by_id[cid] for cid in want_ids if cid in by_id]
            for item, row in zip(reordered, rows):
                _sync_row(item, row)
            if existing != reordered:
                # Move_child sposta i nodi esistenti nel DOM senza smontarli,
                # un elemento alla volta costruendo l'ordine voluto (idempotente:
                # un elemento già nella posizione giusta è un no-op effettivo).
                # La lista non risulta mai vuota in mezzo.
                for i in range(1, len(reordered)):
                    contact_list.move_child(reordered[i], after=reordered[i - 1])
        elif cur_ids and set(cur_ids) < set(want_ids):
            # Superset: nuove righe (es. WhatsApp dopo Signal).
            # Crea SOLO i ListItem mancanti, preserva quelli esistenti,
            # poi riordina tutto con move_child — nessun clear, nessun flash.
            by_id = {getattr(it, "_contact_id", None): it for it in existing}
            for row in rows:
                if row.key not in by_id:
                    item = self._build_row_item(row)
                    self._register_row_widget(row, item)
                    by_id[row.key] = item
                    contact_list.append(item)
                else:
                    _sync_row(by_id[row.key], row)
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
            self._start_progressive_render(rows)

        # Re-apply the active protocol filter so newly added items get the
        # right ``display`` and the highlight lands on the correct row.
        self._apply_contact_visibility()

    def _refresh_header_labels(self) -> None:
        """Recompute every group-header label in place.

        The header label now depends on the active protocol filter (the chevron
        is replaced by a bullet ``•`` when a single protocol is filtered), so a
        Ctrl+W cycle must refresh the labels without a full rebuild.  The
        ``PickerEntry`` of each header is stored on the row at construction time
        (``item._entry``).
        """
        for item in self._group_widgets.values():
            entry = getattr(item, "_entry", None)
            if entry is None:
                continue
            new_text = self._group_label(entry)
            if getattr(item, "_label_text", None) != new_text:
                item._label_text = new_text
                label = item.children[0] if item.children else None
                if label is not None:
                    label.update(new_text)

    def _apply_contact_filter(self) -> None:
        """Re-apply the active protocol filter to the contact list view.

        Re-renders via ``_render_contact_list``: the group order now depends
        on the active filter (per-protocol recency under a single-protocol
        filter), so a filter change must reorder the DOM, not just toggle
        ``display``.  Because the contact SET is unchanged, the render hits
        the in-place reorder fast path (``move_child``, no clear/rebuild) and
        ends with ``_apply_contact_visibility`` — no double visibility pass.
        The DOM keeps all contacts, hidden ones are ``display=False``.  Only
        the CSS border / banner classes are synced here on top of that.
        """
        self._render_contact_list(list(self.contacts))
        # Header labels depend on the filter (chevron shown only in "all").
        self._refresh_header_labels()
        try:
            section_lbl = self.query_one("#ContactsTitle", Label)
        except Exception as _e:
            logger.debug("Contacts title not found", exc_info=True)
            section_lbl = None
        if section_lbl is not None:
            if self._unread_only:
                # No "Contacts" word and no " - " prefix: the long "· Unread"
                # suffix would wrap and the " - " prefix would double the space.
                suffix = self._filter_title_suffix().removeprefix(" - ")
                section_lbl.update(f"📇 {suffix}")
            else:
                section_lbl.update(f"📇 Contacts{self._filter_title_suffix()}")

        # Sync the filter accent across the chat border, the contact list border
        # and the two section banners (📇 Contacts / 💬 Chat).
        cls_signal = "chat-filter-signal"
        cls_whats = "chat-filter-whatsapp"
        cls_telegram = "chat-filter-telegram"
        cls_unread = "chat-filter-unread"
        widgets = [self.chat_log]
        for selector in ("#contact-list", "#ContactsTitle", "#ChatTitle"):
            try:
                widgets.append(self.query_one(selector))
            except Exception as _e:
                logger.debug("Filter widget not found: %s", selector, exc_info=True)
        for node in widgets:
            node.remove_class(cls_signal, cls_whats, cls_telegram, cls_unread)
            if self._protocol_filter == "signal":
                node.add_class(cls_signal)
            elif self._protocol_filter == "whatsapp":
                node.add_class(cls_whats)
            elif self._protocol_filter == "telegram":
                node.add_class(cls_telegram)
                # filtro "all": nessuna classe protocollo -> default (giallo).
            if self._unread_only and self._protocol_filter == "all":
                node.add_class(cls_unread)

        # After the filter changed, open the first visible contact of the new
        # view (the previous selection would otherwise be a "ghost chat" no
        # longer relevant to the filter).  Done HERE (not in
        # ``_apply_contact_visibility``): ``_select_contact`` with the
        # unread-only filter re-enters ``_apply_contact_visibility``, which
        # would recurse if the hook lived there.
        self._select_first_visible_contact()

    def _contact_for_first_visible(self) -> ChatContact | None:
        """Resolve the contact of the first visible row in the contact list.

        Iterates the ListView children in DOM order and returns the contact of
        the first row with ``display=True``.  Member rows resolve by their
        ``_contact_id`` (a ``cache_key``); group-header rows resolve via the
        filtered member (single-protocol filter) or the entry's default
        contact ("all").  Returns ``None`` when no visible row resolves.
        """
        contact_list = self.query_one("#contact-list", ListView)
        for child in contact_list.children:
            if not getattr(child, "display", True):
                continue
            if getattr(child, "_row_kind", "member") == "member":
                cache_key = getattr(child, "_contact_id", None)
                if cache_key is not None:
                    for contact in self.contacts:
                        if contact.cache_key == cache_key:
                            return contact
                continue
            group_key = getattr(child, "_group_key", None)
            if self._protocol_filter != "all":
                member = self._group_member_for_filter(group_key)
                if member is not None:
                    return member
            else:
                entry = getattr(child, "_entry", None)
                if entry is not None:
                    return entry_default_contact(entry)
        return None

    def _select_first_visible_contact(self) -> None:
        """Select the first visible contact after a filter change.

        No-op when the list is empty (``_apply_contact_visibility`` has already
        set ``selected_contact=None``) or when the first visible contact is
        already the selected one (compared by ``cache_key`` ONLY — not the
        dataclass ``__eq__``, which also compares ``extras``).
        """
        contact = self._contact_for_first_visible()
        if contact is None:
            return
        selected_key = (
            self.selected_contact.cache_key if self.selected_contact else None
        )
        if contact.cache_key == selected_key:
            return
        self._select_contact(contact)

    def action_cycle_protocol_filter(self):
        """Ctrl+W: cycle the contact list filter ALL -> SIGNAL -> WHATSAPP -> TELEGRAM."""
        order = ["all", "signal", "whatsapp", "telegram"]
        idx = (
            order.index(self._protocol_filter) if self._protocol_filter in order else 0
        )
        self._protocol_filter = order[(idx + 1) % len(order)]
        self._apply_contact_filter()
        self._sync_status_segments()
        # NB: dopo il cambio filtro viene deliberatamente selezionata la prima
        # chat della nuova vista (``_select_first_visible_contact`` in coda a
        # ``_apply_contact_filter``).  Side-effect voluto: la chat appena aperta
        # viene marcata come letta (mark-read).  Nessun'altra scrittura nella
        # cronologia della conversazione in corso.

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
        self._cancel_edit()
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
        # With the unread-only filter active the just-read contact now has zero
        # unread and must vanish from the list immediately (not at the next
        # flush).  Re-apply visibility before the highlight pass so the
        # row → header → first-visible fallback happens now.  The chat stays
        # open: ``selected_contact`` is preserved across the pass (the pass may
        # deselect when no row remains visible under the filter).
        if self._unread_only:
            selected = self.selected_contact
            self._apply_contact_visibility()
            self.selected_contact = selected
        self._refresh_backend_status_if_idle()

        # Highlight the contact in the left list and remove the *N badge.
        # The ListView keeps ALL contacts in the DOM after the Ctrl+W filter
        # (hidden ones have ``display=False``), so we must locate the row via
        # its ListItem (``_contact_widgets``) and index it by its real position
        # in ``contact_list.children`` — NOT by its position in the filtered
        # list (that only matches when the filter is "all").
        contact_list = self.query_one("#contact-list", ListView)
        item = self._contact_widgets.get(self.selected_contact.cache_key)
        highlight_index: int | None = None
        if item is not None:
            # Refresh the row label (removes the *N unread badge for this contact).
            label = item.children[0] if item.children else None
            if label is not None:
                label.update(self._member_label(self.selected_contact))
            # Only highlight when the row is currently visible (a contact picked
            # from the picker may be filtered out of the active list, or its
            # group may be collapsed).
            if getattr(item, "display", True):
                try:
                    highlight_index = contact_list.children.index(item)
                except ValueError:
                    # Item not currently mounted (e.g. mid progressive render).
                    highlight_index = None

        # Fallback: when the member row is hidden (collapsed group / filtered
        # member), highlight the group header instead — never auto-expand.
        if highlight_index is None:
            group_key = self._member_to_group.get(self.selected_contact.cache_key)
            if group_key is not None:
                header = self._group_widgets.get(group_key)
                if header is not None and getattr(header, "display", True):
                    try:
                        highlight_index = contact_list.children.index(header)
                    except ValueError:
                        highlight_index = None

        if highlight_index is not None:
            contact_list.index = highlight_index

        # Refresh the group header label too: the aggregate unread badge is
        # now suppressed for the selected contact (decision 5).
        group_key = self._member_to_group.get(self.selected_contact.cache_key)
        if group_key is not None:
            header = self._group_widgets.get(group_key)
            entry = getattr(header, "_entry", None) if header is not None else None
            if header is not None and entry is not None:
                new_text = self._group_label(entry)
                if getattr(header, "_label_text", None) != new_text:
                    header._label_text = new_text
                    label = header.children[0] if header.children else None
                    if label is not None:
                        label.update(new_text)

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

    def _toggle_group(self, group_key: str | None) -> None:
        """Toggle the collapse state of *group_key* (display-only).

        Expands/collapses the group by flipping its membership in
        ``_expanded_groups``, updates the header chevron, and re-applies the
        visibility rules.  It does NOT re-render the list, does NOT move the
        focus, and does NOT open any chat.
        """
        if group_key is None:
            return
        if group_key in self._expanded_groups:
            self._expanded_groups.discard(group_key)
        else:
            self._expanded_groups.add(group_key)

        header = self._group_widgets.get(group_key)
        entry = getattr(header, "_entry", None) if header is not None else None
        if header is not None and entry is not None:
            new_text = self._group_label(entry)
            if getattr(header, "_label_text", None) != new_text:
                header._label_text = new_text
                label = header.children[0] if header.children else None
                if label is not None:
                    label.update(new_text)

        # SOLO display: nessun render, nessun clear, nessun cambio di focus.
        # Preserva la posizione evidenziata: togglare un header non deve far
        # "saltare" l'highlight in cima alla lista.  Ma NON sovrascrivere
        # l'highlight se ``_apply_contact_visibility`` ha appena evidenziato la
        # riga MEMBRO del contatto selezionato (gruppo appena espanso): il
        # ripristino dell'header vale solo quando il membro resta nascosto
        # (gruppo collassato) o quando il toggle riguarda un gruppo diverso.
        contact_list = self.query_one("#contact-list", ListView)
        previous = contact_list.index
        self._apply_contact_visibility()
        sel_item = (
            self._contact_widgets.get(self.selected_contact.cache_key)
            if self.selected_contact is not None
            else None
        )
        member_visible = sel_item is not None and getattr(sel_item, "display", True)
        if (
            not member_visible
            and previous is not None
            and 0 <= previous < len(contact_list.children)
            and getattr(contact_list.children[previous], "display", True)
        ):
            contact_list.index = previous
            self._sync_contact_highlight(contact_list, previous)

    def _group_member_for_filter(self, group_key: str | None) -> ChatContact | None:
        """Resolve the member of *group_key* matching the active protocol filter.

        Only meaningful when the filter is a single protocol ("all" returns
        ``None``).  ``group_by_person`` keeps at most one member per protocol,
        so the first (and only) ``self.contacts`` entry whose protocol matches
        the filter and whose ``cache_key`` belongs to the group is returned.
        """
        if group_key is None or self._protocol_filter == "all":
            return None
        member_keys = self._group_members.get(group_key)
        if not member_keys:
            return None
        for contact in self.contacts:
            if (
                contact.protocol == self._protocol_filter
                and contact.cache_key in member_keys
            ):
                return contact
        return None

    def on_list_view_selected(self, event: ListView.Selected):
        """When a contact is selected, show the chat."""
        # Header rows (group kind) are NOT contacts: they only toggle the
        # collapse state (decision 3).  Members follow the existing flow.
        item = event.item
        if getattr(item, "_row_kind", "member") == "group":
            group_key = getattr(item, "_group_key", None)
            if self._protocol_filter in ("signal", "whatsapp", "telegram"):
                # Filter mode: the header row opens the chat of the group's
                # member for that protocol directly (no toggle, no expansion).
                member = self._group_member_for_filter(group_key)
                if member is None:
                    # Defensive no-op: no member resolved (should not happen).
                    # Never touch _expanded_groups while a single protocol is
                    # filtered.
                    return
                if member != self.selected_contact:
                    self._select_contact(member)
                return
            self._toggle_group(group_key)
            return
        # Resolve the contact directly from the clicked ListItem's _contact_id.
        # Using ListView.index + _filtered_contacts() fails when hidden
        # children (display=False) are in the ListView (Ctrl+W filter).
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
