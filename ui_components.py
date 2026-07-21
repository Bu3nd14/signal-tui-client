"""
Custom widgets for Signal TUI Client.
Contains reusable UI components based on Textual.
"""

import asyncio
import logging
from pathlib import Path

from rich.text import Text as RichText
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Label, ListView, Input, Static, RichLog

logger = logging.getLogger(__name__)

# Debug log per dimensioni immagine nella modale
_DEBUG_LOG = Path("./debug_image.log")

def _log_debug(msg: str) -> None:
    """Append a line to the debug log file."""
    try:
        with _DEBUG_LOG.open("a") as f:
            f.write(msg + "\n")
    except Exception:
        pass


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
    """A clickable, focusable widget that displays a text placeholder for an
    image attachment.

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

        _log_debug(
            f"[_start_image_render] RichLog region={img.region} "
            f"available_cols={available_cols} "
            f"catimg_pixels={catimg_pixels}"
        )

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
                "-w", str(self._catimg_pixels),
                str(self._attachment_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=30.0
            )

            if proc.returncode != 0:
                raise RuntimeError(
                    f"catimg exited with code {proc.returncode}: "
                    f"{stderr.decode().strip()}"
                )

            ansi_output = stdout.decode("utf-8", errors="replace")

            # Log catimg output stats
            lines = ansi_output.splitlines()
            max_line_len = max((len(l) for l in lines), default=0)
            _log_debug(
                f"[_render_image] catimg -w {self._catimg_pixels} → "
                f"{len(lines)} lines, max width {max_line_len} chars"
            )

        except (FileNotFoundError, ProcessLookupError):
            logger.warning("catimg not found — cannot render image in modal")
            img.write("⚠️ catimg is not installed on this system.")
            return
        except asyncio.TimeoutError:
            logger.warning("catimg timed out")
            img.write("⚠️ Image rendering timed out.")
            return
        except Exception as exc:
            logger.warning("modal image rendering failed: %s", exc)
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
