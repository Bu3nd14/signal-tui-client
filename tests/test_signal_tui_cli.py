"""Tests for signal_tui.py's command-line argument parsing."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from signal_tui import _parse_args


def test_web_no_auth_defaults_to_false():
    args = _parse_args([])
    assert args.web_no_auth is False


def test_web_no_auth_flag_sets_true():
    args = _parse_args(["--web-no-auth"])
    assert args.web_no_auth is True


def test_web_no_auth_does_not_imply_web():
    """--web-no-auth alone doesn't turn the web UI on; --web still does that."""
    args = _parse_args(["--web-no-auth"])
    assert args.web is None
