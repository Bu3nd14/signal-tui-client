"""Download mode (Ctrl+D): serve text/attachments via HTTP."""

import logging
import shutil
import subprocess
import sys
from pathlib import Path

from textual.containers import Horizontal
from textual.widgets import Static

from models import (
    PROTOCOL_SIGNAL,
)
from server.client import ApiError
from tui.blobs import resolve_attachment_path
from tui.images.detect import ImageSupport
from ui_components import (
    DownloadLinkWidget,
    ImageModalScreen,
    ImageWidget,
    MessageWidget,
)

logger = logging.getLogger(__name__)


class DownloadModeMixin:
    _MEDIA_OPEN_STARTUP_TIMEOUT = 0.5

    def _open_media_path(self, path: Path) -> None:
        """Open a local media file with the platform desktop handler."""
        opener_name = "open" if sys.platform == "darwin" else "xdg-open"
        opener = shutil.which(opener_name)
        if opener is None:
            self._status(f"📎 File available at: {path}")
            return
        try:
            process = subprocess.Popen(
                [opener, str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.run_worker(
                lambda: self._check_media_opener_worker(process, path),
                thread=True,
                exclusive=False,
            )
        except OSError:
            logger.debug("Unable to launch media opener", exc_info=True)
            self._status(f"📎 File available at: {path}")

    def _check_media_opener_worker(self, process: subprocess.Popen, path: Path) -> None:
        """Report an opener that exits unsuccessfully during startup."""
        try:
            return_code = process.wait(timeout=self._MEDIA_OPEN_STARTUP_TIMEOUT)
        except subprocess.TimeoutExpired:
            return
        except OSError:
            logger.debug("Unable to wait for media opener", exc_info=True)
            self.call_from_thread(self._status, f"📎 File available at: {path}")
            return
        if return_code != 0:
            self.call_from_thread(self._status, f"📎 File available at: {path}")

    def on_message_widget_media_open_requested(
        self, event: MessageWidget.MediaOpenRequested
    ) -> None:
        """Resolve and open non-image media, or serve it in download mode."""
        if self._download_mode:
            if event.path is None and event.widget is not None:
                self.run_worker(
                    lambda: self._resolve_media_download_path_worker(
                        event.protocol, event.attachment_id, event.widget
                    ),
                    thread=True,
                    exclusive=False,
                )
                return
            if event.path is not None and event.widget is not None:
                event.widget.update_media_path(event.path)
            self._start_download(
                text=event.path.name if event.path else "attachment",
                attachment_id=event.attachment_id,
                protocol=event.protocol,
            )
            return
        if event.path is not None:
            if event.widget is not None:
                event.widget.update_media_path(event.path)
            self._open_media_path(event.path)
            return
        if event.widget is None:
            self._status("❌ Attachment file not found on server")
            return
        self.run_worker(
            lambda: self._resolve_media_path_worker(
                event.protocol, event.attachment_id, event.widget
            ),
            thread=True,
            exclusive=False,
        )

    def _resolve_media_download_path_worker(
        self,
        protocol: str,
        attachment_id: str,
        widget: MessageWidget,
    ) -> None:
        """Resolve non-image media for HTTP serving without blocking the UI."""
        path = resolve_attachment_path(self.client, protocol, attachment_id)
        self.call_from_thread(
            self._finish_media_download_path_resolve,
            protocol,
            attachment_id,
            widget,
            path,
        )

    def _finish_media_download_path_resolve(
        self,
        protocol: str,
        attachment_id: str,
        widget: MessageWidget,
        path: Path | None,
    ) -> None:
        """Cache resolved media on its widget and serve it when available."""
        widget.update_media_path(path)
        if not widget.is_mounted:
            return
        if path is None:
            self._status(
                f"❌ ERROR: Attachment file not found (id={attachment_id[:80]})"
            )
            self._download_mode = False
            self._update_download_bar()
            return
        self._start_download(
            text=path.name,
            attachment_id=attachment_id,
            protocol=protocol,
        )

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
        """Ask the server to serve the message content on its temporary HTTP server.

        If ``attachment_id`` is provided the server serves that attachment's
        file.  Otherwise the message text is written to a .txt file and served.

        A clickable ``DownloadLinkWidget`` is mounted in the chat log.  The
        request runs in a worker (a remote server adds a round trip); the
        widget is mounted, and download mode left, when the server answers.
        """
        if attachment_id:
            op = "download.serve_attachment"
            data = {
                "protocol": protocol or PROTOCOL_SIGNAL,
                "attachment_id": attachment_id,
            }
        else:
            # Use timestamp to create a unique filename
            fname = f"signal-message-{timestamp}.txt" if timestamp else "message.txt"
            op = "download.serve_text"
            data = {"text": text, "filename": fname}

        def _work() -> None:
            try:
                served = self._call_op(op, data)
            except ApiError as exc:
                self._ui(self._finish_download, None, exc)
                return
            self._ui(self._finish_download, served, None)

        self._offload(_work)

    def _finish_download(self, served: dict | None, error: ApiError | None) -> None:
        """UI thread: show the download link (or the error) and leave the mode."""
        if error is not None:
            if not self._report_client_error(error, "download"):
                self._status(f"❌ {error.message}")
        elif served is not None:
            # Mount a clickable download link widget
            chat_log = self.chat_log
            widget = DownloadLinkWidget(served["url"])
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
        ``media.attachment`` operation (works for both Signal and WhatsApp).  In download
        mode the file is served via HTTP; otherwise a fullscreen
        ``ImageModalScreen`` renders it via ``catimg``.
        """
        att_path = event.attachment_path
        # Lazy resolution: when loaded from cache the path is not yet known,
        # but the attachment_id is.  Use the selected contact's protocol to
        # route the lookup through the correct backend.  The lookup may
        # download the file: it runs in a worker and the click is handled when
        # it returns.
        if att_path is None and event.attachment_id:
            protocol = self.selected_contact.protocol if self.selected_contact else None
            if protocol:

                def _work() -> None:
                    path = resolve_attachment_path(
                        self.client, protocol, event.attachment_id
                    )
                    self._ui(self._finish_image_click, event, path)

                self._offload(_work)
                return
        self._handle_image_click(event, att_path)

    def _finish_image_click(
        self, event: ImageWidget.ImageClicked, path: Path | None
    ) -> None:
        """UI thread: the clicked image's path is resolved; carry on."""
        if event.widget is not None:
            event.widget.attachment_path = path
        self._handle_image_click(event, path)

    def _handle_image_click(
        self, event: ImageWidget.ImageClicked, att_path: Path | None
    ) -> None:
        """UI thread: serve the image in download mode, else open the modal."""
        if self._download_mode:
            text = att_path.name if att_path else "attachment"
            protocol = self.selected_contact.protocol if self.selected_contact else None
            self._start_download(
                text=text,
                attachment_id=event.attachment_id,
                protocol=protocol,
            )
            return

        # OFF semantics (R8): images are disabled → placeholder stays and the
        # click only reports a status (no modal, no catimg).
        image_support = getattr(self, "image_support", ImageSupport.CATIMG)
        if image_support is ImageSupport.OFF:
            self._status("🖼️ Image rendering is disabled")
            return

        if att_path:
            renderer = getattr(self, "_native_renderer", None)
            if image_support is ImageSupport.KITTY and renderer is not None:
                image_id = self._next_native_image_id()
                self.push_screen(
                    ImageModalScreen(
                        att_path,
                        renderer,
                        image_id=image_id,
                        hires_executor=self._hires_executor,
                    )
                )
            else:
                self.push_screen(ImageModalScreen(att_path))
        else:
            self._status("❌ Image file not found on server")
