"""Headless smoke test: the app boots, renders, and exits cleanly."""

from __future__ import annotations

import pytest


@pytest.mark.smoke
async def test_app_boots_and_exits(app_for_test):
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.screen is not None
        await pilot.exit(None)
