"""
Regression tests for ui_components.py — MessageWidget, ImageWidget, etc.
Note: These tests verify logic only (no Textual widget rendering).
"""

from __future__ import annotations

import pytest
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from rich.text import Text as RichText
from ui_components import MessageWidget, ImageWidget, DownloadLinkWidget



class TestMessageWidget:
    """💬 MessageWidget — stato e metodi."""

    def test_initial_state(self):
        """Widget creato con testo e timestamp."""
        w = MessageWidget(text="Ciao!", timestamp=1000, sender="Mario")
        assert w._msg_text == "Ciao!"
        assert w._msg_timestamp == 1000
        assert w._msg_sender == "Mario"
        assert w._msg_is_mine is False
        assert w._selected is False
        assert w._status == "sent"

    def test_initial_state_mine(self):
        """Widget per messaggio proprio."""
        w = MessageWidget(text="Ciao!", timestamp=1000, sender="You", is_mine=True)
        assert w._msg_is_mine is True

    def test_set_status(self):
        """Cambio status."""
        w = MessageWidget(text="Ciao!", timestamp=1000, sender="You", is_mine=True)
        assert w._status == "sent"
        w.set_status("delivered")
        assert w._status == "delivered"
        w.set_status("read")
        assert w._status == "read"

    def test_set_selected(self):
        """Toggle selezione."""
        w = MessageWidget(text="Ciao!", timestamp=1000, sender="Mario")
        assert w._selected is False
        w.set_selected(True)
        assert w._selected is True
        w.set_selected(False)
        assert w._selected is False

    def test_message_clicked_event(self):
        """Click → emette MessageClicked con i dati corretti."""
        w = MessageWidget(text="Ciao!", timestamp=1000, sender="Mario", is_mine=False)
        events = []

        def _handler(msg):
            events.append(msg)

        # Simula la registrazione del messaggio
        w.post_message = lambda msg: events.append(msg)
        w.on_click()

        assert len(events) == 1
        assert events[0].text == "Ciao!"
        assert events[0].timestamp == 1000
        assert events[0].sender == "Mario"
        assert events[0].is_mine is False

    def test_sender_color_renders_prefix(self):
        """Con sender_color, il testo mostra '<sender:> testo'."""
        w = MessageWidget(
            text="Ciao gruppo!",
            timestamp=1000,
            sender="Mario",
            is_mine=False,
            sender_color="#DAA520",
        )
        assert w._sender_color == "#DAA520"
        # Il contenuto del widget deve essere un RichText con il prefisso <Mario:>
        content = w._Static__content
        assert isinstance(content, RichText)
        assert "<Mario:>" in str(content)
        assert "Ciao gruppo!" in str(content)




    def test_sender_color_none_no_prefix(self):
        """Senza sender_color, il testo resta invariato (nessun prefisso)."""
        w = MessageWidget(
            text="Ciao!",
            timestamp=1000,
            sender="Mario",
            is_mine=False,
        )
        assert w._sender_color is None
        rendered = str(w.render())
        assert "<Mario:>" not in rendered
        assert "Ciao!" in rendered

    def test_sender_color_empty_sender_no_prefix(self):
        """Con sender_color ma sender vuoto, nessun prefisso viene aggiunto."""
        w = MessageWidget(
            text="Ciao!",
            timestamp=1000,
            sender="",
            is_mine=False,
            sender_color="#DAA520",
        )

        rendered = str(w.render())
        assert "<:>" not in rendered
        assert "Ciao!" in rendered



class TestImageWidget:
    """🖼️ ImageWidget — stato e metodi."""

    def test_initial_state_with_path(self, tmp_path):
        """Widget con path valido."""
        img_path = tmp_path / "photo.jpg"
        img_path.write_text("fake")
        w = ImageWidget(attachment_path=img_path, attachment_id="att-123")
        assert w.attachment_path == img_path
        assert w.attachment_id == "att-123"

    def test_initial_state_no_path(self):
        """Widget senza path."""
        w = ImageWidget(attachment_path=None)
        assert w.attachment_path is None

    def test_image_clicked_event(self, tmp_path):
        """Click → emette ImageClicked con path e id."""
        img_path = tmp_path / "photo.jpg"
        img_path.write_text("fake")
        w = ImageWidget(attachment_path=img_path, attachment_id="att-123")
        events = []

        w.post_message = lambda msg: events.append(msg)
        w.on_click()

        assert len(events) == 1
        assert events[0].attachment_path == img_path
        assert events[0].attachment_id == "att-123"

    def test_click_no_path_no_event(self):
        """Click senza path → nessun evento."""
        w = ImageWidget(attachment_path=None)
        events = []

        w.post_message = lambda msg: events.append(msg)
        w.on_click()

        assert len(events) == 0


class TestDownloadLinkWidget:
    """📥 DownloadLinkWidget — URL e composizione."""

    def test_url_stored(self):
        """URL passato al widget."""
        w = DownloadLinkWidget(url="http://localhost:10042/file.txt")
        assert w._url == "http://localhost:10042/file.txt"
        assert w._label == "📥 Download"

    def test_custom_label(self):
        """Label personalizzato."""
        w = DownloadLinkWidget(url="http://localhost:10042/file.txt", label="Scarica")
        assert w._label == "Scarica"
