"""
Regression tests for ui_components.py — MessageWidget, ImageWidget, etc.
Note: These tests verify logic only (no Textual widget rendering).
"""

from __future__ import annotations

import sys
from asyncio import run
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from textual.widgets import RichLog, Static

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from rich.text import Text as RichText

from ui_components import (
    DownloadLinkWidget,
    ImageModalScreen,
    ImageWidget,
    MessageWidget,
)


class TestMessageWidget:
    """💬 MessageWidget — stato e metodi."""

    def test_initial_state(self):
        """Widget creato con testo e timestamp."""
        w = MessageWidget(text="Ciao!", timestamp=1000, sender="Mario")
        assert w._msg_text == "Ciao!"
        assert w._msg_timestamp == 1000
        assert w._msg_sender == "Mario"
        assert w._msg_is_mine is False
        assert w._selected is False
        assert w._status == "sent"

    def test_initial_state_mine(self):
        """Widget per messaggio proprio."""
        w = MessageWidget(text="Ciao!", timestamp=1000, sender="You", is_mine=True)
        assert w._msg_is_mine is True

    def test_set_status(self):
        """Cambio status."""
        w = MessageWidget(text="Ciao!", timestamp=1000, sender="You", is_mine=True)
        assert w._status == "sent"
        w.set_status("delivered")
        assert w._status == "delivered"
        w.set_status("read")
        assert w._status == "read"

    def test_pending_and_failed_statuses_have_visual_classes(self):
        w = MessageWidget(text="Ciao!", is_mine=True, status="pending")
        assert w.has_class("msg-pending")
        w.set_status("failed")
        assert w.has_class("msg-failed")
        assert not w.has_class("msg-pending")

    def test_set_selected(self):
        """Toggle selezione."""
        w = MessageWidget(text="Ciao!", timestamp=1000, sender="Mario")
        assert w._selected is False
        w.set_selected(True)
        assert w._selected is True
        w.set_selected(False)
        assert w._selected is False

    def test_message_clicked_event(self):
        """Click → emette MessageClicked con i dati corretti."""
        w = MessageWidget(text="Ciao!", timestamp=1000, sender="Mario", is_mine=False)
        events = []

        def _handler(msg):
            events.append(msg)

        # Simula la registrazione del messaggio
        w.post_message = lambda msg: events.append(msg)
        w.on_click()

        assert len(events) == 1
        assert events[0].text == "Ciao!"
        assert events[0].timestamp == 1000
        assert events[0].sender == "Mario"
        assert events[0].is_mine is False

    def test_blur_and_enter(self):
        w = MessageWidget(text="Ciao", timestamp=1, sender="Mario", protocol="signal")
        events = []
        w.post_message = events.append
        w.on_blur()
        w.key_enter()
        assert events[0].text == "Ciao"

    def test_sender_color_renders_prefix(self):
        """Con sender_color, il testo mostra '<sender:> testo'."""
        w = MessageWidget(
            text="Ciao gruppo!",
            timestamp=1000,
            sender="Mario",
            is_mine=False,
            sender_color="#DAA520",
        )
        assert w._sender_color == "#DAA520"
        # Il contenuto del widget deve essere un RichText con il prefisso <Mario:>
        content = w._Static__content
        assert isinstance(content, RichText)
        assert "<Mario:>" in str(content)
        assert "Ciao gruppo!" in str(content)

    def test_sender_color_none_no_prefix(self):
        """Senza sender_color, il testo resta invariato (nessun prefisso)."""
        w = MessageWidget(
            text="Ciao!",
            timestamp=1000,
            sender="Mario",
            is_mine=False,
        )
        assert w._sender_color is None
        rendered = str(w.render())
        assert "<Mario:>" not in rendered
        assert "Ciao!" in rendered

    def test_sender_color_empty_sender_no_prefix(self):
        """Con sender_color ma sender vuoto, nessun prefisso viene aggiunto."""
        w = MessageWidget(
            text="Ciao!",
            timestamp=1000,
            sender="",
            is_mine=False,
            sender_color="#DAA520",
        )

        rendered = str(w.render())
        assert "<:>" not in rendered
        assert "Ciao!" in rendered


class TestImageWidget:
    """🖼️ ImageWidget — stato e metodi."""

    def test_initial_state_with_path(self, tmp_path):
        """Widget con path valido."""
        img_path = tmp_path / "photo.jpg"
        img_path.write_text("fake")
        w = ImageWidget(attachment_path=img_path, attachment_id="att-123")
        assert w.attachment_path == img_path
        assert w.attachment_id == "att-123"

    def test_initial_state_no_path(self):
        """Widget senza path."""
        w = ImageWidget(attachment_path=None)
        assert w.attachment_path is None

    def test_image_clicked_event(self, tmp_path):
        """Click → emette ImageClicked con path e id."""
        img_path = tmp_path / "photo.jpg"
        img_path.write_text("fake")
        w = ImageWidget(attachment_path=img_path, attachment_id="att-123")
        events = []

        w.post_message = lambda msg: events.append(msg)
        w.on_click()

        assert len(events) == 1
        assert events[0].attachment_path == img_path
        assert events[0].attachment_id == "att-123"

    def test_click_no_path_no_event(self):
        """Click senza path → nessun evento."""
        w = ImageWidget(attachment_path=None)
        events = []

        w.post_message = lambda msg: events.append(msg)
        w.on_click()

        assert len(events) == 0

    def test_focus_blur_and_enter(self, tmp_path):
        path = tmp_path / "photo.jpg"
        path.write_text("x")
        w = ImageWidget(path, "att")
        events = []
        w.post_message = events.append
        w.on_focus()
        w.on_blur()
        w.key_enter()
        assert events[0].attachment_path == path

        empty = ImageWidget(None)
        empty.post_message = MagicMock()
        empty.key_enter()
        empty.post_message.assert_not_called()


class TestDownloadLinkWidget:
    """📥 DownloadLinkWidget — URL e composizione."""

    def test_url_stored(self):
        """URL passato al widget."""
        w = DownloadLinkWidget(url="http://localhost:10042/file.txt")
        assert w._url == "http://localhost:10042/file.txt"
        assert w._label == "📥 Download"

    def test_custom_label(self):
        """Label personalizzato."""
        w = DownloadLinkWidget(url="http://localhost:10042/file.txt", label="Scarica")
        assert w._label == "Scarica"

    def test_url_copied_event(self):
        assert DownloadLinkWidget.URLCopied("http://x").url == "http://x"

    def test_compose_mount_and_focus(self):
        w = DownloadLinkWidget("http://x", label="Link")
        fake_input = MagicMock()
        with patch("ui_components.Input", return_value=fake_input) as input_class:
            assert list(w.compose()) == [fake_input]
        input_class.assert_called_once_with(value="http://x", id="download-url-input")
        w.on_mount()
        assert w.border_title == "Link"
        inp = MagicMock()
        w.query_one = MagicMock(return_value=inp)
        w.on_focus()
        inp.focus.assert_called_once()
        inp.select_all.assert_called_once()


class TestImageModalScreen:
    def test_compose_mount_and_dismiss_keys(self, tmp_path):
        screen = ImageModalScreen(tmp_path / "photo.jpg")
        assert screen._attachment_path == tmp_path / "photo.jpg"
        children = list(screen.compose())
        assert isinstance(children[0], RichLog)
        assert isinstance(children[1], Static)
        image = MagicMock()
        hint = MagicMock()
        screen.query_one = MagicMock(side_effect=[image, hint])
        screen.call_after_refresh = MagicMock()
        screen.on_mount()
        screen.call_after_refresh.assert_called_once_with(screen._start_image_render)
        screen.dismiss = MagicMock()
        screen.key_escape()
        screen.key_q()
        assert screen.dismiss.call_count == 2

    def test_start_render_and_fallback_messages(self, tmp_path):
        screen = ImageModalScreen(tmp_path / "photo.jpg")
        image = MagicMock()
        image.region.width = 10
        screen.query_one = MagicMock(return_value=image)
        screen.run_worker = MagicMock(side_effect=lambda coro, **kwargs: coro.close())
        screen._start_image_render()
        assert screen._catimg_pixels == 80

        async def missing(*args, **kwargs):
            raise FileNotFoundError

        with patch("ui_components.asyncio.create_subprocess_exec", missing):
            run(screen._render_image())
        image.write.assert_called_with("⚠️ catimg is not installed on this system.")

        image.write.reset_mock()

        async def broken(*args, **kwargs):
            raise RuntimeError("boom")

        with patch("ui_components.asyncio.create_subprocess_exec", broken):
            run(screen._render_image())
        assert "⚠️ Could not render image: boom" in image.write.call_args.args[0]

        image.write.reset_mock()
        proc = MagicMock(returncode=0)
        proc.communicate = AsyncMock(return_value=(b"hello", b""))
        with patch(
            "ui_components.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)
        ):
            run(screen._render_image())
        image.write.assert_called_once()

        image.write.reset_mock()

        async def timeout(*args, **kwargs):
            raise TimeoutError

        with patch("ui_components.asyncio.create_subprocess_exec", timeout):
            run(screen._render_image())
        image.write.assert_called_once_with("⚠️ Image rendering timed out.")
