"""
Unit tests for the phase-3 ``ImageModalScreen`` native strategy + OFF semantics.

All headless: the modal is exercised on a bare instance with an injected
``KittyRenderer`` (write callback recorded), the screen size is patched, and
the click handler runs on a ``SimpleNamespace`` app (same convention as
``test_download_mode.py``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

from PIL import Image
from textual.geometry import Size
from textual.screen import ModalScreen, Screen

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tui.download import DownloadModeMixin
from tui.images.detect import ImageSupport
from tui.images.kitty_renderer import KittyRenderer
from ui_components import ImageModalScreen, ImageWidget


def _make_png(path: Path, width: int = 40, height: int = 80) -> None:
    Image.new("RGB", (width, height), "blue").save(path)


class _NativeScreen:
    """Bare native modal + recorder, with inline worker/thread dispatch."""

    def __init__(self, tmp_path: Path, width: int = 40, height: int = 80):
        path = tmp_path / "photo.png"
        _make_png(path, width, height)
        self.written: list[str] = []
        self.renderer = KittyRenderer(write=self.written.append, cell_w=8, cell_h=16)
        self.screen = ImageModalScreen(path, self.renderer, image_id=42)
        self.screen.run_worker = MagicMock(side_effect=lambda fn, **kw: fn())
        # The UI-thread hop must go through the App (``call_from_thread`` lives
        # on App, not Screen/Widget) — provide a fake app and patch ``.app``.
        self.app = MagicMock()
        self.app.call_from_thread = MagicMock(
            side_effect=lambda fn, *a, **k: fn(*a, **k)
        )

    def size(self) -> Size:
        return Size(80, 24)


class TestNativeModal:
    """Native branch: transmit+place on mount, re-place on resize, d=I on dismiss."""

    def _mount(self, ctx: _NativeScreen) -> None:
        with (
            patch.object(ImageModalScreen, "app", PropertyMock(return_value=ctx.app)),
            patch.object(Screen, "size", PropertyMock(return_value=Size(80, 24))),
        ):
            ctx.screen.on_mount()

    def test_transmit_and_place_on_mount(self, tmp_path):
        ctx = _NativeScreen(tmp_path)
        self._mount(ctx)

        assert any("a=t,i=42,f=100,q=2" in s for s in ctx.written)
        places = [s for s in ctx.written if "a=p,i=42" in s]
        assert len(places) == 1
        # 40x80 px image → 5 cols × 5 rows, screen 80x24 (minus 2 reserved rows).
        assert places[0].startswith("\x1b[10;38H")
        assert "w=40,h=80" in places[0]
        assert "C=1,q=2" in places[0]
        assert ctx.screen._native_png is not None

    def test_re_place_on_resize_without_retransmit(self, tmp_path):
        ctx = _NativeScreen(tmp_path)
        with (
            patch.object(ImageModalScreen, "app", PropertyMock(return_value=ctx.app)),
            patch.object(Screen, "size", PropertyMock(return_value=Size(80, 24))),
        ):
            ctx.screen.on_mount()
            before = len([s for s in ctx.written if "a=p,i=42" in s])
            ctx.screen.on_resize(MagicMock())

        after = len([s for s in ctx.written if "a=p,i=42" in s])
        assert after == before + 1
        # Data is transmitted once; resize only re-places.
        assert len([s for s in ctx.written if "a=t,i=42" in s]) == 1

    def test_dismiss_deletes_data(self, tmp_path):
        ctx = _NativeScreen(tmp_path)
        self._mount(ctx)
        mock_dismiss = MagicMock()
        with patch.object(ModalScreen, "dismiss", mock_dismiss):
            ctx.screen.dismiss()

        assert any("a=d,d=I,i=42" in s for s in ctx.written)
        assert ctx.screen._image_id is None
        mock_dismiss.assert_called_once()

    def test_native_compose_yields_nothing(self, tmp_path):
        ctx = _NativeScreen(tmp_path)
        assert list(ctx.screen.compose()) == []

    def test_catimg_compose_unchanged(self, tmp_path):
        screen = ImageModalScreen(tmp_path / "photo.jpg")
        children = list(screen.compose())
        assert len(children) == 2


class TestNativePrepareThreadHop:
    """Regression: ``_prepare_native`` must hop to the UI thread via the APP.

    ``call_from_thread`` lives on ``App``, not ``Screen``/``Widget`` — before
    the fix ``_prepare_native`` raised ``AttributeError`` here.
    """

    def _screen(self, tmp_path) -> ImageModalScreen:
        path = tmp_path / "photo.png"
        _make_png(path, 40, 80)
        renderer = KittyRenderer(write=lambda s: None, cell_w=8, cell_h=16)
        return ImageModalScreen(path, renderer, image_id=42)

    def test_success_branch_calls_app_call_from_thread(self, tmp_path):
        screen = self._screen(tmp_path)
        fake_app = MagicMock()
        with (
            patch.object(ImageModalScreen, "app", PropertyMock(return_value=fake_app)),
            patch.object(Screen, "size", PropertyMock(return_value=Size(80, 24))),
            patch("ui_components.prepare_hi_res", return_value=b"png-bytes"),
        ):
            screen._prepare_native()

        fake_app.call_from_thread.assert_called_once()
        args = fake_app.call_from_thread.call_args.args
        assert args[0] == screen._finish_native
        assert args[1] == b"png-bytes"

    def test_error_branch_calls_app_call_from_thread(self, tmp_path):
        screen = self._screen(tmp_path)
        fake_app = MagicMock()
        with (
            patch.object(ImageModalScreen, "app", PropertyMock(return_value=fake_app)),
            patch.object(Screen, "size", PropertyMock(return_value=Size(80, 24))),
            patch("ui_components.prepare_hi_res", side_effect=ValueError("boom")),
        ):
            screen._prepare_native()

        fake_app.call_from_thread.assert_called_once()
        args = fake_app.call_from_thread.call_args.args
        assert args[0] == screen._native_error
        assert "boom" in args[1]


class TestOffAndClickRouting:
    """OFF status + renderer routing from ``on_image_widget_image_clicked``."""

    def _app(self, image_support: ImageSupport, **extra) -> SimpleNamespace:
        defaults = {
            "manager": MagicMock(),
            "selected_contact": None,
            "_download_mode": False,
            "_start_download": MagicMock(),
            "push_screen": MagicMock(),
            "_status": MagicMock(),
            "image_support": image_support,
        }
        defaults.update(extra)
        return SimpleNamespace(**defaults)

    def test_off_click_shows_disabled_status(self, tmp_path):
        attachment = tmp_path / "photo.jpg"
        attachment.write_text("image")
        app = self._app(ImageSupport.OFF)

        DownloadModeMixin.on_image_widget_image_clicked(
            app, ImageWidget.ImageClicked(attachment)
        )

        app.push_screen.assert_not_called()
        app._status.assert_called_once_with("🖼️ Image rendering is disabled")

    def test_kitty_click_pushes_native_modal(self, tmp_path):
        attachment = tmp_path / "photo.jpg"
        attachment.write_text("image")
        renderer = KittyRenderer(write=lambda s: None, cell_w=8, cell_h=16)
        app = self._app(
            ImageSupport.KITTY,
            _native_renderer=renderer,
            _next_native_image_id=lambda: 99,
        )

        DownloadModeMixin.on_image_widget_image_clicked(
            app, ImageWidget.ImageClicked(attachment)
        )

        app.push_screen.assert_called_once()
        modal = app.push_screen.call_args.args[0]
        assert isinstance(modal, ImageModalScreen)
        assert modal._renderer is renderer
        assert modal._image_id == 99

    def test_catimg_click_pushes_plain_modal(self, tmp_path):
        attachment = tmp_path / "photo.jpg"
        attachment.write_text("image")
        app = self._app(ImageSupport.CATIMG)

        DownloadModeMixin.on_image_widget_image_clicked(
            app, ImageWidget.ImageClicked(attachment)
        )

        app.push_screen.assert_called_once()
        modal = app.push_screen.call_args.args[0]
        assert isinstance(modal, ImageModalScreen)
        assert modal._renderer is None

    def test_download_mode_still_wins_over_off(self, tmp_path):
        attachment = tmp_path / "photo.jpg"
        attachment.write_text("image")
        app = self._app(
            ImageSupport.OFF,
            _download_mode=True,
            selected_contact=SimpleNamespace(protocol="signal"),
        )
        app.manager.get_attachment_path.return_value = attachment

        DownloadModeMixin.on_image_widget_image_clicked(
            app, ImageWidget.ImageClicked(attachment, "att")
        )

        app._start_download.assert_called_once()
        app.push_screen.assert_not_called()
