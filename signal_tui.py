"""
Signal TUI Client — Textual interface integrated with signal-cli via JSON-RPC.
Uses signal-cli daemon over HTTP (localhost) for fast operations (milliseconds).
If the daemon is unavailable, falls back to subprocess (slower but works).
Messages are saved in a local cache for persistence across sessions.
"""

import asyncio
import logging
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import ClassVar, Optional


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


from backend import (
    Contact,
    SignalRPCClient,
    _load_cache,
    _save_cache,
    _prune_cache,
    _is_daemon_running,
    _run_subprocess,
    _send_subprocess,
    get_attachment_path,
    SIGNAL_CLI_PATH,
    USER_NUMBER,
    DAEMON_HTTP_PORT,
)
from ui_components import (
    ContactListWidget,
    ChatAreaWidget,
    MessageWidget,
    ImageWidget,
    ImageModalScreen,
)
from emoji_picker import (
    EmojiPickerScreen,
    EmojiCompletionWidget,
    replace_emoji_aliases,
)


logger = logging.getLogger(__name__)


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

    #contact-list {
        height: 100%;
        border: solid $border;
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

    #chat-log {
        height: 1fr;
        border: solid $border;
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
        self.contacts: list[Contact] = []

        self.selected_contact: Optional[Contact] = None
        self.daemon_proc: Optional[subprocess.Popen] = None
        self.rpc: Optional[SignalRPCClient] = None
        self._use_daemon = False
        self._polling_active = False
        self._seen_timestamps: set[int] = set()
        self._unread_counts: dict[str, int] = {}
        self._cache: dict[str, list[dict]] = {}
        self._loaded_all = False
        self._reply_to: Optional[dict] = None  # message being replied to

    def compose(self):
        yield Header()
        yield Horizontal(
            ContactListWidget(),
            ChatAreaWidget(),
        )
        yield Footer()

    def on_mount(self):
        """On startup, start the daemon and load contacts."""
        self.run_worker(self._startup, exclusive=True, thread=True)

    def action_quit(self):
        """Ctrl+Q: stop polling and exit cleanly."""
        self._polling_active = False
        self.exit()

    def on_exit(self):
        """On exit, stop polling and do NOT kill the daemon."""
        self._polling_active = False

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
    ):
        """Add a message to the chat with correct alignment.

        For image messages, this method launches an async worker that
        renders the image inline via ``catimg``.  If rendering fails, a
        clickable fallback placeholder is shown instead.

        For text messages (not info), a clickable ``MessageWidget`` is
        used so the user can click to reply.
        """
        if text is None:
            text = ""

        chat_log = self.query_one("#chat-log", Vertical)

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
            )
            return

        # ── Non-image messages ──────────────────────────────────────────
        display_text = text
        if msg_type == "sticker":
            display_text = f"🎨 {text}" if text and text != "Media" else "🎨 [Sticker]"
        elif msg_type == "attachment":
            display_text = f"📎 {text}" if text and text != "Media" else "📎 [File]"

        if is_info:
            widget = Static(display_text, classes="msg-info")
        else:
            # Use clickable MessageWidget for all non-info messages
            widget = MessageWidget(
                text=display_text,
                timestamp=timestamp,
                sender=sender,
                is_mine=is_mine,
                classes="msg-right" if is_mine else "msg-left",
            )
        chat_log.mount(widget)
        chat_log.scroll_end(animate=False)

    def _render_image_in_chat(
        self,
        attachment_id: str | None,
        attachment_info: str,
        is_mine: bool,
        chat_log: Vertical,
    ):
        """Resolve the attachment path and mount a clickable placeholder
        ``ImageWidget``.

        The actual image rendering happens on-demand when the user presses
        Enter or clicks the widget, which opens a fullscreen modal.
        """
        # Resolve the file path
        att_path: Path | None = None
        if attachment_id:
            att_path = get_attachment_path(attachment_id)

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
        """Clear the chat."""
        chat_log = self.query_one("#chat-log", Vertical)
        chat_log.remove_children()

    # ─── Contact identification for envelope ────────────────────────────────

    def _identify_contact_for_envelope(self, envelope: dict) -> Optional[Contact]:
        """Identify which contact an envelope belongs to."""
        sync = envelope.get("syncMessage", {})
        sent = sync.get("sentMessage", {})
        if sent:
            dest = sent.get("destination", "")
            dest_number = sent.get("destinationNumber", "")
            dest_uuid = sent.get("destinationUuid", "")
            for contact in self.contacts:
                if dest == contact.number or dest_number == contact.number:
                    return contact
                if dest_uuid and contact.aci and dest_uuid == contact.aci:
                    return contact

        source = envelope.get("source", "")
        source_number = envelope.get("sourceNumber", "")
        source_uuid = envelope.get("sourceUuid", "")
        for contact in self.contacts:
            if source == contact.number or source_number == contact.number:
                return contact
            if source_uuid and contact.aci and source_uuid == contact.aci:
                return contact

        if sent:
            dest = sent.get("destination", "")
            for contact in self.contacts:
                if dest == contact.number:
                    return contact

        return None

    def _extract_message_data(self, envelope: dict) -> dict | None:
        """Extract message data from an envelope."""
        source_name = envelope.get("sourceName", "")
        source_number = envelope.get("sourceNumber", "") or envelope.get("source", "")

        def _classify_attachments(attachments: list) -> tuple[str, str, str | None]:
            """Classify attachments and return (msg_type, info, first_attachment_id)."""
            if not attachments:
                return ("text", None, None)
            for att in attachments:
                content_type = att.get("contentType", "") or ""
                fname = att.get("filename", "") or ""
                caption = att.get("caption", "") or ""
                att_id = att.get("id") or att.get("attachmentId") or None
                if content_type.startswith("image/"):
                    info = caption or f"Image: {fname}" if fname else "🖼️ Image"
                    return ("image", info, att_id)
                if content_type.startswith("video/"):
                    info = caption or f"Video: {fname}" if fname else "🎬 Video"
                    return ("attachment", info, att_id)
                if content_type.startswith("audio/"):
                    info = caption or f"Audio: {fname}" if fname else "🎵 Audio"
                    return ("attachment", info, att_id)
                info = caption or fname or content_type or "📎 File"
                return ("attachment", info, att_id)
            return ("attachment", "📎 File", None)

        def _extract_sticker(sticker: dict | None) -> tuple[str, str] | None:
            if not sticker:
                return None
            pack_id = sticker.get("packId", "")
            sticker_id = sticker.get("stickerId", "")
            info = f"Sticker #{sticker_id}"
            if pack_id:
                info = f"Sticker #{sticker_id} (pack:{pack_id[:8]}…)"
            return ("sticker", info)

        data_msg = envelope.get("dataMessage", {})
        if data_msg:
            text = data_msg.get("message", "") or ""
            sender = source_name or source_number
            quote = data_msg.get("quote", {})
            quote_text = quote.get("text", "") if quote else None

            sticker_data = _extract_sticker(data_msg.get("sticker"))
            if sticker_data:
                msg_type, att_info = sticker_data
                if not text:
                    text = att_info or "🎨 Sticker"
                return {
                    "sender": sender, "text": text, "is_mine": False,
                    "quote_text": quote_text, "msg_type": msg_type,
                    "attachment_info": att_info,
                }

            attachments = data_msg.get("attachments", [])
            msg_type, att_info, att_id = _classify_attachments(attachments)
            if not text and attachments:
                text = att_info or "Media"
            return {
                "sender": sender, "text": text, "is_mine": False,
                "quote_text": quote_text, "msg_type": msg_type,
                "attachment_info": att_info,
                "attachment_id": att_id,
            }

        sync = envelope.get("syncMessage", {})
        sent = sync.get("sentMessage", {})
        if sent:
            text = sent.get("message", "") or ""
            quote = sent.get("quote", {})
            quote_text = quote.get("text", "") if quote else None

            sticker_data = _extract_sticker(sent.get("sticker"))
            if sticker_data:
                msg_type, att_info = sticker_data
                if not text:
                    text = att_info or "🎨 Sticker"
                return {
                    "sender": "You", "text": text, "is_mine": True,
                    "quote_text": quote_text, "msg_type": msg_type,
                    "attachment_info": att_info,
                }

            attachments = sent.get("attachments", [])
            msg_type, att_info, att_id = _classify_attachments(attachments)
            if not text and attachments:
                text = att_info or "Media"
            return {
                "sender": "You", "text": text, "is_mine": True,
                "quote_text": quote_text, "msg_type": msg_type,
                "attachment_info": att_info,
                "attachment_id": att_id,
            }

        return None

    def _get_message_timestamp(self, envelope: dict) -> int:
        """Return the message timestamp."""
        ts = envelope.get("timestamp", 0)
        if not ts:
            data = envelope.get("dataMessage", {})
            ts = data.get("timestamp", 0)
        if not ts:
            sync = envelope.get("syncMessage", {})
            sent = sync.get("sentMessage", {})
            ts = sent.get("timestamp", 0)
        return ts

    # ─── Envelope processing ─────────────────────────────────────────────────

    def _process_envelope(self, envelope: dict) -> bool:
        """Process an envelope: identify the contact, save to cache.
        If the contact is currently selected, show the message immediately."""
        contact = self._identify_contact_for_envelope(envelope)
        if contact is None:
            return False

        ts = self._get_message_timestamp(envelope)
        data = self._extract_message_data(envelope)
        if data is None:
            return False

        if contact.number not in self._cache:
            self._cache[contact.number] = []
        self._cache[contact.number].append({
            "text": data["text"],
            "is_mine": data["is_mine"],
            "sender": data["sender"],
            "timestamp": ts,
            "quote_text": data["quote_text"],
            "msg_type": data["msg_type"],
            "attachment_info": data["attachment_info"],
            "attachment_id": data.get("attachment_id"),
            "read": data["is_mine"],
        })
        _save_cache(self._cache)
        _prune_cache()
        self._cache = _load_cache()

        # If it's the current contact, show the message in the UI immediately
        if self.selected_contact and contact.number == self.selected_contact.number:
            if ts and ts not in self._seen_timestamps:
                self._seen_timestamps.add(ts)
                self.call_from_thread(
                    self._add_message,
                    data["text"],
                    is_mine=data["is_mine"],
                    quote_text=data["quote_text"],
                    msg_type=data["msg_type"],
                    attachment_info=data["attachment_info"],
                    attachment_id=data.get("attachment_id"),
                    timestamp=ts,
                    sender=data.get("sender", ""),
                )
        else:
            # Message for another contact: update unread badge
            self.call_from_thread(self._update_unread_badges)

        return True

    # ─── Startup ────────────────────────────────────────────────────────────

    def _startup(self):
        """Start signal-cli daemon and load contacts."""
        self._cache = _load_cache()
        _prune_cache()
        self._cache = _load_cache()  # reload after prune

        self.call_from_thread(self._add_message, "⏳ Starting signal-cli daemon...", is_info=True)
        self.rpc = SignalRPCClient()

        if _is_daemon_running():
            self._use_daemon = True
            self.call_from_thread(
                self._add_message, "✅ Daemon already active, connecting directly...", is_info=True
            )
            self._load_contacts_rpc()

            self._polling_active = True
            self.run_worker(self._poll_worker, exclusive=True, thread=True)
            return

        self.call_from_thread(
            self._add_message, "⏳ Starting signal-cli daemon...", is_info=True
        )

        self.daemon_proc = subprocess.Popen(
            [
                str(SIGNAL_CLI_PATH),
                "-u", USER_NUMBER,
                "daemon",
                "--http", f"127.0.0.1:{DAEMON_HTTP_PORT}",
                "--receive-mode", "on-connection",
                "--no-receive-stdout",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        for _ in range(15):
            try:
                test = self.rpc._call("listContacts")
                if "result" in test:
                    self._use_daemon = True
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            self.call_from_thread(
                self._add_message,
                "❌ Daemon not available. Using subprocess mode (slower).",
                is_info=True,
            )
            self._use_daemon = False
            self._load_contacts_subprocess()
            return

        self._load_contacts_rpc()

        self._polling_active = True
        self.run_worker(self._poll_worker, exclusive=True, thread=True)

    def _load_contacts_rpc(self):
        """Load contacts via JSON-RPC (daemon already active)."""
        self.call_from_thread(
            self._add_message, "⏳ Loading contacts...", is_info=True
        )

        contacts_data = self.rpc.list_contacts()
        if isinstance(contacts_data, list) and len(contacts_data) > 0:
            self._parse_and_update_contacts(contacts_data)
        else:
            self.call_from_thread(
                self._add_message,
                "⚠️ RPC returned no contacts. Trying subprocess...",
                is_info=True,
            )
            self._load_contacts_subprocess()

    def _load_contacts_subprocess(self):
        """Load contacts via subprocess (fallback)."""
        self.call_from_thread(
            self._add_message, "⏳ Loading contacts (subprocess)...", is_info=True
        )

        try:
            output = _run_subprocess(["listContacts"])
            contacts = self._parse_contacts_from_output(output)
            self.contacts = contacts
            self.call_from_thread(self._update_contacts_ui, contacts)
        except Exception as e:
            self.call_from_thread(
                self._add_message,
                f"❌ Error loading contacts: {e}",
                is_info=True,
            )

    def _parse_contacts_from_output(self, output: str) -> list[Contact]:
        """Parse the output of 'signal-cli listContacts'."""
        contacts = []
        for line in output.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split()
            number = ""
            name = ""
            aci = ""
            for i, p in enumerate(parts):
                if p.startswith("Number:"):
                    number = p.split(":", 1)[1].strip()
                elif p.startswith("Name:"):
                    name = p.split(":", 1)[1].strip()
                elif p.startswith("ACI:"):
                    aci_val = p.split(":", 1)[1].strip()
                    if aci_val and aci_val != "-":
                        aci = aci_val
                elif p == "Profile" and i + 1 < len(parts) and parts[i + 1].startswith("name:"):
                    profile_name = parts[i + 1].split(":", 1)[1].strip()
                    if profile_name and not name:
                        name = profile_name
            if number:
                contacts.append(Contact(number=number, name=name, aci=aci))
        return contacts

    def _parse_and_update_contacts(self, contacts_data: list[dict]):
        """Parse contact data and update the UI."""
        contacts = []
        for c in contacts_data:
            number = c.get("number", "")
            name = (
                c.get("name")
                or c.get("givenName")
                or (c.get("profile") or {}).get("givenName")
                or number
            )
            aci = c.get("uuid", "") or c.get("aci", "")
            contacts.append(Contact(number=number, name=name, aci=aci))

        self.contacts = contacts
        self.call_from_thread(self._update_contacts_ui, contacts)

    def _sort_contacts(self):
        """Sort contacts: unread first, then alphabetical."""
        self.contacts.sort(
            key=lambda c: (
                -self._unread_counts.get(c.number, 0),
                c.display_name.lower(),
            )
        )

    def _update_contacts_ui(self, contacts: list[Contact]):
        """Update the UI with the contact list."""
        self._sort_contacts()
        contact_list = self.query_one("#contact-list", ListView)
        contact_list.clear()
        for c in self.contacts:
            contact_list.append(ListItem(Label(f"📱 {c.display_name}")))

        self._add_message(f"✅ Loaded {len(contacts)} contacts.", is_info=True)
        self._add_message("💡 Select a contact to view chat", is_info=True)

        self._update_unread_badges()

    # ─── Contact selection ─────────────────────────────────────────────────

    def on_list_view_selected(self, event: ListView.Selected):
        """When a contact is selected, show the chat."""
        index = self.query_one("#contact-list", ListView).index
        if index is not None and 0 <= index < len(self.contacts):
            self.selected_contact = self.contacts[index]
            self._seen_timestamps.clear()
            self._clear_chat()
            self._add_message(
                f"📱 Chat with: {self.selected_contact.display_name}", is_info=True
            )
            self._add_message(self.selected_contact.number, is_info=True)
            self._add_message("─" * 40, is_info=True)

            self.run_worker(
                self._load_messages_worker, exclusive=True, thread=True
            )

            # Final refresh: fetch messages that arrived during loading
            self._refresh_chat()

            # Mark all messages from this contact as read
            number = self.selected_contact.number
            if number in self._cache:
                for msg in self._cache[number]:
                    if not msg.get("read", True):
                        msg["read"] = True
                _save_cache(self._cache)
                _prune_cache()
                self._cache = _load_cache()
            self._unread_counts[number] = 0

            # Force label update to remove *N badge
            contact_list = self.query_one("#contact-list", ListView)
            item = contact_list.children[index]
            item.children[0].update(f"📱 {self.selected_contact.display_name}")

    # ─── Message logic ────────────────────────────────────────────────────

    def _load_messages_worker(self):
        """Load messages: last 20 from cache.
        If there are more than 20 messages, show a widget to load the rest."""
        if not self.selected_contact:
            return

        contact = self.selected_contact
        self._loaded_all = False

        cached = self._cache.get(contact.number, [])
        total = len(cached)

        if cached:
            if total > 20:
                messages_to_show = cached[-20:]
                self.call_from_thread(self._add_load_more_widget, total - 20)
            else:
                messages_to_show = cached
                self._loaded_all = True

            for msg in messages_to_show:
                text = msg.get("text", "")
                is_mine = msg.get("is_mine", False)
                quote_text = msg.get("quote_text")
                ts = msg.get("timestamp", 0)
                msg_type = msg.get("msg_type", "text")
                attachment_info = msg.get("attachment_info")
                attachment_id = msg.get("attachment_id")
                sender = msg.get("sender", "")

                if ts:
                    self._seen_timestamps.add(ts)

                self.call_from_thread(
                    self._add_message,
                    text,
                    is_mine=is_mine,
                    quote_text=quote_text,
                    msg_type=msg_type,
                    attachment_info=attachment_info,
                    attachment_id=attachment_id,
                    timestamp=ts,
                    sender=sender,
                )

            self.call_from_thread(
                self._add_message,
                f"📋 Loaded {len(messages_to_show)}/{total} messages",
                is_info=True,
            )
        else:
            self._loaded_all = True
            self.call_from_thread(
                self._add_message, "No message history for this contact", is_info=True
            )

        self.call_from_thread(self._add_message, "✅ Ready", is_info=True)

    def _add_load_more_widget(self, remaining: int):
        """Add a clickable widget to load older messages."""
        chat_log = self.query_one("#chat-log", Vertical)
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

        self.push_screen(EmojiPickerScreen(), _on_emoji_selected)

    def action_open_emoji_picker(self) -> None:
        """Action to open emoji picker (bound to Ctrl+E)."""
        self._open_emoji_picker()

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
        cached = self._cache.get(contact.number, [])

        self._clear_chat()
        self._seen_timestamps.clear()

        for msg in cached:
            text = msg.get("text", "")
            is_mine = msg.get("is_mine", False)
            quote_text = msg.get("quote_text")
            ts = msg.get("timestamp", 0)
            msg_type = msg.get("msg_type", "text")
            attachment_info = msg.get("attachment_info")
            attachment_id = msg.get("attachment_id")
            sender = msg.get("sender", "")

            if ts:
                self._seen_timestamps.add(ts)

            self._add_message(
                text,
                is_mine=is_mine,
                quote_text=quote_text,
                msg_type=msg_type,
                attachment_info=attachment_info,
                attachment_id=attachment_id,
                timestamp=ts,
                sender=sender,
            )

        self._loaded_all = True
        self._add_message(f"📋 Loaded all {len(cached)} messages", is_info=True)

    def _poll_worker(self):
        """Thread worker that polls every 1 second.
        Processes ALL incoming messages and saves them to cache.
        Starts ONCE in _startup() and lives for the entire app lifetime."""
        while self._polling_active:
            if self._use_daemon and self.rpc:
                try:
                    messages = self.rpc.receive()
                    for msg in messages:
                        envelope = msg.get("envelope", {})
                        self._process_envelope(envelope)
                except Exception:
                    pass
            for _ in range(10):
                if not self._polling_active:
                    return
                time.sleep(0.1)

    def _refresh_chat(self):
        """Check cache for new messages of the current contact not yet shown."""
        if not self.selected_contact:
            return

        contact = self.selected_contact
        cached = self._cache.get(contact.number, [])
        new_count = 0

        for msg in cached:
            ts = msg.get("timestamp", 0)
            if ts and ts not in self._seen_timestamps:
                self._seen_timestamps.add(ts)
                text = msg.get("text", "")
                is_mine = msg.get("is_mine", False)
                quote_text = msg.get("quote_text")
                msg_type = msg.get("msg_type", "text")
                attachment_info = msg.get("attachment_info")
                attachment_id = msg.get("attachment_id")
                sender = msg.get("sender", "")
                self._add_message(
                    text,
                    is_mine=is_mine,
                    quote_text=quote_text,
                    msg_type=msg_type,
                    attachment_info=attachment_info,
                    attachment_id=attachment_id,
                    timestamp=ts,
                    sender=sender,
                )
                new_count += 1

        if new_count > 0:
            chat_log = self.query_one("#chat-log", Vertical)
            chat_log.scroll_end(animate=False)

    def _update_unread_badges(self):
        """Check the in-memory cache and update *N badges on contacts.
        If counts change, re-sort the list and rebuild it."""
        if not self.contacts:
            return

        changed = False
        for contact in self.contacts:
            messages = self._cache.get(contact.number, [])
            unread = sum(
                1 for m in messages
                if not m.get("is_mine") and not m.get("read", True)
            )
            old = self._unread_counts.get(contact.number, 0)
            if unread != old:
                self._unread_counts[contact.number] = unread
                changed = True

        if not changed:
            return

        # Re-sort and rebuild the list
        self._sort_contacts()
        contact_list = self.query_one("#contact-list", ListView)
        contact_list.clear()
        for c in self.contacts:
            label = f"📱 {c.display_name}"
            unread = self._unread_counts.get(c.number, 0)
            if unread > 0 and c != self.selected_contact:
                label += f" *{unread}"
            contact_list.append(ListItem(Label(label)))

        # Restore selection on the current contact (if present)
        if self.selected_contact and self.selected_contact in self.contacts:
            contact_list.index = self.contacts.index(self.selected_contact)

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

        Toggles reply selection on the clicked message.
        """
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
        chat_log = self.query_one("#chat-log", Vertical)
        for child in chat_log.children:
            if isinstance(child, MessageWidget) and child._msg_timestamp == event.timestamp:
                child.set_selected(True)
                self._reply_to["_widget"] = child
                break

        self._update_reply_bar()

    # ─── Image modal ─────────────────────────────────────────────────────────

    def on_image_widget_image_clicked(self, event: ImageWidget.ImageClicked):
        """Handle ``ImageClicked`` from an ``ImageWidget``.

        Opens a fullscreen ``ImageModalScreen`` that renders the image
        via ``viu`` asynchronously.
        """
        self.push_screen(ImageModalScreen(event.attachment_path))

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

        number = self.selected_contact.number
        ts = int(time.time() * 1000)

        # Capture reply data before clearing it
        reply_data = self._reply_to
        quote_text = reply_data.get("text") if reply_data else None

        if number not in self._cache:
            self._cache[number] = []
        self._cache[number].append({
            "text": message,
            "is_mine": True,
            "sender": "You",
            "timestamp": ts,
            "quote_text": quote_text,
            "msg_type": "text",
            "attachment_info": None,
            "attachment_id": None,
            "read": True,
        })
        _save_cache(self._cache)
        _prune_cache()
        self._cache = _load_cache()

        # Show the message in the UI immediately (with quote if replying)
        self._add_message(
            message,
            is_mine=True,
            quote_text=quote_text,
            timestamp=ts,
            sender="You",
        )

        event.input.value = ""

        # Cancel the reply highlight
        self._cancel_reply()

        self.run_worker(
            lambda msg=message, rdata=reply_data: self._send_message_worker(msg, rdata),
            exclusive=False,
            thread=True,
        )

    def _send_message_worker(self, message: str, reply_data: dict | None = None):
        """Send a message (via RPC or subprocess fallback).

        If ``reply_data`` is provided, the message is sent as a quote/reply
        to the original message.
        """
        if not self.selected_contact:
            return

        # Extract quote parameters from reply_data
        # quote_author MUST be a phone number, not a display name.
        # We always use the selected contact's number because we are
        # replying to the person we are chatting with.
        quote_timestamp = reply_data.get("timestamp") if reply_data else None
        quote_author = self.selected_contact.number if reply_data else None
        quote_message = reply_data.get("text") if reply_data else None

        if self._use_daemon and self.rpc:
            result = self.rpc.send_message(
                message,
                self.selected_contact.number,
                quote_timestamp=quote_timestamp,
                quote_author=quote_author,
                quote_message=quote_message,
            )
            if "error" in result:
                self.call_from_thread(
                    self._add_message,
                    f"❌ Send error: {result['error']}",
                    is_info=True,
                )
        else:
            try:
                _send_subprocess(
                    message,
                    self.selected_contact.number,
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
    import signal as signal_module

    app = SignalTUI()

    def _handle_sigint(sig, frame):
        """Handle Ctrl+C: stop polling and exit cleanly."""
        app._polling_active = False
        app.exit()

    signal_module.signal(signal_module.SIGINT, _handle_sigint)
    app.run()
