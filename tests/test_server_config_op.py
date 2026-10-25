"""``server.config`` and the TUI's use of it for thumbnail limits."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

from client_harness import StubClient, client_for

from models import DEFAULT_THUMBNAIL_MAX_COLS, DEFAULT_THUMBNAIL_MAX_LINES
from tui.chat_view import ChatViewMixin


def test_op_returns_configured_limits(monkeypatch):
    monkeypatch.setenv("THUMBNAIL_MAX_LINES", "20")
    monkeypatch.setenv("THUMBNAIL_MAX_COLS", "80")
    out = client_for(MagicMock()).call("server.config")
    assert out == {"thumbnail_max_lines": 20, "thumbnail_max_cols": 80}


def test_op_defaults_match_model_constants(monkeypatch, tmp_path):
    monkeypatch.delenv("THUMBNAIL_MAX_LINES", raising=False)
    monkeypatch.delenv("THUMBNAIL_MAX_COLS", raising=False)
    monkeypatch.setattr("protocols.config.PROJECT_DIR", tmp_path)
    out = client_for(MagicMock()).call("server.config")
    assert out["thumbnail_max_lines"] == DEFAULT_THUMBNAIL_MAX_LINES
    assert out["thumbnail_max_cols"] == DEFAULT_THUMBNAIL_MAX_COLS


class _View(ChatViewMixin):
    pass


def test_tui_asks_once_and_caches():
    view = _View()
    view.client = StubClient({"thumbnail_max_lines": 7, "thumbnail_max_cols": 33})
    assert view._thumbnail_limits() == (7, 33)
    assert view._thumbnail_limits() == (7, 33)
    assert [name for name, _ in view.client.calls] == ["server.config"]


def test_tui_falls_back_to_defaults_and_retries():
    view = _View()
    view.client = StubClient(
        RuntimeError("down"), {"thumbnail_max_lines": 5, "thumbnail_max_cols": 9}
    )
    assert view._thumbnail_limits() == (
        DEFAULT_THUMBNAIL_MAX_LINES,
        DEFAULT_THUMBNAIL_MAX_COLS,
    )
    assert view._thumbnail_limits() == (5, 9)


def test_chat_view_has_no_backend_imports():
    tree = ast.parse(Path("tui/chat_view.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("protocols"), node.lineno
