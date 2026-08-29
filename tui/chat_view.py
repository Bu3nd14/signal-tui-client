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
from backends.config import thumbnail_max_cols, thumbnail_max_lines
from models import (
    PROTOCOL_SIGNAL,
    is_media_quote_placeholder,
)
from tui.images.detect import ImageSupport
from ui_components import (
    ImageWidget,
    MessageWidget,
    QuoteWidget,
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


_TECHNICAL_LABELS = frozenset(
    {"🖼️ Image", "🖼️ Photo", "🖼️ Immagine", "Media", "Image", "Photo", "Immagine"}
)
_TECHNICAL_PREFIXES = (
    "Image: ",
    "Photo: ",
    "Immagine: ",
    "🖼️ Image",
    "🖼️ Photo",
    "🖼️ Immagine",
    "Video: ",
    "Audio: ",
)
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
    if s.lower().startswith("upload-"):
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
    if is_media_quote_placeholder(t) or _is_technical_media_label(t):
        t = ""
    if is_media_quote_placeholder(info) or _is_technical_media_label(info):
        info = ""
    if t and not _is_synthetic_media_text(text, attachment_info, attachment_id):
        return t
    if info and not _is_technical_media_label(info):
        return info
    return None


def _is_scrolled_to_bottom(chat_log: Vertical) -> bool:
    """Return True if ``#chat-log`` is at (or near) its scroll bottom.

    Used to keep the chat "stuck to the bottom" when a native thumbnail grows
    the content *after* the initial ``scroll_end`` of a mount (see
    ``_finish_native_thumbnail``).

    Textual 8.2.8 API verified in the venv: ``Widget.scroll_offset`` is an
    ``Offset`` (``.y`` is the rounded scroll position) and ``Widget.max_scroll_y``
    is an int (``max(0, virtual_size.height - container_size.height + …)``);
    there is **no** ``max_scroll_offset_y`` nor ``is_scrolled_to_bottom`` method.
    A chat shorter than the viewport (``max_scroll_y <= 0``) counts as "at the
    bottom".
    """
    max_y = chat_log.max_scroll_y
    if max_y <= 0:
        return True
    return chat_log.scroll_offset.y >= max_y - 1


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
        content_type: str | None = None,
        timestamp: int = 0,
        sender: str = "",
        status: str = "sent",
        protocol: str | None = None,
        message_id: str | None = None,
        edited: bool = False,
        quote_attachment_id: str | None = None,
        quote_attachment_path: Path | None = None,
        quote_content_type: str | None = None,
        quote_timestamp: int | None = None,
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

        if quote_text or quote_timestamp:
            quote_class = "msg-quote-right" if is_mine else "msg-quote"
            quote_widget = QuoteWidget(
                quote_text,
                classes=quote_class,
                attachment_id=quote_attachment_id,
                attachment_path=quote_attachment_path,
                content_type=quote_content_type,
                protocol=protocol,
                quote_timestamp=quote_timestamp,
                contact_key=(
                    self.selected_contact.id
                    if self.selected_contact is not None
                    else None
                ),
            )
            chat_log.mount(quote_widget)
            # Native quote thumbnail (uscita): resolve the already-known path in
            # a worker and register the thumbnail on the quote bubble.
            self._maybe_resolve_quote_thumbnail(quote_widget)

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
                timestamp=timestamp,
                sender=sender,
                message_id=message_id,
                caption=caption,
                content_type=content_type,
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

    def _native_placeholder(self, text: str, *, attachment_id: str | None) -> str:
        """Return empty text when a native thumbnail will cover the widget.

        In native KITTY mode (renderer available AND an attachment id to
        resolve) the widget shows ONLY the kitty image — no textual placeholder.
        In every fallback/degradation case (CATIMG/OFF, renderer gone, no id)
        the given *text* is returned unchanged.
        """
        if (
            self.image_support is ImageSupport.KITTY
            and self._native_renderer is not None
            and attachment_id
        ):
            return ""
        return text

    def _render_image_in_chat(
        self,
        attachment_id: str | None,
        attachment_info: str,
        is_mine: bool,
        chat_log: Vertical,
        protocol: str | None = None,
        timestamp: int = 0,
        sender: str = "",
        message_id: str | None = None,
        caption: str | None = None,
        content_type: str | None = None,
    ):
        """Mount a clickable ``ImageWidget`` placeholder immediately and
        resolve the attachment path in a worker thread.

        Uses the ``BackendManager`` to route the attachment-id resolution to
        the correct protocol backend (Signal, WhatsApp, ...).  Falls back to
        the legacy Signal backend when *protocol* is ``None`` (safety net
        for callers compiled before the multi-protocol routing was added).

        The actual image rendering happens on-demand when the user presses
        Enter or clicks the widget, which opens a fullscreen modal.

        ``timestamp``/``sender``/``message_id``/``caption`` are forwarded to
        the ``ImageWidget`` so Alt+click / Alt+r can raise a ``ReplyRequested``
        carrying the message metadata (bug #37).
        """
        resolved_protocol = protocol or PROTOCOL_SIGNAL

        if not attachment_id:
            widget = ImageWidget(
                attachment_path=None,
                attachment_id="",
                fallback_text=f"[🖼️ Image: {attachment_info}]",
                timestamp=timestamp,
                sender=sender,
                is_mine=is_mine,
                message_id=message_id,
                caption=caption,
                attachment_info=attachment_info,
                protocol=resolved_protocol,
                content_type=content_type,
            )
            widget.classes = "msg-right" if is_mine else "msg-left"
            chat_log.mount(widget)
            chat_log.scroll_end(animate=False)
            return

        widget = ImageWidget(
            attachment_path=None,
            attachment_id=attachment_id,
            fallback_text=self._native_placeholder(
                f"[🖼️ Image: {attachment_info} — loading…]",
                attachment_id=attachment_id,
            ),
            timestamp=timestamp,
            sender=sender,
            is_mine=is_mine,
            message_id=message_id,
            caption=caption,
            attachment_info=attachment_info,
            protocol=resolved_protocol,
            content_type=content_type,
        )
        widget.classes = "msg-right" if is_mine else "msg-left"
        chat_log.mount(widget)
        chat_log.scroll_end(animate=False)

        max_lines = thumbnail_max_lines()
        max_cols = thumbnail_max_cols()
        chat_region = getattr(chat_log, "content_region", None)
        chat_width = chat_region.width if chat_region is not None else 0
        if chat_width > 0:
            max_cols = min(max_cols, max(1, chat_width))

        self.run_worker(
            lambda: self._resolve_attachment_worker(
                resolved_protocol,
                attachment_id,
                widget,
                attachment_info,
                max_lines=max_lines,
                max_cols=max_cols,
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
        *,
        max_lines: int = 0,
        max_cols: int = 0,
        window_token: int | None = None,
    ):
        """Worker thread: resolve the path (and prepare a native thumbnail).

        Runs under a concurrency-limited semaphore (4) so a burst of attachments
        never floods the CPU with parallel Pillow decodes.  For KITTY the PNG
        thumbnail is generated here (Pillow); the transmit and widget-state
        update happen on the UI thread via ``call_from_thread``.

        ``window_token`` (when set) ties this worker to a specific cache-window
        generation: every terminal branch forwards it to the UI handler, which
        decrements the pending counter and re-anchors the chat at the end of the
        window load.
        """
        with self._image_resolve_semaphore:
            path = self.manager.get_attachment_path(protocol, attachment_id)
            if path is None:
                self.call_from_thread(
                    self._finish_attachment_resolve,
                    widget,
                    None,
                    attachment_info,
                    window_token=window_token,
                )
                return
            renderer = self._native_renderer
            if self.image_support is ImageSupport.KITTY and renderer is not None:
                try:
                    png = renderer.prepare_thumbnail(path, max_lines, max_cols)
                except Exception as _e:
                    logger.debug("Thumbnail prepare failed", exc_info=True)
                    self.call_from_thread(
                        self._finish_attachment_resolve,
                        widget,
                        path,
                        attachment_info,
                        window_token=window_token,
                    )
                    return
                self.call_from_thread(
                    self._finish_native_thumbnail,
                    widget,
                    path,
                    png,
                    window_token=window_token,
                )
            else:
                self.call_from_thread(
                    self._finish_attachment_resolve,
                    widget,
                    path,
                    attachment_info,
                    window_token=window_token,
                )

    def _window_worker_done(self, window_token: int | None) -> None:
        """Decrement the pending window counter and re-anchor when it drains.

        Runs on the UI thread (called at the top of the finish handlers), so the
        decrement has no cross-thread race.  A stale ``window_token`` (from a
        previous window) is ignored, so orphan workers never scroll the newly
        opened chat.
        """
        if window_token is None or window_token != self._window_native_token:
            return
        self._window_native_pending -= 1
        if self._window_native_pending <= 0:
            self._window_native_pending = 0
            try:
                self.chat_log.scroll_end(animate=False)
            except Exception as _e:
                logger.debug(
                    "Failed to scroll to bottom after window load", exc_info=True
                )

    def _finish_attachment_resolve(
        self,
        widget: ImageWidget,
        path: Path | None,
        attachment_info: str,
        window_token: int | None = None,
    ):
        """UI thread: update the placeholder with the resolved attachment path."""
        self._window_worker_done(window_token)
        if not widget.is_mounted:
            return
        if path is None:
            widget.update_attachment(None, f"[🖼️ Image: {attachment_info}]")
        else:
            widget.update_attachment(
                path, f"[🖼️ Image: {path.name} — Click Enter to View]"
            )

    def _finish_native_thumbnail(
        self,
        widget: ImageWidget,
        path: Path,
        png: bytes,
        window_token: int | None = None,
    ):
        """UI thread: transmit the thumbnail once and register the widget.

        Placement happens later in the app's ``post_display_hook`` — never here.
        """
        self._window_worker_done(window_token)
        if not widget.is_mounted:
            # Mount is async: stash the PNG; the app hook registers it once the
            # widget is mounted (bounded: cleared by native_cleanup on unmount).
            widget._pending_native_png = png
            widget._pending_native_path = path
            self._native_stashed.add(widget)
            return
        self._register_native_thumbnail(widget, path, png)

    def _register_native_thumbnail(
        self, widget: ImageWidget, path: Path, png: bytes
    ) -> None:
        """Transmit + register a native chat thumbnail (widget is mounted)."""
        renderer = self._native_renderer
        if renderer is None:
            # Renderer vanished (e.g. a resize disabled it): CATIMG fallback.
            widget.update_attachment(
                path, f"[🖼️ Image: {path.name} — Click Enter to View]"
            )
            return
        image_id = self._next_native_image_id()
        renderer.transmit(image_id, png)
        # Native mode: no textual placeholder — the kitty image covers the area.
        widget.update_attachment(path, "")
        # "Stick to bottom": ``show_native_thumbnail`` grows the widget (and thus
        # the chat content) asynchronously, AFTER the mount's ``scroll_end``.
        # Capture the anchored state BEFORE the growth and re-anchor only when
        # the user was already at the bottom — otherwise we'd yank a user who
        # scrolled up to read while thumbnails were still loading.
        chat_log = self.chat_log
        was_at_bottom = _is_scrolled_to_bottom(chat_log)
        widget.show_native_thumbnail(renderer, image_id, png)
        self._native_widgets[image_id] = widget
        if was_at_bottom:
            chat_log.scroll_end(animate=False)

    # ── Native quote thumbnail (uscita + ingresso) ──────────────────────────
    def _maybe_resolve_quote_thumbnail(self, quote_widget: QuoteWidget) -> None:
        """Start native thumbnail generation for a quote bubble.

        Uscita: an already-resolved ``attachment_path`` drives the thumbnail.
        Ingresso: a backend-produced ``attachment_id`` (+ ``protocol``) is
        resolved lazily via ``get_attachment_path`` in the worker (best-effort).

        Falls back silently to the text-only bubble otherwise (degrado): no
        path nor resolvable id, non-kitty, renderer gone, or an
        already-registered thumbnail.
        """
        if quote_widget.native_image_id is not None:
            return  # already has a thumbnail → no double generation
        if self.image_support is not ImageSupport.KITTY:
            return
        if self._native_renderer is None:
            return
        path = quote_widget.attachment_path
        attachment_id = quote_widget.attachment_id
        protocol = quote_widget.protocol
        if (
            path is None
            and not (attachment_id and protocol)
            and not (quote_widget.quote_timestamp and quote_widget.contact_key)
        ):
            # Nessuna sorgente e nessun fallback dalle chat (le reply inviate
            # non persistono i metadati quote) → bolla text-only.
            return
        self.run_worker(
            lambda w=quote_widget, p=path, a=attachment_id, pr=protocol: (
                self._resolve_quote_thumbnail_worker(w, p, a, pr)
            ),
            thread=True,
            exclusive=False,
        )

    def _resolve_quote_thumbnail_worker(
        self,
        widget: QuoteWidget,
        path: Path | None,
        attachment_id: str | None,
        protocol: str | None,
    ) -> None:
        """Worker thread: resolve (ingresso) and generate the 3×6 quote thumb."""
        with self._quote_resolve_semaphore:
            renderer = self._native_renderer
            if renderer is None:
                return
            if path is not None and not Path(path).is_file():
                # Persisted path went stale (cleanup/restart): fall back to the
                # lazy resolve of the quoted attachment id, if any.
                logger.warning(
                    "Quote thumbnail path stale (missing %r) — falling back", path
                )
                path = None
            if path is None:
                # Lazy resolve (ingresso, or stale-path fallback), best-effort.
                if not (attachment_id and protocol):
                    # Fallback per le reply inviate (metadati quote non
                    # persistiti): risolvi l'immagine quotata dalla stessa chat
                    # tramite quote_timestamp (stessa logica del fallback web).
                    resolved_id = self._quoted_attachment_id_from_chat(widget)
                    if resolved_id is None:
                        logger.warning(
                            "Quote thumbnail: no path and no resolvable id "
                            "(protocol=%s id=%r) — text-only bubble",
                            protocol,
                            attachment_id,
                        )
                        return
                    attachment_id = resolved_id
                    protocol = widget.protocol
                try:
                    path = self.manager.get_attachment_path(protocol, attachment_id)
                except Exception as _e:
                    logger.warning(
                        "Quote attachment resolve raised (protocol=%s id=%r)",
                        protocol,
                        attachment_id,
                        exc_info=True,
                    )
                    path = None
                if path is None:
                    # Best-effort: a lazy download (WAHA/Telegram) can fail
                    # silently — no session, unreachable, or media gone.  The
                    # quote stays text-only (no thumbnail, no UI error).
                    logger.warning(
                        "Quote attachment not resolvable (protocol=%s id=%r) "
                        "— text-only bubble",
                        protocol,
                        attachment_id,
                    )
                    return
            try:
                png = renderer.prepare_thumbnail(path, 3, 6)
            except Exception as _e:
                logger.warning("Quote thumbnail prepare failed", exc_info=True)
                return
            self.call_from_thread(self._finish_quote_thumbnail, widget, png)

    def _quoted_attachment_id_from_chat(self, widget: QuoteWidget) -> str | None:
        """Attachment id dell'immagine quotata (stessa chat).

        Match PRIMA esatto sul timestamp: evita falsi positivi quando messaggi
        vicini (±2s) convivono (es. un'immagine appena inviata e la reply).
        La finestra ±2s solo se il timestamp esatto non esiste affatto (drift).
        """
        if not (widget.contact_key and widget.protocol):
            return None
        import sqlite3

        import backend
        from backend.db import _DB_LOCK

        ts = widget.quote_timestamp
        if not ts:
            # Quote senza timestamp quotato: fallback per id messaggio quotato
            # (Telegram/WhatsApp) o per nome file (es. Signal).
            if widget.reply_to_message_id:
                resolved = self._quoted_attachment_id_from_message_id(widget)
                if resolved:
                    return resolved
            return self._quoted_attachment_id_from_filename(widget)
        image_clause = (
            "AND (content_type LIKE 'image/%' OR lower(attachment_id) LIKE '%.jpg' "
            "OR lower(attachment_id) LIKE '%.jpeg' OR lower(attachment_id) LIKE '%.png' "
            "OR lower(attachment_id) LIKE '%.gif' OR lower(attachment_id) LIKE '%.webp')"
        )
        try:
            with _DB_LOCK:
                connection = sqlite3.connect(backend.DB_FILE)
                try:
                    row = connection.execute(
                        "SELECT attachment_id FROM messages "
                        "WHERE protocol = ? AND contact_number = ? "
                        "AND timestamp = ? AND attachment_id IS NOT NULL "
                        + image_clause
                        + " LIMIT 1",
                        (widget.protocol, widget.contact_key, ts),
                    ).fetchone()
                    if row is None:
                        exists = connection.execute(
                            "SELECT 1 FROM messages WHERE protocol = ? AND contact_number = ? "
                            "AND timestamp = ? LIMIT 1",
                            (widget.protocol, widget.contact_key, ts),
                        ).fetchone()
                        if exists:
                            # Il messaggio quotato esiste ma non è un'immagine:
                            # niente miniatura (mai prendere messaggi vicini).
                            return None
                        row = connection.execute(
                            "SELECT attachment_id FROM messages "
                            "WHERE protocol = ? AND contact_number = ? "
                            "AND ABS(timestamp - ?) <= 2000 AND attachment_id IS NOT NULL "
                            + image_clause
                            + " ORDER BY ABS(timestamp - ?) LIMIT 1",
                            (widget.protocol, widget.contact_key, ts, ts),
                        ).fetchone()
                finally:
                    connection.close()
        except (sqlite3.Error, OSError):
            return None
        if row is None or not row[0]:
            # Nessun match per timestamp: per le quote a immagine senza
            # metadati, prova a risolvere per nome file.
            return self._quoted_attachment_id_from_filename(widget)
        return str(row[0])

    def _quoted_attachment_id_from_message_id(
        self,
        widget: QuoteWidget,
    ) -> str | None:
        """Risolve l'immagine quotata per ``msg_id`` (Telegram/WhatsApp).

        Il backend persiste ``reply_to_message_id`` (= msg_id del target) ma
        non sempre il timestamp quotato né l'allegato: il match per id è
        preciso e copre anche i target non in cache all'arrivo.
        """
        if not (widget.reply_to_message_id and widget.contact_key and widget.protocol):
            return None
        image_clause = (
            "AND (content_type LIKE 'image/%' OR lower(attachment_id) LIKE '%.jpg' "
            "OR lower(attachment_id) LIKE '%.jpeg' OR lower(attachment_id) LIKE '%.png' "
            "OR lower(attachment_id) LIKE '%.gif' OR lower(attachment_id) LIKE '%.webp')"
        )
        try:
            import sqlite3

            import backend
            from backend.db import _DB_LOCK

            with _DB_LOCK:
                connection = sqlite3.connect(backend.DB_FILE)
                try:
                    row = connection.execute(
                        "SELECT attachment_id FROM messages "
                        "WHERE protocol = ? AND contact_number = ? "
                        "AND attachment_id IS NOT NULL "
                        "AND (msg_id = ? OR msg_id LIKE '%_' || ?) "
                        + image_clause
                        + " LIMIT 1",
                        (
                            widget.protocol,
                            widget.contact_key,
                            widget.reply_to_message_id,
                            widget.reply_to_message_id,
                        ),
                    ).fetchone()
                finally:
                    connection.close()
        except (sqlite3.Error, OSError):
            return None
        if row is None or not row[0]:
            return None
        return str(row[0])

    def _quoted_attachment_id_from_filename(
        self,
        widget: QuoteWidget,
    ) -> str | None:
        """Risolve l'immagine quotata per nome file (quote senza metadati).

        Es. quote Signal a un'immagine: quote_text = "IMG_1303.jpg — 🖼️
        Immagine" e nessun ``quote_timestamp``/``quote_attachment_id``. Il nome
        file compare in ``attachment_info`` del messaggio quotato.
        """
        if not str(widget.content_type or "").lower().startswith("image/"):
            return None
        quote_text = str(getattr(widget, "_quote_text_raw", "") or "")
        match = re.search(
            r"([A-Za-z0-9][\w.\- ]*\.(?:jpe?g|png|gif|webp))\b",
            quote_text,
            re.IGNORECASE,
        )
        if not match:
            return None
        filename = match.group(1).strip()
        image_clause = (
            "AND (content_type LIKE 'image/%' OR lower(attachment_id) LIKE '%.jpg' "
            "OR lower(attachment_id) LIKE '%.jpeg' OR lower(attachment_id) LIKE '%.png' "
            "OR lower(attachment_id) LIKE '%.gif' OR lower(attachment_id) LIKE '%.webp')"
        )
        try:
            import sqlite3

            import backend
            from backend.db import _DB_LOCK

            with _DB_LOCK:
                connection = sqlite3.connect(backend.DB_FILE)
                try:
                    row = connection.execute(
                        "SELECT attachment_id FROM messages "
                        "WHERE protocol = ? AND contact_number = ? "
                        "AND attachment_id IS NOT NULL "
                        "AND (attachment_info LIKE ? OR attachment_id LIKE ?) "
                        + image_clause
                        + " LIMIT 1",
                        (
                            widget.protocol,
                            widget.contact_key,
                            f"%{filename}%",
                            f"%{filename}%",
                        ),
                    ).fetchone()
                finally:
                    connection.close()
        except (sqlite3.Error, OSError):
            return None
        if row is None or not row[0]:
            return None
        return str(row[0])

    def _finish_quote_thumbnail(self, widget: QuoteWidget, png: bytes) -> None:
        """UI thread: transmit the quote thumbnail once and register the widget.

        Placement happens in the app's ``post_display_hook`` (chunk 3) — never
        here.  The wire (#37) is untouched: the thumbnail is display-only.
        """
        if not widget.is_mounted:
            # Mount is async: stash the PNG; the app hook registers it once the
            # widget is mounted (bounded: cleared by native_cleanup on unmount).
            widget._pending_quote_png = png
            self._native_stashed.add(widget)
            return
        self._register_quote_thumbnail(widget, png)

    def _register_quote_thumbnail(self, widget: QuoteWidget, png: bytes) -> None:
        """Transmit + register a native quote thumbnail (widget is mounted)."""
        renderer = self._native_renderer
        if renderer is None:
            return
        image_id = self._next_native_image_id()
        renderer.transmit(image_id, png)
        widget.show_native_thumbnail(renderer, image_id, png)
        self._native_widgets[image_id] = widget

    def _resolve_mounted_image_paths(self, widgets: list) -> None:
        """Start path resolution for cached image widgets (C4).

        Only relevant for KITTY (native thumbnails); CATIMG keeps its lazy
        on-click resolution (``download.py``).  Reuses the same worker path and
        the concurrency semaphore as the live path.
        """
        if self.image_support is not ImageSupport.KITTY:
            return
        if self._native_renderer is None:
            return
        max_lines = thumbnail_max_lines()
        max_cols = thumbnail_max_cols()
        chat_region = getattr(self.chat_log, "content_region", None)
        chat_width = chat_region.width if chat_region is not None else 0
        if chat_width > 0:
            max_cols = min(max_cols, max(1, chat_width))

        for widget in widgets:
            if isinstance(widget, ImageWidget):
                if widget.attachment_path is not None or not widget.attachment_id:
                    continue
                protocol = widget._protocol or PROTOCOL_SIGNAL
                info = widget._attachment_info or "Photo"
                self._window_native_pending += 1
                token = self._window_native_token
                self.run_worker(
                    lambda w=widget, p=protocol, i=info, t=token: (
                        self._resolve_attachment_worker(
                            p,
                            w.attachment_id,
                            w,
                            i,
                            max_lines=max_lines,
                            max_cols=max_cols,
                            window_token=t,
                        )
                    ),
                    thread=True,
                    exclusive=False,
                )
            elif isinstance(widget, QuoteWidget):
                # Native quote thumbnail (ingresso): resolve the quoted
                # attachment lazily when a resolvable id is present.
                self._maybe_resolve_quote_thumbnail(widget)

    def _clear_chat(self):
        """Clear the chat and reset the render-level de-dup set."""
        chat_log = self.chat_log
        for widget in list(getattr(chat_log, "children", [])):
            if isinstance(widget, (ImageWidget, QuoteWidget)):
                widget.native_cleanup()
        chat_log.remove_children()
        self._shown_in_log.clear()
        self._seen_message_ids.clear()
        # All placements were freed above (d=I); drop their cache keys too.
        self._native_last_key.clear()
        self._chat_native_ids.clear()
        # Invalidate any in-flight window workers: their deferred scroll_end must
        # not fire on the newly-opened chat.
        self._window_native_token += 1
        self._window_native_pending = 0

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
                # C4: cached image widgets mount with path=None; kick off the
                # (KITTY-only) resolution so native thumbnails can appear.
                # Start a fresh window generation: reset the pending counter for
                # the workers started below and invalidate any previous window.
                self._window_native_token += 1
                self._window_native_pending = 0
                self._resolve_mounted_image_paths(widgets)

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
            inc_att = m.get("attachment_id")
            for existing in ui_msgs:
                if bool(existing.get("is_mine", False)) != is_mine:
                    continue
                # Id-first for BOTH directions: an edit keeps the id but
                # changes the text, so the id match must precede the text
                # comparison (mirror of _message_already_cached).  Exception:
                # two entries sharing the id but carrying DISTINCT
                # attachment_ids are separate attachments of one multi-attachment
                # message (Signal) and must NOT collapse; when one side lacks an
                # attachment_id (ack-echo / caption echo) the id-first identity
                # still applies.
                if mid and existing.get("id") and existing.get("id") == mid:
                    ex_att = existing.get("attachment_id")
                    if not (inc_att and ex_att and inc_att != ex_att):
                        return existing
                    # Distinct attachment → keep scanning for a text twin.
                if existing.get("text", "") != text:
                    continue
                existing_ts = int(existing.get("timestamp") or 0)
                if not is_mine:
                    # Incoming: id unreliable -> text + fuzzy timestamp (+/-5s).
                    if abs(existing_ts - ts) <= 5000:
                        return existing
                elif mid:
                    # Outgoing: fallback echo SOLO se la riga UI è ancora
                    # id-less (optimistic non confermato).  Veto id-mismatch:
                    # una riga con id diverso è un messaggio distinto
                    # (bug: 2ª/3ª immagine e "OK"+"OK" ravvicinati collassavano).
                    if not existing.get("id") and abs(existing_ts - ts) <= 600000:
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
                if existing.get("msg_type", "text") == "text" and existing.get(
                    "text", ""
                ) != m.get("text", ""):
                    existing["text"] = m.get("text")
                    existing["edited"] = True
                    changed = True
                # Still upgrade the status if the incoming rank is higher
                # (never downgrade read → sent).
                if _status_rank(m.get("status")) > _status_rank(existing.get("status")):
                    existing["status"] = m.get("status")
                # Additive quoted-attachment metadata: a backend twin may carry
                # the thumbnail fields while the UI entry (optimistic/older) does
                # not — copy them without touching identity/dedup.
                for _key in (
                    "quote_attachment_id",
                    "quote_attachment_path",
                    "quote_content_type",
                ):
                    if existing.get(_key) is None and m.get(_key) is not None:
                        existing[_key] = m.get(_key)
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
        content_type = msg.get("content_type")
        sender = msg.get("sender", "")
        status = msg.get("status", "sent" if is_mine else "read")
        ts = msg.get("timestamp", 0)
        message_id = msg.get("id")

        widgets: list = []
        if quote_text or msg.get("quote_timestamp"):
            quote_class = "msg-quote-right" if is_mine else "msg-quote"
            # Forward future quoted-attachment metadata (chunk 5) when already
            # present in the message dict; resolution stays out of scope here.
            widgets.append(
                QuoteWidget(
                    quote_text or "",
                    classes=quote_class,
                    attachment_id=msg.get("quote_attachment_id"),
                    attachment_path=msg.get("quote_attachment_path"),
                    content_type=msg.get("quote_content_type"),
                    protocol=protocol,
                    quote_timestamp=msg.get("quote_timestamp"),
                    reply_to_message_id=msg.get("reply_to_message_id"),
                    contact_key=(
                        self.selected_contact.id
                        if self.selected_contact is not None
                        else None
                    ),
                )
            )

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
                fallback_text=self._native_placeholder(
                    f"[{display}]", attachment_id=attachment_id
                ),
                timestamp=ts,
                sender=sender,
                is_mine=is_mine,
                message_id=message_id,
                msg_type=msg_type,
                caption=caption,
                attachment_info=attachment_info,
                protocol=protocol,
                content_type=content_type,
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
                content_type=msg.get("content_type"),
                timestamp=ts,
                sender=sender,
                status=status,
                message_id=msg.get("id"),
                edited=msg.get("edited", False),
                quote_attachment_id=msg.get("quote_attachment_id"),
                quote_attachment_path=msg.get("quote_attachment_path"),
                quote_content_type=msg.get("quote_content_type"),
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
                    content_type=msg.get("content_type"),
                    timestamp=ts,
                    sender=sender,
                    status=status,
                    message_id=msg.get("id"),
                    quote_attachment_id=msg.get("quote_attachment_id"),
                    quote_attachment_path=msg.get("quote_attachment_path"),
                    quote_content_type=msg.get("quote_content_type"),
                )
                new_count += 1

        if new_count > 0:
            chat_log = self.chat_log
            chat_log.scroll_end(animate=False)
