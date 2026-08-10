"""
Signal TUI Client — Textual interface integrated with signal-cli via JSON-RPC.
Uses signal-cli daemon over HTTP (localhost) for fast operations (milliseconds).
If the daemon is unavailable, falls back to subprocess (slower but works).
Messages are saved in a local cache for persistence across sessions.
"""

import atexit
import logging
import os
import sys
import time
import traceback
from pathlib import Path
from typing import ClassVar, Optional


LOCK_FILE = "/tmp/signal-tui.lock"


def _acquire_lock() -> bool:
    """Try to acquire a lock file to prevent multiple instances.

    Returns True if the lock was acquired (or no other instance is running),
    False if another instance is already running.
    """
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE) as f:
                old_pid = int(f.read().strip())
            # Check if the process is still alive
            try:
                os.kill(old_pid, 0)
                # Process is alive → another instance is running
                return False
            except OSError:
                # Process is dead → we can take the lock
                pass
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
        return True
    except Exception:
        # If anything goes wrong, allow the app to start anyway
        return True


def _release_lock():
    """Remove the lock file if it belongs to us."""
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE) as f:
                old_pid = int(f.read().strip())
            if old_pid == os.getpid():
                os.remove(LOCK_FILE)
    except Exception:
        pass


# Global exception handler: salva le eccezioni non gestite su file
# per debug, senza interferire con stderr usato da Textual per la TUI.
def _global_exception_handler(exc_type, exc_value, exc_traceback):
    try:
        with open("/tmp/signal-crash.log", "w") as f:
            traceback.print_exception(exc_type, exc_value, exc_traceback, file=f)
    except Exception:
        pass  # non vogliamo causare altri errori
    # Chiama comunque l'handler predefinito per vedere l'errore anche in console
    sys.__excepthook__(exc_type, exc_value, exc_traceback)

sys.excepthook = _global_exception_handler



from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.timer import Timer
from textual.widgets import (
    Header,
    Footer,
    ListView,
    ListItem,
    Label,
    Input,
    Static,
    Button,
)


from models import (
    ChatContact,
    ChatEvent,
    PROTOCOL_SIGNAL,
    PROTOCOL_WHATSAPP,
    contact_cache_key,
    protocol_emoji,
)

from backends import (
    BackendManager,
    SignalBackend,
    WhatsAppBackend,
)
from backends.config import whatsapp_enabled

from backend import (
    serve_text_as_file,
    USER_NUMBER,
    DAEMON_HTTP_PORT,
    ensure_webhook_server,
    WEBHOOK_PORT,
    _dedup_messages,
)

from ui_components import (
    ContactListWidget,
    ChatAreaWidget,
    MessageWidget,
    ImageWidget,
    ImageModalScreen,
    DownloadLinkWidget,
)
from emoji_picker import (
    EmojiPickerScreen,
    EmojiCompletionWidget,
    replace_emoji_aliases,
)
from contact_picker import ContactPickerScreen
from device_link_screen import DeviceLinkPickerScreen



logger = logging.getLogger(__name__)
# Ensure LINK-* logs are written to a file (Textual may suppress stderr)
_link_fh = logging.FileHandler("/tmp/signal-link.log", mode="w")
_link_fh.setLevel(logging.DEBUG)
_link_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_link_fh)
logger.setLevel(logging.DEBUG)


# ─── Main App ────────────────────────────────────────────────────────────────

class SignalTUI(App):
    """Main Signal TUI App with JSON-RPC daemon over HTTP."""

    CSS = """
    Screen {
        background: $surface;
    }

    .section-title {
        text-style: bold;
        padding: 1 1;
        background: $accent;
        color: $text;
        width: 100%;
    }

    #ChatTitle {
        text-align: left;
    }

    #contact-list {
        height: 100%;
        border: solid $accent;
    }

    #contact-list.chat-filter-signal {
        border: solid #39c5e0;
    }

    #contact-list.chat-filter-whatsapp {
        border: solid #25d366;
    }

    #contact-list ListItem {
        padding: 1 1;
    }

    #contact-list ListItem:hover {
        background: $accent 20%;
    }

    #contact-list ListItem:focus {
        background: $accent 40%;
    }

    /* Protocol accents in the contact list */
    .protocol-signal {
        color: #2ecc71;
    }

    .protocol-whatsapp {
        color: #20c997;
    }

    #contact-list .protocol-signal:hover,
    #contact-list .protocol-whatsapp:hover {
        color: $text;
    }

    #chat-log {
        height: 1fr;
        border: solid $accent;
        margin: 0 1;
        overflow-y: auto;
        overflow-x: hidden;
    }

    .msg-left {
        text-align: left;
        padding: 0 1;
        color: $text;
    }

    .msg-right {
        text-align: right;
        padding: 0 1;
        color: $success;
    }

    .msg-info {
        text-align: left;
        padding: 0 1;
        color: $text-muted;
    }

    /* Colore del bordo della chat in base al filtro Ctrl+W (azzurro Signal,
       verde WhatsApp, default/giallo per ALL).  Non usiamo più una "barra"
       laterale (border-left) su ogni messaggio. */
    #chat-log.chat-filter-signal {
        border: solid #39c5e0;
    }

    #chat-log.chat-filter-whatsapp {
        border: solid #25d366;
    }

    /* Banner (titoli di sezione) sincroni col bordo della chat per filtro. */
    #ContactsTitle.chat-filter-signal,
    #ChatTitle.chat-filter-signal {
        background: #39c5e0;
    }

    #ContactsTitle.chat-filter-whatsapp,
    #ChatTitle.chat-filter-whatsapp {
        background: #25d366;
    }
    .msg-quote {
        text-align: left;
        padding: 0 1 0 3;
        color: $text-muted;
        text-style: italic;
    }

    .msg-quote-right {
        text-align: right;
        padding: 0 3 0 1;
        color: $text-muted;
        text-style: italic;
    }

    .msg-load-more {
        text-align: center;
        padding: 1 1;
        color: $accent;
        text-style: bold;
        background: $surface;
        border: solid $accent;
        margin: 1 0;
    }

    .msg-load-more:hover {
        background: $accent 20%;
    }

    #reply-bar {
        height: auto;
        padding: 0 1;
        background: $accent 30%;
        color: $text;
        text-style: bold;
        border: solid $accent;
        margin: 0 1;
    }

    #reply-bar.reply-bar-hidden {
        display: none;
    }

    #reply-text {
        width: 1fr;
        padding: 0 1;
    }

    .reply-cancel-btn {
        width: 3;
        text-align: center;
        color: $error;
        text-style: bold;
        background: transparent;
        border: none;
        padding: 0;
        min-width: 3;
    }

    .reply-cancel-btn:hover {
        background: $error 30%;
    }

    #input-row {
        dock: bottom;
        height: auto;
        margin: 1 0;
    }

    #emoji-btn {
        width: 6;
        min-width: 6;
        margin: 0 0 0 1;
        content-align: left middle;
        padding: 0;
        border: tall $border;
        background: $surface;
        color: $text;
    }

    #emoji-btn:hover {
        background: $accent 30%;
    }

    #message-input {
        width: 1fr;
        margin: 0 1 0 0;
    }

    Horizontal {
        height: 1fr;
    }

    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+e", "open_emoji_picker", "Emoji", priority=True),
        Binding("ctrl+s", "open_contact_picker", "Search", priority=True),
        Binding("ctrl+d", "download_mode", "Download", priority=True),
        Binding("ctrl+w", "cycle_protocol_filter", "Filter", show=True, priority=True),
        Binding("ctrl+l", "open_device_link", "Link", priority=True),
        Binding("ctrl+n", "next_suggestion", "Next", show=False),
        Binding("ctrl+p", "prev_suggestion", "Prev", show=False),
    ]


    # Disable Textual's built-in command palette (Ctrl+P) to avoid conflict
    # with the emoji picker's Ctrl+P for previous category.
    ENABLE_COMMAND_PALETTE = False

    def _is_emoji_picker_open(self) -> bool:
        """Check if the emoji picker modal screen is currently active."""
        return isinstance(self.screen, EmojiPickerScreen)

    def __init__(self):
        super().__init__()
        # Multi-protocol backend manager + the always-registered Signal backend.
        self.manager = BackendManager()
        self.signal_backend = SignalBackend()
        self.manager.register(self.signal_backend)

        # WhatsApp backend is registered only when configured (env/config.json);
        # otherwise it's skipped gracefully and the Signal TUI keeps working.
        self.whatsapp_backend: Optional[WhatsAppBackend] = None
        if whatsapp_enabled():
            self.whatsapp_backend = WhatsAppBackend()
            self.manager.register(self.whatsapp_backend)

        self.contacts: list[ChatContact] = []
        self.selected_contact: Optional[ChatContact] = None

        # Active protocol filter for the unified contact list:
        # "all" -> "signal" -> "whatsapp" (cycled with Ctrl+W).
        self._protocol_filter: str = "all"

        self._polling_active = False
        # Message identity: (protocol, contact_id, timestamp_ms).  Timestamps
        # alone are no longer unique across protocols.
        self._seen_timestamps: set[tuple[str, str, int]] = set()
        # Identity discriminante (protocol, key, ts, testo) usata da _refresh_chat
        # per NON disperdere l'ULTIMO messaggio quando condivide lo stesso
        # timestamp (secondi) col precedente — molto comune su WhatsApp contiguo.
        # _seen_timestamps (timestamp-only) da solo non basta: due messaggi
        # nello stesso secondo sono indistinguibili e il secondo veniva scartato.
        self._seen_message_ids: set[tuple[str, str, int, str]] = set()
        self._unread_counts: dict[str, int] = {}  # keyed by contact_cache_key
        # Flag "lista contatti sporca": se True, a fine batch di messaggi il
        # poll worker esegue UN solo re-render della lista (non uno per evento).
        self._contact_list_dirty = False
        # cache_key dei contatti che hanno "sporchi" l'unread/ordine nel batch
        # corrente.  Il poll worker lo legge e svuota a fine batch: permette al
        # flush lista di ricalcolare l'unread in modo INCREMENTALE (per singolo
        # contatto, O(M)) invece di rifare il giro completo su tutti i contatti
        # (O(N×M)) — fonte di un blocco temporaneo della UI a ogni messaggio.
        self._dirty_contact_keys: set[str] = set()
        #: Se in un batch hanno scritto più di questo numero di chat distinte,
        #: il flush ricade sull'update unread "full" (conservativo).
        self._CONTACT_UPDATE_BATCH_MAX = 4
        self._cache: dict[str, list[dict]] = {}   # keyed by contact_cache_key
        self._loaded_all = False
        # Incremented on every contact selection; a load worker checks it to
        # detect a stale reload after a newer _clear_chat (prevents duplicates).
        self._chat_reload_token = 0
        # Identities of real messages already mounted in the current chat log,
        # used as a render-level de-dup safety net so _refresh_chat and the
        # load workers never mount the same message twice.
        # Chiave = (protocol, cache_key, timestamp, testo): il timestamp da solo
        # (granularità al secondo) non basta — due messaggi WhatsApp distinti
        # nello stesso secondo verrebbero scartati (il secondo non compariva).
        self._shown_in_log: set[tuple[str, str, int, str]] = set()

        self._reply_to: Optional[dict] = None  # message being replied to
        self._download_mode = False  # Ctrl+D download mode active
        self._typing_contacts: dict[str, float] = {}  # contact cache_key → time of last typing STARTED

        self._typing_mumbling: dict[str, float] = {}  # contact cache_key → mumbling expiry
        self._TYPING_TIMEOUT = 10.0  # seconds before a typing indicator auto-expires
        self._TYPING_MUMBLING_DURATION = 60.0  # seconds a contact stays with 💭 after stopping typing

        # cache_key → ListItem: O(1) lookup for _update_typing_label
        self._contact_widgets: dict[str, ListItem] = {}

        # Progressive render state (avoids UI freeze at startup / Ctrl+W).
        self._pending_contacts: list[ChatContact] = []
        self._render_chunk_index: int = 0
        self._render_chunk_size: int = 50
        self._render_timer: Timer | None = None

        # Cached reference to the #chat-log widget — avoids repeated
        # O(N) CSS selector scans via query_one on every message mount.
        self._chat_log: Vertical | None = None

        # Ensure daemon is killed on exit (clean or crash) so the next
        # startup is a fresh daemon with --receive-mode on-start that
        # downloads all pending messages.
        atexit.register(self._cleanup_daemon)

    @property
    def chat_log(self) -> Vertical:
        """The ``#chat-log`` widget, lazily cached on first access."""
        if self._chat_log is None:
            self._chat_log = self.query_one("#chat-log", Vertical)
        return self._chat_log






    def compose(self):
        yield Header()
        yield Horizontal(
            ContactListWidget(),
            ChatAreaWidget(),
        )
        yield Footer()

    def on_mount(self):
        """On startup, start poll worker and backend connections in parallel."""
        self._chat_log = self.query_one("#chat-log", Vertical)
        # Start poll worker immediately — Signal and WhatsApp events flow
        # as soon as their backends are ready (independent workers below).
        self._polling_active = True
        self.run_worker(self._poll_worker, exclusive=True, thread=True)
        if not self.signal_backend.needs_pairing:
            self.run_worker(self._connect_signal, exclusive=False, thread=True)
        if self.whatsapp_backend is not None and not self.whatsapp_backend.needs_pairing:
            self.run_worker(self._connect_whatsapp, exclusive=False, thread=True)

    def action_quit(self):
        """Ctrl+Q: stop polling and exit cleanly."""
        self._polling_active = False
        self.exit()

    def on_exit(self):
        """On exit, stop polling and kill the daemon."""
        self._polling_active = False
        self._cleanup_daemon()


    # ─── Chat helper methods ────────────────────────────────────────────────

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
            if (
                self.selected_contact is not None
                and timestamp
            ):
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
            self._render_image_in_chat(
                attachment_id=attachment_id,
                attachment_info=attachment_info or text,
                is_mine=is_mine,
                chat_log=chat_log,
                protocol=protocol,
            )
            return

        # ── Non-image messages ──────────────────────────────────────────
        display_text = text
        if msg_type == "sticker":
            display_text = f"🎨 {text}" if text and text != "Media" else "🎨 [Sticker]"
        elif msg_type == "attachment":
            display_text = f"📎 {text}" if text and text != "Media" else "📎 [File]"

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
        )

    def _render_image_in_chat(
        self,
        attachment_id: str | None,
        attachment_info: str,
        is_mine: bool,
        chat_log: Vertical,
        protocol: str | None = None,
    ):
        """Resolve the attachment path and mount a clickable placeholder
        ``ImageWidget``.

        Uses the ``BackendManager`` to route the attachment-id resolution to
        the correct protocol backend (Signal, WhatsApp, ...).  Falls back to
        the legacy Signal backend when *protocol* is ``None`` (safety net
        for callers compiled before the multi-protocol routing was added).

        The actual image rendering happens on-demand when the user presses
        Enter or clicks the widget, which opens a fullscreen modal.
        """
        # Resolve the file path via the protocol-appropriate backend.
        att_path: Path | None = None
        if attachment_id:
            resolved_protocol = protocol or PROTOCOL_SIGNAL
            att_path = self.manager.get_attachment_path(resolved_protocol, attachment_id)

        if att_path is None:
            fallback = f"[🖼️ Image: {attachment_info}]"
            widget = ImageWidget(
                attachment_path=None,
                attachment_id=attachment_id or "",
                fallback_text=fallback,
            )
        else:
            widget = ImageWidget(
                attachment_path=att_path,
                attachment_id=attachment_id or "",
                fallback_text=f"[🖼️ Image: {att_path.name} — Click Enter to View]",
            )

        widget.classes = "msg-right" if is_mine else "msg-left"
        chat_log.mount(widget)
        chat_log.scroll_end(animate=False)

    def _clear_chat(self):
        """Clear the chat and reset the render-level de-dup set."""
        chat_log = self.chat_log
        chat_log.remove_children()
        self._shown_in_log.clear()
        self._seen_message_ids.clear()

    # ─── Daemon cleanup ───────────────────────────────────────────────────

    def _cleanup_daemon(self) -> None:
        """Terminate the signal-cli daemon process.

        Called on normal exit (on_exit) and via atexit for crash safety.
        Best-effort: failures are silently ignored.
        """
        try:
            sb = self.signal_backend
            # Stop SSE listener thread
            sb._polling_active = False
            t = sb._sse_thread
            sb._sse_thread = None
            if t is not None and t.is_alive():
                t.join(timeout=3)
            # Terminate daemon subprocess
            if sb.daemon_proc is not None:
                try:
                    sb.daemon_proc.terminate()
                    sb.daemon_proc.wait(timeout=5)
                except Exception:
                    try:
                        sb.daemon_proc.kill()
                    except Exception:
                        pass
                sb.daemon_proc = None
        except Exception:
            pass

    # ─── Envelope processing ─────────────────────────────────────────────────

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


    # ─── Startup ────────────────────────────────────────────────────────────

    # ─── Backend connection workers (parallel, independent) ──────────────────

    def _connect_signal(self) -> None:
        """Connect Signal backend and update UI (runs in worker thread)."""
        try:
            self.call_from_thread(
                self._add_message, "⏳ Starting signal-cli daemon...", is_info=True
            )
            sb = self.signal_backend
            logger.info("LINK-SIG: start, daemon_proc=%s", sb.daemon_proc is not None)
            self.call_from_thread(
                self._add_message, "⏳ Waiting for Signal daemon...", is_info=True
            )
            sb._connect_sync()
            logger.info("LINK-SIG: connect_sync done, use_daemon=%s", sb._use_daemon)

            # Build cache from all backends loaded so far
            self._cache = {}
            for b in self.manager.all():
                for cid, msgs in b.cache.items():
                    self._cache[contact_cache_key(b.protocol, cid)] = list(msgs)
            self._sync_last_ts()

            contacts = self.manager.list_contacts()
            self.contacts = contacts
            logger.info("LINK-SIG: calling _update_contacts_ui with %d contacts", len(contacts))
            self.call_from_thread(self._update_contacts_ui, contacts)
            logger.info("LINK-SIG: done, contacts=%d", len(contacts))
            self.call_from_thread(
                self._add_message,
                f"✅ Loaded {len(contacts)} contacts.",
                is_info=True,
            )
            self.call_from_thread(
                self._add_message, "💡 Select a contact to view chat", is_info=True
            )
            if sb._use_daemon:
                self.call_from_thread(
                    self._add_message,
                    "✅ Daemon active, connecting directly...",
                    is_info=True,
                )
            else:
                self.call_from_thread(
                    self._add_message,
                    "⚠️ Daemon not available. Using subprocess mode (slower).",
                    is_info=True,
                )
        except Exception as e:
            logger.exception("LINK-SIG: failed: %s", e)
            self.call_from_thread(
                self._add_message, f"❌ Signal backend error: {e}", is_info=True
            )

    def _connect_whatsapp(self) -> None:
        """Connect WhatsApp backend and update UI (runs in worker thread)."""
        try:
            logger.info("LINK-WA: start")
            if self.whatsapp_backend.needs_pairing:
                self.call_from_thread(
                    self._add_message, "⏳ Waiting for WhatsApp to sync...", is_info=True
                )
            self.whatsapp_backend.connect_sync()
            n = len(self.whatsapp_backend.contacts)
            logger.info("LINK-WA: connect_sync done, wa_contacts=%d", n)
            try:
                ensure_webhook_server(self.whatsapp_backend)
            except Exception:
                pass
            if n > 0:
                self.call_from_thread(
                    self._add_message,
                    f"💬 WhatsApp backend active ({n} contacts, webhook on :{WEBHOOK_PORT}).",
                    is_info=True,
                )
            # Rebuild unified cache with protocol-aware keys (Signal may
            # have finished first, missing WhatsApp messages in self._cache).
            self._cache = {}
            for b in self.manager.all():
                for cid, msgs in b.cache.items():
                    self._cache[contact_cache_key(b.protocol, cid)] = list(msgs)
            self._sync_last_ts()
            self.contacts = self.manager.list_contacts()
            logger.info("LINK-WA: calling _update_contacts_ui with %d contacts", len(self.contacts))
            self.call_from_thread(self._update_contacts_ui, self.contacts)
            self._resync_wa_history()
            logger.info("LINK-WA: done, total_contacts=%d", len(self.contacts))
        except Exception as exc:
            logger.exception("LINK-WA: failed: %s", exc)
            self.call_from_thread(
                self._add_message,
                f"💬 WhatsApp backend unavailable: {exc}",
                is_info=True,
            )


    def _resync_wa_history(self) -> int:
        """Re-sync best-effort dello storico WhatsApp all'avvio.

        Delega al backend ``resync_history`` (unread ∪ chat con messaggi nel DB)
        e riporta un info-message se ha processato qualche chat.  Non solleva
        mai eccezioni: all'avvio l'UI non deve fallire né per un errore remoto
        né per il reporting.  Ritorna il numero di chat ri-sincronizzate
        (0 se non applicabile).
        """
        if self.whatsapp_backend is None or not getattr(
            self.whatsapp_backend, "_connected", False
        ):
            return 0
        try:
            resync = getattr(self.whatsapp_backend, "resync_history", None)
        except Exception:
            return 0
        if resync is None:
            return 0
        try:
            n = resync()
        except Exception:
            return 0
        if n:
            try:
                self.call_from_thread(
                    self._add_message,
                    f"💬 WhatsApp history re-synced for {n} chats.",
                    is_info=True,
                )
            except Exception:
                pass  # il report è solo informativo
        return n

    @staticmethod
    def _contact_sort_key(c: ChatContact) -> tuple:
        """Key per ordinare i contatti: ultimi messaggi in alto.

        Gruppi (in ordine):
          1. contatti CON messaggi      -> per ``last_message_ts`` desc;
          2. contatti SENZA messaggi ma con un nome -> alfabetici;
          3. contatti SENZA messaggi e SOLO numero (display_name == id) -> in coda.
        """
        ts = c.last_message_ts or 0
        name = (c.display_name or "").lower()
        unnamed = True  # "solo numero": display_name manca o coincide con l'id
        if c.display_name and c.display_name != c.id:
            unnamed = False
        has_messages = ts > 0
        return (
            not has_messages,               # 0 = con messaggi (prima), 1 = senza
            -ts,                            # più recente in alto (solo se has_messages)
            1 if (not has_messages and unnamed) else 0,  # "solo numero senza msg" in coda
            name,                           # alfabetico per i senza messaggi
            c.id,
        )

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
        self._render_contact_list(self._filtered_contacts())

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
                if mts > ts:
                    ts = mts
            if ts > c.last_message_ts:
                c.last_message_ts = ts

    def _filtered_contacts(self) -> list[ChatContact]:
        """Return contacts matching the active protocol filter."""
        if self._protocol_filter in ("signal", "whatsapp"):
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
        if self._protocol_filter == "all":
            return " - All"
        return ""


    def _apply_contact_visibility(self) -> None:
        """Toggle ``display`` on ListItems based on the active protocol filter.

        Unlike ``_render_contact_list`` this *never* destroys widgets — it
        only sets ``display=True`` / ``display=False`` on the children that
        are already in the DOM.  Called on every Ctrl+W cycle instead of a
        full rebuild, keeping the UI responsive even with 600+ contacts.
        """
        contact_list = self.query_one("#contact-list", ListView)

        if self._protocol_filter == "all":
            visible = {c.cache_key for c in self.contacts}
        else:
            visible = {
                c.cache_key for c in self.contacts
                if c.protocol == self._protocol_filter
            }

        first_visible: int | None = None
        for i, child in enumerate(contact_list.children):
            key: str | None = getattr(child, "_contact_id", None)
            show = key in visible
            child.display = show
            if show and first_visible is None:
                first_visible = i

        if first_visible is not None:
            contact_list.index = first_visible
        elif self.selected_contact is not None:
            # No contact visible under this filter — deselect.
            self.selected_contact = None

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

        Called initially from ``_update_contacts_ui`` and then
        self-schedules via ``set_timer`` until all pending contacts are
        rendered.  Each chunk yields control back to the Textual event
        loop so the UI never freezes.
        """
        contact_list = self.query_one("#contact-list", ListView)
        start = self._render_chunk_index
        end = min(start + self._render_chunk_size, len(self._pending_contacts))
        chunk = self._pending_contacts[start:end]

        for c in chunk:
            text = self._contact_label(c)
            item = ListItem(Label(text))
            item._contact_id = c.cache_key
            item._label_text = text
            item.add_class(self._protocol_class(c))
            contact_list.append(item)
            self._contact_widgets[c.cache_key] = item

        self._render_chunk_index = end
        if end < len(self._pending_contacts):
            self._render_timer = self.set_timer(0.05, self._render_next_chunk)
        else:
            self._render_timer = None
            # All chunks rendered — restore selection highlight if a contact
            # was already selected before this progressive render started.
            if self.selected_contact is not None:
                item = self._contact_widgets.get(self.selected_contact.cache_key)
                if item is not None:
                    contact_list.index = contact_list.children.index(item)

    def _render_contact_list(self, filtered: list[ChatContact]) -> None:
        """Renderizza la lista contatti, aggiornandola *in-place* quando la
        composizione/ordine non cambia.

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

        def _sync_item(item, c):
            """Aggiorna testo/classe di un ListItem esistente per il contatto c."""
            label = item.children[0] if item.children else None
            new_text = self._contact_label(c)
            if getattr(item, "_label_text", None) != new_text and label is not None:
                label.update(new_text)
                item._label_text = new_text
            if not item.has_class(self._protocol_class(c)):
                for cl in ("protocol-signal", "protocol-whatsapp"):
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

        if self.selected_contact and self.selected_contact in filtered:
            contact_list.index = filtered.index(self.selected_contact)



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
        except Exception:
            section_lbl = None
        if section_lbl is not None:
            section_lbl.update(f"📇 Contacts{self._filter_title_suffix()}")

        # Sync the filter accent across the chat border, the contact list border
        # and the two section banners (📇 Contacts / 💬 Chat).
        cls_signal = "chat-filter-signal"
        cls_whats = "chat-filter-whatsapp"
        widgets = [self.chat_log]
        for selector in ("#contact-list", "#ContactsTitle", "#ChatTitle"):
            try:
                widgets.append(self.query_one(selector))
            except Exception:
                pass
        for node in widgets:
            node.remove_class(cls_signal, cls_whats)
            if self._protocol_filter == "signal":
                node.add_class(cls_signal)
            elif self._protocol_filter == "whatsapp":
                node.add_class(cls_whats)
                # filtro "all": nessuna classe -> default (giallo).

    def action_cycle_protocol_filter(self):
        """Ctrl+W: cycle the contact list filter ALL -> SIGNAL -> WHATSAPP."""
        order = ["all", "signal", "whatsapp"]
        idx = order.index(self._protocol_filter) if self._protocol_filter in order else 0
        self._protocol_filter = order[(idx + 1) % len(order)]
        self._apply_contact_filter()
        # NB: volutamente NON scriviamo niente nella chat qui: il ctrl+W aggiorna
        # solo il titolo della barra contatti e la lista visibile, senza inquinare
        # la cronologia della conversazione in corso.

    def _update_contacts_ui(self, contacts: list[ChatContact]):
        """Update the UI with the (merged) contact list.

        Delegates to ``_render_contact_list`` which uses progressive render
        when the set changes (startup / new backend), and fast/reorder paths
        on subsequent calls.
        """
        logger.info("LINK-UI: start, contacts=%d", len(contacts))
        self.contacts = contacts
        self._sort_contacts()
        self._render_contact_list(self._filtered_contacts())
        self._update_unread_badges()
        logger.info("LINK-UI: done")

    # ─── Contact selection ─────────────────────────────────────────────────

    def _select_contact(self, contact: ChatContact) -> None:
        """Select a contact and show its chat.

        Shared by both the contact list (``on_list_view_selected``) and the
        contact picker (``_open_contact_picker``).  Sets ``selected_contact``,
        highlights the contact in the left list, loads the chat, marks all
        messages as read, updates unread badges, and returns focus to the
        message input.
        """
        if contact not in self.contacts:
            return

        self.selected_contact = contact
        # Update the chat banner with the selected contact's name.
        chat_title = self.query_one("#ChatTitle", Label)
        chat_title.update(f"{protocol_emoji(contact.protocol)} Chat - {contact.display_name}")
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
        # The ListView holds only the filtered/visible contacts, so we must
        # index it by the position in that filtered list — not in self.contacts
        # (a contact picked from the picker may be beyond the visible subset,
        # which used to raise IndexError: list index out of range).
        contact_list = self.query_one("#contact-list", ListView)
        visible = self._filtered_contacts()
        try:
            idx = visible.index(contact)
            contact_list.index = idx
            if idx < len(contact_list.children):
                item = contact_list.children[idx]
                item.children[0].update(self._contact_label(self.selected_contact))
        except ValueError:
            # Contact is filtered out of the visible list; still select it but
            # don't try to highlight a row that isn't rendered.
            pass

        # Return focus to the message input so the user can start typing
        # immediately after selecting a contact.
        try:
            self.query_one("#message-input", Input).focus()
        except Exception:
            pass

    def on_list_view_selected(self, event: ListView.Selected):
        """When a contact is selected, show the chat."""
        index = self.query_one("#contact-list", ListView).index
        # The ListView is built from the filtered/visible contacts, so resolve
        # the selected row against that filtered list (not self.contacts) to
        # avoid picking the wrong contact when the protocol filter is active.
        visible = self._filtered_contacts()
        if index is not None and 0 <= index < len(visible):
            contact = visible[index]
            # Guard: when _select_contact sets contact_list.index programmatically
            # (e.g. from the contact picker), Textual fires ListView.Selected again.
            # If the contact is already selected, skip to avoid reloading the chat
            # twice (which duplicates the messages).
            if contact == self.selected_contact:
                return
            self._select_contact(contact)



    # ─── Message logic ────────────────────────────────────────────────────

    def _load_messages_worker(self):
        """Load messages: last 20 from cache.
        If there are more than 20 messages, show a widget to load the rest.

        Runs in a worker thread; before each render it verifies the reload
        token is still current (no newer contact selection happened) so a
        stale worker stops mounting messages after a more recent ``_clear_chat``
        — otherwise re-selecting a contact can double the messages.
        """
        if not self.selected_contact:
            return

        contact = self.selected_contact
        reload_token = self._chat_reload_token
        self._loaded_all = False

        cached = self._cache.get(contact.cache_key, [])
        total = len(cached)

        def _is_stale() -> bool:
            """True if a newer selection happened or the contact changed."""
            return (
                self._chat_reload_token != reload_token
                or self.selected_contact != contact
            )

        # Per WhatsApp: WAHA CORE non spinge lo storico (niente WS/stream) e il
        # cache locale si riempie solo tramite il polling live (limitatо alle chat
        # "calde") e dagli invii della TUI.  Per avere sempre i messaggi più
        # recenti — inclusi quelli inviati da UN ALTRO client — scarichiamo lo
        # storico remoto (default ~20) a ogni apertura: `fetch_history` usa il
        # dedup interno di `ingest_message`, quindi i messaggi già presenti non
        # vengono duplicati.  Fallisce in modo non distruttivo.
        if contact.protocol == WhatsAppBackend.protocol and not _is_stale():
            backend = self.manager.get(contact.protocol)
            fetch = getattr(backend, "fetch_history", None)
            if fetch is not None:
                try:
                    # Finestra più ampia: `fetch_history` riconcilia lo storico
                    # remoto con il DB (aggiunge i messaggi mancanti, aggiorna
                    # le entry senza id).  Con limit=20 un DB a cui mancavano
                    # messaggi (corrotto dai vecchi bug di dedup) non veniva
                    # mai riparato perché i 20 più recenti erano già presenti.
                    # Retry sul fetch: se il primo fetch restituisce vuoto (WAHA
                    # può rispondere [] appena avviato anche con session WORKING,
                    # vedi sintomo "No message history"), riproviamo un paio di
                    # volte con una breve pausa prima di arrenderci.  Rispetta
                    # _is_stale() per non montare in una selezione scaduta.
                    _retry = 0
                    while True:
                        fetch(contact.id, limit=50)
                        _bm = getattr(backend, "cache", {}).get(contact.id, [])
                        if _bm or _is_stale() or _retry >= 2:
                            break
                        _retry += 1
                        time.sleep(0.8)

                    # fetch_history alimenta il cache del backend; lo specchiamo
                    # nel cache protocol-aware dell'UI per renderizzarlo.
                    backend_msgs = getattr(backend, "cache", {}).get(contact.id, [])
                    if backend_msgs:
                        self._cache[contact.cache_key] = list(backend_msgs)
                    if contact.cache_key in self._cache:
                        cached = self._cache[contact.cache_key]
                        total = len(cached)
                    # Li sto visualizzando: i messaggi appena scaricati non
                    # devono gonfiare i badge unread (già azzerati da _select_contact).
                    for msg in self._cache.get(contact.cache_key, []):
                        if not msg.get("is_mine"):
                            msg["read"] = True
                except Exception:
                    pass  # fallback: resta sul cache locale

        # Ordina la cache della chat per timestamp (stabile) PRIMA di tagliare
        # la finestra `[-N:]` per il render.  Fiss: la cache può essere popolata
        # fuori ordine (append da più fonti) o riordinata dall'upgrade dell'echo;
        # senza sort, ```[-20:]``` non selezionerebbe davvero gli ultimi messaggi
        # e l'ultimo (es. "Ok ci sentiamo") spariva dalla vista.  sort stabile:
        # a parità di timestamp preserva l'ordine di arrivo (niente tie-break).
        cached = sorted(cached, key=lambda m: int(m.get("timestamp") or 0))

        if cached:
            if total > 20:
                messages_to_show = cached[-20:]
            else:
                messages_to_show = cached
                self._loaded_all = True

            # Fix B: mounting ATOMICO.  Invece di chiamare _add_message una volta
            # per messaggio (via call_from_thread il live può INfilarsi a metà
            # e rompere l'ordine / duplicare l'ultimo), raccogliamo la finestra
            # nel worker e rimontiamo il log UN'UNICA volta sul thread UI, dopo
            # uno _clear_chat.  Così l'ordine finale riflette la cronologia
            # (l'ultimo messaggio sta in fondo) senza interleaving col live.
            batch = []
            for msg in messages_to_show:
                if _is_stale():
                    return
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
                    self._seen_message_ids.add((contact.protocol, contact.cache_key, ts, text))
                    mid = msg.get("id")
                    if mid:
                        self._seen_message_ids.add(
                            (contact.protocol, contact.cache_key, mid)
                        )
                batch.append((text, is_mine, quote_text, msg_type, attachment_info,
                              attachment_id, ts, sender, status))

            def _mount_window():
                # Sul thread UI: svuota il log e rimonta la finestra ordinata in
                # UN SOLO mount così Textual fa un solo layout pass invece di 20+.
                if not _is_stale():
                    try:
                        self._clear_chat()
                    except Exception:
                        pass
                if _is_stale():
                    return

                is_group = (
                    self.selected_contact is not None
                    and self.selected_contact.id.endswith("@g.us")
                )
                protocol = contact.protocol
                widgets: list = []

                for (text, is_mine, quote_text, msg_type, attachment_info,
                     attachment_id, ts, sender, status) in batch:
                    try:
                        if ts:
                            self._shown_in_log.add(
                                (protocol, contact.cache_key, ts, text)
                            )

                        if quote_text:
                            quote_class = "msg-quote-right" if is_mine else "msg-quote"
                            widgets.append(Static(f"▎ {quote_text}", classes=quote_class))

                        if msg_type == "image":
                            # Image placeholder — async rendering is separate
                            from ui_components import ImageWidget
                            display = attachment_info or text or "Image"
                            widgets.append(ImageWidget(
                                attachment_path=None,
                                attachment_id=attachment_id or "",
                                fallback_text=f"[🖼️ {display}]",
                            ))
                        else:
                            display_text = text
                            if msg_type == "sticker":
                                display_text = f"🎨 {text}" if text and text != "Media" else "🎨 [Sticker]"
                            elif msg_type == "attachment":
                                display_text = f"📎 {text}" if text and text != "Media" else "📎 [File]"
                            widgets.append(self._make_message_widget(
                                text=display_text,
                                is_mine=is_mine,
                                timestamp=ts,
                                sender=sender,
                                status=status,
                                protocol=protocol,
                                is_group=is_group,
                            ))
                    except Exception:
                        pass

                if widgets:
                    chat_log = self.chat_log
                    chat_log.mount(*widgets)
                    chat_log.scroll_end(animate=False)

                # Banner "load more" rimontato DOPO lo _clear_chat
                if total > 20 and not _is_stale():
                    try:
                        self._add_load_more_widget(total - 20)
                    except Exception:
                        pass

            try:
                self.call_from_thread(_mount_window)
            except Exception:
                pass  # fallback: resta sul cache UI già specchiato

        else:
            self._loaded_all = True
            self.call_from_thread(
                self._add_message, "No message history for this contact", is_info=True
            )

    def _add_load_more_widget(self, remaining: int):
        """Add a clickable widget to load older messages."""
        chat_log = self.chat_log
        widget = Button(
            f"📜 ↑ {remaining} older messages — click to load",
            classes="msg-load-more",
            id="load-more-msg",
        )
        chat_log.mount(widget, before=0)

    def on_button_pressed(self, event: Button.Pressed):
        """When the user clicks a button."""
        if event.button.id == "load-more-msg":
            self._load_all_messages()
        elif event.button.id == "reply-cancel":
            self._cancel_reply()
        elif event.button.id == "emoji-btn":
            self._open_emoji_picker()

    # ─── Emoji picker ─────────────────────────────────────────────────────────

    def _open_emoji_picker(self) -> None:
        """Open the emoji picker modal."""
        def _on_emoji_selected(emoji_char: str | None) -> None:
            if emoji_char:
                # Insert the selected emoji into the message input
                msg_input = self.query_one("#message-input", Input)
                current = msg_input.value
                cursor = msg_input.cursor_position
                # Insert at cursor position
                new_value = current[:cursor] + emoji_char + current[cursor:]
                msg_input.value = new_value
                msg_input.cursor_position = cursor + len(emoji_char)
                msg_input.focus()
            # Refresh chat to show any messages that arrived while the picker was open
            self._refresh_chat()

        self.push_screen(EmojiPickerScreen(), _on_emoji_selected)

    def action_open_emoji_picker(self) -> None:
        """Action to open emoji picker (bound to Ctrl+E)."""
        self._open_emoji_picker()

    # ─── Contact picker ───────────────────────────────────────────────────────

    def _open_contact_picker(self) -> None:
        """Open the contact search picker modal."""
        def _on_contact_selected(contact: ChatContact | None) -> None:
            if contact:
                # Select the contact's chat (also highlights it in the left list).
                # _select_contact already reloads the full chat from cache, so
                # calling _refresh_chat() afterwards would re-add the same
                # messages (the load worker runs in a separate thread and may
                # not have populated _seen_timestamps yet, making _refresh_chat
                # add everything again → duplicated messages).
                self._select_contact(contact)
            else:
                # Picker dismissed without selecting: refresh to show any
                # messages that arrived while the picker was open.
                self._refresh_chat()


        self.push_screen(ContactPickerScreen(self._filtered_contacts()), _on_contact_selected)

    def action_open_contact_picker(self) -> None:
        """Action to open the contact picker (bound to Ctrl+S)."""
        self._open_contact_picker()

    # ─── Device link picker ────────────────────────────────────────────────────

    def _open_device_link(self) -> None:
        """Open the device link picker modal (Ctrl+L)."""
        def _on_done(_: object) -> None:
            logger.info("LINK-DONE: callback fired")
            if self.signal_backend:
                self.run_worker(self._connect_signal, exclusive=False, thread=True)
            if self.whatsapp_backend:
                self.run_worker(self._connect_whatsapp, exclusive=False, thread=True)

        self.push_screen(
            DeviceLinkPickerScreen(
                signal_number=self.signal_backend.user_number,
                has_whatsapp=self.whatsapp_backend is not None,
            ),
            _on_done,
        )


    def action_open_device_link(self) -> None:
        """Action to open device link picker (bound to Ctrl+L)."""
        self._open_device_link()

    # ─── Emoji alias auto-completion ──────────────────────────────────────────


    def _is_completion_visible(self) -> bool:
        """Check if the emoji completion widget is currently visible."""
        try:
            completion = self.query_one("#emoji-completion", EmojiCompletionWidget)
            return completion.has_class("-visible")
        except Exception:
            return False

    def on_input_changed(self, event: Input.Changed) -> None:
        """Handle input changes for emoji alias auto-completion."""
        if event.input.id != "message-input":
            return

        value = event.value
        # Check if the user is typing an emoji alias (starts with ':')
        if ":" in value:
            # Find the last ':' that starts an alias
            last_colon = value.rfind(":")
            if last_colon >= 0:
                # Check if there's a closing ':' after it
                rest = value[last_colon + 1:]
                # If no space after the colon, it might be an incomplete alias
                if " " not in rest and "/" not in rest:
                    prefix = rest
                    # Try to show suggestions
                    completion = self.query_one("#emoji-completion", EmojiCompletionWidget)
                    completion.show_suggestions(prefix)
                    return

        # Hide completion if no alias is being typed
        try:
            completion = self.query_one("#emoji-completion", EmojiCompletionWidget)
            completion.hide_suggestions()
        except Exception:
            pass

    def _insert_emoji_from_completion(self) -> None:
        """Replace the current :alias: with the selected emoji from completion."""
        try:
            completion = self.query_one("#emoji-completion", EmojiCompletionWidget)
        except Exception:
            return

        if not completion.selected_emoji:
            return

        msg_input = self.query_one("#message-input", Input)
        value = msg_input.value
        last_colon = value.rfind(":")
        if last_colon < 0:
            return

        # Replace from the last ':' to the end with the emoji
        new_value = value[:last_colon] + completion.selected_emoji + " "
        msg_input.value = new_value
        msg_input.cursor_position = len(new_value)
        completion.hide_suggestions()
        msg_input.focus()

    def action_next_suggestion(self) -> None:
        """Ctrl+N: go to next emoji suggestion.
        Does nothing if the emoji picker is open (so Ctrl+N reaches the picker)."""
        if self._is_emoji_picker_open():
            return
        if self._is_completion_visible():
            try:
                completion = self.query_one("#emoji-completion", EmojiCompletionWidget)
                completion.select_next()
            except Exception:
                pass

    def action_prev_suggestion(self) -> None:
        """Ctrl+P: go to previous emoji suggestion.
        Does nothing if the emoji picker is open (so Ctrl+P reaches the picker)."""
        if self._is_emoji_picker_open():
            return
        if self._is_completion_visible():
            try:
                completion = self.query_one("#emoji-completion", EmojiCompletionWidget)
                completion.select_prev()
            except Exception:
                pass

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
                self._seen_message_ids.add((contact.protocol, contact.cache_key, ts, text))
                mid = msg.get("id")
                if mid:
                    self._seen_message_ids.add(
                        (contact.protocol, contact.cache_key, mid)
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
            )

        self._loaded_all = True
        self._add_message(f"📋 Loaded all {len(cached)} messages", is_info=True)

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
            except Exception:
                pass
            # Re-check before the next poll so an empty round still exits.
            if not self._polling_active:
                return


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
        max_seen = max(
            (t for (_p, _k, t) in self._seen_timestamps), default=0
        )
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
                if mid and (contact.protocol, contact.cache_key, mid) in self._seen_message_ids:
                    is_new = False
            if is_new:
                self._seen_timestamps.add((contact.protocol, contact.cache_key, int(ts)))
                self._seen_message_ids.add(identity)
                if msg.get("id"):
                    self._seen_message_ids.add(
                        (contact.protocol, contact.cache_key, msg.get("id"))
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
                )
                new_count += 1

        if new_count > 0:
            chat_log = self.chat_log
            chat_log.scroll_end(animate=False)


    def _recompute_unread(self, contact_cache_key_value: str | None = None) -> bool:
        """Ricalcola i conteggi unread in ``self._unread_counts`` SOLO nei dati.

        Non tocca i widget: è il passo \"dati\" del flush di fine batch, così il
        render della lista avviene una volta sola (e mai a scapito della chat).
        Ritorna ``True`` se almeno un conteggio è cambiato.
        """
        if not self.contacts:
            return False

        def _count_unread(messages: list[dict]) -> int:
            """Conta i messaggi non letti, con dedup per (timestamp, text)."""
            seen: set[tuple[int, str]] = set()
            count = 0
            for m in messages:
                if m.get("is_mine"):
                    continue
                if m.get("read", True):
                    continue
                identity = (int(m.get("timestamp") or 0), m.get("text", ""))
                if identity not in seen:
                    seen.add(identity)
                    count += 1
            return count

        changed = False

        if contact_cache_key_value is not None:
            # Incrementale: solo il contatto indicato (O(M)).
            messages = self._cache.get(contact_cache_key_value, [])
            unread = _count_unread(messages)
            old = self._unread_counts.get(contact_cache_key_value, 0)
            if unread != old:
                self._unread_counts[contact_cache_key_value] = unread
                changed = True
        else:
            # Full: tutti i contatti (startup / ricalcolo globale).
            for contact in self.contacts:
                messages = self._cache.get(contact.cache_key, [])
                unread = _count_unread(messages)
                old = self._unread_counts.get(contact.cache_key, 0)
                if unread != old:
                    self._unread_counts[contact.cache_key] = unread
                    changed = True

        return changed

    def _update_unread_badges(self, contact_cache_key_value: str | None = None):
        """Check the in-memory cache and update *N badges on contacts.
        If counts change, re-sort the list and rebuild it.

        Parameters
        ----------
        contact_cache_key_value:
            If provided (a ``contact_cache_key`` string), only recompute the
            unread count for this single contact (O(M) instead of O(N×M)).
            If None, recompute for all contacts (startup / list load).
        """
        if not self.contacts:
            return

        if not self._recompute_unread(contact_cache_key_value):
            return

        # Re-sort and rebuild the list
        self._sort_contacts()
        self._render_contact_list(self._filtered_contacts())

    # ─── Reply-to (quote) handling ───────────────────────────────────────────

    def _update_reply_bar(self):
        """Show or hide the reply bar based on ``self._reply_to``."""
        bar = self.query_one("#reply-bar", Horizontal)
        text_widget = self.query_one("#reply-text", Static)
        if self._reply_to:
            reply_text = self._reply_to.get("text", "")
            # Truncate long messages for display
            if len(reply_text) > 60:
                reply_text = reply_text[:57] + "..."
            text_widget.update(f"↩️ Replying to: {reply_text}")
            bar.remove_class("reply-bar-hidden")
            bar.styles.display = "block"
        else:
            text_widget.update("")
            bar.add_class("reply-bar-hidden")
            bar.styles.display = "none"

    def _cancel_reply(self):
        """Cancel the current reply selection."""
        # Deselect the previously selected widget
        if self._reply_to is not None:
            prev_widget = self._reply_to.get("_widget")
            if prev_widget is not None:
                try:
                    prev_widget.set_selected(False)
                except Exception:
                    pass
        self._reply_to = None
        self._update_reply_bar()

    def on_message_widget_message_clicked(
        self, event: MessageWidget.MessageClicked
    ):
        """Handle ``MessageClicked`` from a ``MessageWidget``.

        If download mode is active, serve the message text as a .txt file
        for download.  Otherwise toggles reply selection on the clicked
        message.
        """
        if self._download_mode:
            # In download mode: serve the message text as a downloadable file
            self._start_download(
                text=event.text,
                attachment_id=None,
                timestamp=event.timestamp,
            )
            return

        # If clicking the same message, cancel the reply
        if (
            self._reply_to is not None
            and self._reply_to.get("timestamp") == event.timestamp
        ):
            self._cancel_reply()
            return

        # Deselect the previously selected widget
        if self._reply_to is not None:
            prev_widget = self._reply_to.get("_widget")
            if prev_widget is not None:
                try:
                    prev_widget.set_selected(False)
                except Exception:
                    pass

        # Store the new reply target
        self._reply_to = {
            "text": event.text,
            "timestamp": event.timestamp,
            "sender": event.sender,
            "is_mine": event.is_mine,
        }

        # Highlight the clicked widget (find it by timestamp in the chat log)
        chat_log = self.chat_log
        for child in chat_log.children:
            if isinstance(child, MessageWidget) and child._msg_timestamp == event.timestamp:
                child.set_selected(True)
                self._reply_to["_widget"] = child
                break

        self._update_reply_bar()

        # Return focus to the message input so the user can start typing
        # the reply immediately.
        try:
            self.query_one("#message-input", Input).focus()
        except Exception:
            pass

    # ─── Download mode (Ctrl+D) ──────────────────────────────────────────────

    def action_download_mode(self) -> None:
        """Toggle download mode on/off (Ctrl+D).

        When active, clicking a message will serve it for download via a
        temporary HTTP server instead of replying (for text) or opening
        the image modal (for images).
        """
        self._download_mode = not self._download_mode
        self._update_download_bar()

    def _update_download_bar(self) -> None:
        """Show or hide the download mode hint in the reply bar."""
        bar = self.query_one("#reply-bar", Horizontal)
        text_widget = self.query_one("#reply-text", Static)
        if self._download_mode:
            text_widget.update("📥 Download mode — Click a message to download")
            bar.remove_class("reply-bar-hidden")
            bar.styles.display = "block"
        elif not self._reply_to:
            text_widget.update("")
            bar.add_class("reply-bar-hidden")
            bar.styles.display = "none"

    def _start_download(
        self,
        text: str,
        attachment_id: str | None = None,
        timestamp: int = 0,
        protocol: str | None = None,
    ) -> None:
        """Start a temporary HTTP server to serve the message content.

        If ``attachment_id`` is provided and the protocol backend resolves
        a local file, that file is served.  Otherwise the message text is
        written to a .txt file and served.

        A clickable ``DownloadLinkWidget`` is mounted in the chat log.
        """
        from backend import _serve_file_path
        if attachment_id:
            resolved = self.manager.get_attachment_path(
                protocol or PROTOCOL_SIGNAL, attachment_id
            )
            if resolved is not None and resolved.is_file():
                url = _serve_file_path(resolved)
            else:
                url = f"ERROR: Attachment file not found (id={attachment_id[:80]})"
        else:
            # Serve the message text as a .txt file
            # Use timestamp to create a unique filename
            fname = f"signal-message-{timestamp}.txt" if timestamp else "message.txt"
            url = serve_text_as_file(text, filename=fname)

        if url.startswith("ERROR:"):
            self._add_message(f"❌ {url}", is_info=True)
        else:
            # Mount a clickable download link widget
            chat_log = self.chat_log
            widget = DownloadLinkWidget(url)
            chat_log.mount(widget)
            chat_log.scroll_end(animate=False)

        # Exit download mode after serving
        self._download_mode = False
        self._update_download_bar()

    def on_download_link_widget_url_copied(
        self, event: DownloadLinkWidget.URLCopied
    ) -> None:
        """Handle ``URLCopied`` from a ``DownloadLinkWidget``.

        Shows a confirmation message in the chat log.
        """
        self._add_message(
            "📋 URL ready — select it above and press Cmd+C / Ctrl+C to copy",
            is_info=True,
        )

    # ─── Image modal ─────────────────────────────────────────────────────────

    def on_image_widget_image_clicked(self, event: ImageWidget.ImageClicked):
        """Handle ``ImageClicked`` from an ``ImageWidget``.

        If the path is not yet resolved, does a lazy lookup via the
        ``BackendManager`` (works for both Signal and WhatsApp).  In download
        mode the file is served via HTTP; otherwise a fullscreen
        ``ImageModalScreen`` renders it via ``catimg``.
        """
        att_path = event.attachment_path
        # Lazy resolution: when loaded from cache the path is not yet known,
        # but the attachment_id is.  Use the selected contact's protocol to
        # route the lookup through the correct backend.
        if att_path is None and event.attachment_id:
            protocol = self.selected_contact.protocol if self.selected_contact else None
            if protocol:
                att_path = self.manager.get_attachment_path(protocol, event.attachment_id)

        if self._download_mode:
            text = att_path.name if att_path else "attachment"
            protocol = self.selected_contact.protocol if self.selected_contact else None
            self._start_download(
                text=text,
                attachment_id=event.attachment_id,
                protocol=protocol,
            )
            return
        if att_path:
            self.push_screen(ImageModalScreen(att_path))
        else:
            self._add_message("❌ Image file not found on server", is_info=True)

    # ─── Sending messages ─────────────────────────────────────────────────────

    def on_input_submitted(self, event: Input.Submitted):
        """Send a message when the user presses Enter.
        Also converts any :emoji: aliases in the message.
        If emoji completion is visible, insert the selected emoji instead."""
        # If emoji completion is visible, insert the selected emoji
        if self._is_completion_visible():
            self._insert_emoji_from_completion()
            return

        if not self.selected_contact:
            self._add_message("❌ Select a contact first!", is_info=True)
            return

        # Hide completion if visible
        try:
            completion = self.query_one("#emoji-completion", EmojiCompletionWidget)
            completion.hide_suggestions()
        except Exception:
            pass

        # Convert emoji aliases (e.g. :smile: → 😊)
        message = replace_emoji_aliases(event.value.strip())

        if not message:
            return

        contact = self.selected_contact
        contact_id = contact.id
        cache_key = contact.cache_key
        ts = int(time.time() * 1000)

        # Capture reply data before clearing it
        reply_data = self._reply_to
        quote_text = reply_data.get("text") if reply_data else None

        # Save to SQLite (incremental INSERT), protocol-aware.
        data = {
            "text": message,
            "is_mine": True,
            "sender": "You",
            "timestamp": ts,
            "quote_text": quote_text,
            "msg_type": "text",
            "attachment_info": None,
            "attachment_id": None,
        }
        # Ingerisci l'ottimista nel backend CORRETTO del contatto (non hardcoded
        # su signal_backend): per WhatsApp deve finire nel WhatsAppBackend.cache,
        # altrimenti il polling dei messaggi non lo riconosce e ri-accoderebbe
        # l'echo -> messaggio mostrato DOPPIO (che si sistemava solo rientrando).
        ingest_backend = self.manager.get(contact.protocol)
        if ingest_backend is None:
            ingest_backend = self.signal_backend
        ingest_backend.ingest_message(contact_id, data, ts)

        # Update in-memory cache for UI
        if cache_key not in self._cache:
            self._cache[cache_key] = []
        self._cache[cache_key].append({
            "text": message,
            "is_mine": True,
            "sender": "You",
            "timestamp": ts,
            "quote_text": quote_text,
            "msg_type": "text",
            "attachment_info": None,
            "attachment_id": None,
            "read": True,
            "status": "sent",
        })


        # Show the message in the UI immediately (with quote if replying)

        self._add_message(
            message,
            is_mine=True,
            quote_text=quote_text,
            timestamp=ts,
            sender="You",
            status="sent",
        )
        self._seen_timestamps.add((contact.protocol, cache_key, ts))
        self._seen_message_ids.add((contact.protocol, cache_key, int(ts), message))

        event.input.value = ""

        # Cancel the reply highlight
        self._cancel_reply()

        self.run_worker(
            lambda msg=message, ts=ts, rdata=reply_data: self._send_message_worker(msg, ts, rdata),
            exclusive=False,
            thread=True,
        )

    def _send_message_worker(self, message: str, timestamp: int, reply_data: dict | None = None):
        """Send a message via the active backend's send path.

        Parameters
        ----------
        message:
            The message text to send.
        timestamp:
            The client-generated timestamp (ms) used as the message ID.
            Passed to the backend so that receipt timestamps match.
        reply_data:
            If provided, the message is sent as a quote/reply.
        """
        if not self.selected_contact:
            return

        contact = self.selected_contact

        # Extract quote parameters from reply_data
        # quote_author MUST be a contact id, not a display name.
        # We always use the selected contact's id because we are
        # replying to the person we are chatting with.
        quote_timestamp = reply_data.get("timestamp") if reply_data else None
        quote_author = contact.id if reply_data else None
        quote_message = reply_data.get("text") if reply_data else None

        # Send synchronously through the selected contact's backend.  This is
        # a sync call running in a worker thread; it is NOT an async coroutine
        # that needs awaiting, which would otherwise be silently dropped.
        backend = self.manager.get(contact.protocol)
        if backend is None:
            self.call_from_thread(
                self._add_message,
                f"❌ No backend for protocol: {contact.protocol}",
                is_info=True,
            )
            return

        try:
            backend.send_message_sync(
                contact.id,
                message,
                quote_timestamp=quote_timestamp,
                quote_author=quote_author,
                quote_message=quote_message,
            )
        except Exception as e:
            self.call_from_thread(
                self._add_message,
                f"❌ Send error: {e}",
                is_info=True,
            )


if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler("/tmp/signal-tui.log", mode="w")],
    )
    import signal as signal_module

    if not _acquire_lock():
        print("❌ Signal TUI is already running (lock file /tmp/signal-tui.lock).", file=sys.stderr)
        print("   If you're sure it's not running, delete the lock file and try again.", file=sys.stderr)
        sys.exit(1)

    app = SignalTUI()

    def _handle_sigint(sig, frame):
        """Handle Ctrl+C: stop polling and exit cleanly."""
        app._polling_active = False
        app.exit()

    signal_module.signal(signal_module.SIGINT, _handle_sigint)
    app.run()
    _release_lock()
