from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from backends.manager import BackendManager
from backends.signal import SignalBackend
from backends.telegram import TelegramBackend
from backends.whatsapp import WhatsAppBackend


def _backend(protocol: str):
    if protocol == "signal":
        backend = SignalBackend()
        backend.send_message_sync = MagicMock(return_value=str(int(time.time() * 1000)))
    elif protocol == "telegram":
        backend = TelegramBackend()
        backend.send_message_sync = MagicMock(return_value="77")
    else:
        backend = WhatsAppBackend()
        backend.send_message_sync = MagicMock(return_value="wa-77")
    return backend


@pytest.mark.parametrize("protocol", ["signal", "telegram", "whatsapp"])
def test_facade_send_enqueues_outgoing_event_that_is_ingested(protocol):
    backend = _backend(protocol)
    manager = BackendManager()
    manager.register(backend)

    result = manager.send_message_sync(protocol, "42", "from web")
    event = backend.poll_once()[0]
    added = backend.ingest_message(
        event.contact_id, event.payload, event.payload["timestamp"], persist=False
    )

    assert result == backend.send_message_sync.return_value
    assert added is True
    assert backend.cache["42"][0]["text"] == "from web"
    assert backend.cache["42"][0]["is_mine"] is True


@pytest.mark.parametrize("protocol", ["signal", "telegram", "whatsapp"])
def test_facade_reply_mirrors_complete_quote_into_tui_ingest(protocol):
    backend = _backend(protocol)
    manager = BackendManager()
    manager.register(backend)

    manager.send_message_sync(
        protocol,
        "42",
        "answer",
        quote_timestamp=123000,
        quote_author="42",
        quote_message="question",
        reply_to_message_id="11",
    )
    event = backend.poll_once()[0]
    backend.ingest_message(
        event.contact_id, event.payload, event.payload["timestamp"], persist=False
    )

    cached = backend.cache["42"][0]
    assert cached["quote_text"] == "question"
    assert cached["quote_timestamp"] == 123000
    assert cached["quote_author"] == "42"
    assert cached["reply_to_message_id"] == "11"


@pytest.mark.parametrize("protocol", ["signal", "telegram", "whatsapp"])
def test_facade_send_attachment_enqueues_event_with_media_data(
    protocol, tmp_path, monkeypatch
):
    attachment = tmp_path / "photo.png"
    attachment.write_bytes(b"image-data")
    if protocol == "signal":
        monkeypatch.setattr(
            "backends.signal.SIGNAL_CLI_ATTACHMENTS_DIR", tmp_path / "signal-media"
        )
    else:
        if protocol == "telegram":
            monkeypatch.setattr(
                "backends.telegram._media_dir", lambda: tmp_path / "tg-media"
            )
        else:
            backend_media = tmp_path / "wa-media"

    backend = _backend(protocol)
    if protocol == "whatsapp":
        backend.media_dir = str(backend_media)
    backend.send_attachment_sync = MagicMock(
        return_value=backend.send_message_sync.return_value
    )
    manager = BackendManager()
    manager.register(backend)

    result = manager.send_attachment_sync(
        protocol,
        "42",
        attachment,
        caption="from web",
        mime_type="image/png",
    )
    event = backend.poll_once()[0]
    added = backend.ingest_message(
        event.contact_id, event.payload, event.payload["timestamp"], persist=False
    )

    cached = backend.cache["42"][0]
    assert result == backend.send_attachment_sync.return_value
    assert added is True
    assert cached["msg_type"] == "image"
    assert cached["text"] == ""
    assert cached["attachment_info"] == "from web"
    assert cached["attachment_id"]
    if protocol in {"telegram", "whatsapp"}:
        attachment.unlink()
        resolved = backend.get_attachment_path(cached["attachment_id"])
        assert resolved is not None and resolved.read_bytes() == b"image-data"


@pytest.mark.parametrize("protocol", ["signal", "telegram", "whatsapp"])
def test_web_image_mirror_has_empty_text_and_resolvable_attachment(
    protocol, tmp_path, monkeypatch
):
    attachment = tmp_path / "upload-random.png"
    attachment.write_bytes(b"image-data")
    if protocol == "signal":
        monkeypatch.setattr(
            "backends.signal.SIGNAL_CLI_ATTACHMENTS_DIR", tmp_path / "signal-media"
        )
    elif protocol == "telegram":
        monkeypatch.setattr(
            "backends.telegram._media_dir", lambda: tmp_path / "tg-media"
        )
    else:
        backend_media = tmp_path / "wa-media"

    backend = _backend(protocol)
    if protocol == "whatsapp":
        backend.media_dir = str(backend_media)
    backend.send_attachment_sync = MagicMock(
        return_value=backend.send_message_sync.return_value
    )
    manager = BackendManager()
    manager.register(backend)

    manager.send_attachment_sync(
        protocol, "42", attachment, caption=None, mime_type="image/png"
    )
    event = backend.poll_once()[0]

    assert event.payload["text"] == ""
    assert not event.payload["attachment_info"]
    assert backend.get_attachment_path(event.payload["attachment_id"]).is_file()
    assert "upload-" not in event.payload["text"]


@pytest.mark.parametrize("protocol", ["signal", "telegram", "whatsapp"])
def test_facade_send_echo_upgrades_optimistic_without_duplicate(protocol):
    backend = _backend(protocol)
    manager = BackendManager()
    manager.register(backend)
    optimistic_ts = int(time.time() * 1000)
    optimistic = {
        "text": "from tui",
        "is_mine": True,
        "sender": "You",
        "quote_text": None,
        "msg_type": "text",
        "attachment_info": None,
        "status": "pending",
    }
    assert backend.ingest_message("42", optimistic, optimistic_ts, persist=False)

    manager.send_message_sync(protocol, "42", "from tui")
    event = backend.poll_once()[0]
    added = backend.ingest_message(
        event.contact_id, event.payload, event.payload["timestamp"], persist=False
    )

    assert added is False
    assert len(backend.cache["42"]) == 1
    assert backend.cache["42"][0]["id"] == event.payload["id"]
