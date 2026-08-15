"""Download mode (Ctrl+D): serve text/attachments via HTTP."""

import logging

from textual.containers import Horizontal
from textual.widgets import Static

from backend import (
    serve_text_as_file,
)
from models import (
    PROTOCOL_SIGNAL,
)
from ui_components import (
    DownloadLinkWidget,
    ImageModalScreen,
    ImageWidget,
)

logger = logging.getLogger(__name__)


class DownloadModeMixin:
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
            self._status(f"❌ {url}")
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
        self._status(
            "📋 URL ready — select it above and press Cmd+C / Ctrl+C to copy",
        )

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
                att_path = self.manager.get_attachment_path(
                    protocol, event.attachment_id
                )

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
            self._status("❌ Image file not found on server")
