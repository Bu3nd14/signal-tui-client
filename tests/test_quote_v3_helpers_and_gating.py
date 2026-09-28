"""Design v3 — helper display, parser descriptor, gating e anti-leak.

Copre gli item (a)-(g) del piano: formato ``_signal_quote_text`` byte-identico,
``media_quote_display`` su tutti i media kind, parser R6 del descriptor, doppio
gating (Signal sì / WA-TG no), anti-leak dei kwargs sul filo, convergenza
echo-first vs mirror-first e barrier multi-allegato con quote media.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from models import (
    _KIND_TO_PLACEHOLDER,
    MEDIA_KIND_VALUES,
    MEDIA_QUOTE_PLACEHOLDERS,
    media_quote_display,
    parse_quote_attachment_descriptor,
)
from protocols.manager import BackendManager
from protocols.signal import SignalBackend, _signal_quote_text
from protocols.telegram import TelegramBackend
from protocols.whatsapp import WhatsAppBackend

CONTACT = "42"
DESCRIPTOR = "audio/ogg:voice.ogg:/tmp/voice.ogg"


# ─── (a) _signal_quote_text byte-identico ────────────────────────────────────


@pytest.mark.parametrize(
    ("quote", "expected"),
    [
        (None, None),
        ({}, None),
        ({"text": "Che bella!"}, "Che bella!"),
        ({"text": "  "}, None),
        ({"attachments": []}, None),
        ({"attachments": [None]}, None),
        ({"attachments": [{"contentType": "image/jpeg"}]}, "🖼️ Immagine"),
        ({"attachments": [{"contentType": "video/mp4"}]}, "🎬 Video"),
        ({"attachments": [{"contentType": "audio/ogg"}]}, "🎵 Audio"),
        ({"attachments": [{"contentType": "application/pdf"}]}, "📎 File"),
        ({"attachments": [{"contentType": "image/webp"}]}, "🖼️ Immagine"),
        (
            {"attachments": [{"contentType": "image/jpeg", "filename": "photo.jpg"}]},
            "photo.jpg — 🖼️ Immagine",
        ),
        (
            {"text": "caption", "attachments": [{"contentType": "image/jpeg"}]},
            "caption",
        ),
    ],
)
def test_signal_quote_text_byte_identical(quote, expected):
    assert _signal_quote_text(quote) == expected


def test_signal_quote_text_empty_content_type_with_filename_is_none():
    """Comportamento nuovo: senza mime non si sintetizza il placeholder "File".

    Pre-fix un allegato senza ``contentType`` ma con filename produceva
    ``"voice.ogg — 📎 File"``; ora ``media_quote_display(None)`` → None.
    """
    quote = {"attachments": [{"filename": "voice.ogg"}]}
    assert _signal_quote_text(quote) is None


# ─── (b)(c) media_quote_display totale sui media kind ────────────────────────


def test_kind_to_placeholder_is_total_and_labels_are_canonical():
    assert set(_KIND_TO_PLACEHOLDER) == set(MEDIA_KIND_VALUES)
    for kind, placeholder_key in _KIND_TO_PLACEHOLDER.items():
        assert placeholder_key in MEDIA_QUOTE_PLACEHOLDERS, kind


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("image", "🖼️ Immagine"),
        ("gif", "🖼️ Immagine"),
        ("video", "🎬 Video"),
        ("voice", "🎵 Audio"),
        ("audio", "🎵 Audio"),
        ("document", "📎 File"),
        ("sticker", "🎨 Sticker"),
    ],
)
def test_every_media_kind_maps_to_its_canonical_placeholder(kind, expected):
    key = _KIND_TO_PLACEHOLDER[kind]
    assert MEDIA_QUOTE_PLACEHOLDERS[key] == expected


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("image/png", "🖼️ Immagine"),
        ("image/gif", "🖼️ Immagine"),
        ("video/mp4", "🎬 Video"),
        ("audio/aac", "🎵 Audio"),
        ("audio/ogg; codecs=opus", "🎵 Audio"),
        ("application/pdf", "📎 File"),
        ("application/octet-stream", "📎 File"),
        ("image/webp", "🖼️ Immagine"),  # sticker quoted → image, no divergence
    ],
)
def test_media_quote_display_mime_mapping(content_type, expected):
    assert media_quote_display(content_type) == expected


def test_media_quote_display_filename_and_em_dash():
    assert (
        media_quote_display("image/jpeg", filename="photo.jpg")
        == "photo.jpg — 🖼️ Immagine"
    )
    assert media_quote_display("image/jpeg", filename="") == "🖼️ Immagine"
    assert media_quote_display(None, filename="photo.jpg") is None
    assert media_quote_display("") is None


# ─── (d) parser R6 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("descriptor", "expected"),
    [
        (None, (None, None)),
        ("", (None, None)),
        ("   ", (None, None)),
        ("image/png", ("image/png", None)),
        ("image/png:photo.png", ("image/png", "photo.png")),
        ("image/png::", ("image/png", None)),
        ("image/png:photo.png:/tmp/ph:oto.png", ("image/png", "photo.png")),
        (
            "image/png:name with spaces.png:/tmp/x",
            ("image/png", "name with spaces.png"),
        ),
        (" image/png : photo.png : /tmp/x ", ("image/png", "photo.png")),
    ],
)
def test_parse_quote_attachment_descriptor(descriptor, expected):
    assert parse_quote_attachment_descriptor(descriptor) == expected


# ─── helpers per mock backend ────────────────────────────────────────────────


def _backend(protocol: str):
    if protocol == "signal":
        backend = SignalBackend()
        backend.send_message_sync = MagicMock(return_value=str(int(time.time() * 1000)))
        backend.send_attachments_sync = MagicMock(return_value=["1787250931234"])
    elif protocol == "telegram":
        backend = TelegramBackend()
        backend.send_message_sync = MagicMock(return_value="77")
        backend.send_attachments_sync = MagicMock(return_value=["77"])
    else:
        backend = WhatsAppBackend()
        backend.send_message_sync = MagicMock(return_value="wa-77")
        backend.send_attachments_sync = MagicMock(return_value=["wa-77"])
    return backend


# ─── (d)(e) anti-leak: i kwargs di servizio non viaggiano sul filo ────────────


@pytest.mark.parametrize("protocol", ["signal", "telegram", "whatsapp"])
def test_send_message_sync_never_forwards_service_kwargs_to_backend(protocol):
    backend = _backend(protocol)
    manager = BackendManager()
    manager.register(backend)

    manager.send_message_sync(
        protocol,
        CONTACT,
        "answer",
        quote_message="",
        quote_attachments=[DESCRIPTOR],
    )

    sent_kwargs = backend.send_message_sync.call_args.kwargs
    assert sent_kwargs["quote_attachments"] == [DESCRIPTOR]
    assert "quote_content_type" not in sent_kwargs
    assert "quote_filename" not in sent_kwargs


@pytest.mark.parametrize("protocol", ["signal", "telegram", "whatsapp"])
def test_send_attachments_sync_never_forwards_service_kwargs_to_backend(protocol):
    backend = _backend(protocol)
    manager = BackendManager()
    manager.register(backend)

    manager.send_attachments_sync(
        protocol,
        CONTACT,
        [Path("/tmp/a.png")],
        captions=[None],
        mime_types=["image/png"],
        media_kinds=["image"],
        filenames=["a.png"],
        quote_message="",
        quote_attachments=[DESCRIPTOR],
    )

    sent_kwargs = backend.send_attachments_sync.call_args.kwargs
    assert "quote_content_type" not in sent_kwargs
    assert "quote_filename" not in sent_kwargs


@pytest.mark.parametrize("protocol", ["telegram", "whatsapp"])
def test_enqueue_service_kwargs_are_gated_out_for_non_signal(protocol):
    """Doppio gating: WA/TG non ricevono quote_content_type/quote_filename."""
    backend = _backend(protocol)
    backend.enqueue_sent_message = MagicMock()
    manager = BackendManager()
    manager.register(backend)

    manager.send_message_sync(
        protocol,
        CONTACT,
        "answer",
        quote_message="",
        quote_attachments=[DESCRIPTOR],
    )

    kwargs = backend.enqueue_sent_message.call_args.kwargs
    assert "quote_content_type" not in kwargs
    assert "quote_filename" not in kwargs


def test_enqueue_service_kwargs_are_forwarded_for_signal():
    backend = _backend("signal")
    backend.enqueue_sent_message = MagicMock()
    manager = BackendManager()
    manager.register(backend)

    manager.send_message_sync(
        "signal",
        CONTACT,
        "answer",
        quote_message="",
        quote_attachments=[DESCRIPTOR],
    )

    kwargs = backend.enqueue_sent_message.call_args.kwargs
    assert kwargs["quote_content_type"] == "audio/ogg"
    assert kwargs["quote_filename"] == "voice.ogg"


# ─── (f) convergenza echo-first vs mirror-first ──────────────────────────────


def _mirror() -> dict:
    return {
        "id": "1787250931234",
        "text": "",
        "is_mine": True,
        "sender": "You",
        "timestamp": 1787250931234,
        "quote_text": "voice.ogg — 🎵 Audio",
        "msg_type": "image",
        "attachment_info": "photo.jpg",
        "attachment_id": "sent-photo.png",
        "content_type": "image/png",
        "media_kind": "image",
        "quote_timestamp": 1787250930234,
        "quote_author": CONTACT,
        "quote_attachment_id": "voice.ogg",
        "quote_content_type": "audio/ogg",
    }


def _echo() -> dict:
    return {
        **_mirror(),
        "quote_text": "voice.ogg — 🎵 Audio",
    }


def _resulting_quote_text(messages: list[dict]) -> str:
    assert len(messages) == 1
    return messages[0]["quote_text"]


def test_convergence_mirror_first_then_echo():
    backend = SignalBackend()
    assert backend.ingest_message(CONTACT, _mirror(), _mirror()["timestamp"]) is True

    second = backend.ingest_message(CONTACT, _echo(), _echo()["timestamp"])
    assert second is False  # secondo echo no-op

    assert _resulting_quote_text(backend.cache[CONTACT]) == "voice.ogg — 🎵 Audio"


def test_convergence_echo_first_then_mirror():
    backend = SignalBackend()
    assert backend.ingest_message(CONTACT, _echo(), _echo()["timestamp"]) is True

    second = backend.ingest_message(CONTACT, _mirror(), _mirror()["timestamp"])
    assert second is False

    assert _resulting_quote_text(backend.cache[CONTACT]) == "voice.ogg — 🎵 Audio"


def test_convergence_both_orders_yield_the_same_quote_text():
    mirror_first = SignalBackend()
    mirror_first.ingest_message(CONTACT, _mirror(), _mirror()["timestamp"])
    mirror_first.ingest_message(CONTACT, _echo(), _echo()["timestamp"])

    echo_first = SignalBackend()
    echo_first.ingest_message(CONTACT, _echo(), _echo()["timestamp"])
    echo_first.ingest_message(CONTACT, _mirror(), _mirror()["timestamp"])

    assert _resulting_quote_text(mirror_first.cache[CONTACT]) == _resulting_quote_text(
        echo_first.cache[CONTACT]
    )


# ─── (g) barrier: reply con allegato → mirror quote_text non vuoto ───────────


def _batch_backend(tmp_path, monkeypatch, message_id="1787250931234"):
    media_dir = tmp_path / "signal-media"
    media_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("protocols.signal.SIGNAL_CLI_ATTACHMENTS_DIR", media_dir)
    backend = SignalBackend()
    backend._send_message_sync = MagicMock(return_value=message_id)
    return backend, media_dir


def test_barrier_reply_with_attachment_mirrors_quote_text(tmp_path, monkeypatch):
    backend, _media_dir = _batch_backend(tmp_path, monkeypatch)
    upload = tmp_path / "upload.png"
    upload.write_bytes(b"image-data")

    backend.send_attachments_sync(
        CONTACT,
        [upload],
        captions=[None],
        mime_types=["image/png"],
        media_kinds=["image"],
        filenames=["photo.png"],
        batch_id="batch-1",
        quote_timestamp=1787250930234,
        quote_author=CONTACT,
        quote_message="",
        quote_attachments=[DESCRIPTOR],
    )

    row = backend.cache[CONTACT][0]
    assert row["quote_text"] == "voice.ogg — 🎵 Audio"


def test_barrier_real_caption_wins_over_descriptor(tmp_path, monkeypatch):
    backend, _media_dir = _batch_backend(tmp_path, monkeypatch)
    upload = tmp_path / "upload.png"
    upload.write_bytes(b"image-data")

    backend.send_attachments_sync(
        CONTACT,
        [upload],
        captions=["Che bella!"],
        mime_types=["image/png"],
        media_kinds=["image"],
        filenames=["photo.png"],
        quote_message="Che bella!",
        quote_attachments=[DESCRIPTOR],
    )

    assert backend.cache[CONTACT][0]["quote_text"] == "Che bella!"


# ─── FIX B: la fusione quote deve avvenire anche con upgrade allegato ────────


def test_echo_attachment_upgrade_also_merges_missing_quote_fields(
    tmp_path, monkeypatch
):
    """Un echo che *upgrada* l'allegato deve anche riempire i campi quote."""
    remote = tmp_path / "photo.jpg"
    remote.write_bytes(b"remote")

    backend = SignalBackend()
    monkeypatch.setattr(
        backend,
        "get_attachment_path",
        lambda aid: remote if aid == "remote-123" else None,
    )

    mirror = {
        "id": "2000",
        "text": "",
        "is_mine": True,
        "sender": "You",
        "timestamp": 2000,
        "quote_text": "",
        "msg_type": "image",
        "attachment_info": "photo.jpg",
        "attachment_id": "sent-photo.png",
        "content_type": "image/png",
        "media_kind": "image",
    }
    backend.cache[CONTACT] = [mirror]

    echo = {
        **mirror,
        "attachment_id": "remote-123",
        "quote_text": "photo.jpg — 🖼️ Immagine",
        "quote_timestamp": 1000,
        "quote_author": CONTACT,
        "quote_attachment_id": "remote-123",
        "quote_content_type": "image/png",
    }
    result = backend.ingest_message(CONTACT, echo, 2000)

    assert result == "changed"
    assert mirror["quote_text"] == "photo.jpg — 🖼️ Immagine"
    assert mirror["quote_content_type"] == "image/png"


@pytest.mark.parametrize("protocol", ["signal", "telegram", "whatsapp"])
def test_send_attachment_sync_never_forwards_service_kwargs_to_backend(protocol):
    backend = _backend(protocol)
    backend.send_attachment_sync = MagicMock(return_value="att-id")
    backend.enqueue_sent_message = MagicMock()
    manager = BackendManager()
    manager.register(backend)

    manager.send_attachment_sync(
        protocol,
        CONTACT,
        Path("/tmp/a.png"),
        caption=None,
        mime_type="image/png",
        quote_message="",
        quote_attachments=[DESCRIPTOR],
    )

    sent_kwargs = backend.send_attachment_sync.call_args.kwargs
    assert "quote_content_type" not in sent_kwargs
    assert "quote_filename" not in sent_kwargs


def test_signal_media_reply_wire_quote_message_stays_empty():
    """Invariante wire: ``quote_message`` resta "" per media senza caption."""
    backend = _backend("signal")
    manager = BackendManager()
    manager.register(backend)

    manager.send_message_sync(
        "signal",
        CONTACT,
        "answer",
        quote_timestamp=1787250930234,
        quote_author=CONTACT,
        quote_message="",
        quote_attachments=[DESCRIPTOR],
    )

    assert backend.send_message_sync.call_args.kwargs["quote_message"] == ""
    event = backend.poll_once()[0]
    assert event.payload["quote_text"] == "voice.ogg — 🎵 Audio"
