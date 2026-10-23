"""Static contract checks for the Gmail manual-refresh wiring."""

from __future__ import annotations

from pathlib import Path

JS = (Path(__file__).resolve().parent.parent / "web/static/gmail.js").read_text()


def test_refresh_force_is_forwarded_to_the_api():
    assert "async function loadMessages({ force = false } = {})" in JS
    assert 'if (force) params.set("refresh", "1");' in JS


def test_refresh_status_forwards_force_and_refresh_button_uses_it():
    assert "async function loadStatus({ force = false } = {})" in JS
    assert "await loadMessages({ force });" in JS
    assert "loadStatus({ force: true })" in JS


def test_both_delete_paths_force_a_fresh_list():
    assert JS.count("loadMessages({ force: true })") >= 2
