"""
Unit tests for ``tui/images/detect.py`` and the image config getters.

All terminal I/O is injected (``env`` / ``isatty`` / ``which`` / ``query_cb``),
so these tests are headless and deterministic — no real TGP query is emitted.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backends import config
from tui.images.detect import (
    ImageSupport,
    _is_wezterm,
    detect_image_support,
    query_kitty_ok,
)


def _no_catimg(name: str) -> None:
    return None


def _has_catimg(name: str) -> str | None:
    return "/usr/bin/catimg" if name == "catimg" else None


def _detect(
    env: dict | None = None,
    *,
    isatty: bool = True,
    override: str | None = None,
    which=_no_catimg,
    query_cb=None,
) -> ImageSupport:
    return detect_image_support(
        isatty=isatty,
        env=env or {},
        override=override,
        which=which,
        query_cb=query_cb,
    )


class TestOverride:
    def test_kitty(self):
        assert _detect(override="kitty") is ImageSupport.KITTY

    def test_catimg(self):
        assert _detect(override="catimg") is ImageSupport.CATIMG

    def test_off(self):
        assert _detect(override="off") is ImageSupport.OFF

    def test_override_wins_over_non_tty(self):
        assert _detect(override="kitty", isatty=False) is ImageSupport.KITTY

    def test_override_case_insensitive(self):
        assert _detect(override="  KITTY  ") is ImageSupport.KITTY

    def test_auto_falls_through_to_detection(self):
        # "auto" is not an override → non-tty falls back to CATIMG.
        assert _detect(override="auto", isatty=False) is ImageSupport.CATIMG


class TestNonTty:
    def test_non_tty_returns_catimg(self):
        assert _detect(isatty=False) is ImageSupport.CATIMG

    def test_non_tty_never_queries(self):
        calls: list[bool] = []
        result = _detect(
            isatty=False,
            env={"TERM": "xterm-kitty"},
            query_cb=lambda: calls.append(True) or True,
        )
        assert result is ImageSupport.CATIMG
        assert calls == []


class TestTmuxScreenGuard:
    def test_tmux_present(self):
        assert (
            _detect(
                env={"TMUX": "/tmp/tmux-1000/default,1234,0", "TERM": "xterm-kitty"},
                query_cb=lambda: True,
            )
            is ImageSupport.CATIMG
        )

    def test_term_screen(self):
        assert _detect(env={"TERM": "screen"}) is ImageSupport.CATIMG

    def test_term_screen_256color(self):
        assert _detect(env={"TERM": "screen-256color"}) is ImageSupport.CATIMG

    def test_tmux_never_queries(self):
        calls: list[bool] = []
        _detect(
            env={"TMUX": "/tmp/tmux-1000/default,1234,0", "TERM": "xterm-kitty"},
            query_cb=lambda: calls.append(True) or True,
        )
        assert calls == []


class TestKittyGate:
    def test_xterm_kitty_query_ok(self):
        assert (
            _detect(env={"TERM": "xterm-kitty"}, query_cb=lambda: True)
            is ImageSupport.KITTY
        )

    def test_xterm_kitty_query_ko_with_catimg(self):
        assert (
            _detect(
                env={"TERM": "xterm-kitty"}, which=_has_catimg, query_cb=lambda: False
            )
            is ImageSupport.CATIMG
        )

    def test_xterm_kitty_query_ko_without_catimg(self):
        assert (
            _detect(
                env={"TERM": "xterm-kitty"}, which=_no_catimg, query_cb=lambda: False
            )
            is ImageSupport.OFF
        )


class TestFalsePositiveTerm:
    def test_iterm2_xterm_256color_never_kitty(self):
        # iTerm2 / Ghostty advertise TERM=xterm-256color; even if the query
        # (wrongly) answered OK, the TERM gate must prevent KITTY.
        assert (
            _detect(
                env={"TERM": "xterm-256color"}, which=_has_catimg, query_cb=lambda: True
            )
            is ImageSupport.CATIMG
        )

    def test_xterm_256color_without_catimg(self):
        assert (
            _detect(
                env={"TERM": "xterm-256color"}, which=_no_catimg, query_cb=lambda: True
            )
            is ImageSupport.OFF
        )

    def test_xterm_256color_never_queries(self):
        calls: list[bool] = []
        _detect(
            env={"TERM": "xterm-256color"},
            query_cb=lambda: calls.append(True) or True,
        )
        assert calls == []


class TestWezTermGate:
    """WezTerm is a native-graphics terminal (paints kitty placements)."""

    def test_wezterm_pane_marker(self):
        assert (
            _detect(
                env={"TERM": "xterm-256color", "WEZTERM_PANE": "1"},
                which=_has_catimg,
                query_cb=lambda: True,
            )
            is ImageSupport.KITTY
        )

    def test_wezterm_unix_socket_marker(self):
        assert (
            _detect(
                env={"TERM": "xterm-256color", "WEZTERM_UNIX_SOCKET": "/run/wezterm"},
                which=_has_catimg,
                query_cb=lambda: True,
            )
            is ImageSupport.KITTY
        )

    def test_wezterm_term_program(self):
        assert (
            _detect(
                env={"TERM": "xterm-256color", "TERM_PROGRAM": "WezTerm"},
                which=_has_catimg,
                query_cb=lambda: True,
            )
            is ImageSupport.KITTY
        )

    def test_term_wezterm(self):
        assert (
            _detect(env={"TERM": "wezterm"}, which=_has_catimg, query_cb=lambda: True)
            is ImageSupport.KITTY
        )

    def test_query_ko_with_catimg(self):
        # WezTerm marker wins over the query: even a KO query → KITTY (via ssh
        # the TGP reply doesn't reach us, but the rendering works).
        assert (
            _detect(
                env={"TERM": "xterm-256color", "WEZTERM_PANE": "1"},
                which=_has_catimg,
                query_cb=lambda: False,
            )
            is ImageSupport.KITTY
        )

    def test_query_ko_without_catimg(self):
        assert (
            _detect(
                env={"TERM": "xterm-256color", "WEZTERM_PANE": "1"},
                which=_no_catimg,
                query_cb=lambda: False,
            )
            is ImageSupport.KITTY
        )

    def test_tmux_guard_prioritary_never_queries(self):
        calls: list[bool] = []
        result = _detect(
            env={
                "TMUX": "/tmp/tmux-1000/default,1234,0",
                "TERM": "xterm-256color",
                "WEZTERM_PANE": "1",
            },
            which=_has_catimg,
            query_cb=lambda: calls.append(True) or True,
        )
        assert result is ImageSupport.CATIMG
        assert calls == []

    def test_marker_skips_query(self):
        # WezTerm marker is reliable → KITTY WITHOUT any TGP query.
        calls: list[bool] = []
        result = _detect(
            env={"TERM": "xterm-256color", "WEZTERM_PANE": "1"},
            which=_has_catimg,
            query_cb=lambda: calls.append(True) or True,
        )
        assert result is ImageSupport.KITTY
        assert calls == []


class TestIsWezTerm:
    """``_is_wezterm`` è una funzione pura, testabile direttamente."""

    def test_markers(self):
        assert _is_wezterm({"WEZTERM_PANE": "1"}) is True
        assert _is_wezterm({"WEZTERM_UNIX_SOCKET": "/run/wezterm"}) is True
        assert _is_wezterm({"TERM_PROGRAM": "WezTerm"}) is True
        assert _is_wezterm({"TERM": "wezterm"}) is True

    def test_not_wezterm(self):
        assert _is_wezterm({}) is False
        assert _is_wezterm({"TERM": "xterm-256color"}) is False
        assert _is_wezterm({"TERM_PROGRAM": "iTerm.app"}) is False
        assert _is_wezterm({"WEZTERM_PANE": ""}) is False


class TestFallback:
    def test_catimg_present(self):
        assert (
            _detect(env={"TERM": "xterm-256color"}, which=_has_catimg)
            is ImageSupport.CATIMG
        )

    def test_catimg_absent(self):
        assert (
            _detect(env={"TERM": "xterm-256color"}, which=_no_catimg)
            is ImageSupport.OFF
        )


class TestQueryKittyOk:
    def test_non_tty_returns_false(self):
        # A pipe is not a tty → tcgetattr fails → no query, returns False.
        read_fd, write_fd = os.pipe()
        try:
            assert query_kitty_ok(read_fd, write_fd) is False
        finally:
            os.close(read_fd)
            os.close(write_fd)


class TestConfigGetters:
    def test_image_protocol_default(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.delenv("IMAGE_PROTOCOL", raising=False)
        assert config.image_protocol() == "auto"

    def test_image_protocol_env(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.setenv("IMAGE_PROTOCOL", "KITTY")
        assert config.image_protocol() == "kitty"

    def test_image_protocol_config(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.delenv("IMAGE_PROTOCOL", raising=False)
        (tmp_path / "config.json").write_text(json.dumps({"image_protocol": "off"}))
        assert config.image_protocol() == "off"

    def test_image_protocol_env_precedence(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.setenv("IMAGE_PROTOCOL", "catimg")
        (tmp_path / "config.json").write_text(json.dumps({"image_protocol": "kitty"}))
        assert config.image_protocol() == "catimg"

    def test_thumbnail_max_lines_default(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.delenv("THUMBNAIL_MAX_LINES", raising=False)
        assert config.thumbnail_max_lines() == 12

    def test_thumbnail_max_lines_env(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.setenv("THUMBNAIL_MAX_LINES", "20")
        assert config.thumbnail_max_lines() == 20

    def test_thumbnail_max_lines_invalid(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.setenv("THUMBNAIL_MAX_LINES", "abc")
        assert config.thumbnail_max_lines() == 12

    def test_thumbnail_max_cols_default(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.delenv("THUMBNAIL_MAX_COLS", raising=False)
        assert config.thumbnail_max_cols() == 60

    def test_thumbnail_max_cols_config(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.delenv("THUMBNAIL_MAX_COLS", raising=False)
        (tmp_path / "config.json").write_text(json.dumps({"thumbnail_max_cols": 80}))
        assert config.thumbnail_max_cols() == 80
