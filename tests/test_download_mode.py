"""Unit tests for DownloadModeMixin without serving files or mounting a TUI."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tui.download import DownloadModeMixin
from ui_components import DownloadLinkWidget, ImageWidget, MessageWidget


def _bar_app(download_mode: bool = False, reply_to=None):
    bar = MagicMock()
    text = MagicMock()
    app = SimpleNamespace(_download_mode=download_mode, _reply_to=reply_to)
    app.query_one = MagicMock(side_effect=[bar, text])
    return app, bar, text


class TestDownloadMode:
    def test_open_media_reports_path_when_xdg_open_exits_unsuccessfully(self, tmp_path):
        path = tmp_path / "manual.pdf"
        process = MagicMock()
        process.wait.return_value = 1
        app = DownloadModeMixin()
        app._status = MagicMock()
        app.call_from_thread = MagicMock(side_effect=lambda fn, *args: fn(*args))
        app.run_worker = MagicMock(side_effect=lambda work, **_kwargs: work())

        with (
            patch("tui.download.shutil.which", return_value="/usr/bin/xdg-open"),
            patch("tui.download.subprocess.Popen", return_value=process),
        ):
            app._open_media_path(path)

        process.wait.assert_called_once_with(timeout=0.5)
        app._status.assert_called_once_with(f"📎 File available at: {path}")

    def test_action_download_mode_toggles_and_updates_bar(self):
        app = SimpleNamespace(_download_mode=False, _update_download_bar=MagicMock())

        DownloadModeMixin.action_download_mode(app)

        assert app._download_mode is True
        app._update_download_bar.assert_called_once()

    def test_update_download_bar_shows_hint_when_enabled(self):
        app, bar, text = _bar_app(download_mode=True)

        DownloadModeMixin._update_download_bar(app)

        text.update.assert_called_once_with(
            "📥 Download mode — Click a message to download"
        )
        bar.remove_class.assert_called_once_with("reply-bar-hidden")
        assert bar.styles.display == "block"

    def test_update_download_bar_hides_when_disabled_without_reply(self):
        app, bar, text = _bar_app(download_mode=False, reply_to=None)

        DownloadModeMixin._update_download_bar(app)

        text.update.assert_called_once_with("")
        bar.add_class.assert_called_once_with("reply-bar-hidden")
        assert bar.styles.display == "none"

    def test_update_download_bar_preserves_reply_bar_when_reply_active(self):
        app, bar, text = _bar_app(download_mode=False, reply_to={"text": "reply"})

        DownloadModeMixin._update_download_bar(app)

        text.update.assert_not_called()
        bar.add_class.assert_not_called()

    def test_start_download_serves_text_and_mounts_link(self):
        chat_log = MagicMock()
        app = SimpleNamespace(
            manager=MagicMock(),
            chat_log=chat_log,
            _download_mode=True,
            _status=MagicMock(),
            _update_download_bar=MagicMock(),
        )
        with patch(
            "tui.download.serve_text_as_file", return_value="http://x/file.txt"
        ) as serve:
            DownloadModeMixin._start_download(app, "hello", timestamp=42)

        serve.assert_called_once_with("hello", filename="signal-message-42.txt")
        mounted = chat_log.mount.call_args.args[0]
        assert isinstance(mounted, DownloadLinkWidget)
        assert mounted._url == "http://x/file.txt"
        chat_log.scroll_end.assert_called_once_with(animate=False)
        assert app._download_mode is False
        app._update_download_bar.assert_called_once()

    def test_start_download_serves_resolved_attachment(self, tmp_path):
        attachment = tmp_path / "photo.jpg"
        attachment.write_text("image")
        chat_log = MagicMock()
        manager = MagicMock()
        manager.get_attachment_path.return_value = attachment
        app = SimpleNamespace(
            manager=manager,
            chat_log=chat_log,
            _download_mode=True,
            _status=MagicMock(),
            _update_download_bar=MagicMock(),
        )
        with patch(
            "protocols.download._serve_file_path", return_value="http://x/photo.jpg"
        ) as serve:
            DownloadModeMixin._start_download(
                app, "photo", attachment_id="att", protocol="signal"
            )

        serve.assert_called_once_with(attachment)
        assert isinstance(chat_log.mount.call_args.args[0], DownloadLinkWidget)

    def test_start_download_uses_cached_attachment_path(self, tmp_path):
        attachment = tmp_path / "cached.mp4"
        attachment.write_text("video")
        manager = MagicMock()
        app = SimpleNamespace(
            manager=manager,
            chat_log=MagicMock(),
            _download_mode=True,
            _status=MagicMock(),
            _update_download_bar=MagicMock(),
        )

        with patch(
            "protocols.download._serve_file_path", return_value="http://x/cached.mp4"
        ):
            DownloadModeMixin._start_download(
                app,
                "cached.mp4",
                attachment_id="att",
                protocol="signal",
                attachment_path=attachment,
            )

        manager.get_attachment_path.assert_not_called()

    def test_media_download_resolves_once_and_caches_path(self, tmp_path):
        attachment = tmp_path / "clip.mp4"
        attachment.write_text("video")
        manager = MagicMock()
        manager.get_attachment_path.return_value = attachment
        widget = MessageWidget("🎬 clip.mp4", attachment_ref=("att", "telegram"))
        widget._is_mounted = True
        event = widget._make_media_open_requested()
        app = DownloadModeMixin()
        app.manager = manager
        app._download_mode = True
        app._start_download = MagicMock()
        app.call_from_thread = MagicMock(side_effect=lambda fn, *args: fn(*args))
        app.run_worker = MagicMock(side_effect=lambda work, **_kwargs: work())

        app.on_message_widget_media_open_requested(event)

        manager.get_attachment_path.assert_called_once_with("telegram", "att")
        assert widget.attachment_path == attachment
        app._start_download.assert_called_once_with(
            text="clip.mp4",
            attachment_id="att",
            protocol="telegram",
            attachment_path=attachment,
        )

    def test_start_download_reports_attachment_and_server_errors(self):
        manager = MagicMock()
        manager.get_attachment_path.return_value = None
        app = SimpleNamespace(
            manager=manager,
            chat_log=MagicMock(),
            _download_mode=True,
            _status=MagicMock(),
            _update_download_bar=MagicMock(),
        )

        DownloadModeMixin._start_download(app, "photo", attachment_id="missing")

        assert app._status.call_args.args[0].startswith(
            "❌ ERROR: Attachment file not found"
        )

    def test_url_copied_reports_status(self):
        app = SimpleNamespace(_status=MagicMock())
        event = DownloadLinkWidget.URLCopied("http://x")

        DownloadModeMixin.on_download_link_widget_url_copied(app, event)

        app._status.assert_called_once_with(
            "📋 URL ready — select it above and press Cmd+C / Ctrl+C to copy"
        )

    def test_image_click_resolves_then_downloads(self, tmp_path):
        attachment = tmp_path / "photo.jpg"
        attachment.write_text("image")
        manager = MagicMock()
        manager.get_attachment_path.return_value = attachment
        app = SimpleNamespace(
            manager=manager,
            selected_contact=SimpleNamespace(protocol="signal"),
            _download_mode=True,
            _start_download=MagicMock(),
            push_screen=MagicMock(),
            _status=MagicMock(),
        )
        event = ImageWidget.ImageClicked(None, "att")

        DownloadModeMixin.on_image_widget_image_clicked(app, event)

        manager.get_attachment_path.assert_called_once_with("signal", "att")
        app._start_download.assert_called_once_with(
            text="photo.jpg",
            attachment_id="att",
            protocol="signal",
            attachment_path=attachment,
        )

    def test_image_click_opens_modal_or_reports_missing(self, tmp_path):
        attachment = tmp_path / "photo.jpg"
        attachment.write_text("image")
        app = SimpleNamespace(
            manager=MagicMock(),
            selected_contact=None,
            _download_mode=False,
            _start_download=MagicMock(),
            push_screen=MagicMock(),
            _status=MagicMock(),
        )

        DownloadModeMixin.on_image_widget_image_clicked(
            app, ImageWidget.ImageClicked(attachment)
        )
        app.push_screen.assert_called_once()

        DownloadModeMixin.on_image_widget_image_clicked(
            app, ImageWidget.ImageClicked(None)
        )
        app._status.assert_called_once_with("❌ Image file not found on server")
