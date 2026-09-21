from __future__ import annotations

import sqlite3
import stat
import threading
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
        filename="photo.png",
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
        protocol,
        "42",
        attachment,
        caption=None,
        mime_type="image/png",
        filename="photo.png",
    )
    event = backend.poll_once()[0]

    assert event.payload["text"] == ""
    assert event.payload["attachment_info"] == "photo.png"
    assert backend.get_attachment_path(event.payload["attachment_id"]).is_file()
    assert "upload-" not in event.payload["text"]


@pytest.mark.parametrize("protocol", ["signal", "telegram", "whatsapp"])
def test_web_document_mirror_keeps_filename_and_caption_text(
    protocol, tmp_path, monkeypatch
):
    attachment = tmp_path / "upload.pdf"
    attachment.write_bytes(b"pdf")
    if protocol == "signal":
        monkeypatch.setattr(
            "protocols.signal.SIGNAL_CLI_ATTACHMENTS_DIR", tmp_path / "signal-media"
        )
    elif protocol == "telegram":
        monkeypatch.setattr(
            "protocols.telegram._media_dir", lambda: tmp_path / "tg-media"
        )

    backend = _backend(protocol)
    if protocol == "whatsapp":
        backend.media_dir = str(tmp_path / "wa-media")
    backend.send_attachment_sync = MagicMock(
        return_value=backend.send_message_sync.return_value
    )
    manager = BackendManager()
    manager.register(backend)

    manager.send_attachment_sync(
        protocol,
        "42",
        attachment,
        caption="document caption",
        mime_type="application/pdf",
        filename="report.pdf",
    )
    event = backend.poll_once()[0]

    assert event.payload["msg_type"] == "attachment"
    assert event.payload["text"] == "document caption"
    assert event.payload["attachment_info"] == "report.pdf"


def _image_ingest_data(attachment_info: str) -> dict:
    return {
        "id": "1787250931234",
        "text": "",
        "is_mine": True,
        "sender": "You",
        "quote_text": None,
        "quote_timestamp": None,
        "quote_author": None,
        "reply_to_message_id": None,
        "msg_type": "image",
        "attachment_info": attachment_info,
        "attachment_id": None,
        "content_type": "image/png",
        "media_kind": "image",
    }


@pytest.mark.parametrize("protocol", ["signal", "telegram", "whatsapp"])
@pytest.mark.parametrize("order", ["mirror-echo", "echo-mirror"])
def test_image_caption_race_always_persists_caption(protocol, order):
    from protocols import db

    backend = _backend(protocol)
    mirror = _image_ingest_data("photo.png")
    echo = _image_ingest_data("caption from echo")
    messages = (mirror, echo) if order == "mirror-echo" else (echo, mirror)

    for message in messages:
        backend.ingest_message("42", message, 1787250931234)

    assert len(backend.cache["42"]) == 1
    assert backend.cache["42"][0]["text"] == ""
    assert backend.cache["42"][0]["attachment_info"] == "caption from echo"
    with sqlite3.connect(db.DB_FILE) as conn:
        rows = conn.execute(
            "SELECT attachment_info FROM messages WHERE protocol = ? AND contact_number = ?",
            (protocol, "42"),
        ).fetchall()
    assert rows == [("caption from echo",)]


@pytest.mark.parametrize("protocol", ["signal", "telegram", "whatsapp"])
def test_image_echo_filename_does_not_replace_existing_caption(protocol):
    backend = _backend(protocol)
    captioned = _image_ingest_data("existing caption")
    filename_echo = _image_ingest_data("server-photo.jpg")

    backend.ingest_message("42", captioned, 1787250931234)
    changed = backend.ingest_message("42", filename_echo, 1787250931234)

    assert changed is False
    assert backend.cache["42"][0]["attachment_info"] == "existing caption"


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
        "signal",
        "42",
        "1787250931234",
        1787250931234,
        incoming.name,
        expected_attachment_id=current.name,
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


# ─── Multi-attachment batch send (Signal barrier, design §4.4/§4.5) ──────────


def _batch_backend(tmp_path, monkeypatch, message_id="1787250931234"):
    media_dir = tmp_path / "signal-media"
    media_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("protocols.signal.SIGNAL_CLI_ATTACHMENTS_DIR", media_dir)
    backend = SignalBackend()
    backend._send_message_sync = MagicMock(return_value=message_id)
    return backend, media_dir


def _uploads(tmp_path, count=3):
    files = []
    for index in range(count):
        upload = tmp_path / f"upload-{index}.png"
        upload.write_bytes(b"image-data")
        files.append(upload)
    return files


def _send_batch(backend, files, *, batch_id="batch-1", filenames=None):
    return backend.send_attachments_sync(
        "42",
        files,
        captions=["batch caption"] + [None] * (len(files) - 1),
        mime_types=["image/png"] * len(files),
        media_kinds=["image"] * len(files),
        filenames=filenames or [f"file-{index}.png" for index in range(len(files))],
        batch_id=batch_id,
    )


def _echo_payload(attachment_id: str) -> dict:
    return {
        "id": "1787250931234",
        "text": "",
        "is_mine": True,
        "sender": "You",
        "quote_text": None,
        "msg_type": "image",
        "attachment_info": None,
        "attachment_id": attachment_id,
        "content_type": "image/png",
        "media_kind": "image",
    }


def _batch_rows():
    from protocols import db

    with sqlite3.connect(db.DB_FILE) as connection:
        return connection.execute(
            "SELECT msg_id, attachment_id, batch_id, batch_index FROM messages "
            "WHERE protocol = 'signal' AND contact_number = '42' ORDER BY id"
        ).fetchall()


def test_signal_send_attachments_sync_materializes_batch_rows(tmp_path, monkeypatch):
    backend, _media_dir = _batch_backend(tmp_path, monkeypatch)
    files = _uploads(tmp_path)

    message_ids = _send_batch(backend, files, batch_id="batch-7")

    assert message_ids == ["1787250931234"]
    attachments = backend._send_message_sync.call_args.kwargs["attachments"]
    assert [Path(path).name for path in attachments] == [
        "file-0.png",
        "file-1.png",
        "file-2.png",
    ]
    assert all(Path(path).is_file() for path in attachments)
    assert len(backend.cache["42"]) == 3

    rows = _batch_rows()
    assert [row[0] for row in rows] == ["1787250931234"] * 3
    assert len({row[1] for row in rows}) == 3
    assert [row[2] for row in rows] == ["batch-7"] * 3
    assert [row[3] for row in rows] == [0, 1, 2]


def test_signal_send_attachments_barrier_blocks_echo_until_complete(
    tmp_path, monkeypatch
):
    backend, media_dir = _batch_backend(tmp_path, monkeypatch)
    files = _uploads(tmp_path)
    remote = media_dir / "remote-echo-0"
    remote.write_bytes(b"remote")

    inside_barrier = threading.Event()
    echo_attempted = threading.Event()
    release_barrier = threading.Event()
    ingest_order: list[str] = []
    original_ingest = backend.ingest_message

    def wrapped_ingest(contact_id, data, ts, persist=True):
        if str(data.get("attachment_id", "")).startswith("remote-"):
            result = original_ingest(contact_id, data, ts, persist=persist)
            ingest_order.append("echo")
            return result
        if data.get("batch_index") == 0:
            # Frozen while holding _ingest_lock: the echo below must block.
            inside_barrier.set()
            release_barrier.wait(timeout=10)
        result = original_ingest(contact_id, data, ts, persist=persist)
        ingest_order.append(f"mirror:{data.get('batch_index')}")
        return result

    backend.ingest_message = wrapped_ingest
    echo_data = _echo_payload(remote.name)
    outcome = {}

    def run_send():
        try:
            outcome["ids"] = _send_batch(backend, files, batch_id="batch-1")
        except Exception as exc:  # noqa: BLE001 - pragma: no cover, defensive
            outcome["error"] = exc

    def run_echo():
        echo_attempted.set()
        backend.ingest_message("42", echo_data, 1787250931234)

    sender = threading.Thread(target=run_send)
    sender.start()
    assert inside_barrier.wait(timeout=10)
    echo_thread = threading.Thread(target=run_echo)
    echo_thread.start()
    assert echo_attempted.wait(timeout=10)
    # Give the echo thread time to reach (and block on) _ingest_lock.
    time.sleep(0.2)
    release_barrier.set()

    sender.join(timeout=10)
    echo_thread.join(timeout=10)
    assert not sender.is_alive()
    assert not echo_thread.is_alive()
    assert outcome == {"ids": ["1787250931234"]}
    # The echo ran only after the whole barrier completed: no interleaving.
    assert ingest_order == ["mirror:0", "mirror:1", "mirror:2", "echo"]

    rows = _batch_rows()
    assert [row[2] for row in rows] == ["batch-1"] * 3
    assert [row[3] for row in rows] == [0, 1, 2]
    assert rows[0][1] == remote.name  # the blocked echo upgraded mirror 0


def test_signal_multi_attachment_echo_upgrades_each_mirror_row(tmp_path, monkeypatch):
    backend, media_dir = _batch_backend(tmp_path, monkeypatch)
    files = _uploads(tmp_path)
    assert _send_batch(backend, files) == ["1787250931234"]
    remotes = []
    for index in range(3):
        remote = media_dir / f"remote-{index}"
        remote.write_bytes(b"remote")
        remotes.append(remote)

    results = [
        backend.ingest_message("42", _echo_payload(remote.name), 1787250931234)
        for remote in remotes
    ]

    assert results == ["changed", "changed", "changed"]
    assert len(backend.cache["42"]) == 3
    assert [message["attachment_id"] for message in backend.cache["42"]] == [
        remote.name for remote in remotes
    ]

    from protocols import db

    with sqlite3.connect(db.DB_FILE) as connection:
        rows = connection.execute(
            "SELECT batch_index, attachment_id FROM messages "
            "WHERE protocol = 'signal' AND contact_number = '42' ORDER BY id"
        ).fetchall()
    assert rows == [(index, remote.name) for index, remote in enumerate(remotes)]


def test_signal_same_filename_attachments_keep_unambiguous_association(
    tmp_path, monkeypatch
):
    backend, media_dir = _batch_backend(tmp_path, monkeypatch)
    files = _uploads(tmp_path, count=2)

    assert _send_batch(backend, files, filenames=["foto.png", "foto.png"]) == [
        "1787250931234"
    ]
    assert sorted(path.name for path in media_dir.iterdir()) == [
        "foto (1).png",
        "foto.png",
    ]

    remotes = []
    for index in range(2):
        remote = media_dir / f"remote-{index}"
        remote.write_bytes(b"remote")
        remotes.append(remote)
    for remote in remotes:
        assert (
            backend.ingest_message("42", _echo_payload(remote.name), 1787250931234)
            == "changed"
        )

    assert [message["attachment_id"] for message in backend.cache["42"]] == [
        remote.name for remote in remotes
    ]
    from protocols import db

    with sqlite3.connect(db.DB_FILE) as connection:
        rows = connection.execute(
            "SELECT batch_index, attachment_id FROM messages "
            "WHERE protocol = 'signal' AND contact_number = '42' ORDER BY id"
        ).fetchall()
    assert rows == [(index, remote.name) for index, remote in enumerate(remotes)]


def test_signal_multi_attachment_echo_without_full_mirror_never_duplicates(
    tmp_path, monkeypatch
):
    backend, media_dir = _batch_backend(tmp_path, monkeypatch)
    # A registered (non-legacy) mirror row: upgradable to the remote id.
    mirror = media_dir / "mirror-file.png"
    mirror.write_bytes(b"mirror")
    upload = tmp_path / "upload-mirror.png"
    upload.write_bytes(b"mirror")
    backend._sent_attachment_paths[str(upload.resolve())] = mirror

    assert backend.ingest_message("42", _echo_payload(mirror.name), 1787250931234)

    remotes = []
    for index in range(3):
        remote = media_dir / f"remote-{index}"
        remote.write_bytes(b"remote")
        remotes.append(remote)
    results = [
        backend.ingest_message("42", _echo_payload(remote.name), 1787250931234)
        for remote in remotes
    ]

    # First echo upgrades the mirrored row, the others become NEW rows: the
    # pre-fix behaviour overwrote the first row with every incoming id.
    assert results == ["changed", True, True]
    assert len(backend.cache["42"]) == 3
    assert [message["attachment_id"] for message in backend.cache["42"]] == [
        remote.name for remote in remotes
    ]
    from protocols import db

    with sqlite3.connect(db.DB_FILE) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM messages WHERE protocol = 'signal' "
                "AND contact_number = '42'"
            ).fetchone()[0]
            == 3
        )


def test_signal_send_attachments_barrier_rollback_cleans_partial_rows(
    tmp_path, monkeypatch
):
    from protocols.db import _add_message_to_cache

    backend, media_dir = _batch_backend(tmp_path, monkeypatch)
    files = _uploads(tmp_path)
    # Stranger rows sharing identity pieces with the batch: same attachment
    # id (different msg_id) and same msg_id (different attachment id) must
    # both survive the rollback DELETE.
    _add_message_to_cache(
        "42",
        "",
        is_mine=True,
        sender="You",
        timestamp=999_000,
        msg_type="image",
        attachment_id="file-0.png",
        protocol="signal",
        msg_id="other-message",
    )
    _add_message_to_cache(
        "42",
        "",
        is_mine=True,
        sender="You",
        timestamp=1_787_250_931_234,
        msg_type="image",
        attachment_id="unrelated.png",
        protocol="signal",
        msg_id="1787250931234",
    )

    original_ingest = backend.ingest_message

    def failing_ingest(contact_id, data, ts, persist=True):
        result = original_ingest(contact_id, data, ts, persist=persist)
        if data.get("batch_index") == 1:
            # Fail AFTER the row hit the DB: the append-before-ingest
            # tracking must still roll it back.
            raise RuntimeError("boom during barrier")
        return result

    backend.ingest_message = failing_ingest

    with pytest.raises(RuntimeError, match="boom during barrier"):
        _send_batch(backend, files, batch_id="batch-1")

    assert backend.cache.get("42") == []
    for index in range(3):
        assert not (media_dir / f"file-{index}.png").exists()

    from protocols import db

    with sqlite3.connect(db.DB_FILE) as connection:
        rows = connection.execute(
            "SELECT msg_id, attachment_id FROM messages "
            "WHERE protocol = 'signal' AND contact_number = '42' ORDER BY id"
        ).fetchall()
    assert rows == [
        ("other-message", "file-0.png"),
        ("1787250931234", "unrelated.png"),
    ]
