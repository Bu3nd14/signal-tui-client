"""
Custom widgets for Signal TUI Client.
Contains reusable UI components based on Textual.
"""

import asyncio
import logging
from pathlib import Path
from typing import ClassVar

from rich.text import Text as RichText
from textual import events
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
    TextArea,
)

from emoji_picker import EmojiCompletionWidget

logger = logging.getLogger(__name__)


class MessageTextArea(TextArea):
    """Compact message editor with send-on-Enter semantics."""

    class Submitted(Message):
        """Posted when the user submits the message with Enter."""

        def __init__(self, text_area: "MessageTextArea") -> None:
            super().__init__()
            self.text_area = text_area
            self.value = text_area.text

    BINDINGS: ClassVar[list] = [
        *TextArea.BINDINGS,
        ("shift+enter,ctrl+j,ctrl+enter", "insert_newline", "New line"),
    ]

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self))
            return
        await super()._on_key(event)

    def action_insert_newline(self) -> None:
        result = self.insert("\n")
        self.move_cursor(result.end_location)

    async def _on_paste(self, event: events.Paste) -> None:
        if self.read_only:
            return
        text = event.text.replace("\r\n", "\n").replace("\r", "\n")
        if result := self._replace_via_keyboard(text, *self.selection):
            self.move_cursor(result.end_location)
            self.focus()

    def action_paste(self) -> None:
        if self.read_only:
            return
        text = self.app.clipboard.replace("\r\n", "\n").replace("\r", "\n")
        if result := self._replace_via_keyboard(text, *self.selection):
            self.move_cursor(result.end_location)

    def insert_at_cursor(self, text: str) -> None:
        result = self.insert(text)
        self.move_cursor(result.end_location)

    def replace_completion(self, emoji_char: str) -> None:
        line, column = self.cursor_location
        lines = self.text.split("\n")
        before_cursor = "\n".join(lines[:line])
        if line:
            before_cursor += "\n"
        before_cursor += lines[line][:column]
        alias_start = before_cursor.rfind(":")
        if alias_start < 0:
            return
        start_line = before_cursor.count("\n", 0, alias_start)
        start_column = alias_start - (before_cursor.rfind("\n", 0, alias_start) + 1)
        result = self.replace(
            emoji_char + " ", (start_line, start_column), (line, column)
        )
        self.move_cursor(result.end_location)


class ContactListView(ListView):
    """ListView per la lista contatti.

    ``ALLOW_SELECT = False`` disabilita la selezione di testo con il mouse che
    Textual prova a gestire a ogni ``MouseDown``.  Con la lista contatti oggi
    ricostruita spesso (poll ~1s), quella selezione (che usa
    ``content_widget.parent.region``) poteva scattare su un elemento già
    smontato -> ``AttributeError: 'NoneType' object has no attribute 'region'``.
    La selezione di *riga* (``ListView.index``) resta attiva.
    """

    ALLOW_SELECT = False

    BINDINGS: ClassVar[list] = [
        Binding("space", "toggle_group", show=False),
    ]

    # NB: a ogni MouseDown lo Screen di Textual mette a fuoco automaticamente
    # il primo widget focusable sotto il cursore, se `focus_on_click()` è True
    # (default `Widget.FOCUS_ON_CLICK = True`).  Qui lo disabilitiamo: la lista
    # contatti non deve rubare il focus all'input quando ci si clicca sopra,
    # altrimenti il bordo dell'input lampeggia (input→ListView→input).
    FOCUS_ON_CLICK = False

    def action_toggle_group(self) -> None:
        """Space: toggle the highlighted group header (members are untouched)."""
        item = self.highlighted_child
        if item is None or getattr(item, "_row_kind", "member") != "group":
            return
        group_key = getattr(item, "_group_key", None)
        if group_key is None:
            return
        app = self.app
        if app is not None and hasattr(app, "_toggle_group"):
            app._toggle_group(group_key)

    def _on_list_item__child_clicked(self, event: ListItem._ChildClicked) -> None:
        """Gestisci il click su un elemento senza crash se è stato rimosso.

        La lista contatti viene ricostruita in modo non-distruttivo (update
        in-place), ma nel breve intervallo di un update un ``ListItem`` può non
        essere più figlio dell'albero quando il click viene elaborato: Textual
        farebbe ``self._nodes.index(event.item)`` e solleverebbe ValueError.
        Qui ignoriamo il click su un item ormai estraneo (l'utente ricliccherà).

        NB: NON chiamiamo ``self.focus()`` (a differenza del default Textual):
        ``_select_contact`` riporta comunque subito il focus sull'input, quindi
        un ``focus()`` qui causerebbe solo un giro input→ListView→input che fa
        lampeggiare il bordo dell'input.
        """
        event.stop()
        # Suppress the base ListView._on_list_item__child_clicked: Textual's
        # naming-convention dispatch invokes BOTH this override and the parent's
        # handler (which would re-focus and post a SECOND ``Selected``).  A
        # single click must emit exactly one ``Selected`` (a duplicate would
        # double-toggle a group header).
        event.prevent_default()
        try:
            index = self._nodes.index(event.item)
        except ValueError:
            return  # item già rimosso/ricostruito: ignora il click
        self.index = index
        self.post_message(self.Selected(self, event.item, index))


class ContactListWidget(Vertical):
    """Left column: contact list."""

    def compose(self):
        yield Label("📇 Contacts", classes="section-title", id="ContactsTitle")
        yield ContactListView(id="contact-list")

    def on_mount(self):
        self.styles.width = 30


class ChatAreaWidget(Vertical):
    """Right column: messages area + reply bar + input."""

    def compose(self):
        yield Label("💬 Chat", classes="section-title", id="ChatTitle")
        yield Vertical(id="chat-log")
        yield Horizontal(
            Static("", id="reply-text"),
            Button("✕", id="reply-cancel", classes="reply-cancel-btn"),
            id="reply-bar",
            classes="reply-bar-hidden",
        )
        yield EmojiCompletionWidget(id="emoji-completion")

        yield Horizontal(
            Button("😊", id="emoji-btn", classes="emoji-toggle-btn"),
            MessageTextArea(
                placeholder="Type a message...",
                id="message-input",
            ),
            id="input-row",
        )


class MessageWidget(Static):
    """A clickable, focusable widget that displays a text message.

    When the user clicks on this widget, it emits a ``MessageClicked``
    message carrying the message data so the parent can set it as the
    message to reply to.

    The widget can be visually toggled as "selected" (the message being
    replied to) via ``set_selected()``.

    For messages sent by the current user (``is_mine=True``), the widget
    supports three visual statuses via ``set_status()``:

    - ``"sent"`` → *italic* (message sent but not yet delivered)
    - ``"delivered"`` → **bold** (message delivered to recipient's device)
    - ``"read"`` → normal (message read by the recipient)
    """

    class MessageClicked(Message):
        """Posted when the user clicks this message widget."""

        def __init__(
            self,
            text: str,
            timestamp: int,
            sender: str,
            is_mine: bool,
            status: str,
            message_id: str | None = None,
        ) -> None:
            super().__init__()
            self.text = text
            self.timestamp = timestamp
            self.sender = sender
            self.is_mine = is_mine
            self.status = status
            self.message_id = message_id

    class EditRequested(Message):
        """Posted on Alt+click / Alt+e: request to edit this (own) message."""

        def __init__(
            self, text, timestamp, sender, is_mine, status, message_id=None
        ) -> None:
            super().__init__()
            self.text = text
            self.timestamp = timestamp
            self.sender = sender
            self.is_mine = is_mine
            self.status = status
            self.message_id = message_id

    BINDINGS: ClassVar[list] = [
        Binding("alt+e", "request_edit", "Edit message", show=False),
    ]

    def __init__(
        self,
        text: str,
        timestamp: int = 0,
        sender: str = "",
        is_mine: bool = False,
        classes: str = "",
        status: str = "sent",
        protocol: str = "",
        sender_color: str | None = None,
        message_id: str | None = None,
        edited: bool = False,
    ) -> None:
        """Initialise the message widget.

        Parameters
        ----------
        text:
            The message text to display.
        timestamp:
            Unix timestamp (ms) of the message.
        sender:
            Display name / number of the sender.
        is_mine:
            Whether this message was sent by the current user.
        classes:
            CSS classes to apply (e.g. "msg-left" or "msg-right").
        status:
            Delivery status for sent messages: "sent", "delivered", or "read".
            Defaults to "sent".
        protocol:
            Source protocol ("signal", "whatsapp", ...).  Stored for future
            per-protocol styling (color accents); defaults to "".
        sender_color:
            Optional color for the sender name prefix (e.g. "#DAA520").

            When set and ``sender`` is non-empty, the message is rendered as
            ``<sender:> text`` with the sender name in the given color.
        """
        self._msg_text = text
        self._msg_timestamp = timestamp
        self._msg_sender = sender
        self._msg_is_mine = is_mine
        self._selected = False
        self._status = status
        self._message_id = message_id
        self._protocol = protocol
        self._sender_color = sender_color
        self._edited = edited

        super().__init__(self._build_content(), markup=False, classes=classes)
        self.can_focus = True
        self._apply_status_style()
        self.set_class(status == "pending", "msg-pending")
        self.set_class(status == "failed", "msg-failed")
        self._apply_protocol_accent()

    _PROTOCOL_ACCENT: ClassVar[dict[str, str]] = {
        "signal": "msg-signal",
        "whatsapp": "msg-whatsapp",
    }

    def _remove_protocol_accent(self) -> None:
        """Remove any protocol accent class from the widget."""
        for cls in self._PROTOCOL_ACCENT.values():
            if self.has_class(cls):
                self.remove_class(cls)

    def _apply_protocol_accent(self) -> None:
        """Apply a subtle CSS accent for the message's protocol.

        Toggling a CSS class keeps the widget's inline borders free for the
        reply/focus highlight and is easy to style and test.  When not selected
        the accent is applied; when selected the reply highlight wins.
        """
        accent = self._PROTOCOL_ACCENT.get(self._protocol)
        self._remove_protocol_accent()
        if accent and not self._selected:
            self.add_class(accent)

    def _apply_status_style(self) -> None:
        """Apply the CSS text style based on the current status.

        Only applies to messages sent by the current user (is_mine=True).
        """
        if not self._msg_is_mine:
            return

        if self._status == "sent":
            self.styles.text_style = "italic"
        elif self._status == "pending":
            self.styles.text_style = "dim"
        elif self._status in ("failed", "delivered"):
            self.styles.text_style = "bold"
        elif self._status == "read":
            self.styles.text_style = "none"

    def set_status(self, status: str) -> None:
        """Update the delivery status and refresh the visual style.

        Parameters
        ----------
        status:
            New status: "sent", "delivered", or "read".
        """
        self._status = status
        self.set_class(status == "pending", "msg-pending")
        self.set_class(status == "failed", "msg-failed")
        self._apply_status_style()
        self.refresh()

    def set_selected(self, selected: bool) -> None:
        """Toggle the visual "selected" state (reply highlight)."""
        self._selected = selected
        if selected:
            # Selection/focus uses a clear full border; drop the accent so the
            # reply highlight is unambiguous.
            self.styles.border = ("solid", "#4ebf71")
            self._remove_protocol_accent()
        else:
            self.styles.border = None
            self._apply_protocol_accent()

    def on_click(self, event: events.Click | None = None) -> None:
        """Mouse click → Alt+click edits, otherwise emit ``MessageClicked``."""
        if event is not None and event.meta:
            self.post_message(
                self.EditRequested(
                    text=self._msg_text,
                    timestamp=self._msg_timestamp,
                    sender=self._msg_sender,
                    is_mine=self._msg_is_mine,
                    status=self._status,
                    message_id=self._message_id,
                )
            )
            return
        self.post_message(
            self.MessageClicked(
                text=self._msg_text,
                timestamp=self._msg_timestamp,
                sender=self._msg_sender,
                is_mine=self._msg_is_mine,
                status=self._status,
                message_id=self._message_id,
            )
        )

    def action_request_edit(self) -> None:
        """Alt+e → emit ``EditRequested``."""
        self.post_message(
            self.EditRequested(
                text=self._msg_text,
                timestamp=self._msg_timestamp,
                sender=self._msg_sender,
                is_mine=self._msg_is_mine,
                status=self._status,
                message_id=self._message_id,
            )
        )

    def _build_content(self):
        text = self._msg_text + (" (modificato)" if self._edited else "")
        if self._sender_color and self._msg_sender:
            rt = RichText()
            rt.append(f"<{self._msg_sender}:> ", style=self._sender_color)
            rt.append(text)
            return rt
        return text

    def update_text(self, new_text: str, edited: bool = True) -> None:
        """Rewrite the bubble text in place (no unmount/remount)."""
        self._msg_text = new_text
        self._edited = edited
        self.update(self._build_content())
        self.refresh()

    def on_focus(self) -> None:
        """Visual feedback when focused."""
        if not self._selected:
            self.styles.border = ("solid", "#4ebf71")
            self._remove_protocol_accent()

    def on_blur(self) -> None:
        """Remove focus border if not in selected state."""
        if not self._selected:
            self.styles.border = None
            self._apply_protocol_accent()

    def key_enter(self) -> None:
        """Enter key → emit ``MessageClicked``."""
        self.post_message(
            self.MessageClicked(
                text=self._msg_text,
                timestamp=self._msg_timestamp,
                sender=self._msg_sender,
                is_mine=self._msg_is_mine,
                status=self._status,
                message_id=self._message_id,
            )
        )


class ImageWidget(Static):
    """A clickable, focusable widget that displays a text placeholder for an
    image attachment.

    When the user presses Enter or clicks on this widget, it emits an
    ``ImageClicked`` message carrying the attachment path so the parent
    can open a fullscreen modal.
    """

    class ImageClicked(Message):
        """Posted when the user activates this image widget."""

        def __init__(
            self, attachment_path: Path | None, attachment_id: str = ""
        ) -> None:
            super().__init__()
            self.attachment_path = attachment_path
            self.attachment_id = attachment_id

    def __init__(
        self,
        attachment_path: Path | None,
        attachment_id: str = "",
        fallback_text: str = "[🖼️ Image: Click Enter to View]",
    ) -> None:
        """Initialise the image widget.

        Parameters
        ----------
        attachment_path:
            Resolved path to the attachment file on disk, or None if the
            file could not be located.
        attachment_id:
            The raw signal-cli attachment UUID (for reference / logging).
        fallback_text:
            Plain-text placeholder shown in the chat.
        """
        self.attachment_path = attachment_path
        self.attachment_id = attachment_id

        super().__init__(fallback_text, markup=False)
        self.can_focus = True

    def on_click(self) -> None:
        """Mouse click → emit ``ImageClicked``."""
        if self.attachment_path or self.attachment_id:
            self.post_message(
                self.ImageClicked(self.attachment_path, self.attachment_id)
            )

    def update_attachment(
        self, attachment_path: Path | None, fallback_text: str
    ) -> None:
        """Update the resolved attachment path and refresh the placeholder text."""
        self.attachment_path = attachment_path
        self.update(fallback_text)

    def on_focus(self) -> None:
        """Visual feedback when focused."""
        self.styles.border = ("solid", "#4ebf71")

    def on_blur(self) -> None:
        """Remove focus border."""
        self.styles.border = None

    def key_enter(self) -> None:
        """Enter key → emit ``ImageClicked``."""
        if self.attachment_path or self.attachment_id:
            self.post_message(
                self.ImageClicked(self.attachment_path, self.attachment_id)
            )


class ImageModalScreen(ModalScreen):
    """Fullscreen modal that renders an image via ``catimg`` and displays it
    inside a scrollable ``RichLog`` widget.

    The image is rendered asynchronously so the UI stays responsive.
    Dismiss with ``Escape`` or ``q``.
    """

    def __init__(self, attachment_path: Path) -> None:
        super().__init__()
        self._attachment_path = attachment_path

    def compose(self):
        yield RichLog(id="modal-image", highlight=True, markup=False, wrap=False)
        yield Static("Press Escape or q to close", id="modal-hint")

    def on_mount(self) -> None:
        """Set up widget styles on mount.

        Rendering is deferred via ``call_after_refresh`` so that the
        RichLog has final layout dimensions before we read its height.
        """
        img = self.query_one("#modal-image", RichLog)
        img.styles.width = "100%"
        img.styles.height = "1fr"
        img.styles.margin = (1, 0)
        hint = self.query_one("#modal-hint", Static)
        hint.styles.text_align = "center"
        hint.styles.color = "#888888"
        hint.styles.margin = (0, 2)

        # Defer rendering until after the next layout pass, when
        # widget regions are guaranteed to have non-zero dimensions.
        self.call_after_refresh(self._start_image_render)

    def _start_image_render(self) -> None:
        """Called after layout is complete — widget regions are now valid."""
        img = self.query_one("#modal-image", RichLog)
        # region.width is in character columns; subtract 2 for side margins.
        # catimg -w expects *pixels*, not columns.  Each half-block
        # character (▄) covers 2 pixels horizontally, so we multiply
        # by 2 to fill the available width.
        available_cols = max(40, img.region.width - 2)
        catimg_pixels = available_cols * 2

        self._catimg_pixels = catimg_pixels
        self.run_worker(self._render_image(), exclusive=False)

    async def _render_image(self) -> None:
        """Async worker that spawns ``catimg``, captures its ANSI output,
        and writes it into the ``RichLog`` widget line by line.

        Falls back gracefully if ``catimg`` fails or is not installed.
        """
        img = self.query_one("#modal-image", RichLog)

        try:
            proc = await asyncio.create_subprocess_exec(
                "catimg",
                "-w",
                str(self._catimg_pixels),
                str(self._attachment_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)

            if proc.returncode != 0:
                raise RuntimeError(
                    f"catimg exited with code {proc.returncode}: "
                    f"{stderr.decode().strip()}"
                )

            ansi_output = stdout.decode("utf-8", errors="replace")

        except (FileNotFoundError, ProcessLookupError):
            img.write("⚠️ catimg is not installed on this system.")
            return
        except TimeoutError:
            img.write("⚠️ Image rendering timed out.")
            return
        except Exception as exc:  # noqa: BLE001
            img.write(f"⚠️ Could not render image: {exc}")
            return

        # Convert ANSI → RichText, then write into RichLog.
        # RichLog with markup=False does not interpret ANSI codes directly,
        # so we parse them via RichText.from_ansi() first.
        img.write(RichText.from_ansi(ansi_output))

    def key_escape(self) -> None:
        self.dismiss()

    def key_q(self) -> None:
        self.dismiss()


class DownloadLinkWidget(Static):
    """A widget that displays a download URL in a readonly ``Input`` field.

    The URL is displayed in a selectable ``Input`` widget so the user can
    copy it with **Cmd+C / Ctrl+C** (or right-click → Copy) on any
    terminal — Mac, Windows, Linux, SSH, etc.  No special terminal
    features required.
    """

    class URLCopied(Message):
        """Posted when the URL has been copied to the clipboard."""

        def __init__(self, url: str) -> None:
            super().__init__()
            self.url = url

    def __init__(self, url: str, label: str = "📥 Download") -> None:
        """Initialise the download link widget.

        Parameters
        ----------
        url:
            The full download URL to display and copy.
        label:
            Optional label prefix (default ``📥 Download``).
        """
        self._url = url
        self._label = label
        super().__init__()
        self.can_focus = True

    def compose(self):
        yield Input(value=self._url, id="download-url-input")

    def on_mount(self) -> None:
        """Set a border and label on the container."""
        self.styles.border = ("solid", "#4ebf71")
        self.styles.padding = (0, 1)
        self.styles.margin = (0, 0, 0, 0)
        self.border_title = self._label

    def on_focus(self) -> None:
        """When the container gets focus, pass it to the Input and select all."""
        inp = self.query_one("#download-url-input", Input)
        inp.focus()
        inp.select_all()
