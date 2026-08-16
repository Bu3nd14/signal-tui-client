"""SignalTUI main App — composes the functional mixins and owns app lifecycle."""

import logging
from typing import ClassVar

from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.timer import Timer
from textual.widgets import (
    Button,
    Footer,
    Header,
    ListItem,
    Static,
)

from backends import (
    BackendManager,
    SignalBackend,
    TelegramBackend,
    WhatsAppBackend,
)
from backends.config import telegram_enabled, whatsapp_enabled
from models import (
    ChatContact,
)
from tui.backend_connect import BackendConnectMixin
from tui.chat_view import ChatViewMixin
from tui.contacts import ContactListMixin
from tui.css import APP_CSS
from tui.download import DownloadModeMixin
from tui.events import EventHandlingMixin
from tui.pickers import PickerMixin
from tui.polling import PollingMixin
from tui.send import SendMixin
from tui.unread_reply import UnreadReplyMixin
from ui_components import (
    ChatAreaWidget,
    ContactListWidget,
)

logger = logging.getLogger(__name__)


class SignalTUI(
    App,
    ChatViewMixin,
    EventHandlingMixin,
    ContactListMixin,
    BackendConnectMixin,
    PollingMixin,
    SendMixin,
    UnreadReplyMixin,
    DownloadModeMixin,
    PickerMixin,
):
    """Main Signal TUI App with JSON-RPC daemon over HTTP."""

    CSS = APP_CSS

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

    def __init__(self):
        super().__init__()
        # Multi-protocol backend manager + the always-registered Signal backend.
        self.manager = BackendManager()
        self.signal_backend = SignalBackend()
        self.manager.register(self.signal_backend)

        # WhatsApp backend is registered only when configured (env/config.json);
        # otherwise it's skipped gracefully and the Signal TUI keeps working.
        self.whatsapp_backend: WhatsAppBackend | None = None
        if whatsapp_enabled():
            self.whatsapp_backend = WhatsAppBackend()
            self.manager.register(self.whatsapp_backend)

        # Telegram backend is registered only when credentials are configured;
        # otherwise it's skipped gracefully.
        self.telegram_backend: TelegramBackend | None = None
        if telegram_enabled():
            self.telegram_backend = TelegramBackend()
            self.manager.register(self.telegram_backend)

        self.contacts: list[ChatContact] = []
        self.selected_contact: ChatContact | None = None

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
        self._cache: dict[str, list[dict]] = {}  # keyed by contact_cache_key
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

        self._reply_to: dict | None = None  # message being replied to
        self._download_mode = False  # Ctrl+D download mode active
        self._typing_contacts: dict[
            str, float
        ] = {}  # contact cache_key → time of last typing STARTED

        self._typing_mumbling: dict[
            str, float
        ] = {}  # contact cache_key → mumbling expiry
        self._TYPING_TIMEOUT = 10.0  # seconds before a typing indicator auto-expires
        self._TYPING_MUMBLING_DURATION = (
            60.0  # seconds a contact stays with 💭 after stopping typing
        )

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

        # WhatsApp link guard: prevents duplicate concurrent connect workers.
        self._wa_connecting: bool = False

        # Telegram link guard: prevents duplicate concurrent connect workers
        # (two Ctrl+L→Esc in a row used to race on the shared client state).
        self._tg_connecting: bool = False

        # Backends currently connecting (pending a ready/failure report).
        # Populated as each connect worker starts and drained when the backend
        # reports (ready OR failed); when empty, the startup auto-selection runs.
        self._pending_backends: set[str] = set()

        # Status bar auto-clear timer.
        self._status_timer: Timer | None = None

    def compose(self):
        yield Header()
        yield Horizontal(
            ContactListWidget(),
            ChatAreaWidget(),
        )
        with Horizontal(id="bottom-bar"):
            yield Footer()
            yield Static("", id="status-bar")

    def on_mount(self):
        """On startup, start poll worker and backend connections in parallel."""
        self._chat_log = self.query_one("#chat-log", Vertical)
        # Start poll worker immediately — Signal and WhatsApp events flow
        # as soon as their backends are ready (independent workers below).
        self._polling_active = True
        self.run_worker(self._poll_worker, exclusive=True, thread=True)
        # Only connect backends that are already linked (skip slow daemon
        # startup for unlinked accounts — they connect after Ctrl+L link).
        self.run_worker(self._connect_signal, exclusive=False, thread=True)
        if (
            self.whatsapp_backend is not None
            and not self.whatsapp_backend.needs_pairing
            # Only auto-connect at boot if the session is truly WORKING
            # (not just "not pairing" — could be failed/stopped).
            and self.whatsapp_backend.is_working
        ):
            self.run_worker(self._connect_whatsapp, exclusive=False, thread=True)
        if (
            self.telegram_backend is not None
            and not self.telegram_backend.needs_pairing
        ):
            self.run_worker(self._connect_telegram, exclusive=False, thread=True)

    def action_quit(self):
        """Ctrl+Q: stop polling and exit cleanly."""
        self._polling_active = False
        self.exit()

    def on_exit(self):
        """On exit, stop polling and disconnect backends."""
        self._polling_active = False
        if self.telegram_backend is not None:
            try:
                self.telegram_backend.disconnect_sync()
            except Exception as _e:
                logger.debug("Telegram disconnect on exit failed", exc_info=True)
        # No flush needed — SQLite writes are incremental

    def _status(self, text: str, duration: float = 3.0) -> None:
        """Update the status bar (bottom-right, thread-safe).

        Clears automatically after *duration* seconds (0 = persistent).
        New messages cancel the previous auto-clear timer.
        """
        try:
            self.query_one("#status-bar", Static).update(text)
            if self._status_timer is not None:
                self._status_timer.stop()
                self._status_timer = None
            # Schedule the auto-clear timer only if the message pump is running.
            # In unit tests the app is instantiated without an event loop, so
            # set_timer() would raise "no running event loop" and leak a
            # coroutine (RuntimeWarning: 'Timer._run_timer' was never awaited).
            if duration > 0 and self.is_running:
                self._status_timer = self.set_timer(duration, self._status_clear)
        except Exception as _e:
            logger.debug("Failed to update status bar", exc_info=True)

    def _status_clear(self) -> None:
        """Clear the status bar."""
        try:
            self.query_one("#status-bar", Static).update("")
            self._status_timer = None
        except Exception as _e:
            logger.debug("Failed to clear status bar", exc_info=True)

    def on_button_pressed(self, event: Button.Pressed):
        """When the user clicks a button."""
        if event.button.id == "load-more-msg":
            self._load_all_messages()
        elif event.button.id == "reply-cancel":
            self._cancel_reply()
        elif event.button.id == "emoji-btn":
            self._open_emoji_picker()
