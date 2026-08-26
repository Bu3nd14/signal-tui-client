"""SignalTUI main App — composes the functional mixins and owns app lifecycle."""

import logging
import threading
from typing import ClassVar

from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import (
    Button,
    Footer,
    Header,
    ListItem,
)

from backends import (
    BackendManager,
    SignalBackend,
    TelegramBackend,
    WhatsAppBackend,
)
from backends.config import telegram_enabled, whatsapp_enabled
from contact_picker import BackendChoiceScreen, ContactPickerScreen
from models import (
    PROTOCOL_SIGNAL,
    PROTOCOL_TELEGRAM,
    PROTOCOL_WHATSAPP,
    ChatContact,
    protocol_emoji,
)
from tui.backend_connect import BackendConnectMixin
from tui.chat_view import ChatViewMixin
from tui.contacts import ContactListMixin
from tui.css import APP_CSS
from tui.download import DownloadModeMixin
from tui.edit import EditMessageMixin
from tui.events import EventHandlingMixin
from tui.images.cellsize import get_cell_size_ioctl
from tui.images.detect import ImageSupport
from tui.images.kitty_renderer import KittyRenderer, compute_source_rect
from tui.pickers import PickerMixin
from tui.polling import PollingMixin
from tui.send import SendMixin
from tui.unread_reply import UnreadReplyMixin
from ui_components import (
    ChatAreaWidget,
    ContactListWidget,
    ImageWidget,
    QuoteWidget,
    StatusBar,
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
    EditMessageMixin,
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
        Binding("ctrl+u", "toggle_unread_filter", "Unread", priority=True),
        Binding("ctrl+a", "go_to_all", show=False, priority=True),
        Binding("ctrl+l", "open_device_link", "Link", priority=True),
        Binding("ctrl+n", "next_suggestion", "Next", show=False),
        Binding("ctrl+p", "prev_suggestion", "Prev", show=False),
    ]
    # Disable Textual's built-in command palette (Ctrl+P) to avoid conflict
    # with the emoji picker's Ctrl+P for previous category.
    ENABLE_COMMAND_PALETTE = False

    def __init__(
        self,
        image_support: ImageSupport = ImageSupport.CATIMG,
        *,
        initial_cell_size: tuple[int, int] | None = None,
        web_enabled: bool = False,
        web_port: int = 4242,
        web_host: str = "127.0.0.1",
        web_token: str = "",
    ):
        super().__init__()
        # Terminal image backend detected in ``signal_tui`` before ``run()``.
        # Defaults to CATIMG so existing tests instantiating ``SignalTUI()``
        # with no arguments keep the current (placeholder + catimg) behaviour.
        self.image_support = image_support
        # Cell size measured BEFORE ``run()`` (P2): the CSI fallback can only run
        # safely outside the app (no Textual key-thread racing for stdin).
        self._initial_cell_size = initial_cell_size
        self._web_enabled = web_enabled
        self._web_port = web_port
        self._web_host = web_host
        self._web_token = web_token
        self._web_server = None

        # Native kitty image rendering state (phase 2).  The renderer is created
        # in ``on_mount`` when the terminal supports it; placements are reconciled
        # against the frame in ``post_display_hook``.
        self._native_renderer: KittyRenderer | None = None
        # image_id → last placed (row, col, x_src, y_src, w_px, h_px) key, used
        # to skip no-op re-emissions during scroll.
        self._native_last_key: dict[int, tuple] = {}
        # Chat image ids (P3): the screen-stack gate deletes only these, never
        # the modal's own placement.
        self._chat_native_ids: set[int] = set()
        self._screen_stack_cleared = False
        self._native_image_counter = 0
        # Concurrency gate for attachment path resolution + thumbnail prepare.
        self._image_resolve_semaphore = threading.Semaphore(4)
        # Dedicated gate for quote thumbnails (P3): they are small/fast and must
        # not starve behind slow image downloads (WAHA/tgref up to 30s).
        self._quote_resolve_semaphore = threading.Semaphore(2)
        # Window load anchor: token + pending-native-worker counter, used to
        # scroll to the bottom exactly once when the cache-window thumbnails
        # have all finished growing (see ChatViewMixin).
        self._window_native_token = 0
        self._window_native_pending = 0

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

        # Unread-only filter (toggled with Ctrl+U): orthogonal to the protocol
        # filter; only contacts with at least one unread message are shown.
        self._unread_only: bool = False

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
        # Incremented on every contact-picker open/close; an address-book worker
        # checks it to detect a stale fetch after the picker was dismissed.
        self._address_book_token = 0
        # Identities of real messages already mounted in the current chat log,
        # used as a render-level de-dup safety net so _refresh_chat and the
        # load workers never mount the same message twice.
        # Chiave = (protocol, cache_key, timestamp, testo): il timestamp da solo
        # (granularità al secondo) non basta — due messaggi WhatsApp distinti
        # nello stesso secondo verrebbero scartati (il secondo non compariva).
        self._shown_in_log: set[tuple[str, str, int, str]] = set()

        self._reply_to: dict | None = None  # message being replied to
        self._editing_message: dict | None = None  # message being edited
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

        # Contact grouping (Sprint 2): group headers, member→group mapping, and
        # the set of EXPANDED groups.  ``_expanded_groups`` starts EMPTY so every
        # group is COLLAPSED at startup (only headers visible); expansion is
        # opt-in per group via toggle (Enter/click/Space on the header).
        self._group_widgets: dict[str, ListItem] = {}  # group_key → header row
        self._member_to_group: dict[str, str] = {}  # cache_key → group_key
        self._expanded_groups: set[str] = set()  # groups currently expanded
        self._group_members: dict[str, list[str]] = {}  # group_key → member cache_keys

        # Progressive render state (avoids UI freeze at startup / Ctrl+W).
        self._pending_rows: list = []
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
        # True while a transient or persistent status message is on display.
        self._status_active: bool = False

    def compose(self):
        yield Header()
        yield Horizontal(
            ContactListWidget(),
            ChatAreaWidget(),
        )
        with Horizontal(id="bottom-bar"):
            yield Footer()
            yield StatusBar(id="status-bar")

    def on_mount(self):
        """On startup, start poll worker and backend connections in parallel."""
        self._chat_log = self.query_one("#chat-log", Vertical)
        if self._web_enabled:
            from web.server import start_web_server

            self._web_server = start_web_server(
                self.manager, self._web_port, self._web_token, host=self._web_host
            )
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

        self._init_native_renderer()

    def _init_native_renderer(self) -> None:
        """Set up the kitty renderer when the terminal supports it (R3).

        Prefers the cell size measured pre-run in ``signal_tui`` (P2); falls
        back to an ioctl-only detection on the driver's tty.  The CSI ``16 t``
        fallback never runs inside the app (it would race the key-thread for
        stdin).
        """
        if self.image_support is not ImageSupport.KITTY:
            return
        driver = self._driver
        if driver is None:
            return
        cell_size = self._initial_cell_size
        if cell_size is None:
            fd = getattr(driver, "fileno", None)
            if fd is None:
                return
            try:
                cell_size = get_cell_size_ioctl(fd)
            except Exception as _e:
                logger.debug("Cell-size detection failed", exc_info=True)
                return
        if cell_size is None:
            return
        cell_w, cell_h = cell_size
        self._native_renderer = KittyRenderer(
            write=driver.write, cell_w=cell_w, cell_h=cell_h
        )
        # Safety net: ``post_display_hook`` is the primary trigger; this low
        # frequency interval catches frames that slipped through (resize settle).
        if self.is_running:
            self.set_interval(0.25, self._native_sync_tick)

    def _next_native_image_id(self) -> int:
        """Return a new monotonic kitty image id (UI thread only)."""
        self._native_image_counter += 1
        return self._native_image_counter

    def post_display_hook(self) -> None:
        """Reconcile native image placements right after each frame flush."""
        self._native_sync_tick()

    def _native_sync_tick(self) -> None:
        """Gate + reconcile placements (shared by the hook and the timer)."""
        renderer = self._native_renderer
        if self.image_support is not ImageSupport.KITTY or renderer is None:
            return
        # C5 screen-stack gate: while a modal/picker sits on top, the chat
        # placements would bleed over it.  Drop ONLY the chat placements (P3:
        # per-id ``d=i``, keeping data) — never the modal's own placement.
        if self.screen is not self.default_screen:
            if not self._screen_stack_cleared:
                for image_id in list(self._chat_native_ids):
                    renderer.delete(image_id, keep_data=True)
                self._screen_stack_cleared = True
                self._native_last_key.clear()
            return
        # Back on the default screen: invalidate positions and re-emit.
        if self._screen_stack_cleared:
            self._screen_stack_cleared = False
            self._native_last_key.clear()
        self._sync_native_images()

    def _consume_pending_thumbnails(self) -> None:
        """Register thumbnails stashed while their widget was not yet mounted (P1).

        ``mount()`` is async, so a fast worker can hand its PNG to the UI thread
        before the widget's ``Mount`` event; ``_finish_*_thumbnail`` stashes the
        PNG on the widget and this hook (running after every frame) registers it
        once the widget is mounted.  Cleared by ``native_cleanup`` on unmount.
        """
        if self.image_support is not ImageSupport.KITTY:
            return
        if self._native_renderer is None:
            return
        for widget in self.query("ImageWidget, QuoteWidget"):
            if not getattr(widget, "is_mounted", False):
                continue
            if isinstance(widget, QuoteWidget):
                pending = getattr(widget, "_pending_quote_png", None)
                if pending is not None and widget.native_image_id is None:
                    widget._pending_quote_png = None
                    self._register_quote_thumbnail(widget, pending)
            else:
                pending = getattr(widget, "_pending_native_png", None)
                if pending is not None and widget.native_image_id is None:
                    widget._pending_native_png = None
                    path = getattr(widget, "_pending_native_path", None)
                    widget._pending_native_path = None
                    self._register_native_thumbnail(widget, path, pending)

    def _sync_native_images(self) -> None:
        """Place/delete kitty placements for every native image widget."""
        renderer = self._native_renderer
        if renderer is None:
            return
        self._consume_pending_thumbnails()
        chat_log = self._chat_log
        if chat_log is None:
            return
        try:
            container = chat_log.content_region
        except Exception as _e:
            logger.debug("Failed to read chat-log content region", exc_info=True)
            return
        cell_w = renderer.cell_w
        cell_h = renderer.cell_h
        chat_ids: set[int] = set()
        for widget in self.query("ImageWidget, QuoteWidget"):
            image_id = widget.native_image_id
            if image_id is None or not widget.visible:
                continue
            if widget.native_width_px is None:
                continue
            # For a QuoteWidget the native thumbnail is placed over its internal
            # thumbnail slot (not the container, which would cover the text).
            if isinstance(widget, QuoteWidget):
                region = widget.thumbnail_region()
                if region is None:
                    continue
            else:
                region = widget.content_region
            rect = compute_source_rect(
                region, container, cell_w, cell_h, widget.native_width_px
            )
            placement_id = image_id
            chat_ids.add(image_id)
            if rect is None:
                # Out of viewport: drop the placement but keep the data (d=i).
                if image_id in self._native_last_key:
                    renderer.delete(image_id, keep_data=True)
                    del self._native_last_key[image_id]
                continue
            row, col, x_src, y_src, w_px, h_px = rect
            # P4: right-align — the thumb must end at the content region's right
            # edge, matching the placeholder ``text-align: right``.  A QuoteWidget
            # carries its alignment in ``aligned_right`` (the class is applied to
            # the internal Static, not the container); an ImageWidget uses the
            # ``msg-right`` class on the widget itself.
            right_aligned = (
                widget.aligned_right
                if isinstance(widget, QuoteWidget)
                else widget.has_class("msg-right")
            )
            if right_aligned:
                image_cols = (w_px + cell_w - 1) // cell_w
                # For a QuoteWidget anchor to the CONTAINER's right edge: the
                # internal slot collapses when the placeholder text is hidden,
                # so thumbnail_region().right is no longer the bubble's right.
                anchor = (
                    widget.content_region if isinstance(widget, QuoteWidget) else region
                )
                col = anchor.right - image_cols + 1
            key = (image_id, placement_id, row, col, x_src, y_src, w_px, h_px)
            if self._native_last_key.get(image_id) != key:
                renderer.place(
                    image_id,
                    placement_id,
                    row=row,
                    col=col,
                    x_src=x_src,
                    y_src=y_src,
                    w_px=w_px,
                    h_px=h_px,
                )
                self._native_last_key[image_id] = key
        self._chat_native_ids = chat_ids

    def on_resize(self, event) -> None:
        """Re-detect cell size and force delayed re-emissions (kitty remap)."""
        if self.image_support is not ImageSupport.KITTY:
            return
        renderer = self._native_renderer
        if renderer is None:
            return
        driver = self._driver
        fd = getattr(driver, "fileno", None) if driver is not None else None
        if fd is not None:
            try:
                # P2: ioctl only — never the CSI fallback inside the app.  If the
                # terminal stops reporting pixels we keep the previous value.
                new_cell = get_cell_size_ioctl(fd)
            except Exception as _e:
                logger.debug("Cell-size re-detection failed", exc_info=True)
                new_cell = None
            if new_cell is not None and new_cell != (renderer.cell_w, renderer.cell_h):
                renderer.cell_w, renderer.cell_h = new_cell
                # P1: font-zoom changed the cell height — reflow the native
                # widget heights so they match the newly-detected cell size.
                self._reflow_native_widget_heights()
        self._native_last_key.clear()
        self.call_after_refresh(self._force_native_reemit)
        self.set_timer(0.1, self._force_native_reemit)
        self.set_timer(0.3, self._force_native_reemit)

    def _reflow_native_widget_heights(self) -> None:
        """Recompute native widget heights after a cell-size change (P1)."""
        renderer = self._native_renderer
        if renderer is None:
            return
        for widget in self.query(ImageWidget):
            if widget.native_height_px is not None:
                rows = max(
                    1,
                    (widget.native_height_px + renderer.cell_h - 1) // renderer.cell_h,
                )
                widget.styles.height = rows
                widget.refresh(layout=True)

    def _force_native_reemit(self) -> None:
        """Invalidate the placement cache and re-emit immediately."""
        if self.image_support is not ImageSupport.KITTY:
            return
        if self._native_renderer is None:
            return
        self._native_last_key.clear()
        # Go through the gate: a resize while a modal is open must NOT re-place
        # the chat images over it (the gate clears + suspends instead).
        self._native_sync_tick()

    def on_unmount(self) -> None:
        """On exit, free every kitty image and placement (d=A)."""
        renderer = self._native_renderer
        if renderer is None:
            return
        try:
            renderer.clear_all()
        except Exception as _e:
            logger.debug("Failed to clear kitty images on exit", exc_info=True)

    def action_quit(self):
        """Ctrl+Q: stop polling and exit cleanly."""
        self._polling_active = False
        self.exit()

    def action_toggle_unread_filter(self):
        """Ctrl+U: toggle the unread-only filter on the contact list."""
        self._unread_only = not self._unread_only
        self._apply_contact_filter()
        self._sync_status_segments()

    def action_go_to_all(self):
        """Ctrl+A (undocumented): reset to the "all" view (no filter, no unread)."""
        self._protocol_filter = "all"
        self._unread_only = False
        self._apply_contact_filter()
        self._sync_status_segments()

    def on_exit(self):
        """On exit, stop polling and disconnect backends."""
        self._polling_active = False
        if self._web_enabled:
            from web.server import stop_web_server

            stop_web_server(self._web_server)
        if self.telegram_backend is not None:
            try:
                self.telegram_backend.disconnect_sync()
            except Exception as _e:
                logger.debug("Telegram disconnect on exit failed", exc_info=True)
        # No flush needed — SQLite writes are incremental

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Gate the global Ctrl+W / Ctrl+U / Ctrl+A filters while a picker is open.

        Textual resolves *priority* bindings from the App down, so the app-level
        ``ctrl+w → cycle_protocol_filter`` binding would otherwise win over the
        picker's own ``ctrl+w → cycle_filter`` binding.  Returning ``False`` here
        (for those actions, while a picker screen is active) lets the modal
        binding win and keeps the main contact-list filter untouched.
        """
        if action in (
            "cycle_protocol_filter",
            "toggle_unread_filter",
            "go_to_all",
        ) and isinstance(self.screen, (ContactPickerScreen, BackendChoiceScreen)):
            return False
        return super().check_action(action, parameters)

    def _status(self, text: str, duration: float = 3.0) -> None:
        """Update the status bar (bottom-right, thread-safe).

        Clears automatically after *duration* seconds (0 = persistent).
        New messages cancel the previous auto-clear timer.
        """
        try:
            self.query_one("#status-bar").show_message(text)
            self._status_active = True
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

    def _backend_unread_total(self, protocol: str) -> int:
        """Sum of unread counts across all contacts of *protocol*."""
        return sum(
            self._unread_counts.get(c.cache_key, 0)
            for c in self.contacts
            if c.protocol == protocol
        )

    def _backend_totals(self) -> dict[str, int]:
        """Per-protocol unread totals in fixed Signal → WhatsApp → Telegram order."""
        return {
            PROTOCOL_SIGNAL: self._backend_unread_total(PROTOCOL_SIGNAL),
            PROTOCOL_WHATSAPP: self._backend_unread_total(PROTOCOL_WHATSAPP),
            PROTOCOL_TELEGRAM: self._backend_unread_total(PROTOCOL_TELEGRAM),
        }

    @staticmethod
    def _backend_unread_text(totals: dict[str, int]) -> str:
        """Format *totals* into the legacy status-bar text (pure, for tests)."""
        return (
            f"{protocol_emoji(PROTOCOL_SIGNAL)} {totals.get(PROTOCOL_SIGNAL, 0) or '-'}  "
            f"{protocol_emoji(PROTOCOL_WHATSAPP)} {totals.get(PROTOCOL_WHATSAPP, 0) or '-'}  "
            f"{protocol_emoji(PROTOCOL_TELEGRAM)} {totals.get(PROTOCOL_TELEGRAM, 0) or '-'}"
        )

    def _render_backend_unread_status(self) -> None:
        """Render the default per-backend unread totals into ``#status-bar``."""
        totals = self._backend_totals()
        try:
            self.query_one("#status-bar").set_counts(totals)
            self.query_one("#status-bar").show_default(totals)
        except Exception as _e:
            logger.debug("Failed to render backend unread status", exc_info=True)

    def _refresh_backend_status_if_idle(self) -> None:
        """Refresh the default status bar unless a message is on display."""
        if not self._status_active:
            self._render_backend_unread_status()

    def _status_clear(self) -> None:
        """Clear the status bar, restoring the default unread totals."""
        self._status_active = False
        self._status_timer = None
        self._render_backend_unread_status()

    def _sync_status_segments(self) -> None:
        """Sync the ``-active`` class on the status-bar segments with state."""
        try:
            self.query_one("#status-bar").sync_active(
                self._protocol_filter, self._unread_only
            )
        except Exception as _e:
            logger.debug("Failed to sync status segments", exc_info=True)

    def on_status_segment_pressed(self, event) -> None:
        """Handle a click on a per-protocol status-bar segment."""
        if isinstance(self.screen, ModalScreen):
            return
        self._activate_backend_unread(event.protocol)

    def _activate_backend_unread(self, protocol: str) -> None:
        """Set the protocol filter from a segment click (conditional unread).

        Re-clicking the active unread segment turns the unread view off (the
        protocol filter is kept).  Otherwise the protocol filter is set and the
        unread-only view is enabled only when that backend actually has unread
        (a backend with zero unread behaves like a plain Ctrl+W filter).
        """
        if self._protocol_filter == protocol and self._unread_only:
            self._unread_only = False
        else:
            self._protocol_filter = protocol
            self._unread_only = self._backend_unread_total(protocol) > 0
        self._apply_contact_filter()
        self._sync_status_segments()

    def on_button_pressed(self, event: Button.Pressed):
        """When the user clicks a button."""
        if event.button.id == "load-more-msg":
            self._load_all_messages()
        elif event.button.id == "reply-cancel":
            self._cancel_reply()
            self._cancel_edit()
        elif event.button.id == "emoji-btn":
            self._open_emoji_picker()
