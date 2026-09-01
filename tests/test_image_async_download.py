"""
Regression tests for Fix 3 — asynchronous image attachment download.

Before the fix, ``_render_image_in_chat`` called ``manager.get_attachment_path``
synchronously on the UI thread (for WhatsApp this runs a 60s ``urlopen`` +
``write_bytes``).  The fix mounts a placeholder ``ImageWidget`` immediately and
resolves the path in a worker thread, then updates the widget on the UI thread.

These tests verify the fix is behavior-preserving:
  T3a: ``_render_image_in_chat`` returns immediately, does NOT download
       synchronously, and mounts a placeholder (path=None, id set).
  T3b: after resolution the widget has the correct path + click-to-view text.
  T3c: resolution returning None → path=None fallback, still clickable.
  T3d: ``_finish_attachment_resolve`` on an unmounted widget is a no-op.
  T3e: Signal/Telegram with an existing local file → correct path;
       missing file → fallback.
  T3f: ``ImageWidget.update_attachment`` sets the path and the next click
       emits ``ImageClicked`` with the new path.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from models import PROTOCOL_SIGNAL, PROTOCOL_TELEGRAM
from protocols import BackendManager, SignalBackend, TelegramBackend
from signal_tui import SignalTUI
from ui_components import ImageWidget


class _FakeChatLog:
    """Minimal stand-in for the #chat-log widget; marks mounted widgets as mounted."""

    def __init__(self) -> None:
        self.children: list = []

    def mount(self, *widgets, before=None, after=None):
        for w in widgets:
            w._is_mounted = True
            self.children.append(w)

    def scroll_end(self, animate: bool = False) -> None:
        pass


def _make_app() -> SignalTUI:
    """App with a real manager, worker captured inline, no real backends."""
    app = SignalTUI()
    app.manager = BackendManager()
    app.run_worker = MagicMock()
    app.call_from_thread = MagicMock(side_effect=lambda fn, *a, **k: fn(*a, **k))
    return app


def _run_worker(app: SignalTUI) -> None:
    """Execute the single scheduled worker inline."""
    app.run_worker.call_args.args[0]()


class TestImageAsyncDownload:
    """🖼️ T3a-f — download immagini asincrono, senza freeze UI."""

    # ── T3a: placeholder mounted, no synchronous download ────────────────

    def test_render_mounts_placeholder_without_sync_download(self):
        """(a) placeholder subito (path=None, id valorizzato), niente download sync."""
        app = _make_app()
        app.manager.get_attachment_path = MagicMock(return_value=None)

        log = _FakeChatLog()
        app._render_image_in_chat(
            attachment_id="img-1",
            attachment_info="Info",
            is_mine=False,
            chat_log=log,
            protocol=PROTOCOL_SIGNAL,
        )

        # No synchronous resolution on the UI thread.
        app.manager.get_attachment_path.assert_not_called()
        assert len(log.children) == 1
        widget = log.children[0]
        assert isinstance(widget, ImageWidget)
        assert widget.attachment_path is None
        assert widget.attachment_id == "img-1"
        assert "loading" in str(widget.render())
        # The resolution was deferred to a worker.
        assert app.run_worker.call_count == 1

    # ── T3b: resolved path updates the widget ────────────────────────────

    def test_resolve_updates_widget_with_path(self, tmp_path):
        """(b) a fine risoluzione: path corretto e testo 'Click Enter to View'."""
        app = _make_app()
        resolved = tmp_path / "photo.jpg"
        resolved.write_text("fake image")
        app.manager.get_attachment_path = MagicMock(return_value=resolved)

        log = _FakeChatLog()
        app._render_image_in_chat(
            attachment_id="img-1",
            attachment_info="Info",
            is_mine=False,
            chat_log=log,
            protocol=PROTOCOL_SIGNAL,
        )
        widget = log.children[0]

        _run_worker(app)

        assert widget.attachment_path == resolved
        assert "Click Enter to View" in str(widget.render())

    # ── T3c: resolution None → fallback, still clickable ─────────────────

    def test_resolve_none_keeps_fallback_and_clickable(self):
        """(c) path=None → fallback ``[🖼️ Image: {info}]``, ancora cliccabile."""
        app = _make_app()
        app.manager.get_attachment_path = MagicMock(return_value=None)

        log = _FakeChatLog()
        app._render_image_in_chat(
            attachment_id="img-1",
            attachment_info="Info",
            is_mine=False,
            chat_log=log,
            protocol=PROTOCOL_SIGNAL,
        )
        widget = log.children[0]
        _run_worker(app)

        assert widget.attachment_path is None
        assert "[🖼️ Image: Info]" in str(widget.render())

        events = []
        widget.post_message = lambda msg: events.append(msg)
        widget.on_click()
        assert len(events) == 1
        assert events[0].attachment_path is None
        assert events[0].attachment_id == "img-1"

    # ── T3d: finish on unmounted widget is a no-op ───────────────────────

    def test_finish_resolve_on_unmounted_widget_is_noop(self):
        """(d) ``_finish_attachment_resolve`` su widget non montato → no-op."""
        app = _make_app()
        widget = ImageWidget(
            attachment_path=None, attachment_id="img-1", fallback_text="placeholder"
        )
        # Not mounted: _is_mounted is False by default.
        app._finish_attachment_resolve(widget, Path("some.jpg"), "info")

        assert widget.attachment_path is None
        assert str(widget.render()) == "placeholder"

    # ── T3e: Signal / Telegram local file resolution ─────────────────────

    def test_signal_existing_file_resolves(self, tmp_path):
        """(e) Signal con file locale esistente → path corretto."""
        att_dir = tmp_path / "attachments"
        att_dir.mkdir()
        (att_dir / "att-1").write_text("fake")

        app = _make_app()
        app.manager.register(SignalBackend())
        log = _FakeChatLog()

        with patch("protocols.rpc.SIGNAL_CLI_ATTACHMENTS_DIR", att_dir):
            app._render_image_in_chat(
                attachment_id="att-1",
                attachment_info="Info",
                is_mine=False,
                chat_log=log,
                protocol=PROTOCOL_SIGNAL,
            )
            widget = log.children[0]
            _run_worker(app)

        assert widget.attachment_path == att_dir / "att-1"
        assert "Click Enter to View" in str(widget.render())

    def test_signal_missing_file_falls_back(self, tmp_path):
        """(e) Signal con file inesistente → fallback."""
        att_dir = tmp_path / "attachments"
        att_dir.mkdir()

        app = _make_app()
        app.manager.register(SignalBackend())
        log = _FakeChatLog()

        with patch("protocols.rpc.SIGNAL_CLI_ATTACHMENTS_DIR", att_dir):
            app._render_image_in_chat(
                attachment_id="att-missing",
                attachment_info="Info",
                is_mine=False,
                chat_log=log,
                protocol=PROTOCOL_SIGNAL,
            )
            widget = log.children[0]
            _run_worker(app)

        assert widget.attachment_path is None
        assert "[🖼️ Image: Info]" in str(widget.render())

    def test_telegram_existing_file_resolves(self, tmp_path):
        """(e) Telegram con file locale esistente → path corretto."""
        f = tmp_path / "photo.jpg"
        f.write_text("fake")

        app = _make_app()
        app.manager.register(TelegramBackend())
        log = _FakeChatLog()

        app._render_image_in_chat(
            attachment_id=str(f),
            attachment_info="Info",
            is_mine=False,
            chat_log=log,
            protocol=PROTOCOL_TELEGRAM,
        )
        widget = log.children[0]
        _run_worker(app)

        assert widget.attachment_path == f

    def test_telegram_missing_file_falls_back(self, tmp_path):
        """(e) Telegram con file inesistente → fallback."""
        missing = tmp_path / "nope.jpg"

        app = _make_app()
        app.manager.register(TelegramBackend())
        log = _FakeChatLog()

        app._render_image_in_chat(
            attachment_id=str(missing),
            attachment_info="Info",
            is_mine=False,
            chat_log=log,
            protocol=PROTOCOL_TELEGRAM,
        )
        widget = log.children[0]
        _run_worker(app)

        assert widget.attachment_path is None
        assert "[🖼️ Image: Info]" in str(widget.render())

    # ── T3f: update_attachment + click emits new path ────────────────────

    def test_update_attachment_sets_path_and_click_emits_new_path(self, tmp_path):
        """(f) ``update_attachment`` setta path/testo e il click emette il nuovo path."""
        widget = ImageWidget(
            attachment_path=None, attachment_id="img-1", fallback_text="old"
        )
        new_path = tmp_path / "new.jpg"
        new_path.write_text("fake")

        widget.update_attachment(new_path, "[🖼️ Image: new.jpg — Click Enter to View]")

        assert widget.attachment_path == new_path
        assert "Click Enter to View" in str(widget.render())

        events = []
        widget.post_message = lambda msg: events.append(msg)
        widget.on_click()
        assert len(events) == 1
        assert events[0].attachment_path == new_path
        assert events[0].attachment_id == "img-1"
