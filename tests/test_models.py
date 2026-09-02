"""Tests for the pure media-quote helpers in ``models.py`` (bug #37)."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from models import (
    MEDIA_QUOTE_PLACEHOLDERS,
    ChatMessage,
    is_caption_like,
    is_media_quote_placeholder,
    is_media_quote_placeholder_composite,
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


class TestIsMediaQuotePlaceholderComposite:
    """Chunk 6 — ``is_media_quote_placeholder_composite`` (canonico + composito)."""

    def test_canonical_placeholders_are_recognised(self):
        for value in MEDIA_QUOTE_PLACEHOLDERS.values():
            assert is_media_quote_placeholder_composite(value) is True

    def test_composed_form_is_recognised(self):
        assert is_media_quote_placeholder_composite("photo.jpg — 🖼️ Immagine") is True
        assert is_media_quote_placeholder_composite("video.mp4 — 🎬 Video") is True

    def test_real_caption_is_not_recognised(self):
        assert is_media_quote_placeholder_composite("Che bella!") is False
        assert is_media_quote_placeholder_composite("photo.jpg") is False
        assert is_media_quote_placeholder_composite("") is False
        assert is_media_quote_placeholder_composite(None) is False


def test_is_caption_like_rejects_media_metadata():
    for value in (
        None,
        "",
        "photo.jpg",
        "photo.bmp",
        "photo.avif",
        "photo.tiff",
        "photo.svg",
        "image/jpeg",
        "Photo",
        "🖼️ Immagine",
        "Media: https://example.test/photo.jpg",
    ):
        assert is_caption_like(value) is False


def test_is_caption_like_accepts_user_text():
    assert is_caption_like("Che bella!") is True
    assert is_caption_like("La foto delle vacanze") is True
    assert is_caption_like("tramonto sul mare") is True
    assert is_caption_like("quando hai le idee chiare...") is True
    assert is_caption_like("foto 2024.jpg") is True
