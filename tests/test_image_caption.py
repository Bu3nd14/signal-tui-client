"""Regression tests for BUG #31 (image alignment from cache) and BUG #36 (image captions).

- BUG #31: images built from the cache/history path (``_build_message_widgets``)
  were never assigned ``msg-right``/``msg-left``, so sent photos were rendered
  left-aligned and indistinguishable from received ones.
- BUG #36: image captions were lost (or duplicated as emoji labels) because
  there was no caption bubble; the caption lived only inside the placeholder.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backends import BackendManager
from models import (
    PROTOCOL_SIGNAL,
    PROTOCOL_TELEGRAM,
    PROTOCOL_WHATSAPP,
)
from signal_tui import SignalTUI
from tui.chat_view import _image_caption
from ui_components import ImageWidget, MessageWidget


class _FakeChatLog:
    """Minimal stand-in for the #chat-log widget used by ``_add_message``."""

    def __init__(self) -> None:
        self.scrolled = False
        self.children: list = []

    def scroll_end(self, animate: bool = False) -> None:
        self.scrolled = True

    def mount(self, *widgets, before=None, after=None):
        for w in widgets:
            w._is_mounted = True
            self.children.append(w)

    def remove_children(self):
        self.children = []


def _make_app() -> SignalTUI:
    """App with a real manager and a neutralized worker (live-path tests)."""
    app = SignalTUI()
    app.manager = BackendManager()
    app.run_worker = MagicMock()
    app.call_from_thread = MagicMock(side_effect=lambda fn, *a, **k: fn(*a, **k))
    return app


def _image_message(**overrides) -> dict:
    """Build a cached image-message dict (cache/history path)."""
    msg = {
        "text": "",
        "is_mine": False,
        "sender": "Mario",
        "timestamp": 1234,
        "quote_text": None,
        "msg_type": "image",
        "attachment_info": "🖼️ Image",
        "attachment_id": "img-1",
        "status": "read",
    }
    msg.update(overrides)
    return msg


class TestCacheImageAlignment:
    """📐 BUG #31 — photo alignment/color from the cache path."""

    @pytest.mark.parametrize(
        "protocol", [PROTOCOL_SIGNAL, PROTOCOL_WHATSAPP, PROTOCOL_TELEGRAM]
    )
    def test_cached_sent_image_is_msg_right(self, protocol):
        app = SignalTUI()
        widgets = app._build_message_widgets(
            protocol, False, _image_message(is_mine=True)
        )

        image_widget = widgets[0]
        assert isinstance(image_widget, ImageWidget)
        assert image_widget.has_class("msg-right")
        assert not image_widget.has_class("msg-left")

    @pytest.mark.parametrize(
        "protocol", [PROTOCOL_SIGNAL, PROTOCOL_WHATSAPP, PROTOCOL_TELEGRAM]
    )
    def test_cached_received_image_is_msg_left(self, protocol):
        app = SignalTUI()
        widgets = app._build_message_widgets(
            protocol, False, _image_message(is_mine=False)
        )

        image_widget = widgets[0]
        assert isinstance(image_widget, ImageWidget)
        assert image_widget.has_class("msg-left")
        assert not image_widget.has_class("msg-right")


class TestImageCaptionResolver:
    """🧩 Unit tests for the central ``_image_caption`` resolver."""

    def test_signal_body_caption(self):
        assert (
            _image_caption("guarda!", "Image: photo.jpg", "att-1", PROTOCOL_SIGNAL)
            == "guarda!"
        )

    def test_signal_synthetic_text_not_caption(self):
        assert (
            _image_caption("🖼️ Image: att-1", "🖼️ Image", "att-1", PROTOCOL_SIGNAL)
            is None
        )
        for protocol in (PROTOCOL_SIGNAL, PROTOCOL_TELEGRAM, PROTOCOL_WHATSAPP):
            assert _image_caption("🖼️ Immagine", "🖼️ Immagine", "att-1", protocol) is None
            assert _image_caption("Immagine", "Immagine", "att-1", protocol) is None
            assert (
                _image_caption(
                    "Immagine: upload-random.png",
                    "upload-random.png",
                    "att-1",
                    protocol,
                )
                is None
            )

    def test_signal_per_attachment_caption(self):
        assert _image_caption("nice: att-1", "nice", "att-1", PROTOCOL_SIGNAL) == "nice"

    def test_whatsapp_caption_from_attachment_info(self):
        assert (
            _image_caption("Media: https://x", "Guarda!", "u", PROTOCOL_WHATSAPP)
            == "Guarda!"
        )

    @pytest.mark.parametrize(
        "info",
        [
            "image/jpeg",
            "photo.jpg",
            "Media",
            "imageMessage (ABCD…)",
            "Image: photo.jpg",
        ],
    )
    def test_whatsapp_technical_attachment_info_not_caption(self, info):
        assert _image_caption("Media: https://x", info, "u", PROTOCOL_WHATSAPP) is None

    def test_whatsapp_synthetic_text_never_caption(self):
        assert _image_caption("Media: https://x", None, "u", PROTOCOL_WHATSAPP) is None

    def test_telegram_text_is_caption(self):
        assert (
            _image_caption("che bello", "Photo", "u", PROTOCOL_TELEGRAM) == "che bello"
        )
        assert (
            _image_caption("che bello", "🖼️ Photo", "u", PROTOCOL_TELEGRAM)
            == "che bello"
        )

    def test_telegram_empty_text_no_caption(self):
        assert _image_caption("", "Photo", "u", PROTOCOL_TELEGRAM) is None


class TestCaptionBubbleLive:
    """💬 BUG #36 — caption bubble via the LIVE path (``_add_message``)."""

    def test_live_received_image_with_caption_shows_bubble(self):
        app = _make_app()
        app._chat_log = _FakeChatLog()

        app._add_message(
            text="Media: https://x",
            msg_type="image",
            attachment_info="Guarda!",
            attachment_id="u",
            is_mine=False,
            protocol=PROTOCOL_WHATSAPP,
        )

        children = app._chat_log.children
        assert len(children) == 2
        assert isinstance(children[0], ImageWidget)
        assert isinstance(children[1], MessageWidget)
        assert children[1]._msg_text == "Guarda!"
        assert children[1].has_class("msg-left")

    def test_live_sent_image_with_caption_bubble_msg_right(self):
        app = _make_app()
        app._chat_log = _FakeChatLog()

        app._add_message(
            text="Media: https://x",
            msg_type="image",
            attachment_info="Guarda!",
            attachment_id="u",
            is_mine=True,
            protocol=PROTOCOL_WHATSAPP,
        )

        children = app._chat_log.children
        assert len(children) == 2
        assert children[0].has_class("msg-right")
        assert children[1].has_class("msg-right")

    def test_live_image_without_caption_shows_no_bubble(self):
        cases = (
            (PROTOCOL_WHATSAPP, "Media: https://x", "image/jpeg"),
            (PROTOCOL_WHATSAPP, "🖼️ Immagine", "🖼️ Immagine"),
            (PROTOCOL_SIGNAL, "🖼️ Immagine", "🖼️ Immagine"),
            (PROTOCOL_TELEGRAM, "🖼️ Immagine", "🖼️ Immagine"),
        )
        for protocol, text, attachment_info in cases:
            app = _make_app()
            app._chat_log = _FakeChatLog()

            app._add_message(
                text=text,
                msg_type="image",
                attachment_info=attachment_info,
                attachment_id="u",
                is_mine=False,
                protocol=protocol,
            )

            children = app._chat_log.children
            assert len(children) == 1
            assert isinstance(children[0], ImageWidget)

    def test_live_telegram_photo_caption_in_text(self):
        app = _make_app()
        app._chat_log = _FakeChatLog()

        app._add_message(
            text="che bello",
            msg_type="image",
            attachment_info="Photo",
            attachment_id="u",
            is_mine=False,
            protocol=PROTOCOL_TELEGRAM,
        )

        children = app._chat_log.children
        assert len(children) == 2
        assert isinstance(children[0], ImageWidget)
        assert isinstance(children[1], MessageWidget)
        assert children[1]._msg_text == "che bello"
        # Placeholder generico "Photo" (niente caption duplicata nel placeholder).
        assert "🖼️ Image: Photo" in str(children[0].render())


class TestCaptionBubbleCache:
    """💬 BUG #36 — caption bubble via the CACHE/history path (``_build_message_widgets``)."""

    def test_cached_image_with_caption_shows_bubble(self):
        app = SignalTUI()
        widgets = app._build_message_widgets(
            PROTOCOL_WHATSAPP,
            False,
            _image_message(text="Media: https://x", attachment_info="Guarda!"),
        )

        assert len(widgets) == 2
        assert isinstance(widgets[0], ImageWidget)
        assert isinstance(widgets[1], MessageWidget)
        assert widgets[1]._msg_text == "Guarda!"
        # Placeholder generico, non ripete la caption.
        assert str(widgets[0].render()) == "[🖼️ Photo]"

    def test_cached_image_without_caption_single_widget(self):
        app = SignalTUI()
        cases = (
            (PROTOCOL_WHATSAPP, "Media: https://x", "photo.jpg"),
            (PROTOCOL_WHATSAPP, "🖼️ Immagine", "🖼️ Immagine"),
            (PROTOCOL_SIGNAL, "🖼️ Immagine", "🖼️ Immagine"),
            (PROTOCOL_TELEGRAM, "🖼️ Immagine", "🖼️ Immagine"),
        )
        for protocol, text, attachment_info in cases:
            widgets = app._build_message_widgets(
                protocol,
                False,
                _image_message(text=text, attachment_info=attachment_info),
            )

            assert len(widgets) == 1
            assert isinstance(widgets[0], ImageWidget)

    @pytest.mark.parametrize("info", ["Photo", "🖼️ Photo"])
    def test_cached_telegram_photo_no_double_emoji(self, info):
        app = SignalTUI()
        widgets = app._build_message_widgets(
            PROTOCOL_TELEGRAM,
            False,
            _image_message(text="", attachment_info=info),
        )

        assert len(widgets) == 1
        assert str(widgets[0].render()) == "[🖼️ Photo]"

    def test_cached_telegram_photo_caption_bubble(self):
        app = SignalTUI()
        widgets = app._build_message_widgets(
            PROTOCOL_TELEGRAM,
            False,
            _image_message(text="didascalia", attachment_info="Photo"),
        )

        assert len(widgets) == 2
        assert isinstance(widgets[0], ImageWidget)
        assert isinstance(widgets[1], MessageWidget)
        assert widgets[1]._msg_text == "didascalia"
        assert str(widgets[0].render()) == "[🖼️ Photo]"

    def test_cached_signal_body_caption(self):
        app = SignalTUI()
        widgets = app._build_message_widgets(
            PROTOCOL_SIGNAL,
            False,
            _image_message(text="guarda!", attachment_info="Image: photo.jpg"),
        )

        assert len(widgets) == 2
        assert isinstance(widgets[0], ImageWidget)
        assert isinstance(widgets[1], MessageWidget)
        assert widgets[1]._msg_text == "guarda!"
