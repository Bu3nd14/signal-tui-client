"""
Regression tests for ui_components.py — MessageWidget, ImageWidget, etc.
Note: These tests verify logic only (no Textual widget rendering).
"""

from __future__ import annotations

import sys
from asyncio import run
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from textual import events
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

    def test_message_clicked_event_carries_protocol_message_id(self):
        w = MessageWidget(text="Ciao", timestamp=1, message_id="42")
        events = []
        w.post_message = events.append
        w.on_click()
        assert events[0].message_id == "42"

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


class TestMessageWidgetEditing:
    """✏️ MessageWidget — editing (Fase 6): suffix, Alt+click/Alt+e, update_text."""

    def _click(self, meta: bool) -> events.Click:
        return events.Click(
            widget=None,
            x=0,
            y=0,
            delta_x=0,
            delta_y=0,
            button=1,
            shift=False,
            meta=meta,
            ctrl=False,
        )

    def test_constructor_edited_shows_suffix(self):
        """``edited=True`` → contenuto con suffisso " (modificato)"."""
        w = MessageWidget(text="ciao", timestamp=1, edited=True)
        assert w._edited is True
        assert w._Static__content == "ciao (modificato)"

    def test_constructor_not_edited_no_suffix(self):
        """``edited=False`` (default) → contenuto senza suffisso."""
        w = MessageWidget(text="ciao", timestamp=1)
        assert w._edited is False
        assert w._Static__content == "ciao"

    def test_update_text_updates_msg_text_and_suffix(self):
        """``update_text`` aggiorna ``_msg_text`` e il contenuto renderizzato."""
        w = MessageWidget(text="ciao", timestamp=1)
        w.update_text("nuovo", edited=True)
        assert w._msg_text == "nuovo"
        assert w._edited is True
        assert w._Static__content == "nuovo (modificato)"

        w.update_text("altro", edited=False)
        assert w._msg_text == "altro"
        assert w._edited is False
        assert w._Static__content == "altro"

    def test_update_text_preserves_sender_color(self):
        """Con ``sender_color``, il prefisso ``<sender:>`` è preservato dopo l'edit.

        ``Static.update`` → ``visualize`` richiede un'app attiva per i contenuti
        RichText; qui stub-iamo ``update``/``refresh`` per asserire sul contenuto
        prodotto da ``_build_content()`` (oggetto passato a ``update``).
        """
        w = MessageWidget(
            text="ciao", timestamp=1, sender="Mario", sender_color="#DAA520"
        )
        captured = []
        w.update = captured.append
        w.refresh = lambda: None
        w.update_text("nuovo", edited=True)

        assert w._msg_text == "nuovo"
        assert w._edited is True
        assert len(captured) == 1
        content = captured[0]
        assert isinstance(content, RichText)
        assert "<Mario:>" in str(content)
        assert "nuovo (modificato)" in str(content)

    def test_on_click_meta_posts_edit_requested(self):
        """Alt+click (``meta=True``) → posta ``EditRequested`` con i dati."""
        w = MessageWidget(
            text="ciao", timestamp=1, sender="You", is_mine=True, message_id="42"
        )
        events = []
        w.post_message = events.append

        w.on_click(self._click(meta=True))

        assert len(events) == 1
        ev = events[0]
        assert isinstance(ev, MessageWidget.EditRequested)
        assert ev.text == "ciao"
        assert ev.timestamp == 1
        assert ev.sender == "You"
        assert ev.is_mine is True
        assert ev.message_id == "42"

    def test_on_click_no_meta_posts_message_clicked(self):
        """Click normale (``meta=False``) → ``MessageClicked`` (regressione reply)."""
        w = MessageWidget(text="ciao", timestamp=1, sender="Mario", is_mine=False)
        events = []
        w.post_message = events.append

        w.on_click(self._click(meta=False))

        assert len(events) == 1
        assert isinstance(events[0], MessageWidget.MessageClicked)
        assert not isinstance(events[0], MessageWidget.EditRequested)

    def test_action_request_edit_posts_edit_requested(self):
        """``alt+e`` (``action_request_edit``) → posta ``EditRequested``."""
        w = MessageWidget(
            text="ciao", timestamp=1, sender="You", is_mine=True, message_id="42"
        )
        events = []
        w.post_message = events.append

        w.action_request_edit()

        assert len(events) == 1
        ev = events[0]
        assert isinstance(ev, MessageWidget.EditRequested)
        assert ev.text == "ciao"
        assert ev.timestamp == 1
        assert ev.is_mine is True
        assert ev.message_id == "42"


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


class TestImageWidgetReply:
    """🖼️ Bug #37 — ImageWidget: Alt+click / Alt+r → ReplyRequested, click/Enter invariati."""

    def _click(self, meta: bool) -> events.Click:
        return events.Click(
            widget=None,
            x=0,
            y=0,
            delta_x=0,
            delta_y=0,
            button=1,
            shift=False,
            meta=meta,
            ctrl=False,
        )

    def _widget(self, **overrides) -> ImageWidget:
        kw = {
            "attachment_path": Path("/tmp/photo.jpg"),
            "attachment_id": "att-1",
            "timestamp": 1000,
            "sender": "Mario",
            "is_mine": False,
            "message_id": "msg-1",
            "msg_type": "image",
            "caption": None,
            "attachment_info": "photo.jpg",
            "protocol": "signal",
        }
        kw.update(overrides)
        return ImageWidget(**kw)

    def test_image_widget_alt_click_emits_reply_requested(self):
        """Alt+click (``meta=True``) → posta ``ReplyRequested`` con i metadati."""
        w = self._widget()
        events = []
        w.post_message = events.append

        w.on_click(self._click(meta=True))

        assert len(events) == 1
        ev = events[0]
        assert isinstance(ev, ImageWidget.ReplyRequested)
        assert ev.text == "🖼️ Immagine"  # caption None → placeholder
        assert ev.caption is None
        assert ev.timestamp == 1000
        assert ev.sender == "Mario"
        assert ev.is_mine is False
        assert ev.message_id == "msg-1"
        assert ev.attachment_id == "att-1"

    def test_image_widget_reply_requested_carries_content_type(self):
        """(C) ``content_type`` è esposto su ``ImageWidget`` e sul ``ReplyRequested``."""
        w = self._widget(content_type="image/png")
        events = []
        w.post_message = events.append

        w.on_click(self._click(meta=True))

        assert len(events) == 1
        ev = events[0]
        assert isinstance(ev, ImageWidget.ReplyRequested)
        assert ev.content_type == "image/png"

    def test_image_widget_alt_click_caption_preferred_and_kept_distinct(self):
        """La caption reale ha priorità nel display ed è esposta separata da ``text``."""
        w = self._widget(caption="Che bella!")
        events = []
        w.post_message = events.append

        w.on_click(self._click(meta=True))

        assert events[0].text == "Che bella!"
        assert events[0].caption == "Che bella!"

    def test_image_widget_alt_r_action(self):
        """``alt+r`` (``action_request_reply``) → posta ``ReplyRequested``."""
        w = self._widget()
        events = []
        w.post_message = events.append

        w.action_request_reply()

        assert len(events) == 1
        assert isinstance(events[0], ImageWidget.ReplyRequested)
        assert events[0].text == "🖼️ Immagine"
        assert events[0].message_id == "msg-1"

    def test_image_widget_click_still_emits_image_clicked(self):
        """Click normale → ancora ``ImageClicked`` (regressione modal)."""
        w = self._widget()
        events = []
        w.post_message = events.append

        w.on_click(self._click(meta=False))

        assert len(events) == 1
        assert isinstance(events[0], ImageWidget.ImageClicked)
        assert not isinstance(events[0], ImageWidget.ReplyRequested)

    def test_image_widget_enter_still_emits_image_clicked(self):
        """Enter → ancora ``ImageClicked`` (regressione modal)."""
        w = self._widget()
        events = []
        w.post_message = events.append

        w.key_enter()

        assert len(events) == 1
        assert isinstance(events[0], ImageWidget.ImageClicked)

    def test_image_widget_set_selected_border_toggle(self):
        """set_selected togglia il bordo verde di evidenza."""
        w = self._widget()
        assert w._selected is False
        assert not bool(w.styles.border)  # nessun bordo evidenziato

        w.set_selected(True)
        assert w._selected is True
        assert w.styles.border.top[0] == "solid"
        assert w.styles.border.top[1].hex.lower() == "#4ebf71"

        w.set_selected(False)
        assert w._selected is False
        assert not bool(w.styles.border)

    def test_image_widget_defaults_are_backward_compatible(self):
        """I nuovi parametri keyword-only sono opzionali a default neutro."""
        w = ImageWidget(attachment_path=None, attachment_id="")
        assert w._timestamp == 0
        assert w._sender == ""
        assert w._is_mine is False
        assert w._message_id is None
        assert w._caption is None
        assert w._reply_text() == "🖼️ Immagine"
