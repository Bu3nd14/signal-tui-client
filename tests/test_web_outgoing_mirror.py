from __future__ import annotations

import stat
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from protocols.manager import BackendManager
from protocols.signal import SignalBackend
from protocols.telegram import TelegramBackend
from protocols.whatsapp import WhatsAppBackend


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
            "protocols.signal.SIGNAL_CLI_ATTACHMENTS_DIR", tmp_path / "signal-media"
        )
    else:
        if protocol == "telegram":
            monkeypatch.setattr(
                "protocols.telegram._media_dir", lambda: tmp_path / "tg-media"
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
            "protocols.signal.SIGNAL_CLI_ATTACHMENTS_DIR", tmp_path / "signal-media"
        )
    elif protocol == "telegram":
        monkeypatch.setattr(
            "protocols.telegram._media_dir", lambda: tmp_path / "tg-media"
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


def test_mirror_copy_failure_warns_and_enqueues_without_attachment(
    tmp_path, monkeypatch, caplog
):
    attachment = tmp_path / "photo.png"
    attachment.write_bytes(b"image-data")
    backend = _backend("whatsapp")
    backend.media_dir = str(tmp_path / "wa-media")
    backend.send_attachment_sync = MagicMock(return_value="wa-77")
    manager = BackendManager()
    manager.register(backend)
    monkeypatch.setattr(
        "protocols.whatsapp.shutil.copy2",
        MagicMock(side_effect=OSError("copy failed")),
    )

    manager.send_attachment_sync(
        "whatsapp", "42", attachment, caption=None, mime_type="image/png"
    )
    event = backend.poll_once()[0]
    backend.ingest_message(
        event.contact_id, event.payload, event.payload["timestamp"], persist=False
    )

    assert event.payload["attachment_id"] is None
    assert backend.cache["42"][0]["attachment_id"] is None
    assert "Unable to copy sent attachment while mirroring" in caplog.text


def test_signal_attachment_rpc_and_echo_reuse_persistent_file(tmp_path, monkeypatch):
    media_dir = tmp_path / "signal-media"
    monkeypatch.setattr("protocols.signal.SIGNAL_CLI_ATTACHMENTS_DIR", media_dir)
    upload = tmp_path / "upload.png"
    upload.write_bytes(b"image-data")
    backend = SignalBackend()
    backend._use_daemon = True
    backend._rpc.send_message = MagicMock(
        return_value={"result": {"timestamp": 1787250931234}}
    )

    message_id = backend.send_attachment_sync(
        "+391234567890",
        upload,
        caption=None,
        mime_type="image/png",
        filename="foto originale.png",
    )
    persistent = Path(backend._rpc.send_message.call_args.kwargs["attachments"][0])
    assert persistent.is_file()
    assert persistent.parent == media_dir
    assert persistent.name == "foto originale.png"
    assert stat.S_IMODE(persistent.stat().st_mode) == 0o644

    backend.enqueue_sent_message(
        "+391234567890",
        str(message_id),
        "",
        attachment_path=upload,
        mime_type="image/png",
        filename="foto originale.png",
    )
    event = backend.poll_once()[0]
    assert event.payload["attachment_id"] == persistent.name
    assert event.payload["attachment_info"] == "foto originale.png"
    assert list(media_dir.iterdir()) == [persistent]


def test_signal_attachment_filename_is_sanitized_and_collision_safe(
    tmp_path, monkeypatch
):
    media_dir = tmp_path / "signal-media"
    monkeypatch.setattr("protocols.signal.SIGNAL_CLI_ATTACHMENTS_DIR", media_dir)
    upload = tmp_path / "upload.pdf"
    upload.write_bytes(b"pdf")
    backend = SignalBackend()
    backend._send_message_sync = MagicMock(return_value="1787250931234")

    backend.send_attachment_sync(
        "42",
        upload,
        mime_type="application/pdf",
        filename=r"../private/relazione?.pdf",
    )
    backend.send_attachment_sync(
        "42",
        upload,
        mime_type="application/pdf",
        filename=r"../private/relazione?.pdf",
    )

    sent_paths = [
        Path(call.kwargs["attachments"][0])
        for call in backend._send_message_sync.call_args_list
    ]
    assert [path.name for path in sent_paths] == [
        "relazione_.pdf",
        "relazione_ (1).pdf",
    ]
    assert all(path.parent == media_dir for path in sent_paths)


def test_signal_named_attachment_is_upgraded_by_outgoing_echo(tmp_path, monkeypatch):
    media_dir = tmp_path / "signal-media"
    media_dir.mkdir()
    monkeypatch.setattr("protocols.signal.SIGNAL_CLI_ATTACHMENTS_DIR", media_dir)
    source = tmp_path / "upload.pdf"
    current = media_dir / "relazione.pdf"
    incoming = media_dir / "echo-real-id"
    source.write_bytes(b"pdf")
    current.write_bytes(b"pdf")
    incoming.write_bytes(b"pdf")
    backend = SignalBackend()
    backend._sent_attachment_paths[str(source.resolve())] = current
    message = {
        "id": "1787250931234",
        "text": "relazione.pdf",
        "is_mine": True,
        "sender": "You",
        "timestamp": 1787250931234,
        "quote_text": None,
        "msg_type": "attachment",
        "attachment_info": "relazione.pdf",
        "attachment_id": current.name,
    }
    backend.cache["42"] = [message]

    with monkeypatch.context() as context:
        update = MagicMock()
        context.setattr("protocols.signal._update_message_attachment_id", update)
        context.setattr("protocols.signal._update_message_id", MagicMock())
        changed = backend.ingest_message(
            "42",
            {
                **message,
                "attachment_id": incoming.name,
            },
            1787250931234,
            persist=False,
        )

    assert changed == "changed"
    assert len(backend.cache["42"]) == 1
    assert message["attachment_id"] == incoming.name
    update.assert_called_once_with(
        "signal", "42", "1787250931234", 1787250931234, incoming.name
    )


def test_signal_attachment_forwards_quote_attachments(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "protocols.signal.SIGNAL_CLI_ATTACHMENTS_DIR", tmp_path / "signal-media"
    )
    upload = tmp_path / "upload.png"
    upload.write_bytes(b"image-data")
    backend = SignalBackend()
    backend._send_message_sync = MagicMock(return_value="1787250931234")

    backend.send_attachment_sync(
        "+391234567890",
        upload,
        mime_type="image/png",
        quote_attachments=["image/jpeg:quoted.jpg:/tmp/quoted.jpg"],
    )

    assert backend._send_message_sync.call_args.kwargs["quote_attachments"] == [
        "image/jpeg:quoted.jpg:/tmp/quoted.jpg"
    ]


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
