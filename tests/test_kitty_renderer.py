"""
Unit tests for ``tui/images/kitty_renderer.py`` + the native-image wiring.

All headless: the renderer's ``write`` callback is injected, the cell size is
monkeypatched, and the app hook is exercised on a bare ``SignalTUI`` (no
``run_test``), so no real terminal I/O happens.
"""

from __future__ import annotations

import io
import struct
import sys
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from PIL import Image
from textual.geometry import Region

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tui import app as app_module
from tui.images import cellsize as cellsize_mod
from tui.images.detect import ImageSupport
from tui.images.kitty_renderer import (
    KittyRenderer,
    compute_source_rect,
    dcs_clear_all,
    dcs_clear_placements,
    dcs_delete,
    dcs_place,
    dcs_transmit,
    png_size,
    prepare_hi_res,
)
from ui_components import ImageWidget


def _make_png(width: int = 16, height: int = 32) -> bytes:
    """Encode a solid PNG of the given size via Pillow."""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "red").save(buf, "PNG")
    return buf.getvalue()


class TestDcsTransmit:
    """Golden bytes for the ``a=t`` transmit DCS (C1: ``q=2`` everywhere)."""

    def test_first_chunk_has_q2_and_m1(self):
        png = b"\x89PNG\r\n\x1a\n" + b"x" * 10000
        chunks = dcs_transmit(42, png)
        assert len(chunks) > 1
        assert chunks[0].startswith("\x1b_Ga=t,i=42,f=100,q=2,m=1;")
        assert chunks[0].endswith("\x1b\\")

    def test_last_chunk_is_m0_without_extra_keys(self):
        png = b"y" * 9000
        chunks = dcs_transmit(7, png)
        last = chunks[-1]
        assert last.startswith("\x1b_Gm=0;")
        assert "i=" not in last
        assert "f=" not in last
        assert "q=" not in last
        assert last.endswith("\x1b\\")

    def test_data_chunks_bounded_and_multiple_of_4(self):
        png = b"z" * 9500
        chunks = dcs_transmit(3, png)
        for index, chunk in enumerate(chunks):
            payload = chunk.split(";", 1)[1][:-2]  # strip the ST terminator
            assert len(payload) <= 4096
            if index < len(chunks) - 1:
                assert len(payload) % 4 == 0

    def test_single_small_png_is_one_chunk_m0(self):
        chunks = dcs_transmit(1, b"small")
        assert len(chunks) == 1
        assert chunks[0].startswith("\x1b_Ga=t,i=1,f=100,q=2,m=0;")
        assert chunks[0].endswith("\x1b\\")

    def test_empty_payload_is_single_chunk_m0(self):
        chunks = dcs_transmit(1, b"")
        assert len(chunks) == 1
        assert chunks[0] == "\x1b_Ga=t,i=1,f=100,q=2,m=0;\x1b\\"


class TestDcsPlace:
    def test_place_contains_cursor_move_source_rect_and_q2(self):
        out = dcs_place(10, 99, row=5, col=3, y_src=160, w_px=320, h_px=64)
        assert out.startswith("\x1b[5;3H")
        assert "a=p,i=10,p=99" in out
        assert "x=0,y=160" in out
        assert "w=320,h=64" in out
        assert "C=1,q=2" in out
        assert out.endswith("\x1b\\")

    def test_place_propagates_x_src(self):
        out = dcs_place(10, 99, row=5, col=3, x_src=16, y_src=160, w_px=320, h_px=64)
        assert "x=16,y=160" in out


class TestDcsDelete:
    def test_delete_placement_keeps_data(self):
        out = dcs_delete(10, keep_data=True)
        assert "a=d,d=i,i=10,q=2" in out
        assert out.endswith("\x1b\\")

    def test_delete_data(self):
        out = dcs_delete(10, keep_data=False)
        assert "a=d,d=I,i=10,q=2" in out


class TestDcsClear:
    def test_clear_placements(self):
        assert dcs_clear_placements() == "\x1b_Ga=d,d=a,q=2\x1b\\"

    def test_clear_all(self):
        assert dcs_clear_all() == "\x1b_Ga=d,d=A,q=2\x1b\\"

    def test_renderer_clear_all_resets_state(self):
        written: list[str] = []
        renderer = KittyRenderer(write=written.append, cell_w=8, cell_h=16)
        renderer.transmit(5, b"\x89PNG\r\n\x1a\n" + b"x" * 5000)
        renderer.place(5, 5, row=1, col=1, y_src=0, w_px=10, h_px=10)
        renderer.clear_all()
        assert "a=d,d=A,q=2" in written[-1]
        assert renderer._transmitted == set()
        assert renderer._placed == set()


class TestRendererSplit:
    """R5: ``_transmitted`` (data) split from ``_placed`` (placements)."""

    def _renderer(self):
        written: list[str] = []
        renderer = KittyRenderer(write=written.append, cell_w=8, cell_h=16)
        return renderer, written

    def test_transmit_twice_emits_data_once(self):
        renderer, written = self._renderer()
        png = b"\x89PNG\r\n\x1a\n" + b"x" * 5000
        renderer.transmit(5, png)
        count = len(written)
        renderer.transmit(5, png)
        assert len(written) == count
        assert 5 in renderer._transmitted

    def test_has_data_reflects_transmitted_state(self):
        renderer, _ = self._renderer()
        assert renderer.has_data is False
        renderer.transmit(5, b"png")
        assert renderer.has_data is True
        renderer.delete(5, keep_data=False)
        assert renderer.has_data is False

    def test_prepared_transmit_uses_one_write(self):
        renderer, written = self._renderer()
        renderer.transmit_prepared(5, "chunk-1chunk-2")
        assert written == ["chunk-1chunk-2"]
        assert renderer.has_data is True

    def test_reenter_viewport_only_replaces(self):
        renderer, written = self._renderer()
        png = b"\x89PNG\r\n\x1a\n" + b"x" * 5000
        renderer.transmit(5, png)
        renderer.place(5, 5, row=1, col=1, y_src=0, w_px=100, h_px=50)
        assert (5, 5) in renderer._placed
        renderer.delete(5, keep_data=True)  # scrolled out of view
        assert (5, 5) not in renderer._placed
        assert 5 in renderer._transmitted  # data kept in kitty
        marker = len(written)
        renderer.place(5, 5, row=2, col=1, y_src=0, w_px=100, h_px=50)
        after = written[marker:]
        assert any("a=p" in s for s in after)
        assert not any("a=t" in s for s in after)

    def test_delete_data_forgets_transmission(self):
        renderer, _ = self._renderer()
        renderer.transmit(5, b"\x89PNG\r\n\x1a\n" + b"x" * 5000)
        renderer.delete(5, keep_data=False)
        assert 5 not in renderer._transmitted
        assert 5 not in {i for (i, _p) in renderer._placed}


class TestComputeSourceRect:
    """C2: vertical + horizontal clipping with expected values."""

    def test_fully_above_viewport_returns_none(self):
        assert (
            compute_source_rect(
                Region(2, -20, 40, 10), Region(0, 0, 60, 40), 8, 16, 320
            )
            is None
        )

    def test_fully_below_viewport_returns_none(self):
        assert (
            compute_source_rect(Region(2, 50, 40, 10), Region(0, 0, 60, 40), 8, 16, 320)
            is None
        )

    def test_vertical_clip_scrolled_up(self):
        rect = compute_source_rect(
            Region(2, -10, 40, 12), Region(0, 0, 60, 40), 8, 16, 320
        )
        # cut_top=10 rows → y_src=160, h_px=32; row clamped to 1.
        assert rect == (1, 3, 0, 160, 320, 32)

    def test_horizontal_clip_panorama(self):
        rect = compute_source_rect(
            Region(2, 5, 80, 12), Region(0, 0, 60, 40), 8, 16, 640
        )
        # cut_right=22 cols → visible_w=58 → w_px=464, capped by max_w_px=640.
        assert rect == (6, 3, 0, 0, 464, 192)

    def test_width_capped_by_max_w_px(self):
        rect = compute_source_rect(
            Region(0, 0, 40, 10), Region(0, 0, 60, 40), 8, 16, 100
        )
        # 40 cols * 8px = 320 available, but the image is only 100px wide.
        assert rect[4] == 100

    def test_horizontal_left_clip_propagates_x_src(self):
        # Widget starts left of the container (x=-10) → cut_left=10 cols.
        rect = compute_source_rect(
            Region(-10, 5, 40, 12), Region(0, 0, 60, 40), 8, 16, 320
        )
        # cut_left=10 → x_src=80; visible_w=30 → w_px=min(240, 320-80)=240.
        assert rect == (6, 1, 80, 0, 240, 192)

    def test_vertical_overlap_null_returns_none(self):
        # Widget bottom exactly at container top → no visible overlap.
        assert (
            compute_source_rect(
                Region(2, -10, 10, 10), Region(0, 0, 60, 40), 8, 16, 320
            )
            is None
        )

    def test_horizontal_out_of_viewport_returns_none(self):
        # Widget fully left of the container.
        assert (
            compute_source_rect(
                Region(-30, 5, 20, 10), Region(0, 0, 60, 40), 8, 16, 320
            )
            is None
        )
        # Widget fully right of the container.
        assert (
            compute_source_rect(Region(70, 5, 20, 10), Region(0, 0, 60, 40), 8, 16, 320)
            is None
        )

    def test_horizontal_overlap_null_returns_none(self):
        # Widget right edge exactly at container left → no visible overlap.
        assert (
            compute_source_rect(
                Region(-20, 5, 20, 10), Region(0, 0, 60, 40), 8, 16, 320
            )
            is None
        )


class TestCellSize:
    """R3: cell size via ioctl, with CSI-16-t fallback."""

    def test_ioctl_direct(self, monkeypatch):
        monkeypatch.setattr(
            cellsize_mod.fcntl,
            "ioctl",
            lambda fd, req, buf: struct.pack("HHHH", 30, 100, 800, 510),
        )
        assert cellsize_mod.get_cell_size(0) == (8, 17)

    def test_ioctl_zero_pixels_falls_back_to_query(self, monkeypatch):
        monkeypatch.setattr(
            cellsize_mod.fcntl,
            "ioctl",
            lambda fd, req, buf: struct.pack("HHHH", 30, 100, 0, 0),
        )
        monkeypatch.setattr(
            cellsize_mod, "query_pixel_size", lambda fd, timeout=0.15: (1200, 510)
        )
        assert cellsize_mod.get_cell_size(0) == (12, 17)

    def test_ioctl_failure_returns_none(self, monkeypatch):
        def _boom(*_args, **_kwargs):
            raise OSError("not a tty")

        monkeypatch.setattr(cellsize_mod.fcntl, "ioctl", _boom)
        assert cellsize_mod.get_cell_size(0) is None

    def test_zero_grid_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            cellsize_mod.fcntl,
            "ioctl",
            lambda fd, req, buf: struct.pack("HHHH", 0, 0, 800, 510),
        )
        assert cellsize_mod.get_cell_size(0) is None

    def test_ioctl_only_never_queries_csi(self, monkeypatch):
        monkeypatch.setattr(
            cellsize_mod.fcntl,
            "ioctl",
            lambda fd, req, buf: struct.pack("HHHH", 30, 100, 0, 0),
        )
        # get_cell_size_ioctl must NOT fall back to the CSI query (P2).
        assert cellsize_mod.get_cell_size_ioctl(0) is None


class TestPngSize:
    def test_reads_dimensions(self):
        assert png_size(_make_png(16, 32)) == (16, 32)

    def test_rejects_non_png(self):
        with pytest.raises(ValueError):
            png_size(b"not a png")


class TestPrepareHiRes:
    """Modal hi-res preparation (cap on the long side, fit box)."""

    def test_fits_box_and_preserves_aspect(self, tmp_path):
        path = tmp_path / "photo.png"
        Image.new("RGB", (400, 200), "green").save(path)
        png = prepare_hi_res(path, 1600, 800)
        w, h = png_size(png)
        assert (w, h) == (400, 200)  # already fits → untouched

    def test_downscales_long_side(self, tmp_path):
        path = tmp_path / "photo.png"
        Image.new("RGB", (3200, 1600), "green").save(path)
        png = prepare_hi_res(path, 1600, 1600)
        w, h = png_size(png)
        assert max(w, h) <= 1600
        assert (w, h) == (1600, 800)  # long side capped, aspect preserved


class TestPrepareThumbnail:
    """Renderer thumbnail preparation (Pillow resize to a cell-based box)."""

    def test_resizes_proportionally_to_cell_box(self, tmp_path):
        path = tmp_path / "photo.png"
        Image.new("RGB", (300, 100), "green").save(path)
        renderer = KittyRenderer(write=lambda s: None, cell_w=8, cell_h=16)
        # max_lines=12 (192px), max_cols=60 (480px) → 300x100 already fits.
        png = renderer.prepare_thumbnail(path, 12, 60)
        assert png_size(png) == (300, 100)

    def test_downscales_to_box_preserving_aspect(self, tmp_path):
        path = tmp_path / "photo.png"
        Image.new("RGB", (1000, 1000), "green").save(path)
        renderer = KittyRenderer(write=lambda s: None, cell_w=8, cell_h=16)
        png = renderer.prepare_thumbnail(path, 12, 60)
        w, h = png_size(png)
        assert w <= 480 and h <= 192
        assert (w, h) == (192, 192)  # square → both capped at 192


class TestImageWidgetNative:
    """ImageWidget extension (C3) on a bare instance — no app required."""

    def test_show_native_thumbnail_registers_state(self):
        renderer = KittyRenderer(write=lambda s: None, cell_w=8, cell_h=16)
        widget = ImageWidget(attachment_path=None)
        widget.show_native_thumbnail(renderer, 5, _make_png(16, 32))
        assert widget.native_renderer is renderer
        assert widget.native_image_id == 5
        assert widget.native_width_px == 16
        assert widget.native_height_px == 32
        assert widget.styles.height.value == 2  # 32px / 16px per cell
        assert widget.styles.width.value == 100  # "100%" of the container

    def test_show_native_thumbnail_clears_placeholder_content(self):
        renderer = KittyRenderer(write=lambda s: None, cell_w=8, cell_h=16)
        widget = ImageWidget(attachment_path=None, fallback_text="[🖼️ Image]")
        widget.show_native_thumbnail(renderer, 5, _make_png(16, 32))
        # Native mode shows only the kitty image — no textual placeholder.
        assert widget.content == ""
        assert str(widget.render()) == ""

    def test_native_cleanup_deletes_data_and_clears_state(self):
        written: list[str] = []
        renderer = KittyRenderer(write=written.append, cell_w=8, cell_h=16)
        widget = ImageWidget(attachment_path=None)
        widget.show_native_thumbnail(renderer, 7, _make_png())
        widget.native_cleanup()
        assert widget.native_renderer is None
        assert widget.native_image_id is None
        assert any("a=d,d=I,i=7" in s for s in written)

    def test_native_cleanup_noop_without_native_state(self):
        written: list[str] = []
        renderer = KittyRenderer(write=written.append, cell_w=8, cell_h=16)
        widget = ImageWidget(attachment_path=None)
        widget.native_renderer = renderer
        widget.native_cleanup()
        assert written == []

    def test_native_cleanup_releases_pending_count(self):
        widget = ImageWidget(attachment_path=None)
        widget._pending_native_png = b"png"
        app = MagicMock(_native_pending_count=1)

        with patch.object(ImageWidget, "app", PropertyMock(return_value=app)):
            widget.native_cleanup()

        assert app._native_pending_count == 0
        assert widget._pending_native_png is None

    def test_focus_border_still_works(self):
        widget = ImageWidget(attachment_path=None)
        widget.set_selected(True)
        assert widget._selected is True
        assert widget.styles.border.top[0] == "solid"


class _FakeChatLog:
    def __init__(self, region: Region) -> None:
        self.content_region = region


class _FakeImageWidget:
    def __init__(
        self,
        region: Region,
        image_id: int = 5,
        width_px: int = 320,
        msg_right: bool = False,
    ):
        self.content_region = region
        self.native_image_id = image_id
        self.native_width_px = width_px
        self.visible = True
        self._msg_right = msg_right

    def has_class(self, cls: str) -> bool:
        return self._msg_right and cls == "msg-right"


class TestAppHook:
    """post_display_hook: CATIMG no-op + screen-stack gate (C5)."""

    def test_catimg_hook_writes_nothing(self, app_for_test):
        app = app_for_test
        assert app.image_support is ImageSupport.CATIMG
        driver = MagicMock()
        app._driver = driver
        app.post_display_hook()
        driver.write.assert_not_called()

    def test_gate_screen_stack_deletes_only_chat_placements(self, app_for_test):
        app = app_for_test
        app.image_support = ImageSupport.KITTY
        written: list[str] = []
        app._native_renderer = KittyRenderer(write=written.append, cell_w=8, cell_h=16)
        app._chat_native_ids = {5, 6}  # chat ids, NOT the modal's id (99)
        app._native_renderer.transmit(5, b"png")
        app._native_renderer.transmit(6, b"png")
        written.clear()

        main_screen = object()
        modal_screen = object()
        app._screen_stacks[app._current_mode] = [main_screen, modal_screen]
        app._compose_screen = main_screen

        app.post_display_hook()
        # Per-id delete (d=i), keep data; never the global d=a.
        assert any("a=d,d=i,i=5" in s for s in written)
        assert any("a=d,d=i,i=6" in s for s in written)
        assert not any("a=d,d=a" in s for s in written)
        assert not any("a=p" in s for s in written)

        # Second call while still on the modal: already cleared → no-op.
        written.clear()
        app.post_display_hook()
        assert written == []

    def test_empty_renderer_gate_skips_widget_query(self, app_for_test):
        app = app_for_test
        app.image_support = ImageSupport.KITTY
        app._native_renderer = KittyRenderer(write=lambda _s: None, cell_w=8, cell_h=16)
        app.query = MagicMock()

        app.post_display_hook()

        app.query.assert_not_called()

    def test_sync_places_visible_native_widget(self, app_for_test):
        app = app_for_test
        app.image_support = ImageSupport.KITTY
        written: list[str] = []
        app._native_renderer = KittyRenderer(write=written.append, cell_w=8, cell_h=16)
        app._chat_log = _FakeChatLog(Region(0, 0, 60, 40))
        app.query = lambda *a, **k: [_FakeImageWidget(Region(2, 5, 40, 12))]
        app._native_last_key.clear()

        app._sync_native_images()

        assert len(written) == 1
        assert written[0].startswith("\x1b[6;3H")
        assert "a=p,i=5,p=5" in written[0]
        assert "x=0,y=0,w=320,h=192" in written[0]
        assert app._native_last_key[5] == (5, 5, 6, 3, 0, 0, 320, 192)
        assert app._chat_native_ids == {5}

    def test_sync_right_aligns_msg_right(self, app_for_test):
        app = app_for_test
        app.image_support = ImageSupport.KITTY
        written: list[str] = []
        app._native_renderer = KittyRenderer(write=written.append, cell_w=8, cell_h=16)
        app._chat_log = _FakeChatLog(Region(0, 0, 60, 40))
        # Region 40 cols wide, image only 160px (20 cols) → right-align shifts it.
        app.query = lambda *a, **k: [
            _FakeImageWidget(Region(2, 5, 40, 12), width_px=160, msg_right=True)
        ]

        app._sync_native_images()

        # region.right=42, image_cols=20 → col = 42 - 20 + 1 = 23.
        assert written[0].startswith("\x1b[6;23H")

    def test_sync_skips_unchanged_placement(self, app_for_test):
        app = app_for_test
        app.image_support = ImageSupport.KITTY
        written: list[str] = []
        app._native_renderer = KittyRenderer(write=written.append, cell_w=8, cell_h=16)
        app._chat_log = _FakeChatLog(Region(0, 0, 60, 40))
        app.query = lambda *a, **k: [_FakeImageWidget(Region(2, 5, 40, 12))]

        app._sync_native_images()
        first_len = len(written)
        app._sync_native_images()
        assert len(written) == first_len  # same key → no re-place

    def test_gate_transition_reemits_after_modal_closes(self, app_for_test):
        app = app_for_test
        app.image_support = ImageSupport.KITTY
        written: list[str] = []
        app._native_renderer = KittyRenderer(write=written.append, cell_w=8, cell_h=16)
        app._chat_log = _FakeChatLog(Region(0, 0, 60, 40))
        app.query = lambda *a, **k: [_FakeImageWidget(Region(2, 5, 40, 12))]

        # Populate chat ids by syncing once on the default screen.
        app._native_renderer.transmit(5, b"png")
        app._sync_native_images()
        written.clear()

        main_screen = object()
        modal_screen = object()
        app._screen_stacks[app._current_mode] = [main_screen, modal_screen]
        app._compose_screen = main_screen

        # Modal open → chat placements deleted per-id (d=i), data kept.
        app.post_display_hook()
        assert any("a=d,d=i,i=5" in s for s in written)
        assert not any("a=d,d=a" in s for s in written)
        written.clear()

        # Dismiss: back on the default screen → re-emit placements.
        app._screen_stacks[app._current_mode] = [main_screen]
        app.post_display_hook()
        assert any("a=p" in s for s in written)
        assert not any("a=d,d=a,q=2" in s for s in written)

    def test_on_resize_reflows_native_widget_heights(self, app_for_test):
        app = app_for_test
        app.image_support = ImageSupport.KITTY
        app._native_renderer = KittyRenderer(write=lambda s: None, cell_w=8, cell_h=16)
        widget = ImageWidget(attachment_path=None)
        widget.native_height_px = 32  # 2 rows @ 16px cell height
        app.query = lambda *a, **k: [widget]
        app._driver = MagicMock()
        app._driver.fileno = 0
        app.call_after_refresh = MagicMock()
        app.set_timer = MagicMock()

        # Font zoom: cell height 16 → 32. The widget must reflow to 1 row (P1).
        with patch.object(app_module, "get_cell_size_ioctl", return_value=(8, 32)):
            app.on_resize(MagicMock())

        assert app._native_renderer.cell_h == 32
        assert widget.styles.height.value == 1  # 32px / 32px per cell

    def test_on_resize_keeps_old_cell_size_when_ioctl_returns_none(self, app_for_test):
        app = app_for_test
        app.image_support = ImageSupport.KITTY
        app._native_renderer = KittyRenderer(write=lambda s: None, cell_w=8, cell_h=16)
        app.query = lambda *a, **k: []
        app._driver = MagicMock()
        app._driver.fileno = 0
        app.call_after_refresh = MagicMock()
        app.set_timer = MagicMock()

        # ioctl reports zero pixels → keep the previous cell size (P2).
        with patch.object(app_module, "get_cell_size_ioctl", return_value=None):
            app.on_resize(MagicMock())

        assert app._native_renderer.cell_h == 16
