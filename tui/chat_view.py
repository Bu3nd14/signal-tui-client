"""Chat rendering: message widgets, chat window, history loading."""

import logging
import re
import time
from pathlib import Path

from textual.containers import Vertical
from textual.widgets import Button, Static

from backends import (
    WhatsAppBackend,
)
from models import (
    PROTOCOL_SIGNAL,
    PROTOCOL_TELEGRAM,
)
from ui_components import (
    ImageWidget,
    MessageWidget,
)

logger = logging.getLogger(__name__)

# Delivery status rank: used to decide whether a merge may overwrite the status
# of an already-present entry (never downgrade read → sent).
_STATUS_RANK = {"pending": 0, "failed": 0, "sent": 1, "delivered": 2, "read": 3}


def _status_rank(status: str | None) -> int:
    """Return the numeric rank of a delivery status (default 0)."""
    return _STATUS_RANK.get(status, 0)


def _media_display_text(text: str, attachment_info: str | None, msg_type: str) -> str:
    """Return the user-facing label for non-image media."""
    if msg_type == "sticker":
        return f"🎨 {attachment_info or '[Sticker]'}"
    if msg_type == "attachment":
        return f"📎 {attachment_info or '[File]'}"
    return text


_TECHNICAL_LABELS = frozenset({"🖼️ Image", "🖼️ Photo", "Media", "Image", "Photo"})
_TECHNICAL_PREFIXES = ("Image: ", "Video: ", "Audio: ")
_MIME_RE = re.compile(r"^[\w.-]+/[\w.+-]+$")
_MEDIA_KEY_RE = re.compile(r"^(image|video|audio|document|sticker)Message( \(.+\))?$")
_MEDIA_EXT_RE = re.compile(
    r"\.(jpe?g|png|gif|webp|bmp|tiff?|heic|heif|mp4|mov|mkv|webm|avi|mp3|ogg|opus|aac|m4a|wav|pdf)$",
    re.IGNORECASE,
)


def _is_technical_media_label(label: str) -> bool:
    """True se `label` è un'etichetta tecnica (filename/mime/fallback), non una caption."""
    s = (label or "").strip()
    if not s:
        return True
    if s in _TECHNICAL_LABELS:
        return True
    if s.startswith(_TECHNICAL_PREFIXES):
        return True
    if _MIME_RE.match(s):
        return True
    if _MEDIA_KEY_RE.match(s):
        return True
    if s.startswith(("http://", "https://")):
        return True
    return bool(" " not in s and _MEDIA_EXT_RE.search(s))


def _is_synthetic_media_text(
    text: str, attachment_info: str | None, attachment_id: str | None
) -> bool:
    """True se `text` è un'identità sintetica generata dal backend, non una caption."""
    t = (text or "").strip()
    if not t:
        return True
    if t.startswith("Media: "):
        return True
    info = (attachment_info or "").strip()
    att = str(attachment_id or "").strip()
    if info and t == info:
        return True
    return bool(info and att and t == f"{info}: {att}")


def _image_caption(
    text: str,
    attachment_info: str | None,
    attachment_id: str | None,
    protocol: str | None,
) -> str | None:
    """Caption reale di una foto, o None. Regole per protocollo (deterministiche):

    - Telegram: la caption è ``text`` (``attachment_info`` è un'etichetta statica).
    - Signal: la caption è il body ``dataMessage.message`` (in ``text`` sul primo
      attachment); se assente/sintetico, la caption per-attachment in ``attachment_info``.
    - WhatsApp: la caption è in ``attachment_info`` (il ``text`` è sempre sintetico).

    Caso limite noto e accettato: una caption utente identica a un bare
    filename (``"photo.jpg"``) viene classificata tecnica e non mostrata come
    bolla (resta nel placeholder).
    """
    t = (text or "").strip()
    info = (attachment_info or "").strip()
    if protocol == PROTOCOL_TELEGRAM:
        return t or None
    if (
        protocol == PROTOCOL_SIGNAL
        and t
        and not _is_synthetic_media_text(text, attachment_info, attachment_id)
    ):
        return t
    if info and not _is_technical_media_label(info):
        return info
    return None


class ChatViewMixin:
    @property
    def chat_log(self) -> Vertical:
        """The ``#chat-log`` widget, lazily cached on first access."""
        if self._chat_log is None:
            self._chat_log = self.query_one("#chat-log", Vertical)
        return self._chat_log

    def _add_message(
        self,
        text: str,
        is_mine: bool = False,
        is_info: bool = False,
        quote_text: str | None = None,
        msg_type: str = "text",
        attachment_info: str | None = None,
        attachment_id: str | None = None,
        timestamp: int = 0,
        sender: str = "",
        status: str = "sent",
        protocol: str | None = None,
        message_id: str | None = None,
        edited: bool = False,
    ):
        """Add a message to the chat with correct alignment.

        For image messages, this method launches an async worker that
        renders the image inline via ``catimg``.  If rendering fails, a
        clickable fallback placeholder is shown instead.

        For text messages (not info), a clickable ``MessageWidget`` is
        used so the user can click to reply.

        Parameters
        ----------
        status:
            Delivery status for sent messages: "sent", "delivered", or "read".
            Only meaningful when ``is_mine=True``.
        protocol:
            Source protocol for the message widget.  Defaults to the selected
            contact's protocol when available.
        """
        if text is None:
            text = ""

        if protocol is None and self.selected_contact is not None:
            protocol = self.selected_contact.protocol

        # Render-level de-dup: never mount the same real message twice in the
        # current view, regardless of _seen_timestamps state.  Info messages
        # (headers, separators, hints) are not part of the message feed and so
        # are not de-duplicated here.
        # L'identità include il TESTO: il timestamp da solo (granularità al
        # secondo) non basta — due messaggi WhatsApp distinti nello stesso
        # secondo verrebbero scartati (il secondo non compariva mai).
        if not is_info:
            if (
                self.selected_contact is not None
                and timestamp
                and (protocol, self.selected_contact.cache_key, timestamp, text)
                in self._shown_in_log
            ):
                return
            if self.selected_contact is not None and timestamp:
                self._shown_in_log.add(
                    (protocol, self.selected_contact.cache_key, timestamp, text)
                )

        chat_log = self.chat_log

        if quote_text:
            quote_class = "msg-quote-right" if is_mine else "msg-quote"
            quote_widget = Static(f"▎ {quote_text}", classes=quote_class)
            chat_log.mount(quote_widget)

        # ── Image messages: render inline via async worker ──────────────
        if msg_type == "image":
            caption = _image_caption(text, attachment_info, attachment_id, protocol)
            info_for_placeholder = attachment_info or text
            if caption and (info_for_placeholder or "").strip() == caption:
                info_for_placeholder = None
            self._render_image_in_chat(
                attachment_id=attachment_id,
                attachment_info=info_for_placeholder or "Photo",
                is_mine=is_mine,
                chat_log=chat_log,
                protocol=protocol,
            )
            if caption:
                is_group = bool(
                    self.selected_contact and self.selected_contact.id.endswith("@g.us")
                )
                caption_widget = self._make_message_widget(
                    text=caption,
                    is_mine=is_mine,
                    timestamp=timestamp,
                    sender=sender,
                    status=status,
                    protocol=protocol or "",
                    is_group=is_group,
                    message_id=message_id,
                )
                chat_log.mount(caption_widget)
                chat_log.scroll_end(animate=False)
            return

        # ── Non-image messages ──────────────────────────────────────────
        display_text = _media_display_text(text, attachment_info, msg_type)

        if is_info:
            if self.selected_contact is not None:
                return  # suppress system messages when a chat is open
            widget = Static(display_text, classes="msg-info")
            self._chat_log.mount(widget)
            self.set_timer(3.0, widget.remove)
            return
        else:
            # Use clickable MessageWidget for all non-info messages.
            # Per i messaggi di gruppo WhatsApp (chat @g.us) non miei,
            # prependiamo "<nome_contatto:>" con il nome in #DAA520 (goldenrod).
            sender_color = None
            if (
                not is_mine
                and sender
                and self.selected_contact is not None
                and self.selected_contact.id.endswith("@g.us")
            ):
                sender_color = "#DAA520"

            widget = MessageWidget(
                text=display_text,
                timestamp=timestamp,
                sender=sender,
                is_mine=is_mine,
                classes="msg-right" if is_mine else "msg-left",
                status=status,
                protocol=protocol or "",
                sender_color=sender_color,
                message_id=message_id,
                edited=edited,
            )

        chat_log.mount(widget)
        chat_log.scroll_end(animate=False)

    @staticmethod
    def _make_message_widget(
        text: str,
        is_mine: bool = False,
        is_info: bool = False,
        timestamp: int = 0,
        sender: str = "",
        status: str = "sent",
        protocol: str = "",
        is_group: bool = False,
        message_id: str | None = None,
        edited: bool = False,
    ) -> Static:
        """Build a message widget without mounting it.

        Returns a ``Static`` (info messages) or ``MessageWidget`` ready to be
        mounted.  Does NOT touch the DOM — callers are responsible for
        ``mount()`` and ``scroll_end()``.
        """
        display_text = text
        if is_info:
            return Static(display_text, classes="msg-info")

        sender_color = None
        if not is_mine and sender and is_group:
            sender_color = "#DAA520"

        return MessageWidget(
            text=display_text,
            timestamp=timestamp,
            sender=sender,
            is_mine=is_mine,
            classes="msg-right" if is_mine else "msg-left",
            status=status,
            protocol=protocol,
            sender_color=sender_color,
            message_id=message_id,
            edited=edited,
        )

    def _render_image_in_chat(
        self,
        attachment_id: str | None,
        attachment_info: str,
        is_mine: bool,
        chat_log: Vertical,
        protocol: str | None = None,
    ):
        """Mount a clickable ``ImageWidget`` placeholder immediately and
        resolve the attachment path in a worker thread.

        Uses the ``BackendManager`` to route the attachment-id resolution to
        the correct protocol backend (Signal, WhatsApp, ...).  Falls back to
        the legacy Signal backend when *protocol* is ``None`` (safety net
        for callers compiled before the multi-protocol routing was added).

        The actual image rendering happens on-demand when the user presses
        Enter or clicks the widget, which opens a fullscreen modal.
        """
        resolved_protocol = protocol or PROTOCOL_SIGNAL

        if not attachment_id:
            widget = ImageWidget(
                attachment_path=None,
                attachment_id="",
                fallback_text=f"[🖼️ Image: {attachment_info}]",
            )
            widget.classes = "msg-right" if is_mine else "msg-left"
            chat_log.mount(widget)
            chat_log.scroll_end(animate=False)
            return

        widget = ImageWidget(
            attachment_path=None,
            attachment_id=attachment_id,
            fallback_text=f"[🖼️ Image: {attachment_info} — loading…]",
        )
        widget.classes = "msg-right" if is_mine else "msg-left"
        chat_log.mount(widget)
        chat_log.scroll_end(animate=False)

        self.run_worker(
            lambda: self._resolve_attachment_worker(
                resolved_protocol, attachment_id, widget, attachment_info
            ),
            thread=True,
            exclusive=False,
        )

    def _resolve_attachment_worker(
        self,
        protocol: str,
        attachment_id: str,
        widget: ImageWidget,
        attachment_info: str,
    ):
        """Worker thread: resolve the attachment path without blocking the UI."""
        path = self.manager.get_attachment_path(protocol, attachment_id)
        self.call_from_thread(
            self._finish_attachment_resolve, widget, path, attachment_info
        )

    def _finish_attachment_resolve(
        self, widget: ImageWidget, path: Path | None, attachment_info: str
    ):
        """UI thread: update the placeholder with the resolved attachment path."""
        if not widget.is_mounted:
            return
        if path is None:
            widget.update_attachment(None, f"[🖼️ Image: {attachment_info}]")
        else:
            widget.update_attachment(
                path, f"[🖼️ Image: {path.name} — Click Enter to View]"
            )

    def _clear_chat(self):
        """Clear the chat and reset the render-level de-dup set."""
        chat_log = self.chat_log
        chat_log.remove_children()
        self._shown_in_log.clear()
        self._seen_message_ids.clear()

    def _load_messages_worker(self):
        """Load messages: last 20 from cache, then reconcile remote history.

        Phase 1 paints the newest cached messages immediately (no network
        wait).  For WhatsApp, phase 2 downloads the remote history in this
        same worker thread and merges ONLY the messages not already on
        screen (diff + append), so the chronological order stays correct and
        there is no clear+remount flash.

        Runs in a worker thread; before each render it verifies the reload
        token is still current (no newer contact selection happened) so a
        stale worker stops mounting messages after a more recent
        ``_clear_chat`` — otherwise re-selecting a contact can double the
        messages.
        """
        if not self.selected_contact:
            return

        contact = self.selected_contact
        reload_token = self._chat_reload_token
        self._loaded_all = False

        def _is_stale() -> bool:
            """True if a newer selection happened or the contact changed."""
            return (
                self._chat_reload_token != reload_token
                or self.selected_contact != contact
            )

        is_whatsapp = contact.protocol == WhatsAppBackend.protocol

        # Phase 1: paint instantly from the local cache (no network wait).
        rendered_any = self._render_chat_window(
            contact, _is_stale, pending_fetch=is_whatsapp
        )

        if not is_whatsapp or _is_stale():
            return

        # Phase 2 (WhatsApp): WAHA CORE does not push history over a stream, so
        # the local cache only fills from live polling and TUI sends.  To always
        # show the most recent messages — including those sent from ANOTHER
        # client — download the remote history on every open.  `fetch_history`
        # dedups internally via `ingest_message`, so already-known messages are
        # not duplicated.  Fails non-destructively.
        backend = self.manager.get(contact.protocol)
        fetch = getattr(backend, "fetch_history", None)
        cache_changed = False
        if fetch is not None:
            try:
                # Wider window: `fetch_history` reconciles remote history with
                # the DB (adds missing messages, upgrades entries without id).
                # With limit=20 a DB missing messages could never be repaired
                # because the 20 newest were already present.  Retry: WAHA can
                # return [] right after boot even with a WORKING session, so
                # retry a couple of times with a short pause.  Respects
                # _is_stale() to avoid mounting into a stale selection.
                _retry = 0
                while True:
                    fetch(contact.id, limit=50)
                    _bm = getattr(backend, "cache", {}).get(contact.id, [])
                    if _bm or _is_stale() or _retry >= 2:
                        break
                    _retry += 1
                    time.sleep(0.8)

                # fetch_history fills the BACKEND cache; merge only the
                # genuinely new messages into the protocol-aware UI cache
                # (never replace: the UI cache must not shrink).
                cache_changed = self._merge_backend_cache(contact, backend)
                # I'm viewing them now: freshly downloaded messages must not
                # inflate unread badges (already zeroed by _select_contact).
                for msg in self._cache.get(contact.cache_key, []):
                    if not msg.get("is_mine"):
                        msg["read"] = True
            except Exception as _e:  # fallback: stay on the local cache
                logger.debug(
                    "History fetch failed, staying on local cache", exc_info=True
                )

        if _is_stale():
            return

        # Phase 2 render: only when the fetch actually added new messages or
        # nothing was on screen yet (empty cache at phase 1).  A single full
        # render recomputes the window AND the "load more" banner from the final
        # cache, so both order and banner stay correct by construction.
        if cache_changed or not rendered_any:
            self._render_chat_window(contact, _is_stale, pending_fetch=False)

    def _render_chat_window(
        self, contact, is_stale, pending_fetch: bool = False
    ) -> bool:
        """Sort the contact's cache, take the newest window and mount it atomically.

        Returns ``True`` if at least one message was rendered, ``False`` if the
        cache was empty (a status is shown instead).  When ``pending_fetch`` is
        true, an empty cache shows a transient "loading" status instead of
        "no message history" (a remote fetch is about to fill it).
        """
        cached = self._cache.get(contact.cache_key, [])
        total = len(cached)
        # Sort the chat cache by timestamp (stable) BEFORE slicing the `[-N:]`
        # render window.  The cache can be populated out of order (append from
        # multiple sources) or reordered by the echo upgrade; without the sort,
        # `[-20:]` would not really pick the newest messages.
        cached = sorted(cached, key=lambda m: int(m.get("timestamp") or 0))

        if not cached:
            if pending_fetch:
                try:
                    self.call_from_thread(self._status, "⏳ Loading message history…")
                except Exception as _e:
                    logger.debug("Failed to show loading status", exc_info=True)
                return False
            self._loaded_all = True
            try:
                self.call_from_thread(
                    self._status, "No message history for this contact"
                )
            except Exception as _e:
                logger.debug("Failed to show empty-history status", exc_info=True)
            return False

        if total > 20:
            messages_to_show = cached[-20:]
        else:
            messages_to_show = cached
            self._loaded_all = True

        batch = []
        for msg in messages_to_show:
            if is_stale():
                return False
            text = msg.get("text", "")
            ts = msg.get("timestamp", 0)
            if ts:
                self._seen_timestamps.add((contact.protocol, contact.cache_key, ts))
                self._seen_message_ids.add(
                    (contact.protocol, contact.cache_key, ts, text)
                )
                mid = msg.get("id")
                if mid:
                    self._seen_message_ids.add(
                        (contact.protocol, contact.cache_key, mid, text)
                    )
            batch.append(msg)

        def _mount_window():
            # On the UI thread: empty the log and remount the ordered window in
            # ONE mount so Textual does a single layout pass instead of 20+.
            if not is_stale():
                try:
                    self._clear_chat()
                except Exception as _e:
                    logger.debug("Failed to clear chat log", exc_info=True)
            if is_stale():
                return

            protocol = contact.protocol
            is_group = (
                self.selected_contact is not None
                and self.selected_contact.id.endswith("@g.us")
            )
            widgets: list = []
            for msg in batch:
                try:
                    text = msg.get("text", "")
                    ts = msg.get("timestamp", 0)
                    if ts:
                        self._shown_in_log.add((protocol, contact.cache_key, ts, text))
                    widgets.extend(self._build_message_widgets(protocol, is_group, msg))
                except Exception as _e:
                    logger.debug("Failed to build a message widget", exc_info=True)

            if widgets:
                chat_log = self.chat_log
                chat_log.mount(*widgets)
                chat_log.scroll_end(animate=False)

            # "load more" banner remounted AFTER the _clear_chat.
            if total > 20 and not is_stale():
                try:
                    self._add_load_more_widget(total - 20)
                except Exception as _e:
                    logger.debug("Failed to mount load-more widget", exc_info=True)

        try:
            self.call_from_thread(_mount_window)
        except Exception as _e:  # fallback: stay on the already-mirrored UI cache
            logger.debug("Failed to schedule window mount", exc_info=True)

        return True

    def _merge_backend_cache(self, contact, backend) -> bool:
        """Merge ``backend.cache`` into the UI cache for *contact*, add-only.

        ``fetch_history`` fills the BACKEND cache (keyed by raw id); this
        mirrors only the genuinely new messages into the protocol-aware UI
        cache.  It never removes existing entries, so a backend cache that is
        temporarily smaller than the UI cache can not shrink the rendered
        history (and can not drop the "load more" banner).

        Dedup mirrors ``WhatsAppBackend._message_already_cached``: for INCOMING
        messages the id differs between webhook and REST, so the identity is
        (text, timestamp +/-5s); for OUTGOING messages the id is stable (echo
        fallback by text + a wider window).

        Returns ``True`` if at least one new message was added or an existing
        message's text was updated (an edit).  A status-only upgrade does NOT
        count as a change (nothing to re-render).
        """
        backend_msgs = getattr(backend, "cache", {}).get(contact.id, [])
        if not backend_msgs:
            return False

        ui_key = contact.cache_key
        ui_msgs = self._cache.setdefault(ui_key, [])

        def _find_existing(m: dict) -> dict | None:
            is_mine = bool(m.get("is_mine", False))
            text = m.get("text", "")
            ts = int(m.get("timestamp") or 0)
            mid = m.get("id")
            for existing in ui_msgs:
                if bool(existing.get("is_mine", False)) != is_mine:
                    continue
                # Id-first for BOTH directions: an edit keeps the id but
                # changes the text, so the id match must precede the text
                # comparison (mirror of _message_already_cached).
                if mid and existing.get("id") and existing.get("id") == mid:
                    return existing
                if existing.get("text", "") != text:
                    continue
                existing_ts = int(existing.get("timestamp") or 0)
                if not is_mine:
                    # Incoming: id unreliable -> text + fuzzy timestamp (+/-5s).
                    if abs(existing_ts - ts) <= 5000:
                        return existing
                elif mid:
                    # Outgoing: text + echo window (10 min) when id absent.
                    if abs(existing_ts - ts) <= 600000:
                        return existing
                elif abs(existing_ts - ts) <= 5000:
                    return existing
            return None

        added = False
        changed = False
        for m in backend_msgs:
            existing = _find_existing(m)
            if existing is not None:
                # Already present: reconcile an edit in place (update text and
                # mark edited) WITHOUT touching timestamp/id; the caller re-
                # renders the window when `changed` is reported.
                if (
                    existing.get("msg_type", "text") == "text"
                    and existing.get("text", "") != m.get("text", "")
                ):
                    existing["text"] = m.get("text")
                    existing["edited"] = True
                    changed = True
                # Still upgrade the status if the incoming rank is higher
                # (never downgrade read → sent).
                if _status_rank(m.get("status")) > _status_rank(existing.get("status")):
                    existing["status"] = m.get("status")
                continue
            ui_msgs.append(m)
            added = True

        if added:
            ui_msgs.sort(key=lambda m: int(m.get("timestamp") or 0))
        return added or changed

    def _build_message_widgets(self, protocol: str, is_group: bool, msg: dict) -> list:
        """Build the widgets for a single cached message (quote + message).

        Does NOT mount anything: callers are responsible for ``mount()``.
        """
        text = msg.get("text", "")
        is_mine = msg.get("is_mine", False)
        quote_text = msg.get("quote_text")
        msg_type = msg.get("msg_type", "text")
        attachment_info = msg.get("attachment_info")
        attachment_id = msg.get("attachment_id")
        sender = msg.get("sender", "")
        status = msg.get("status", "sent" if is_mine else "read")
        ts = msg.get("timestamp", 0)
        message_id = msg.get("id")

        widgets: list = []
        if quote_text:
            quote_class = "msg-quote-right" if is_mine else "msg-quote"
            widgets.append(Static(f"▎ {quote_text}", classes=quote_class))

        if msg_type == "image":
            caption = _image_caption(text, attachment_info, attachment_id, protocol)
            display = attachment_info or text or "Photo"
            if caption and display.strip() == caption:
                display = "Photo"
            if not display.startswith("🖼️"):
                display = f"🖼️ {display}"
            image_widget = ImageWidget(
                attachment_path=None,
                attachment_id=attachment_id or "",
                fallback_text=f"[{display}]",
            )
            image_widget.classes = "msg-right" if is_mine else "msg-left"
            widgets.append(image_widget)
            if caption:
                widgets.append(
                    self._make_message_widget(
                        text=caption,
                        is_mine=is_mine,
                        timestamp=ts,
                        sender=sender,
                        status=status,
                        protocol=protocol,
                        is_group=is_group,
                        message_id=message_id,
                    )
                )
        else:
            display_text = _media_display_text(text, attachment_info, msg_type)
            widgets.append(
                self._make_message_widget(
                    text=display_text,
                    is_mine=is_mine,
                    timestamp=ts,
                    sender=sender,
                    status=status,
                    protocol=protocol,
                    is_group=is_group,
                    message_id=message_id,
                    edited=msg.get("edited", False),
                )
            )
        return widgets

    def _add_load_more_widget(self, remaining: int):
        """Add a clickable widget to load older messages."""
        chat_log = self.chat_log
        widget = Button(
            f"📜 ↑ {remaining} older messages — click to load",
            classes="msg-load-more",
            id="load-more-msg",
        )
        chat_log.mount(widget, before=0)

    def _load_all_messages(self):
        """Load ALL messages from cache and rebuild the chat."""

        if not self.selected_contact:
            return

        contact = self.selected_contact
        cached = self._cache.get(contact.cache_key, [])

        self._clear_chat()
        self._seen_timestamps.clear()
        self._seen_message_ids.clear()

        for msg in cached:
            text = msg.get("text", "")
            is_mine = msg.get("is_mine", False)
            quote_text = msg.get("quote_text")
            ts = msg.get("timestamp", 0)
            msg_type = msg.get("msg_type", "text")
            attachment_info = msg.get("attachment_info")
            attachment_id = msg.get("attachment_id")
            sender = msg.get("sender", "")
            status = msg.get("status", "sent" if is_mine else "read")

            if ts:
                self._seen_timestamps.add((contact.protocol, contact.cache_key, ts))
                self._seen_message_ids.add(
                    (contact.protocol, contact.cache_key, ts, text)
                )
                mid = msg.get("id")
                if mid:
                    self._seen_message_ids.add(
                        (contact.protocol, contact.cache_key, mid, text)
                    )

            self._add_message(
                text,
                is_mine=is_mine,
                quote_text=quote_text,
                msg_type=msg_type,
                attachment_info=attachment_info,
                attachment_id=attachment_id,
                timestamp=ts,
                sender=sender,
                status=status,
                message_id=msg.get("id"),
                edited=msg.get("edited", False),
            )

        self._loaded_all = True
        self._status(f"📋 Loaded all {len(cached)} messages")

    def _refresh_chat(self):
        """Check cache for new messages of the current contact not yet shown.

        Only messages *newer* than the last message already displayed are
        added.  This avoids re-adding older messages that were intentionally
        not shown because the chat was loaded with a limited window (e.g. the
        last 20 messages) — otherwise closing the emoji picker would append
        all the older cached messages and jump the view to them.
        """
        if not self.selected_contact:
            return

        contact = self.selected_contact
        cached = self._cache.get(contact.cache_key, [])
        new_count = 0

        # Processa in ordine cronologico (stabile): con la cache ordinata, il
        # "nuovo" rispetto a max_seen è calcolato correttamente e l'ULTIMO
        # messaggio non viene saltato (stabile: stesse-ts restano in ordine di
        # arrivo, niente tie-break alfabetico).
        cached = sorted(cached, key=lambda m: int(m.get("timestamp") or 0))

        # Only consider messages newer than the newest one already shown.
        max_seen = max((t for (_p, _k, t) in self._seen_timestamps), default=0)
        for msg in cached:
            ts = msg.get("timestamp", 0)
            text = msg.get("text", "")
            identity = (contact.protocol, contact.cache_key, int(ts), text)
            # Un messaggio è da mostrare se è più recente dell'ultimo visto
            # (ts > max_seen) OPPURE condivide lo stesso ts dell'ultimo (stesso
            # secondo) ma è un messaggio DISTINTO non ancora mostrato — evita il
            # baco "manca l'ULTIMO messaggio" quando due messaggi WhatsApp
            # arrivano nello stesso secondo (identità = ts + testo, non solo ts).
            is_new = (
                bool(ts)
                and (ts > max_seen or ts == max_seen)
                and identity not in self._seen_message_ids
            )
            # Identity stabile via id: un messaggio già mostrato (e poi aggiornato
            # dall'echo, con ts/testo nuovi) non deve essere rimontato come nuovo.
            if is_new:
                mid = msg.get("id")
                if (
                    mid
                    and (contact.protocol, contact.cache_key, mid, text)
                    in self._seen_message_ids
                ):
                    is_new = False
            if is_new:
                self._seen_timestamps.add(
                    (contact.protocol, contact.cache_key, int(ts))
                )
                self._seen_message_ids.add(identity)
                if msg.get("id"):
                    self._seen_message_ids.add(
                        (contact.protocol, contact.cache_key, msg.get("id"), text)
                    )
                is_mine = msg.get("is_mine", False)
                quote_text = msg.get("quote_text")
                msg_type = msg.get("msg_type", "text")
                attachment_info = msg.get("attachment_info")
                attachment_id = msg.get("attachment_id")
                sender = msg.get("sender", "")
                status = msg.get("status", "sent" if is_mine else "read")
                self._add_message(
                    text,
                    is_mine=is_mine,
                    quote_text=quote_text,
                    msg_type=msg_type,
                    attachment_info=attachment_info,
                    attachment_id=attachment_id,
                    timestamp=ts,
                    sender=sender,
                    status=status,
                    message_id=msg.get("id"),
                )
                new_count += 1

        if new_count > 0:
            chat_log = self.chat_log
            chat_log.scroll_end(animate=False)
