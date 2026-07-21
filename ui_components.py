"""
Custom widgets for Signal TUI Client.
Contains reusable UI components based on Textual.
"""

from pathlib import Path

from rich.text import Text as RichText
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Label, ListView, Input, Static


class ContactListWidget(Vertical):
    """Left column: contact list."""

    def compose(self):
        yield Label("📇 Contacts", classes="section-title")
        yield ListView(id="contact-list")

    def on_mount(self):
        self.styles.width = 30


class ChatAreaWidget(Vertical):
    """Right column: messages area + input."""

    def compose(self):
        yield Label("💬 Chat", classes="section-title")
        yield Vertical(id="chat-log")
        yield Input(placeholder="Type a message...", id="message-input")


class ImageWidget(Static):
    """A clickable, focusable widget that displays an ANSI-rendered image
    or a fallback text placeholder.

    When the user presses Enter or clicks on this widget, it emits an
    ``ImageClicked`` message carrying the attachment path so the parent
    can open a fullscreen modal.
    """

    class ImageClicked(Message):
        """Posted when the user activates this image widget."""

        def __init__(self, attachment_path: Path) -> None:
            super().__init__()
            self.attachment_path = attachment_path

    def __init__(
        self,
        attachment_path: Path | None,
        attachment_id: str = "",
        rendered: str | None = None,
        fallback_text: str = "[🖼️ Image Attachment: Click Enter to View]",
    ) -> None:
        """Initialise the image widget.

        Parameters
        ----------
        attachment_path:
            Resolved path to the attachment file on disk, or None if the
            file could not be located.
        attachment_id:
            The raw signal-cli attachment UUID (for reference / logging).
        rendered:
            ANSI-encoded image string from ``catimg``.  If provided it will
            be parsed via ``Rich.Text.from_ansi()`` for display.
        fallback_text:
            Plain-text fallback shown when *rendered* is empty or None.
        """
        self.attachment_path = attachment_path
        self.attachment_id = attachment_id

        if rendered:
            display = RichText.from_ansi(rendered)
        else:
            display = fallback_text

        super().__init__(display)
        self.can_focus = True

    def on_click(self) -> None:
        """Mouse click → emit ``ImageClicked``."""
        if self.attachment_path:
            self.post_message(self.ImageClicked(self.attachment_path))

    def on_focus(self) -> None:
        """Visual feedback when focused."""
        self.styles.border = ("solid", "#4ebf71")

    def on_blur(self) -> None:
        """Remove focus border."""
        self.styles.border = None

    def key_enter(self) -> None:
        """Enter key → emit ``ImageClicked``."""
        if self.attachment_path:
            self.post_message(self.ImageClicked(self.attachment_path))


class ImageModalScreen(ModalScreen):
    """Fullscreen modal that displays a larger version of an image rendered
    via ``catimg``.

    Dismiss with ``Escape`` or ``q``.
    """

    def __init__(self, ansi_data: str) -> None:
        super().__init__()
        self._ansi_data = ansi_data

    def compose(self):
        yield Static(RichText.from_ansi(self._ansi_data), id="modal-image")
        yield Static("Press Escape or q to close", id="modal-hint")

    def on_mount(self) -> None:
        """Centre the image on screen."""
        img = self.query_one("#modal-image", Static)
        img.styles.width = "80%"
        img.styles.height = "80%"
        img.styles.margin = (1, 2)
        hint = self.query_one("#modal-hint", Static)
        hint.styles.text_align = "center"
        hint.styles.color = "#888888"
        hint.styles.margin = (0, 2)

    def key_escape(self) -> None:
        self.dismiss()

    def key_q(self) -> None:
        self.dismiss()
