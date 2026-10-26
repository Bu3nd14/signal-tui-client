"""Native-image shutdown safety: teardown gate and one-shot exit flag.

Regression tests for the crash at teardown: ``App._close_all`` clears the
screen stack before the final ``_flush_next_callbacks`` runs a timer callback
already queued via ``call_next``.  Reading ``self.screen`` then raises
``ScreenStackError`` (not ``IndexError``).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tui.images.detect import ImageSupport
from tui.images.kitty_renderer import KittyRenderer


def _native_app(app_for_test):
    app = app_for_test
    app.image_support = ImageSupport.KITTY
    written: list[str] = []
    app._native_renderer = KittyRenderer(write=written.append, cell_w=8, cell_h=16)
    return app, written


def _heat_gate(app) -> None:
    app._native_widgets[1] = MagicMock()


def _empty_screen_stack(app) -> None:
    app._screen_stacks[app._current_mode] = []


@pytest.mark.parametrize("shutting_down", [False, True])
@pytest.mark.parametrize(
    "last_sync,leading",
    [(float("inf"), False), (-float("inf"), True)],
    ids=["trailing", "leading"],
)
def test_post_display_hook_gated_by_shutting_down(
    app_for_test, shutting_down, last_sync, leading
):
    app, _written = _native_app(app_for_test)
    _heat_gate(app)
    app._native_sync_timer = None
    app._last_sync = last_sync
    app._native_shutting_down = shutting_down
    app.set_timer = MagicMock()
    app._native_sync_tick = MagicMock()

    app.post_display_hook()

    if shutting_down:
        app.set_timer.assert_not_called()
        app._native_sync_tick.assert_not_called()
    elif leading:
        app._native_sync_tick.assert_called_once()
        app.set_timer.assert_not_called()
    else:
        app.set_timer.assert_called_once_with(0.075, app._fire_sync)
        app._native_sync_tick.assert_not_called()


def test_native_sync_tick_skips_empty_screen_stack(app_for_test):
    app, written = _native_app(app_for_test)
    _heat_gate(app)
    _empty_screen_stack(app)
    app._native_sync_tick()
    assert written == []


def test_post_display_hook_leading_tick_survives_empty_stack(app_for_test):
    app, written = _native_app(app_for_test)
    _heat_gate(app)
    _empty_screen_stack(app)
    app._native_shutting_down = False
    app._native_sync_timer = None
    app._last_sync = -float("inf")
    app.post_display_hook()
    assert written == []
