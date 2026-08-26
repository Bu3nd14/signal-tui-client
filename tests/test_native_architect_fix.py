"""Acceptance tests for the native-image CPU/modal/placement fix (8e3cf89)."""

from __future__ import annotations

import asyncio
import io
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

from PIL import Image
from textual.geometry import Region, Size
from textual.screen import Screen

import ui_components
from tui.images.detect import ImageSupport
from tui.images.kitty_renderer import KittyRenderer, transmit_chunks
from ui_components import ImageModalScreen, ImageWidget


def _png(width: int = 16, height: int = 32, *, padding: int = 0) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), "navy").save(output, "PNG")
    return output.getvalue() + b"x" * padding


class _ChatLog:
    content_region = Region(0, 0, 80, 40)
    max_scroll_y = 0
    scroll_end = MagicMock()


def _native_app(app_for_test):
    app = app_for_test
    app.image_support = ImageSupport.KITTY
    written: list[str] = []
    app._native_renderer = KittyRenderer(write=written.append, cell_w=8, cell_h=16)
    app._chat_log = _ChatLog()
    main_screen = object()
    app._screen_stacks[app._current_mode] = [main_screen]
    app._compose_screen = main_screen
    return app, written


def test_first_direct_mounted_registration_is_placed_on_next_tick(
    app_for_test, tmp_path
):
    """A transmit itself heats the gate; no pending stash is required."""
    app, written = _native_app(app_for_test)
    widget = ImageWidget(attachment_path=tmp_path / "photo.png")
    widget._is_mounted = True
    png = _png()
    app._register_native_thumbnail(widget, tmp_path / "photo.png", png)
    assert app._native_pending_count == 0
    assert app._native_renderer.has_data is True
    written.clear()

    app.query = lambda *_a, **_kw: [widget]
    with patch.object(
        ImageWidget, "content_region", PropertyMock(return_value=Region(2, 5, 20, 2))
    ):
        app._native_sync_tick()

    assert len([item for item in written if "a=p,i=1,p=1" in item]) == 1


def test_cold_gate_skips_every_sync_and_rearms_after_cleanup(app_for_test):
    app, _written = _native_app(app_for_test)
    sync = MagicMock()
    app._sync_native_images = sync

    for _ in range(20):
        app._native_sync_tick()
    sync.assert_not_called()

    widget = ImageWidget(attachment_path=None)
    app._native_renderer.transmit(7, _png())
    widget.show_native_thumbnail(app._native_renderer, 7, _png())
    app._native_sync_tick()
    sync.assert_called_once()

    widget.native_cleanup()
    assert app._native_renderer.has_data is False
    for _ in range(20):
        app._native_sync_tick()
    sync.assert_called_once()


def test_hires_modal_bypasses_saturated_thumbnail_workers_and_writes_once(
    app_for_test, tmp_path
):
    """Decode and DCS chunking run on the dedicated executor, not the UI thread."""
    app, _written = _native_app(app_for_test)
    driver_write = MagicMock()
    renderer = KittyRenderer(write=driver_write, cell_w=8, cell_h=16)
    path = tmp_path / "large.png"
    path.write_bytes(_png())
    modal = ImageModalScreen(
        path, renderer, image_id=99, hires_executor=app._hires_executor
    )

    release = threading.Event()
    all_blocked = threading.Event()
    blocked = 0
    lock = threading.Lock()

    def slow_thumbnail(*_args):
        nonlocal blocked
        with lock:
            blocked += 1
            if blocked == 4:
                all_blocked.set()
        assert release.wait(5)

    thumbnail_renderer = MagicMock()
    thumbnail_renderer.prepare_thumbnail.side_effect = slow_thumbnail
    thumbnail_pool = ThreadPoolExecutor(max_workers=4)
    thumbnail_jobs = [
        thumbnail_pool.submit(thumbnail_renderer.prepare_thumbnail, path, 12, 60)
        for _ in range(4)
    ]
    assert all_blocked.wait(2), "thumbnail workers did not saturate"

    ui_thread = threading.get_ident()
    worker_threads: list[int] = []

    def prepare(*_args):
        worker_threads.append(threading.get_ident())
        return _png(40, 80, padding=10_000)

    def chunk(image_id, png):
        worker_threads.append(threading.get_ident())
        return transmit_chunks(image_id, png)

    try:
        with (
            patch.object(ui_components, "prepare_hi_res", side_effect=prepare),
            patch.object(ui_components, "transmit_chunks", side_effect=chunk),
            patch.object(Screen, "size", PropertyMock(return_value=Size(80, 24))),
        ):
            asyncio.run(asyncio.wait_for(modal._prepare_native(99, (640, 352)), 2))
    finally:
        release.set()
        wait(thumbnail_jobs, timeout=5)
        thumbnail_pool.shutdown(wait=True)
        app._hires_executor.shutdown(wait=True)

    assert worker_threads and all(thread != ui_thread for thread in worker_threads)
    assert len(set(worker_threads)) == 1
    transmit_writes = [
        call.args[0]
        for call in driver_write.call_args_list
        if "a=t,i=99" in call.args[0]
    ]
    assert len(transmit_writes) == 1
    assert transmit_writes[0].count("\x1b_G") > 1  # concatenated DCS chunks


def test_screen_stack_deletes_chat_once_keeps_modal_and_replaces_chat(app_for_test):
    app, written = _native_app(app_for_test)
    renderer = app._native_renderer
    renderer.transmit(5, _png())
    renderer.place(5, 5, row=2, col=2, w_px=16, h_px=32)
    renderer.transmit(99, _png())
    renderer.place(99, 99, row=4, col=4, w_px=16, h_px=32)
    app._chat_native_ids = {5}
    written.clear()

    main, modal = object(), object()
    app._screen_stacks[app._current_mode] = [main, modal]
    app._compose_screen = main
    app._native_sync_tick()
    app._native_sync_tick()

    deletes = [item for item in written if "a=d,d=i,i=5" in item]
    assert len(deletes) == 1
    assert (99, 99) in renderer._placed
    assert (5, 5) not in renderer._placed

    widget = SimpleNamespace(
        native_image_id=5,
        native_width_px=16,
        visible=True,
        content_region=Region(2, 5, 2, 2),
        has_class=lambda _class: False,
    )
    app.query = lambda *_a, **_kw: [widget]
    app._screen_stacks[app._current_mode] = [main]
    written.clear()
    app._native_sync_tick()
    assert len([item for item in written if "a=p,i=5,p=5" in item]) == 1
    assert (99, 99) in renderer._placed


def test_pending_stash_cleanup_cools_gate(app_for_test, tmp_path):
    app, _written = _native_app(app_for_test)
    widget = ImageWidget(attachment_path=None)
    widget._is_mounted = False
    app._finish_native_thumbnail(widget, tmp_path / "photo.png", _png())
    assert app._native_pending_count == 1

    with patch.object(ImageWidget, "app", PropertyMock(return_value=app)):
        widget.native_cleanup()

    assert app._native_pending_count == 0
    assert widget._pending_native_png is None
    app.query = MagicMock()
    app._native_sync_tick()
    app.query.assert_not_called()
