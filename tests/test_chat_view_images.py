"""
Unit tests for the native (KITTY) branch of chat attachment resolution.

Covers ``_resolve_attachment_worker`` (KITTY), ``_finish_native_thumbnail`` and
``_resolve_mounted_image_paths`` in ``tui/chat_view.py`` — the CATIMG branch is
already covered by ``test_image_async_download.py``.  Headless: the renderer is
injected with a recording write callback and the UI-thread hops run inline.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from models import PROTOCOL_SIGNAL
from tui.app import SignalTUI
from tui.images.detect import ImageSupport
from tui.images.kitty_renderer import KittyRenderer
from ui_components import ImageWidget


def _make_kitty_app() -> tuple[SignalTUI, list[str]]:
    written: list[str] = []
    app = SignalTUI(image_support=ImageSupport.KITTY)
    app._native_renderer = KittyRenderer(write=written.append, cell_w=8, cell_h=16)
    app.manager = MagicMock()
    app.run_worker = MagicMock()
    app.call_from_thread = MagicMock(side_effect=lambda fn, *a, **k: fn(*a, **k))
    return app, written


class _FakeChatLog:
    """Minimal stand-in exposing what the chat image paths need."""

    def __init__(self, width: int = 60) -> None:
        self.content_region = SimpleNamespace(width=width)
        self.children: list = []

    def mount(self, *widgets, before=None, after=None):
        for w in widgets:
            w._is_mounted = True
            self.children.append(w)

    def scroll_end(self, animate: bool = False) -> None:
        pass


class TestResolveAttachmentWorkerKitty:
    def test_kitty_branch_prepares_transmits_and_registers(self, tmp_path):
        app, written = _make_kitty_app()
        img_path = tmp_path / "photo.png"
        Image.new("RGB", (64, 32), "red").save(img_path)
        app.manager.get_attachment_path.return_value = img_path

        widget = ImageWidget(attachment_path=None, attachment_id="att-1")
        widget._is_mounted = True

        app._resolve_attachment_worker(
            PROTOCOL_SIGNAL, "att-1", widget, "Photo", max_lines=12, max_cols=60
        )

        # Transmit (a=t) + register widget native state on the UI thread.
        assert any("a=t" in s for s in written)
        assert widget.native_image_id == 1
        assert widget.native_renderer is app._native_renderer
        assert widget.attachment_path == img_path
        # Native mode: no textual placeholder — the kitty image covers it.
        assert widget.content == ""

    def test_worker_uses_semaphore(self, tmp_path):
        app, _ = _make_kitty_app()
        app._image_resolve_semaphore = MagicMock()
        app.manager.get_attachment_path.return_value = None
        widget = ImageWidget(attachment_path=None, attachment_id="att-1")

        app._resolve_attachment_worker(PROTOCOL_SIGNAL, "att-1", widget, "Photo")

        app._image_resolve_semaphore.__enter__.assert_called_once()
        app._image_resolve_semaphore.__exit__.assert_called_once()

    def test_semaphore_limits_concurrency_to_4(self):
        app, _ = _make_kitty_app()
        sem = app._image_resolve_semaphore
        for _ in range(4):
            assert sem.acquire(blocking=False) is True
        assert sem.acquire(blocking=False) is False  # 5th would block
        for _ in range(4):
            sem.release()

    def test_prepare_failure_falls_back_to_catimg_resolve(self, tmp_path):
        app, written = _make_kitty_app()
        img_path = tmp_path / "not-an-image.png"
        img_path.write_text("garbage")
        app.manager.get_attachment_path.return_value = img_path
        widget = ImageWidget(attachment_path=None, attachment_id="att-1")
        widget._is_mounted = True

        app._resolve_attachment_worker(
            PROTOCOL_SIGNAL, "att-1", widget, "Photo", max_lines=12, max_cols=60
        )

        # No transmit; the CATIMG finish path updated the placeholder.
        assert not any("a=t" in s for s in written)
        assert widget.native_image_id is None
        assert widget.attachment_path == img_path
        # Degraded (prepare failed) → the textual placeholder reappears.
        assert "Click Enter to View" in str(widget.render())


class TestFinishNativeThumbnail:
    def test_unmounted_widget_is_noop(self, tmp_path):
        app, written = _make_kitty_app()
        widget = ImageWidget(attachment_path=None, attachment_id="att-1")

        app._finish_native_thumbnail(widget, tmp_path / "x.png", b"png-bytes")

        assert written == []
        assert widget.native_image_id is None

    def test_no_renderer_falls_back_to_catimg(self, tmp_path):
        app, _ = _make_kitty_app()
        app._native_renderer = None
        path = tmp_path / "photo.jpg"
        widget = ImageWidget(attachment_path=None, attachment_id="att-1")
        widget._is_mounted = True

        app._finish_native_thumbnail(widget, path, b"png-bytes")

        assert widget.attachment_path == path
        assert widget.native_image_id is None
        assert "Click Enter to View" in str(widget.render())


class TestResolveMountedImagePaths:
    def _make_widget(self, attachment_id, path=None):
        widget = ImageWidget(attachment_path=path, attachment_id=attachment_id)
        widget._protocol = PROTOCOL_SIGNAL
        widget._attachment_info = "Photo"
        return widget

    def test_schedules_worker_only_for_eligible_widgets(self, tmp_path):
        app, _ = _make_kitty_app()
        app._chat_log = _FakeChatLog()

        with_id = self._make_widget("att-1")
        no_id = self._make_widget("")
        resolved = self._make_widget("att-2", path=tmp_path / "x.jpg")
        not_image = MagicMock()

        app._resolve_mounted_image_paths([with_id, no_id, resolved, not_image])

        # Only `with_id` (image, no path, has id) is eligible.
        assert app.run_worker.call_count == 1
        args = app.run_worker.call_args.args[0]
        assert args() is None  # the worker lambda is callable

    def test_noop_when_not_kitty(self):
        app, _ = _make_kitty_app()
        app.image_support = ImageSupport.CATIMG
        app._chat_log = _FakeChatLog()
        widget = self._make_widget("att-1")

        app._resolve_mounted_image_paths([widget])

        app.run_worker.assert_not_called()

    def test_noop_when_no_renderer(self):
        app, _ = _make_kitty_app()
        app._native_renderer = None
        app._chat_log = _FakeChatLog()
        widget = self._make_widget("att-1")

        app._resolve_mounted_image_paths([widget])

        app.run_worker.assert_not_called()


class TestNativePlaceholder:
    """Native mode shows no placeholder; degraded/fallback modes keep it."""

    def test_empty_in_kitty_with_id(self):
        app, _ = _make_kitty_app()
        assert app._native_placeholder("[🖼️ Image]", attachment_id="att-1") == ""

    def test_text_when_catimg(self):
        app = SignalTUI()  # default CATIMG, no renderer
        assert (
            app._native_placeholder("[🖼️ Image]", attachment_id="att-1") == "[🖼️ Image]"
        )

    def test_text_when_no_renderer(self):
        app, _ = _make_kitty_app()
        app._native_renderer = None
        assert (
            app._native_placeholder("[🖼️ Image]", attachment_id="att-1") == "[🖼️ Image]"
        )

    def test_text_when_no_attachment_id(self):
        app, _ = _make_kitty_app()
        assert app._native_placeholder("[🖼️ Image]", attachment_id=None) == "[🖼️ Image]"

    def test_build_message_widgets_empty_in_kitty(self):
        app, _ = _make_kitty_app()
        message = {
            "text": "",
            "is_mine": False,
            "sender": "Mario",
            "timestamp": 1000,
            "msg_type": "image",
            "attachment_info": "photo.jpg",
            "attachment_id": "att-1",
            "read": False,
            "status": "read",
        }

        widgets = app._build_message_widgets(PROTOCOL_SIGNAL, False, message)
        image_widget = widgets[0]

        assert isinstance(image_widget, ImageWidget)
        assert image_widget.content == ""

    def test_render_image_in_chat_empty_in_kitty(self):
        app, _ = _make_kitty_app()
        log = _FakeChatLog()

        app._render_image_in_chat(
            attachment_id="att-1",
            attachment_info="photo.jpg",
            is_mine=False,
            chat_log=log,
            protocol=PROTOCOL_SIGNAL,
        )

        widget = log.children[0]
        assert isinstance(widget, ImageWidget)
        assert widget.content == ""
        assert app.run_worker.call_count == 1
