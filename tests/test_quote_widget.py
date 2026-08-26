"""
Unit tests for ``QuoteWidget`` (quote bubble + native thumbnail slot).

Headless: the renderer is injected with a recording write callback, and the
widget's ``compose()`` is inspected directly (no real terminal I/O).  This
chunk only covers the widget surface + native-state registration; the actual
wiring / placement / path resolution arrive in later chunks.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

from PIL import Image
from textual.geometry import Region
from textual.widgets import Static

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tui.app import SignalTUI
from tui.images.detect import ImageSupport
from tui.images.kitty_renderer import KittyRenderer
from ui_components import ImageWidget, QuoteWidget


def _make_png(width: int = 16, height: int = 32) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "red").save(buf, "PNG")
    return buf.getvalue()


def _children(widget: QuoteWidget) -> tuple[Static, Static]:
    """Return ``(text_static, thumb_static)`` from the widget's compose()."""
    text_static, thumb_static = list(widget.compose())
    return text_static, thumb_static


class TestQuoteWidgetContent:
    def test_textual_content_identical(self):
        widget = QuoteWidget("🖼️ Immagine")
        text_static, _ = _children(widget)
        assert isinstance(text_static, Static)
        # Byte-identical to today's ``Static(f"▎ {quote_text}")`` bubble.
        assert text_static.content == "▎ 🖼️ Immagine"

    def test_textual_content_with_real_caption(self):
        widget = QuoteWidget("Che bella!")
        text_static, _ = _children(widget)
        assert text_static.content == "▎ Che bella!"


class TestQuoteWidgetLayout:
    def test_layout_with_caption(self):
        widget = QuoteWidget("Che bella!")
        text_static, thumb_static = _children(widget)
        assert isinstance(text_static, Static)
        assert isinstance(thumb_static, Static)
        assert text_static.content == "▎ Che bella!"
        assert thumb_static.content == ""

    def test_layout_without_caption(self):
        widget = QuoteWidget("🖼️ Immagine")
        text_static, thumb_static = _children(widget)
        assert isinstance(text_static, Static)
        assert isinstance(thumb_static, Static)
        assert text_static.content == "▎ 🖼️ Immagine"
        assert thumb_static.content == ""
        # Thumbnail slot is a distinct, always-present (empty) Static.
        assert thumb_static is not text_static

    def test_constructor_stores_metadata(self, tmp_path):
        path = tmp_path / "photo.jpg"
        widget = QuoteWidget(
            "🖼️ Immagine",
            classes="msg-quote-right",
            attachment_id="att-1",
            attachment_path=path,
            content_type="image/jpeg",
        )
        assert widget.attachment_id == "att-1"
        assert widget.attachment_path == path
        assert widget.content_type == "image/jpeg"
        text_static, _ = _children(widget)
        assert text_static.has_class("msg-quote-right")

    def test_aligned_right_flag(self):
        assert QuoteWidget("x", classes="msg-quote").aligned_right is False
        assert QuoteWidget("x", classes="msg-quote-right").aligned_right is True


class TestQuoteWidgetNative:
    def test_show_native_thumbnail_registers_state(self):
        renderer = KittyRenderer(write=lambda s: None, cell_w=8, cell_h=16)
        widget = QuoteWidget("🖼️ Immagine")

        widget.show_native_thumbnail(renderer, 5, _make_png(16, 32))

        assert widget.native_renderer is renderer
        assert widget.native_image_id == 5
        assert widget.native_width_px == 16
        assert widget.native_height_px == 32
        assert widget.styles.height.value == 2  # 32px / 16px per cell

    def test_show_hides_placeholder_text(self):
        """Il segnaposto tipizzato viene nascosto quando la thumb è attiva."""
        renderer = KittyRenderer(write=lambda s: None, cell_w=8, cell_h=16)
        widget = QuoteWidget("🖼️ Immagine")

        widget.show_native_thumbnail(renderer, 5, _make_png(16, 32))

        assert widget._text_static.display is False

    def test_show_keeps_real_caption_visible(self):
        """Una caption reale resta visibile accanto alla thumbnail."""
        renderer = KittyRenderer(write=lambda s: None, cell_w=8, cell_h=16)
        widget = QuoteWidget("Che bella!")

        widget.show_native_thumbnail(renderer, 5, _make_png(16, 32))

        assert widget._text_static.display is True

    def test_show_hides_composite_placeholder(self):
        """Il caso composito "filename — placeholder" viene nascosto."""
        renderer = KittyRenderer(write=lambda s: None, cell_w=8, cell_h=16)
        widget = QuoteWidget("foto.png — 🖼️ Immagine")

        widget.show_native_thumbnail(renderer, 5, _make_png(16, 32))

        assert widget._text_static.display is False

    def test_native_cleanup_reshows_text(self):
        """Al cleanup il fallback testuale riappare."""
        renderer = KittyRenderer(write=lambda s: None, cell_w=8, cell_h=16)
        widget = QuoteWidget("🖼️ Immagine")
        widget.show_native_thumbnail(renderer, 5, _make_png(16, 32))
        assert widget._text_static.display is False

        widget.native_cleanup()

        assert widget._text_static.display is True

    def test_text_visible_without_thumbnail(self):
        """Senza thumbnail (non-kitty / non ancora risolta) il testo è visibile."""
        widget = QuoteWidget("🖼️ Immagine")
        assert widget._text_static.display is True

    def test_show_native_thumbnail_does_not_touch_text(self):
        renderer = KittyRenderer(write=lambda s: None, cell_w=8, cell_h=16)
        widget = QuoteWidget("🖼️ Immagine")

        widget.show_native_thumbnail(renderer, 5, _make_png(16, 32))

        text_static, _ = _children(widget)
        assert text_static.content == "▎ 🖼️ Immagine"

    def test_show_native_thumbnail_only_registers_state(self):
        written: list[str] = []
        renderer = KittyRenderer(write=written.append, cell_w=8, cell_h=16)
        widget = QuoteWidget("🖼️ Immagine")

        widget.show_native_thumbnail(renderer, 5, _make_png(16, 32))

        # Registration only: no transmit/place/delete emitted here.
        assert written == []

    def test_native_cleanup_deletes_once_and_idempotent(self):
        written: list[str] = []
        renderer = KittyRenderer(write=written.append, cell_w=8, cell_h=16)
        widget = QuoteWidget("🖼️ Immagine")
        widget.show_native_thumbnail(renderer, 7, _make_png(16, 32))

        widget.native_cleanup()

        assert any("a=d,d=I,i=7" in s for s in written)
        assert widget.native_renderer is None
        assert widget.native_image_id is None
        assert widget.native_width_px is None
        assert widget.native_height_px is None

        # Second call is a no-op (idempotent).
        count = len(written)
        widget.native_cleanup()
        assert len(written) == count

    def test_no_renderer_emission_without_show(self):
        written: list[str] = []
        renderer = KittyRenderer(write=written.append, cell_w=8, cell_h=16)
        widget = QuoteWidget("🖼️ Immagine")
        # Even when a renderer is bound, construction + compose never write
        # to it (renderer I/O happens only in show_native_thumbnail/cleanup).
        widget.native_renderer = renderer
        _children(widget)
        assert written == []


class _HookFakeChatLog:
    """Minimal stand-in for ``#chat-log`` used by the app hook tests."""

    def __init__(self, region: Region) -> None:
        self.content_region = region
        self.children: list = []

    def remove_children(self) -> None:
        self.children = []

    def scroll_end(self, animate: bool = False) -> None:
        pass


class TestQuoteWidgetHook:
    """post_display_hook placement + cleanup for ``QuoteWidget`` (chunk 3)."""

    def _kitty_app(self, app_for_test):
        app = app_for_test
        app.image_support = ImageSupport.KITTY
        written: list[str] = []
        app._native_renderer = KittyRenderer(write=written.append, cell_w=8, cell_h=16)
        return app, written

    def _quote_widget(
        self,
        image_id: int = 5,
        width_px: int = 48,
        thumb_region: Region | None = None,
    ) -> QuoteWidget:
        widget = QuoteWidget("🖼️ Immagine")
        widget.native_image_id = image_id
        widget.native_width_px = width_px
        widget.native_height_px = 54
        if thumb_region is None:
            thumb_region = Region(2, 5, 6, 3)
        widget.thumbnail_region = lambda: thumb_region
        return widget

    def test_sync_places_quote_widget_from_thumb_region(self, app_for_test):
        app, written = self._kitty_app(app_for_test)
        app._chat_log = _HookFakeChatLog(Region(0, 0, 60, 40))
        app.query = lambda *a, **k: [self._quote_widget()]
        app._native_last_key.clear()

        app._sync_native_images()

        # Placement derived from the 6×3 thumb region (not the container).
        assert len(written) == 1
        assert written[0].startswith("\x1b[6;3H")
        assert "a=p,i=5,p=5" in written[0]
        assert "x=0,y=0,w=48,h=48" in written[0]
        assert app._chat_native_ids == {5}

    def test_sync_right_aligns_quote_widget(self, app_for_test):
        app, written = self._kitty_app(app_for_test)
        app._chat_log = _HookFakeChatLog(Region(0, 0, 60, 40))
        # 16px thumbnail (2 cols) inside a full-width bubble (right=60).
        widget = self._quote_widget(width_px=16)
        widget.aligned_right = True
        app.query = lambda *a, **k: [widget]
        app._native_last_key.clear()

        with patch.object(
            QuoteWidget,
            "content_region",
            PropertyMock(return_value=Region(0, 5, 60, 3)),
        ):
            app._sync_native_images()

        # Container content region right=60, image_cols=2 → col = 60 - 2 + 1 = 59.
        assert len(written) == 1
        assert written[0].startswith("\x1b[6;59H")

    def test_sync_skips_out_of_viewport_quote_widget(self, app_for_test):
        app, written = self._kitty_app(app_for_test)
        app._chat_log = _HookFakeChatLog(Region(0, 0, 60, 40))
        # Thumb region fully below the container → no placement.
        app.query = lambda *a, **k: [
            self._quote_widget(thumb_region=Region(2, 100, 6, 3))
        ]
        app._native_last_key.clear()

        app._sync_native_images()

        assert written == []
        assert app._chat_native_ids == {5}

    def test_clear_chat_cleans_up_quote_widgets(self, app_for_test):
        app = app_for_test
        written: list[str] = []
        renderer = KittyRenderer(write=written.append, cell_w=8, cell_h=16)
        widget = QuoteWidget("🖼️ Immagine")
        widget.native_renderer = renderer
        widget.native_image_id = 7
        log = _HookFakeChatLog(Region(0, 0, 60, 40))
        log.children = [widget]
        app._chat_log = log

        app._clear_chat()

        assert any("a=d,d=I,i=7" in s for s in written)
        assert widget.native_renderer is None
        assert widget.native_image_id is None

    def test_gate_deletes_quote_placements_and_reemits(self, app_for_test):
        app, written = self._kitty_app(app_for_test)
        app._chat_log = _HookFakeChatLog(Region(0, 0, 60, 40))
        app.query = lambda *a, **k: [self._quote_widget()]

        # Sync on the default screen → place + track in _chat_native_ids.
        app._sync_native_images()
        assert app._chat_native_ids == {5}
        written.clear()

        main_screen = object()
        modal_screen = object()
        app._screen_stacks[app._current_mode] = [main_screen, modal_screen]
        app._compose_screen = main_screen

        # Modal open → quote placement dropped (d=i), data kept.
        app.post_display_hook()
        assert any("a=d,d=i,i=5" in s for s in written)
        assert not any("a=p" in s for s in written)
        written.clear()

        # Dismiss → re-emitted.
        app._screen_stacks[app._current_mode] = [main_screen]
        app.post_display_hook()
        assert any("a=p" in s for s in written)

    def test_non_kitty_no_writes(self, app_for_test):
        app = app_for_test  # default CATIMG
        written: list[str] = []
        app._native_renderer = KittyRenderer(write=written.append, cell_w=8, cell_h=16)
        app._chat_log = _HookFakeChatLog(Region(0, 0, 60, 40))
        app.query = lambda *a, **k: [self._quote_widget()]

        app.post_display_hook()  # image_support CATIMG → gate returns early

        assert written == []


def _make_kitty_app() -> tuple[SignalTUI, list[str]]:
    """KITTY app with a recording renderer and inline worker/thread dispatch."""
    written: list[str] = []
    app = SignalTUI(image_support=ImageSupport.KITTY)
    app._native_renderer = KittyRenderer(write=written.append, cell_w=8, cell_h=16)
    app.run_worker = MagicMock(side_effect=lambda fn, **kw: fn())
    app.call_from_thread = MagicMock(side_effect=lambda fn, *a, **k: fn(*a, **k))
    return app, written


class TestQuoteThumbnailFlow:
    """Uscita (chunk 4): quote thumbnail generated for a reply from an image."""

    def _write_png(self, tmp_path, name="photo.png") -> Path:
        path = tmp_path / name
        Image.new("RGB", (48, 54), "red").save(path)
        return path

    def test_reply_requested_carries_attachment_path(self, tmp_path):
        path = self._write_png(tmp_path)
        widget = ImageWidget(attachment_path=path, attachment_id="att-1")
        events: list = []
        widget.post_message = events.append
        widget.action_request_reply()
        assert events[0].attachment_path == path

    def test_generates_thumbnail_for_resolved_path(self, tmp_path):
        app, written = _make_kitty_app()
        quote = QuoteWidget("🖼️ Immagine", attachment_path=self._write_png(tmp_path))
        quote._is_mounted = True

        app._maybe_resolve_quote_thumbnail(quote)

        assert any("a=t" in s for s in written)  # transmit
        assert quote.native_image_id == 1
        assert quote.native_renderer is app._native_renderer
        assert quote.native_width_px is not None
        assert quote.native_height_px is not None

    def test_skips_when_path_none(self):
        app, written = _make_kitty_app()
        quote = QuoteWidget("🖼️ Immagine")  # no path

        app._maybe_resolve_quote_thumbnail(quote)

        assert app.run_worker.call_count == 0
        assert written == []

    def test_skips_when_no_renderer(self, tmp_path):
        app, written = _make_kitty_app()
        app._native_renderer = None
        quote = QuoteWidget("🖼️ Immagine", attachment_path=self._write_png(tmp_path))

        app._maybe_resolve_quote_thumbnail(quote)

        assert app.run_worker.call_count == 0
        assert written == []

    def test_skips_non_kitty(self, tmp_path):
        app, written = _make_kitty_app()
        app.image_support = ImageSupport.CATIMG
        quote = QuoteWidget("🖼️ Immagine", attachment_path=self._write_png(tmp_path))

        app._maybe_resolve_quote_thumbnail(quote)

        assert app.run_worker.call_count == 0
        assert written == []

    def test_prepare_failure_no_transmit(self, tmp_path):
        app, written = _make_kitty_app()
        bad = tmp_path / "bad.png"
        bad.write_text("garbage")
        quote = QuoteWidget("🖼️ Immagine", attachment_path=bad)
        quote._is_mounted = True

        app._maybe_resolve_quote_thumbnail(quote)

        assert not any("a=t" in s for s in written)
        assert quote.native_image_id is None

    def test_no_double_generation(self, tmp_path):
        app, _ = _make_kitty_app()
        quote = QuoteWidget("🖼️ Immagine", attachment_path=self._write_png(tmp_path))
        quote._is_mounted = True

        app._maybe_resolve_quote_thumbnail(quote)
        first = app.run_worker.call_count
        # After registration the thumbnail is already present → no re-run.
        app._maybe_resolve_quote_thumbnail(quote)

        assert app.run_worker.call_count == first

    def test_worker_uses_semaphore(self, tmp_path):
        app, _ = _make_kitty_app()
        app._image_resolve_semaphore = MagicMock()
        quote = QuoteWidget("🖼️ Immagine", attachment_path=self._write_png(tmp_path))

        app._maybe_resolve_quote_thumbnail(quote)

        app._image_resolve_semaphore.__enter__.assert_called_once()
        app._image_resolve_semaphore.__exit__.assert_called_once()

    # ── Ingresso (chunk 5): resolve via quote_attachment_id ──────────────
    def test_generates_thumbnail_from_attachment_id(self, tmp_path):
        app, written = _make_kitty_app()
        img_path = self._write_png(tmp_path)
        app.manager = MagicMock()
        app.manager.get_attachment_path.return_value = img_path
        quote = QuoteWidget(
            "🖼️ Immagine", attachment_id="tgref:42:12", protocol="telegram"
        )
        quote._is_mounted = True

        app._maybe_resolve_quote_thumbnail(quote)

        assert any("a=t" in s for s in written)
        assert quote.native_image_id == 1
        app.manager.get_attachment_path.assert_called_once_with(
            "telegram", "tgref:42:12"
        )

    def test_attachment_id_unresolvable_no_thumbnail(self):
        app, written = _make_kitty_app()
        app.manager = MagicMock()
        app.manager.get_attachment_path.return_value = None
        quote = QuoteWidget(
            "🖼️ Immagine", attachment_id="tgref:42:12", protocol="telegram"
        )
        quote._is_mounted = True

        app._maybe_resolve_quote_thumbnail(quote)

        assert not any("a=t" in s for s in written)
        assert quote.native_image_id is None

    def test_attachment_id_no_protocol_no_thumbnail(self):
        app, written = _make_kitty_app()
        # No protocol → cannot route the resolution → text-only bubble.
        quote = QuoteWidget("🖼️ Immagine", attachment_id="tgref:42:12")
        quote._is_mounted = True

        app._maybe_resolve_quote_thumbnail(quote)

        assert app.run_worker.call_count == 0
        assert written == []

    def test_uscita_lazy_whatsapp_attachment_id(self, tmp_path):
        """Uscita con path=None ma attachment_id+protocol → lazy resolve (WA)."""
        app, written = _make_kitty_app()
        img_path = self._write_png(tmp_path)
        app.manager = MagicMock()
        app.manager.get_attachment_path.return_value = img_path
        quote = QuoteWidget(
            "🖼️ Immagine", attachment_id="wa-media-123", protocol="whatsapp"
        )
        quote._is_mounted = True

        app._maybe_resolve_quote_thumbnail(quote)

        assert any("a=t" in s for s in written)
        assert quote.native_image_id == 1
        app.manager.get_attachment_path.assert_called_once_with(
            "whatsapp", "wa-media-123"
        )

    def test_stale_path_falls_back_to_lazy_resolve(self, tmp_path):
        """Path persistito ma file mancante → fallback lazy su attachment_id."""
        app, written = _make_kitty_app()
        img_path = self._write_png(tmp_path)
        app.manager = MagicMock()
        app.manager.get_attachment_path.return_value = img_path
        stale = tmp_path / "gone.png"  # never created → missing
        quote = QuoteWidget(
            "🖼️ Immagine",
            attachment_path=stale,
            attachment_id="tgref:42:12",
            protocol="telegram",
        )
        quote._is_mounted = True

        app._maybe_resolve_quote_thumbnail(quote)

        # Stale path detected → lazy resolve produced a valid file → thumbnail.
        assert any("a=t" in s for s in written)
        assert quote.native_image_id == 1
        app.manager.get_attachment_path.assert_called_once_with(
            "telegram", "tgref:42:12"
        )

    def test_stale_path_and_lazy_fails_text_only(self, tmp_path):
        """Path stale + lazy resolve None → testo, nessun crash."""
        app, written = _make_kitty_app()
        app.manager = MagicMock()
        app.manager.get_attachment_path.return_value = None
        stale = tmp_path / "gone.png"
        quote = QuoteWidget(
            "🖼️ Immagine",
            attachment_path=stale,
            attachment_id="tgref:42:12",
            protocol="telegram",
        )
        quote._is_mounted = True

        app._maybe_resolve_quote_thumbnail(quote)

        assert not any("a=t" in s for s in written)
        assert quote.native_image_id is None
