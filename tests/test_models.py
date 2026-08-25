"""Tests for the pure media-quote helpers in ``models.py`` (bug #37)."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from models import (
    MEDIA_QUOTE_PLACEHOLDERS,
    ChatMessage,
    is_media_quote_placeholder,
    media_quote_placeholder,
)


class TestChatMessageQuoteAttachment:
    """Campi additivi della quote-media (chunk 5): default None."""

    def test_quote_attachment_fields_default_none(self):
        msg = ChatMessage(
            id="1",
            contact_id="42",
            protocol="telegram",
            text="hi",
            is_mine=False,
            sender="Ada",
            timestamp=1,
        )
        assert msg.quote_attachment_id is None
        assert msg.quote_attachment_path is None
        assert msg.quote_content_type is None


class TestMediaQuotePlaceholder:
    """Decisione A — ``media_quote_placeholder`` mappatura/priorità."""

    def test_mapping_covers_all_canonical_types(self):
        assert MEDIA_QUOTE_PLACEHOLDERS == {
            "image": "🖼️ Immagine",
            "sticker": "🎨 Sticker",
            "attachment": "📎 File",
            "audio": "🎵 Audio",
            "video": "🎬 Video",
        }

    def test_known_types_map_to_canonical_labels(self):
        assert media_quote_placeholder("image") == "🖼️ Immagine"
        assert media_quote_placeholder("sticker") == "🎨 Sticker"
        assert media_quote_placeholder("attachment") == "📎 File"
        assert media_quote_placeholder("audio") == "🎵 Audio"
        assert media_quote_placeholder("video") == "🎬 Video"

    def test_unknown_type_degrades_to_attachment(self):
        assert media_quote_placeholder("bogus") == "📎 File"

    def test_detail_takes_priority_over_placeholder(self):
        assert media_quote_placeholder("image", "Che bella!") == "Che bella!"
        assert media_quote_placeholder("video", "film.mp4") == "film.mp4"


class TestIsMediaQuotePlaceholder:
    """Decisione A — ``is_media_quote_placeholder`` predicato puro."""

    def test_canonical_placeholders_are_recognised(self):
        for value in MEDIA_QUOTE_PLACEHOLDERS.values():
            assert is_media_quote_placeholder(value) is True

    def test_non_placeholder_text_is_not_recognised(self):
        assert is_media_quote_placeholder("ciao") is False
        assert is_media_quote_placeholder("🖼️ Immagine!") is False
        assert is_media_quote_placeholder("") is False
        assert is_media_quote_placeholder(None) is False

    def test_composed_form_is_not_recognised(self):
        # La forma "filename — segnaposto" esiste solo sul display.
        assert is_media_quote_placeholder("photo.jpg — 🖼️ Immagine") is False
