"""
Diagnostic coverage tests for the quote-thumbnail flow (PR #70-73).

These tests target the gaps that let the observed instability patterns through:

  A. Signal live: no thumbnail on live ingress, appears after exit/re-enter.
  B. WhatsApp/Telegram: thumbnail on live uscita, disappears on re-enter.

Each test asserts the DESIRED end-state.  A test that FAILS marks a real
coverage/behaviour gap; a test that PASSES only documents current (correct)
behaviour.  None of them touch the wire: they are headless.
"""

from __future__ import annotations

import io
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import protocols.db as backend_mod
from models import PROTOCOL_SIGNAL, ChatContact
from protocols.telegram import TelegramBackend
from protocols.whatsapp import WhatsAppBackend
from tui.app import SignalTUI
from tui.chat_view import (
    _is_scrolled_to_bottom,  # noqa: F401  (module import side effect)
)
from tui.images.detect import ImageSupport
from tui.images.kitty_renderer import KittyRenderer
from ui_components import QuoteWidget


def _db_rows(db_file: Path) -> list[dict]:
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM messages ORDER BY id")]
    conn.close()
    return rows


def _png_bytes(width: int = 8, height: int = 8) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "red").save(buf, "PNG")
    return buf.getvalue()


def _png_path(tmp_path: Path, name: str = "photo.png") -> Path:
    path = tmp_path / name
    Image.new("RGB", (48, 54), "red").save(path)
    return path


def _kitty_app() -> tuple[SignalTUI, list[str]]:
    written: list[str] = []
    app = SignalTUI(image_support=ImageSupport.KITTY)
    app._native_renderer = KittyRenderer(write=written.append, cell_w=8, cell_h=16)
    app.manager = MagicMock()
    app.run_worker = MagicMock(side_effect=lambda fn, **kw: fn())
    app.call_from_thread = MagicMock(side_effect=lambda fn, *a, **k: fn(*a, **k))
    return app, written


class _FakeChatLog:
    def __init__(self) -> None:
        self.content_region = MagicMock(width=60)
        self.children: list = []
        self.max_scroll_y = 0
        self.scroll_offset = MagicMock(y=0)

    def mount(self, *widgets, before=None, after=None):
        for w in widgets:
            w._is_mounted = True
            self.children.append(w)

    def scroll_end(self, animate: bool = False) -> None:
        pass


@pytest.fixture
def tmp_db(tmp_path: Path):
    db_file = tmp_path / "messages.db"
    with (
        patch.object(backend_mod, "DB_FILE", db_file),
        patch.object(backend_mod, "CACHE_DIR", tmp_path),
    ):
        yield db_file


# ── DIAG-B: WhatsApp/Telegram persistenza drop ``quote_attachment_path`` ──
class TestReenterDropsLocalQuotePath:
    """Pattern B: WA/TG live uscita shows the thumb (resolved local path), but
    re-enter must lazy-resolve the attachment id because the local path is NOT
    persisted.  These assert the path survives — they currently FAIL."""

    def _data(self, tmp_path: Path) -> dict:
        return {
            "text": "reply",
            "is_mine": True,
            "sender": "You",
            "timestamp": 1000,
            "quote_text": "🖼️ Immagine",
            "msg_type": "text",
            "attachment_info": None,
            "attachment_id": None,
            "status": "pending",
            "quote_attachment_id": "wa-media-123",
            "quote_attachment_path": _png_path(tmp_path, "resolved.png"),
            "quote_content_type": "image/png",
        }

    def test_whatsapp_persists_quote_attachment_path(self, tmp_db, tmp_path):
        be = WhatsAppBackend(
            api_url="http://localhost:3000", media_dir=str(tmp_path), session_name="t"
        )
        data = self._data(tmp_path)
        be.ingest_message("1111@s.whatsapp.net", data, 1000, persist=True)
        rows = _db_rows(tmp_db)
        assert rows[0]["quote_attachment_path"] == str(data["quote_attachment_path"])

    def test_telegram_persists_quote_attachment_path(self, tmp_db, tmp_path):
        be = TelegramBackend()
        data = self._data(tmp_path)
        be.ingest_message("1111", data, 1000, persist=True)
        rows = _db_rows(tmp_db)
        assert rows[0]["quote_attachment_path"] == str(data["quote_attachment_path"])


# ── DIAG-A: Signal re-enter with a stale path recovers via the attachment id ─
class TestSignalStalePathNoRecovery:
    """Pattern A (re-enter side): Signal persists ``quote_attachment_path`` and,
    after P4, also ``quote_attachment_id`` (from ``quote.attachments[].id``), so
    a stale path recovers via the lazy ``get_attachment_path`` fallback."""

    def test_signal_reenter_stale_path_recovers_thumbnail(self, tmp_path):
        app, written = _kitty_app()
        app.manager.get_attachment_path.return_value = _png_path(tmp_path)
        stale = tmp_path / "gone.png"  # never created → cleaned/missing
        quote = QuoteWidget(
            "🖼️ Immagine",
            attachment_path=stale,
            attachment_id="att-1",  # P4: Signal now stores the quoted attachment id
            protocol=PROTOCOL_SIGNAL,
        )
        quote._is_mounted = True

        app._maybe_resolve_quote_thumbnail(quote)

        # Stale path detected → lazy resolve via the cached attachment id.
        assert quote.native_image_id is not None
        assert any("a=t" in s for s in written)

    def test_signal_live_payload_lacks_quote_attachment_id(self, tmp_path):
        """Document the asymmetry: Signal live payload has path, not id."""
        from protocols.signal import SignalBackend

        be = SignalBackend()
        png = _png_bytes()
        import base64

        be._set_contacts(
            [
                ChatContact(
                    id="+391234567890",
                    display_name="Mario",
                    protocol=PROTOCOL_SIGNAL,
                )
            ]
        )
        envelope = {
            "source": "+391234567890",
            "sourceNumber": "+391234567890",
            "sourceName": "Mario",
            "timestamp": 2000,
            "dataMessage": {
                "message": "Guarda!",
                "timestamp": 2000,
                "quote": {
                    "id": 1000,
                    "author": "+391234567890",
                    "attachments": [
                        {
                            "contentType": "image/png",
                            "thumbnail": base64.b64encode(png).decode(),
                        }
                    ],
                },
            },
        }
        with patch("protocols.signal.CACHE_DIR", tmp_path):
            events = be.envelope_to_event(envelope)
        payload = events[0].payload
        assert payload["quote_attachment_path"] is not None
        # The asymmetry: no resolvable id on the Signal side.
        assert payload.get("quote_attachment_id") is None


# ── DIAG-C: worker finishes after the widget is unmounted → silent drop ────
class TestQuoteFinishAfterUnmount:
    """Race: the worker (semaphore + lazy resolve) can finish after the chat
    changed.  ``_finish_quote_thumbnail`` stashes the PNG on the unmounted widget
    (P1) and the app hook registers it once mounted — no silent drop."""

    def test_finish_after_unmount_stashes_and_retries(self, tmp_path):
        app, _written = _kitty_app()
        app._chat_log = _FakeChatLog()
        quote = QuoteWidget("🖼️ Immagine", attachment_path=_png_path(tmp_path))
        quote._is_mounted = True

        # Worker resolves + generates while mounted.
        app._maybe_resolve_quote_thumbnail(quote)
        assert quote.native_image_id is not None  # resolution ran inline

        # Simulate _clear_chat: unmount + native_cleanup.
        quote._is_mounted = False
        quote.native_cleanup()
        quote.native_image_id = None

        # A late worker finish on the unmounted widget STASHES the PNG (no drop).
        app._finish_quote_thumbnail(quote, _png_bytes())
        assert quote._pending_quote_png is not None

        # Once mounted again, the hook consumes the stash → thumbnail registered.
        quote._is_mounted = True
        app._consume_pending_thumbnails()
        assert quote.native_image_id is not None


# ── DIAG-D: dedicated quote semaphore (no starvation) ──────────────────────
class TestQuoteSemaphoreSharing:
    """Quote thumbnails use a DEDICATED ``_quote_resolve_semaphore`` (2), so slow
    image downloads (``_image_resolve_semaphore``, 4) never starve them."""

    def test_quote_has_dedicated_semaphore(self):
        app, _ = _kitty_app()
        app.image_support = ImageSupport.KITTY
        image_sem = app._image_resolve_semaphore
        quote_sem = app._quote_resolve_semaphore
        # Independent semaphores.
        assert image_sem is not quote_sem
        # Image semaphore: 4 slots.
        for _ in range(4):
            assert image_sem.acquire(blocking=False) is True
        assert image_sem.acquire(blocking=False) is False
        for _ in range(4):
            image_sem.release()
        # Quote semaphore: 2 slots, not consumed by the image semaphore.
        for _ in range(2):
            assert quote_sem.acquire(blocking=False) is True
        assert quote_sem.acquire(blocking=False) is False
        for _ in range(2):
            quote_sem.release()
