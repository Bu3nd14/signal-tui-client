from __future__ import annotations

import asyncio
import sqlite3
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models import ChatContact, ChatEvent
from protocols.manager import BackendManager
from protocols.signal import SignalBackend
from protocols.telegram import TelegramBackend
from protocols.whatsapp import WhatsAppBackend
from tui.events import EventHandlingMixin


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


def test_signal_send_attachments_sync_empty_list_sends_nothing(tmp_path, monkeypatch):
    backend, media_dir = _batch_backend(tmp_path, monkeypatch)

    message_ids = backend.send_attachments_sync(
        "42",
        [],
        captions=[],
        mime_types=[],
        media_kinds=[],
        filenames=[],
    )

    # An empty batch must be a no-op: signal-cli would otherwise send an
    # empty message and the barrier would mirror a bogus row for it.
    assert message_ids == []
    backend._send_message_sync.assert_not_called()
    assert list(media_dir.iterdir()) == []
    assert "42" not in backend.cache


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


def test_signal_batch_echo_before_barrier_still_gets_batch_slots(tmp_path, monkeypatch):
    """T3/BUG-3: an echo ingested between the send and the barrier must not
    leave the batch rows without batch_id/batch_index."""
    backend, media_dir = _batch_backend(tmp_path, monkeypatch)
    files = _uploads(tmp_path)
    remotes = []
    for index in range(3):
        remote = media_dir / f"remote-{index}"
        remote.write_bytes(b"remote")
        remotes.append(remote)

    def send_then_echo(contact_id, text, **kwargs):
        # The SSE thread ingests the sent-message echo while the sender is
        # still between the signal-cli response and the barrier: the echo
        # rows exist BEFORE any mirror row is materialized.
        for remote in remotes:
            backend.ingest_message("42", _echo_payload(remote.name), 1787250931234)
        return "1787250931234"

    backend._send_message_sync = MagicMock(side_effect=send_then_echo)

    assert _send_batch(backend, files, batch_id="batch-1") == ["1787250931234"]

    # The mirror rows dedup'ed onto the echo rows: 3 rows survive with the
    # remote attachment ids, and the post-barrier repair stamped them with
    # the batch slot (k-th row ↔ k-th file) in cache...
    assert [message["attachment_id"] for message in backend.cache["42"]] == [
        remote.name for remote in remotes
    ]
    assert [message["batch_index"] for message in backend.cache["42"]] == [0, 1, 2]
    assert {message["batch_id"] for message in backend.cache["42"]} == {"batch-1"}
    # ...and in the DB.
    rows = _batch_rows()
    assert [row[1] for row in rows] == [remote.name for remote in remotes]
    assert [row[2] for row in rows] == ["batch-1"] * 3
    assert [row[3] for row in rows] == [0, 1, 2]


def test_signal_batch_partial_echo_before_barrier_still_gets_all_slots(
    tmp_path, monkeypatch
):
    """T5/BUG-5 (partial race): only ONE echo row exists when the barrier
    starts, the remaining echoes land after it.  Every row must still end up
    with its own batch slot — no row without one, no duplicate, and the
    mirrors must not collapse onto the already-paired echo row."""
    backend, media_dir = _batch_backend(tmp_path, monkeypatch)
    files = _uploads(tmp_path)
    remotes = []
    for index in range(3):
        remote = media_dir / f"remote-{index}"
        remote.write_bytes(b"remote")
        remotes.append(remote)

    def send_then_first_echo(contact_id, text, **kwargs):
        # The SSE thread wins the send→barrier race for ONE echo only: a
        # single row exists when the barrier starts.
        backend.ingest_message("42", _echo_payload(remotes[0].name), 1787250931234)
        return "1787250931234"

    backend._send_message_sync = MagicMock(side_effect=send_then_first_echo)

    assert _send_batch(backend, files, batch_id="batch-1") == ["1787250931234"]
    # The remaining echoes are ingested only after the barrier completed.
    for remote in remotes[1:]:
        backend.ingest_message("42", _echo_payload(remote.name), 1787250931234)

    # Mirror 0 dedup'ed onto the early echo row, mirrors 1/2 materialized
    # beside it: three rows survive, none without a slot, none duplicated...
    assert len(backend.cache["42"]) == 3
    assert [message["attachment_id"] for message in backend.cache["42"]] == [
        remote.name for remote in remotes
    ]
    assert [message["batch_index"] for message in backend.cache["42"]] == [0, 1, 2]
    assert {message["batch_id"] for message in backend.cache["42"]} == {"batch-1"}
    # ...and the DB agrees.
    rows = _batch_rows()
    assert len(rows) == 3
    assert [row[1] for row in rows] == [remote.name for remote in remotes]
    assert [row[2] for row in rows] == ["batch-1"] * 3
    assert [row[3] for row in rows] == [0, 1, 2]


def test_signal_batch_repair_more_rows_than_files_leaves_extras_slotless(
    tmp_path, monkeypatch, caplog
):
    """More rows than files (leftover echo rows): the repair stamps the first
    rows with the available slots, the extras stay slotless and a warning is
    logged — never a crash nor a re-assignment of taken slots."""
    backend, media_dir = _batch_backend(tmp_path, monkeypatch)
    files = _uploads(tmp_path)
    remotes = []
    for index in range(4):
        remote = media_dir / f"remote-{index}"
        remote.write_bytes(b"remote")
        remotes.append(remote)

    def send_then_echoes(contact_id, text, **kwargs):
        # Four echoes for a three-file batch win the race together.
        for remote in remotes:
            backend.ingest_message("42", _echo_payload(remote.name), 1787250931234)
        return "1787250931234"

    backend._send_message_sync = MagicMock(side_effect=send_then_echoes)

    with caplog.at_level("WARNING"):
        assert _send_batch(backend, files, batch_id="batch-1") == ["1787250931234"]

    assert len(backend.cache["42"]) == 4
    assert [message["attachment_id"] for message in backend.cache["42"]] == [
        remote.name for remote in remotes
    ]
    assert [message.get("batch_index") for message in backend.cache["42"]] == [
        0,
        1,
        2,
        None,
    ]
    assert [message.get("batch_id") for message in backend.cache["42"]] == [
        "batch-1",
        "batch-1",
        "batch-1",
        None,
    ]
    assert "found 4 rows for 3 files" in caplog.text
    assert "extra rows left without a slot" in caplog.text

    rows = _batch_rows()
    assert len(rows) == 4
    assert [row[2] for row in rows] == ["batch-1"] * 3 + [None]
    assert [row[3] for row in rows] == [0, 1, 2, None]


def test_signal_batch_slots_survive_cache_reload(tmp_path, monkeypatch):
    """FIX-3: ``_load_cache`` must expose ``batch_id``/``batch_index`` so the
    ``(timestamp, id)`` positional matching keeps working across restarts
    (design §4.5)."""
    from protocols.db import _load_cache

    backend, _media_dir = _batch_backend(tmp_path, monkeypatch)
    files = _uploads(tmp_path)
    assert _send_batch(backend, files, batch_id="batch-7") == ["1787250931234"]

    reloaded = _load_cache(protocol="signal")["42"]

    assert [message["batch_id"] for message in reloaded] == ["batch-7"] * 3
    assert [message["batch_index"] for message in reloaded] == [0, 1, 2]


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


def test_signal_barrier_rollback_clears_sent_attachment_registry(tmp_path, monkeypatch):
    """T4/BUG-4: the rollback must also drop the _sent_attachment_paths
    entries, otherwise _is_sent_attachment() stays True for deleted files."""
    backend, _media_dir = _batch_backend(tmp_path, monkeypatch)
    files = _uploads(tmp_path)
    original_ingest = backend.ingest_message

    def failing_ingest(contact_id, data, ts, persist=True):
        result = original_ingest(contact_id, data, ts, persist=persist)
        if data.get("batch_index") == 0:
            # Fail AFTER the registry was populated and the first row hit
            # the DB: the rollback must undo the registration too.
            raise RuntimeError("boom during barrier")
        return result

    backend.ingest_message = failing_ingest

    with pytest.raises(RuntimeError, match="boom during barrier"):
        _send_batch(backend, files, batch_id="batch-1")

    assert backend._sent_attachment_paths == {}
    assert not backend._is_sent_attachment("file-0.png")
    assert not backend._is_sent_attachment("file-1.png")
    assert not backend._is_sent_attachment("file-2.png")


# ─── Multi-attachment batch send: WhatsApp / Telegram (design §4.4/§4.8) ─────


def _wa_batch_backend(tmp_path, monkeypatch):
    backend = WhatsAppBackend(api_url="http://api.test", media_dir=str(tmp_path / "wa"))
    monkeypatch.setattr(backend, "_resolve_send_chat_id", lambda cid: cid)
    return backend


def _wa_files(tmp_path, count=3):
    files = []
    for index in range(count):
        upload = tmp_path / f"photo-{index}.png"
        upload.write_bytes(b"image-data")
        files.append(upload)
    return files


def test_whatsapp_send_attachments_sync_sends_one_call_per_file(tmp_path, monkeypatch):
    backend = _wa_batch_backend(tmp_path, monkeypatch)
    backend._rest.send_image = MagicMock(
        side_effect=lambda chat, path, **kw: {"id": f"wa-{kw['filename']}"}
    )
    files = _wa_files(tmp_path, count=3)

    message_ids = backend.send_attachments_sync(
        "39333@c.us",
        files,
        captions=["batch caption"] + [None] * 2,
        mime_types=["image/png"] * 3,
        media_kinds=["image"] * 3,
        filenames=["file-0.png", "file-1.png", "file-2.png"],
        batch_id="batch-9",
        reply_to_message_id="quote-id",
    )

    assert message_ids == ["wa-file-0.png", "wa-file-1.png", "wa-file-2.png"]
    assert backend._rest.send_image.call_count == 3
    first, second, third = backend._rest.send_image.call_args_list
    assert first.args == ("39333@c.us", files[0])
    assert first.kwargs["caption"] == "batch caption"
    assert first.kwargs["reply_to_message_id"] == "quote-id"
    assert second.kwargs["caption"] is None
    assert second.kwargs["reply_to_message_id"] is None
    assert third.kwargs["caption"] is None
    assert third.kwargs["reply_to_message_id"] is None
    assert [
        call.kwargs["filename"] for call in backend._rest.send_image.call_args_list
    ] == [
        "file-0.png",
        "file-1.png",
        "file-2.png",
    ]


def test_whatsapp_send_attachments_sync_routes_kind_per_file(tmp_path, monkeypatch):
    backend = _wa_batch_backend(tmp_path, monkeypatch)
    backend._rest.send_image = MagicMock(return_value={"id": "wa-img"})
    backend._rest.send_video = MagicMock(return_value={"id": "wa-vid"})
    backend._rest.send_file = MagicMock(return_value={"id": "wa-doc"})
    files = _wa_files(tmp_path, count=3)

    message_ids = backend.send_attachments_sync(
        "39333@c.us",
        files,
        captions=[None] * 3,
        mime_types=["image/png", "video/mp4", "application/pdf"],
        media_kinds=["image", None, None],
        filenames=["a.png", "b.mp4", "c.pdf"],
    )

    assert message_ids == ["wa-img", "wa-vid", "wa-doc"]
    backend._rest.send_image.assert_called_once()
    backend._rest.send_video.assert_called_once()
    backend._rest.send_file.assert_called_once()


def test_whatsapp_send_attachments_sync_stops_early_on_failure(
    tmp_path, monkeypatch, caplog
):
    backend = _wa_batch_backend(tmp_path, monkeypatch)
    backend._rest.last_status = 502
    backend._rest.last_error = "boom"
    backend._rest.send_image = MagicMock(
        side_effect=[{"id": "wa-0"}, None, {"id": "wa-2"}]
    )
    files = _wa_files(tmp_path, count=3)

    with pytest.raises(RuntimeError, match="status=502.*boom"):
        backend.send_attachments_sync(
            "39333@c.us",
            files,
            captions=[None] * 3,
            mime_types=["image/png"] * 3,
            media_kinds=["image"] * 3,
            filenames=["file-0.png", "file-1.png", "file-2.png"],
        )

    # Stop-early: the 3rd file is never sent, the 1st is already delivered.
    assert backend._rest.send_image.call_count == 2
    assert "multi-attach failed at index 1/3: 1 messages already sent" in caplog.text


def test_whatsapp_manager_batch_mirrors_n_events_with_batch_index(
    tmp_path, monkeypatch
):
    from protocols import db

    backend = _wa_batch_backend(tmp_path, monkeypatch)
    manager = BackendManager()
    manager.register(backend)
    backend._rest.send_image = MagicMock(
        side_effect=[{"id": "wa-0"}, {"id": "wa-1"}, {"id": "wa-2"}]
    )
    files = _wa_files(tmp_path, count=3)

    message_ids = manager.send_attachments_sync(
        "whatsapp",
        "39333@c.us",
        files,
        captions=["batch caption"] + [None] * 2,
        mime_types=["image/png"] * 3,
        media_kinds=["image"] * 3,
        filenames=["file-0.png", "file-1.png", "file-2.png"],
        batch_id="batch-1",
    )

    assert message_ids == ["wa-0", "wa-1", "wa-2"]
    events = backend.poll_once()
    assert [event.payload["id"] for event in events] == ["wa-0", "wa-1", "wa-2"]
    assert [event.payload["attachment_info"] for event in events] == [
        "batch caption",
        "file-1.png",
        "file-2.png",
    ]
    for index, event in enumerate(events):
        assert event.type == "message"
        assert event.payload["batch_id"] == "batch-1"
        assert event.payload["batch_index"] == index
        assert backend.ingest_message(
            "39333@c.us", event.payload, event.payload["timestamp"]
        )

    with sqlite3.connect(db.DB_FILE) as connection:
        rows = connection.execute(
            "SELECT msg_id, batch_id, batch_index FROM messages "
            "WHERE protocol = 'whatsapp' AND contact_number = '39333@c.us' "
            "ORDER BY batch_index"
        ).fetchall()
    assert rows == [
        ("wa-0", "batch-1", 0),
        ("wa-1", "batch-1", 1),
        ("wa-2", "batch-1", 2),
    ]


def test_manager_batch_without_batch_id_keeps_legacy_backend_mirror(
    tmp_path, monkeypatch
):
    """T1/BUG-1: without a batch_id the manager must not forward
    ``batch_index`` — backends with the historical ``enqueue_sent_message``
    signature would reject the extra kwarg and silently lose the mirror
    (single attachments are routed through the batch API too)."""
    backend = _wa_batch_backend(tmp_path, monkeypatch)
    backend.send_attachments_sync = MagicMock(return_value=["wa-0"])
    enqueued = []

    def legacy_enqueue_sent_message(
        contact_id,
        message_id,
        text,
        *,
        quote_timestamp=None,
        quote_author=None,
        quote_message=None,
        reply_to_message_id=None,
        attachment_path=None,
        mime_type=None,
        media_kind=None,
        filename=None,
    ):
        # Historical signature: no batch_id/batch_index parameters.
        enqueued.append((contact_id, message_id, text))

    backend.enqueue_sent_message = legacy_enqueue_sent_message
    manager = BackendManager()
    manager.register(backend)
    upload = tmp_path / "photo-0.png"
    upload.write_bytes(b"image-data")

    message_ids = manager.send_attachments_sync(
        "whatsapp",
        "39333@c.us",
        [upload],
        captions=[None],
        mime_types=["image/png"],
        media_kinds=["image"],
        filenames=["file-0.png"],
    )

    assert message_ids == ["wa-0"]
    assert enqueued == [("39333@c.us", "wa-0", "")]


def test_manager_batch_with_batch_id_forwards_batch_metadata(tmp_path, monkeypatch):
    """The BUG-1 fix must not drop the batch metadata for backends that DO
    accept it: a batch_id-carrying send still forwards both kwargs."""
    backend = _wa_batch_backend(tmp_path, monkeypatch)
    backend.send_attachments_sync = MagicMock(return_value=["wa-0", "wa-1"])
    enqueued = []

    def enqueue_sent_message(contact_id, message_id, text, **kwargs):
        enqueued.append((message_id, kwargs.get("batch_id"), kwargs.get("batch_index")))

    backend.enqueue_sent_message = enqueue_sent_message
    manager = BackendManager()
    manager.register(backend)
    files = _wa_files(tmp_path, count=2)

    manager.send_attachments_sync(
        "whatsapp",
        "39333@c.us",
        files,
        captions=[None, None],
        mime_types=["image/png"] * 2,
        media_kinds=["image"] * 2,
        filenames=["file-0.png", "file-1.png"],
        batch_id="batch-4",
    )

    assert enqueued == [
        ("wa-0", "batch-4", 0),
        ("wa-1", "batch-4", 1),
    ]


def _tg_batch_backend():
    backend = TelegramBackend()
    backend._loop = MagicMock()
    backend._resolve_input_entity = AsyncMock(return_value="entity")

    class CompletedFuture:
        def __init__(self, value):
            self.value = value

        def result(self, timeout):
            self.timeout = timeout
            return self.value

    return backend, CompletedFuture


def test_telegram_send_attachments_sync_sends_album(monkeypatch, tmp_path):
    backend, CompletedFuture = _tg_batch_backend()
    uploaded = SimpleNamespace(name="uploaded-0")
    backend._client = SimpleNamespace(
        upload_file=AsyncMock(return_value=uploaded),
        send_file=AsyncMock(
            return_value=[SimpleNamespace(id=71), SimpleNamespace(id=72)]
        ),
    )

    def schedule(coro, _loop):
        return CompletedFuture(asyncio.run(coro))

    monkeypatch.setattr("protocols.telegram.asyncio.run_coroutine_threadsafe", schedule)
    files = []
    for index in range(2):
        upload = tmp_path / f"photo-{index}.png"
        upload.write_bytes(b"image-data")
        files.append(upload)

    message_ids = backend.send_attachments_sync(
        "42",
        files,
        captions=["album caption", None],
        mime_types=["image/png"] * 2,
        media_kinds=["image"] * 2,
        filenames=["file-0.png", "file-1.png"],
        batch_id="batch-1",
        reply_to_message_id="12",
    )

    # One album request carrying both media, caption on the first only.
    assert message_ids == ["71", "72"]
    backend._client.upload_file.assert_any_await(str(files[0]), file_name="file-0.png")
    backend._client.upload_file.assert_any_await(str(files[1]), file_name="file-1.png")
    backend._client.send_file.assert_awaited_once_with(
        "entity",
        [uploaded, uploaded],
        caption="album caption",
        reply_to=12,
        force_document=False,
    )


def test_telegram_send_attachments_sync_scales_timeout_with_batch_size(
    monkeypatch, tmp_path
):
    backend, _CompletedFuture = _tg_batch_backend()
    backend._client = SimpleNamespace(
        upload_file=AsyncMock(),
        send_file=AsyncMock(return_value=[SimpleNamespace(id=71)]),
    )
    future = MagicMock()
    future.result.return_value = ["71"]

    def schedule(coro, _loop):
        coro.close()
        return future

    monkeypatch.setattr("protocols.telegram.asyncio.run_coroutine_threadsafe", schedule)
    files = []
    for index in range(3):
        upload = tmp_path / f"photo-{index}.png"
        upload.write_bytes(b"image-data")
        files.append(upload)

    backend.send_attachments_sync(
        "42",
        files,
        captions=[None] * 3,
        mime_types=["image/png"] * 3,
        media_kinds=["image"] * 3,
        filenames=[None] * 3,
    )

    # The timeout scales with the batch size (design §4.4.3).
    future.result.assert_called_once_with(timeout=360)


def test_telegram_send_attachments_sync_normalizes_single_message(
    monkeypatch, tmp_path
):
    backend, CompletedFuture = _tg_batch_backend()
    # A single media (or documents delivered outside the album) makes
    # Telethon return a bare message instead of a list.
    backend._client = SimpleNamespace(
        upload_file=AsyncMock(),
        send_file=AsyncMock(return_value=SimpleNamespace(id=71)),
    )

    def schedule(coro, _loop):
        return CompletedFuture(asyncio.run(coro))

    monkeypatch.setattr("protocols.telegram.asyncio.run_coroutine_threadsafe", schedule)
    upload = tmp_path / "photo.png"
    upload.write_bytes(b"image-data")

    message_ids = backend.send_attachments_sync(
        "42",
        [upload],
        captions=[None],
        mime_types=["image/png"],
        media_kinds=["image"],
        filenames=[None],
    )

    assert message_ids == ["71"]


def test_telegram_send_attachments_sync_rejects_invalid_ids(monkeypatch, tmp_path):
    backend, CompletedFuture = _tg_batch_backend()
    backend._client = SimpleNamespace(upload_file=AsyncMock(), send_file=AsyncMock())
    upload = tmp_path / "photo.png"
    upload.write_bytes(b"image-data")

    def schedule(coro, _loop):
        return CompletedFuture(asyncio.run(coro))

    monkeypatch.setattr("protocols.telegram.asyncio.run_coroutine_threadsafe", schedule)
    with pytest.raises(ValueError, match="Invalid Telegram contact id"):
        backend.send_attachments_sync(
            "bad",
            [upload],
            captions=[None],
            mime_types=["image/png"],
            media_kinds=["image"],
            filenames=[None],
        )


def test_telegram_manager_batch_mirrors_n_events_with_batch_index(
    monkeypatch, tmp_path
):
    from protocols import db

    backend, CompletedFuture = _tg_batch_backend()
    backend._client = SimpleNamespace(
        upload_file=AsyncMock(),
        send_file=AsyncMock(
            return_value=[SimpleNamespace(id=71), SimpleNamespace(id=72)]
        ),
    )
    manager = BackendManager()
    manager.register(backend)

    def schedule(coro, _loop):
        return CompletedFuture(asyncio.run(coro))

    monkeypatch.setattr("protocols.telegram.asyncio.run_coroutine_threadsafe", schedule)
    monkeypatch.setattr("protocols.telegram._media_dir", lambda: tmp_path / "tg-media")
    files = []
    for index in range(2):
        upload = tmp_path / f"photo-{index}.png"
        upload.write_bytes(b"image-data")
        files.append(upload)

    message_ids = manager.send_attachments_sync(
        "telegram",
        "42",
        files,
        captions=["album caption", None],
        mime_types=["image/png"] * 2,
        media_kinds=["image"] * 2,
        filenames=["file-0.png", "file-1.png"],
        batch_id="batch-2",
    )

    assert message_ids == ["71", "72"]
    events = backend.poll_once()
    assert len(events) == 2
    for index, event in enumerate(events):
        assert event.payload["id"] == str(71 + index)
        assert event.payload["batch_id"] == "batch-2"
        assert event.payload["batch_index"] == index

    for event in events:
        assert backend.ingest_message("42", event.payload, event.payload["timestamp"])
    with sqlite3.connect(db.DB_FILE) as connection:
        rows = connection.execute(
            "SELECT msg_id, batch_id, batch_index FROM messages "
            "WHERE protocol = 'telegram' AND contact_number = '42' "
            "ORDER BY batch_index"
        ).fetchall()
    assert rows == [
        ("71", "batch-2", 0),
        ("72", "batch-2", 1),
    ]


# ─── Batch slot fusion on echo/mirror dedup (Telegram / WhatsApp) ────────────


def _telegram_echo(message_id: str, attachment_id: str | None) -> dict:
    return {
        "id": message_id,
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


def _whatsapp_echo(message_id: str, attachment_id: str | None) -> dict:
    return {
        "id": message_id,
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


@pytest.mark.parametrize("order", ["echo-first", "mirror-first"])
def test_telegram_batch_slot_fuses_on_echo_mirror_dedup(order):
    from protocols import db

    backend = TelegramBackend()
    echo = _telegram_echo("71", "remote-71")
    mirror = {**echo, "batch_id": "batch-2", "batch_index": 0}
    first, second = (echo, mirror) if order == "echo-first" else (mirror, echo)

    assert backend.ingest_message("42", first, 1787250931234)
    backend.ingest_message("42", second, 1787250931234)

    assert len(backend.cache["42"]) == 1
    assert backend.cache["42"][0]["batch_id"] == "batch-2"
    assert backend.cache["42"][0]["batch_index"] == 0
    with sqlite3.connect(db.DB_FILE) as connection:
        rows = connection.execute(
            "SELECT batch_id, batch_index FROM messages "
            "WHERE protocol = 'telegram' AND contact_number = '42'"
        ).fetchall()
    assert rows == [("batch-2", 0)]


def test_telegram_batch_slots_fuse_per_message():
    from protocols import db

    backend = TelegramBackend()
    echoes = [_telegram_echo("71", "remote-71"), _telegram_echo("72", "remote-72")]
    mirrors = [
        {**echoes[0], "batch_id": "batch-2", "batch_index": 0},
        {**echoes[1], "batch_id": "batch-2", "batch_index": 1},
    ]
    for message in [*echoes, *mirrors]:
        backend.ingest_message("42", message, 1787250931234)

    assert len(backend.cache["42"]) == 2
    assert {message["batch_id"] for message in backend.cache["42"]} == {"batch-2"}
    assert sorted(message["batch_index"] for message in backend.cache["42"]) == [0, 1]
    with sqlite3.connect(db.DB_FILE) as connection:
        rows = connection.execute(
            "SELECT msg_id, batch_id, batch_index FROM messages "
            "WHERE protocol = 'telegram' AND contact_number = '42' ORDER BY msg_id"
        ).fetchall()
    assert rows == [("71", "batch-2", 0), ("72", "batch-2", 1)]


def test_telegram_batch_slot_fuses_onto_optimistic_row():
    from protocols import db

    backend = TelegramBackend()
    optimistic = {**_telegram_echo(None, None), "id": None}
    assert backend.ingest_message("42", optimistic, 1787250931234)

    mirror = {**_telegram_echo("71", None), "batch_id": "batch-1", "batch_index": 0}
    assert backend.ingest_message("42", mirror, 1787250931234) == "changed"

    assert len(backend.cache["42"]) == 1
    assert backend.cache["42"][0]["batch_id"] == "batch-1"
    assert backend.cache["42"][0]["batch_index"] == 0
    with sqlite3.connect(db.DB_FILE) as connection:
        assert connection.execute(
            "SELECT batch_id, batch_index FROM messages "
            "WHERE protocol = 'telegram' AND contact_number = '42'"
        ).fetchall() == [("batch-1", 0)]


def test_telegram_batch_slot_never_overwrites_existing_batch():
    backend = TelegramBackend()
    mirror = {
        **_telegram_echo("71", "remote-71"),
        "batch_id": "batch-1",
        "batch_index": 0,
    }
    assert backend.ingest_message("42", mirror, 1787250931234)
    other = {
        **_telegram_echo("71", "remote-71"),
        "batch_id": "batch-9",
        "batch_index": 3,
    }
    backend.ingest_message("42", other, 1787250931234)

    assert backend.cache["42"][0]["batch_id"] == "batch-1"
    assert backend.cache["42"][0]["batch_index"] == 0


@pytest.mark.parametrize("order", ["echo-first", "mirror-first"])
def test_whatsapp_batch_slot_fuses_on_echo_mirror_dedup(order, tmp_path, monkeypatch):
    from protocols import db

    backend = _wa_batch_backend(tmp_path, monkeypatch)
    contact = "39333@c.us"
    echo = _whatsapp_echo("wa-71", "waha-remote")
    mirror = {**echo, "batch_id": "batch-9", "batch_index": 1}
    first, second = (echo, mirror) if order == "echo-first" else (mirror, echo)

    assert backend.ingest_message(contact, first, 1787250931234)
    backend.ingest_message(contact, second, 1787250931234)

    assert len(backend.cache[contact]) == 1
    assert backend.cache[contact][0]["batch_id"] == "batch-9"
    assert backend.cache[contact][0]["batch_index"] == 1
    with sqlite3.connect(db.DB_FILE) as connection:
        rows = connection.execute(
            "SELECT batch_id, batch_index FROM messages "
            "WHERE protocol = 'whatsapp' AND contact_number = ?",
            (contact,),
        ).fetchall()
    assert rows == [("batch-9", 1)]


def test_whatsapp_batch_slot_fuses_onto_optimistic_row(tmp_path, monkeypatch):
    from protocols import db

    backend = _wa_batch_backend(tmp_path, monkeypatch)
    contact = "39333@c.us"
    optimistic = {**_whatsapp_echo(None, None), "id": None}
    assert backend.ingest_message(contact, optimistic, 1787250931234)

    mirror = {
        **_whatsapp_echo("wa-71", None),
        "batch_id": "batch-9",
        "batch_index": 1,
    }
    assert backend.ingest_message(contact, mirror, 1787250931234) == "changed"

    assert len(backend.cache[contact]) == 1
    assert backend.cache[contact][0]["batch_id"] == "batch-9"
    with sqlite3.connect(db.DB_FILE) as connection:
        assert connection.execute(
            "SELECT batch_id, batch_index FROM messages "
            "WHERE protocol = 'whatsapp' AND contact_number = ?",
            (contact,),
        ).fetchall() == [("batch-9", 1)]


def test_whatsapp_batch_slot_never_overwrites_existing_batch(tmp_path, monkeypatch):
    backend = _wa_batch_backend(tmp_path, monkeypatch)
    contact = "39333@c.us"
    mirror = {
        **_whatsapp_echo("wa-71", "waha-remote"),
        "batch_id": "batch-1",
        "batch_index": 0,
    }
    assert backend.ingest_message(contact, mirror, 1787250931234)
    other = {
        **_whatsapp_echo("wa-71", "waha-remote"),
        "batch_id": "batch-9",
        "batch_index": 3,
    }
    backend.ingest_message(contact, other, 1787250931234)

    assert backend.cache[contact][0]["batch_id"] == "batch-1"
    assert backend.cache[contact][0]["batch_index"] == 0


# ─── TUI routing of the "sent-mirror" event (design §4.6.1) ──────────────────


class _MirrorApp(EventHandlingMixin):
    """Minimal app instance exposing the attributes the handler touches."""

    def __init__(self, contacts=None, *, web_enabled=False, backend=None):
        self.manager = SimpleNamespace(get=lambda _protocol: backend)
        self.contacts = list(contacts or [])
        self.selected_contact = None
        self._contact_list_dirty = False
        self._dirty_contact_keys = set()
        self._web_enabled = web_enabled


def _mirror_app(contacts=None, backend=None, web_enabled=False):
    if backend is None:
        backend = SimpleNamespace(
            contacts=list(contacts or []),
            _identify_contact=MagicMock(return_value=None),
        )
    return _MirrorApp(contacts=contacts, backend=backend, web_enabled=web_enabled)


def _mirror_event(contact, ts=1_787_250_931_234, protocol="signal"):
    return ChatEvent(
        type="sent-mirror",
        protocol=protocol,
        contact_id=contact.id,
        payload={"id": "1787250931234", "timestamp": ts, "batch_id": "batch-1"},
    )


def test_sent_mirror_event_is_routed_to_dedicated_handler():
    contact = ChatContact(id="+391234567890", display_name="Alice", protocol="signal")
    app = _mirror_app(contacts=[contact])
    event = _mirror_event(contact)

    with (
        patch.object(app, "_handle_sent_mirror_event", return_value=True) as handler,
        patch.object(app, "_handle_message_event") as message_handler,
    ):
        assert app._handle_event(event)

    handler.assert_called_once_with(event)
    message_handler.assert_not_called()


def test_sent_mirror_updates_real_contact_object_and_flags():
    contact = ChatContact(id="+391234567890", display_name="Alice", protocol="signal")
    app = _mirror_app(contacts=[contact])
    event = _mirror_event(contact)

    assert app._handle_sent_mirror_event(event) is True
    # The REAL object in self.contacts (not a placeholder) was updated.
    assert contact.last_message_ts == 1_787_250_931_234
    assert app._contact_list_dirty is True
    assert app._dirty_contact_keys == {contact.cache_key}
    # No placeholder duplicate was created.
    assert app.contacts == [contact]
    app.manager.get("signal")._identify_contact.assert_not_called()


def test_sent_mirror_prefers_self_contacts_over_rebuilt_payload_copy():
    rebuilt = ChatContact(id="+391234567890", display_name="Alice", protocol="signal")
    real = ChatContact(id="+391234567890", display_name="Alice", protocol="signal")
    app = _mirror_app(contacts=[real])
    # The payload (and the backend) carry the rebuilt object, but the TUI
    # list holds its own copy: THAT one must be updated.
    event = _mirror_event(rebuilt)
    event.payload["contact"] = rebuilt
    app.manager.get("signal")._identify_contact.return_value = rebuilt

    assert app._handle_sent_mirror_event(event) is True
    assert real.last_message_ts == 1_787_250_931_234
    assert rebuilt.last_message_ts == 0
    assert app.contacts == [real]


def test_sent_mirror_uses_backend_identify_when_not_in_tui_list():
    known = ChatContact(id="42", display_name="Ada", protocol="telegram")
    app = _mirror_app(contacts=[])
    app.manager.get("telegram")._identify_contact.return_value = known
    event = _mirror_event(known, protocol="telegram")

    assert app._handle_sent_mirror_event(event) is True
    assert known.last_message_ts == 1_787_250_931_234
    # Known contact: no placeholder appended, no duplicates.
    assert app.contacts == []


def test_sent_mirror_creates_placeholder_for_unknown_contact():
    app = _mirror_app(contacts=[])
    event = _mirror_event(
        ChatContact(id="+391111111111", display_name="+391111111111", protocol="signal")
    )

    assert app._handle_sent_mirror_event(event) is True
    assert len(app.contacts) == 1
    placeholder = app.contacts[0]
    assert placeholder.id == "+391111111111"
    assert placeholder.cache_key == "signal:+391111111111"
    assert placeholder.last_message_ts == 1_787_250_931_234
    assert app.manager.get("signal").contacts == [placeholder]
    assert app._contact_list_dirty is True
    assert app._dirty_contact_keys == {placeholder.cache_key}


def test_sent_mirror_does_not_ingest_or_push_web_event():
    contact = ChatContact(id="+391234567890", display_name="Alice", protocol="signal")
    backend = SimpleNamespace(
        contacts=[contact],
        _identify_contact=MagicMock(return_value=None),
        ingest_message=MagicMock(),
    )
    app = _mirror_app(contacts=[contact], backend=backend, web_enabled=True)
    app.selected_contact = contact
    event = _mirror_event(contact)

    with patch("web.bridge.push_event") as push_event:
        assert app._handle_sent_mirror_event(event) is True

    # No double DB write and no duplicate web push: the selected contact's
    # timestamp is refreshed but the list stays clean.
    backend.ingest_message.assert_not_called()
    push_event.assert_not_called()
    assert app._contact_list_dirty is False
    assert app._dirty_contact_keys == set()
