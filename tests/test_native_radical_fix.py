"""Acceptance tests for the radical native-image CPU/modal fix (62a2d15)."""

from __future__ import annotations

import asyncio
import io
import threading
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from PIL import Image, JpegImagePlugin
from textual.app import App
from textual.geometry import Region, Size
from textual.screen import ModalScreen, Screen
from textual.widgets import Static

import tui.app as app_module
from tui.images.detect import ImageSupport
from tui.images.kitty_renderer import KittyRenderer, prepare_hi_res
from ui_components import ImageModalScreen, ImageWidget


def _png(width: int = 80, height: int = 16) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), "navy").save(output, "PNG")
    return output.getvalue()


def _native_app(app_for_test):
    app = app_for_test
    app.image_support = ImageSupport.KITTY
    written: list[str] = []
    app._native_renderer = KittyRenderer(write=written.append, cell_w=8, cell_h=16)
    return app, written


@pytest.mark.integration
async def test_registry_sync_does_not_scan_large_dom(app_for_test):
    """One frame visits five registry entries, not 1,000 unrelated widgets."""
    app, written = _native_app(app_for_test)
    async with app.run_test() as pilot:
        await app._chat_log.mount(*(Static("") for _ in range(1_000)))
        await pilot.pause()
        container = app._chat_log.content_region
        widgets = []
        for image_id in range(1, 6):
            widget = SimpleNamespace(
                native_image_id=image_id,
                native_width_px=80,
                visible=True,
                is_mounted=True,
                content_region=Region(container.x + image_id, container.y, 10, 1),
                has_class=lambda _name: False,
            )
            widgets.append(widget)
            app._native_widgets[image_id] = widget

        written.clear()
        app._last_sync = 0.0
        with (
            patch.object(App, "query", wraps=App.query) as query_spy,
            patch.object(
                app_module,
                "compute_source_rect",
                wraps=app_module.compute_source_rect,
            ) as rect_spy,
        ):
            app.post_display_hook()

        query_spy.assert_not_called()
        assert rect_spy.call_count == len(widgets) == 5
        placements = [entry for entry in written if "a=p" in entry]
        assert len(placements) == 5
        for image_id, widget in enumerate(widgets, 1):
            expected_col = widget.content_region.x + 1
            assert any(
                f"[{container.y + 1};{expected_col}H" in entry
                and f"a=p,i={image_id},p={image_id}" in entry
                and "x=0,y=0,w=80,h=16" in entry
                for entry in placements
            )


def test_cleanup_and_chat_switch_empty_registry_and_cool_gate(app_for_test):
    app, _written = _native_app(app_for_test)
    widgets = [ImageWidget(attachment_path=None) for _ in range(2)]
    for image_id, widget in enumerate(widgets, 1):
        app._native_renderer.transmit(image_id, _png())
        widget.show_native_thumbnail(app._native_renderer, image_id, _png())
        app._native_widgets[image_id] = widget

    with patch.object(ImageWidget, "app", PropertyMock(return_value=app)):
        widgets[0].native_cleanup()
    assert 1 not in app._native_widgets

    chat_log = SimpleNamespace(
        children=[widgets[1]], remove_children=MagicMock(), max_scroll_y=0
    )
    app._chat_log = chat_log
    with patch.object(ImageWidget, "app", PropertyMock(return_value=app)):
        app._clear_chat()
    assert app._native_widgets == {}
    assert app._native_stashed == set()

    app.query = MagicMock()
    app._sync_native_images = MagicMock()
    app.post_display_hook()
    app.query.assert_not_called()
    app._sync_native_images.assert_not_called()


def test_post_display_hook_is_leading_trailing_and_bounded(app_for_test):
    app, _written = _native_app(app_for_test)
    app._native_widgets[1] = object()
    positions: list[int] = []
    current = 0
    now = 100.0
    scheduled: list[tuple[float, object]] = []

    def sync() -> None:
        positions.append(current)

    def set_timer(delay, callback):
        scheduled.append((now + delay, callback))
        return object()

    def fire_due() -> None:
        due = [timer for timer in scheduled if timer[0] <= now]
        for timer in due:
            scheduled.remove(timer)
            timer[1]()

    app._native_sync_tick = MagicMock(side_effect=sync)
    app.set_timer = set_timer
    app._last_sync = 0.0
    with patch.object(app_module.time, "monotonic", side_effect=lambda: now):
        app.post_display_hook()
        assert positions == [0], "first placement must be synchronous (leading)"
        for current in range(1, 31):
            now = 100.0 + current * 0.01
            fire_due()
            app.post_display_hook()
        calls_at_stop = len(positions)
        now += 0.1
        fire_due()

    assert len(positions) <= 6
    assert len(positions) > calls_at_stop
    assert positions[-1] == 30


def test_prepare_hires_24mp_jpeg_uses_draft_and_meets_budget(tmp_path):
    """Benchmark includes decode/resize/PNG encode, but excludes fixture creation."""
    path = tmp_path / "photo-24mp.jpg"
    source = Image.effect_noise((1_500, 1_000), 35).convert("RGB")
    source.resize((6_000, 4_000), Image.Resampling.BICUBIC).save(
        path, "JPEG", quality=88
    )
    drafted_sizes: list[tuple[int, int]] = []
    original_draft = JpegImagePlugin.JpegImageFile.draft

    def recording_draft(self, mode, size):
        result = original_draft(self, mode, size)
        drafted_sizes.append(self.size)
        return result

    with patch.object(JpegImagePlugin.JpegImageFile, "draft", recording_draft):
        started = perf_counter()
        payload = prepare_hi_res(path, 1_600, 1_600)
        elapsed = perf_counter() - started

    assert drafted_sizes and drafted_sizes[0][0] * drafted_sizes[0][1] < 24_000_000
    assert elapsed < 0.600, f"24MP preparation took {elapsed * 1000:.1f}ms"
    assert len(payload) < 3.5 * 1024 * 1024


@pytest.mark.integration
async def test_modal_loading_removed_when_ready_and_dismiss_cancels(tmp_path):
    path = tmp_path / "photo.jpg"
    Image.new("RGB", (100, 50), "blue").save(path, "JPEG")
    written: list[str] = []
    renderer = KittyRenderer(write=written.append, cell_w=8, cell_h=16)
    executor = ThreadPoolExecutor(max_workers=1)
    modal = ImageModalScreen(path, renderer, image_id=9, hires_executor=executor)

    loading = next(iter(modal.compose()))
    assert loading.id == "native-modal-loading"
    loading.remove = MagicMock()
    modal.query_one = MagicMock(return_value=loading)
    with patch.object(Screen, "size", PropertyMock(return_value=Size(80, 24))):
        await modal._prepare_native(9, (1_600, 1_600))
    loading.remove.assert_called_once()

    started = threading.Event()
    release = threading.Event()

    def blocked_payload(*_args):
        started.set()
        assert release.wait(2)
        return _png(), "payload"

    modal2 = ImageModalScreen(path, renderer, image_id=10, hires_executor=executor)
    modal2._build_native_payload = blocked_payload
    task = asyncio.create_task(modal2._prepare_native(10, (1_600, 1_600)))
    assert await asyncio.to_thread(started.wait, 1)
    with patch.object(ModalScreen, "dismiss", return_value=None):
        modal2.dismiss()
    release.set()
    await task
    executor.shutdown(wait=True)

    assert modal2._cancelled is True
    assert modal2._native_png is None
    assert not any("a=t,i=10" in entry for entry in written)
