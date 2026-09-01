from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

import protocols.signal as signal_mod
from protocols import SignalBackend

CONTACT = "+391234567890"
T0 = 1_700_000_000_000


@pytest.fixture
def signal_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SignalBackend:
    """Build an offline Signal backend while retaining outgoing media ids."""
    attachment = tmp_path / "attachment.bin"
    attachment.write_bytes(b"test attachment")
    backend = SignalBackend()
    monkeypatch.setattr(backend, "get_attachment_path", lambda _id: attachment)
    return backend


def _outgoing(
    message_id: str | None,
    *,
    text: str = "",
    msg_type: str = "text",
    attachment_id: str | None = None,
) -> dict:
    return {
        "id": message_id,
        "text": text,
        "is_mine": True,
        "sender": "You",
        "quote_text": None,
        "msg_type": msg_type,
        "attachment_info": None,
        "attachment_id": attachment_id,
        "content_type": None,
    }


def _db_rows() -> list[tuple[str | None, str | None]]:
    import protocols.db as backend_mod

    with sqlite3.connect(backend_mod.DB_FILE) as conn:
        return conn.execute(
            "SELECT msg_id, attachment_id FROM messages "
            "WHERE protocol = 'signal' ORDER BY timestamp"
        ).fetchall()


def test_two_images_rapid_echoes_not_merged(signal_backend: SignalBackend):
    """Two image echoes 4.8 s apart are separate persisted messages."""
    first = _outgoing("image-A", msg_type="image", attachment_id="photo-A")
    second = _outgoing("image-B", msg_type="image", attachment_id="photo-B")

    results = [
        signal_backend.ingest_message(CONTACT, first, T0),
        signal_backend.ingest_message(CONTACT, second, T0 + 4_800),
    ]

    assert results == [True, True]
    assert len(signal_backend.cache[CONTACT]) == 2
    assert {row["id"] for row in signal_backend.cache[CONTACT]} == {
        "image-A",
        "image-B",
    }
    assert {row["attachment_id"] for row in signal_backend.cache[CONTACT]} == {
        "photo-A",
        "photo-B",
    }
    assert _db_rows() == [("image-A", "photo-A"), ("image-B", "photo-B")]


def test_two_identical_texts_confirmed_not_merged(signal_backend: SignalBackend):
    """Confirmed equal texts with distinct ids remain two cache and DB rows."""
    results = [
        signal_backend.ingest_message(CONTACT, _outgoing("text-A", text="OK"), T0),
        signal_backend.ingest_message(
            CONTACT, _outgoing("text-B", text="OK"), T0 + 4_000
        ),
    ]

    assert results == [True, True]
    assert len(signal_backend.cache[CONTACT]) == 2
    assert [row["id"] for row in signal_backend.cache[CONTACT]] == [
        "text-A",
        "text-B",
    ]
    assert _db_rows() == [("text-A", None), ("text-B", None)]


def test_echo_upgrades_only_idless_row(signal_backend: SignalBackend):
    """An echo upgrades its optimistic twin, never a differently-id'd row."""
    confirmed = {
        **_outgoing("confirmed-A", text="ciao"),
        "timestamp": T0,
    }
    optimistic = {
        **_outgoing(None, text="ciao"),
        "timestamp": T0 + 1_000,
    }
    signal_backend.cache[CONTACT] = [confirmed, optimistic]

    with patch.object(signal_mod, "_update_message_id") as update_id:
        added = signal_backend.ingest_message(
            CONTACT, _outgoing("echo-B", text="ciao"), T0 + 2_000
        )

    assert added is False
    assert len(signal_backend.cache[CONTACT]) == 2
    assert confirmed["id"] == "confirmed-A"
    assert confirmed["timestamp"] == T0
    assert optimistic["id"] == "echo-B"
    assert optimistic["timestamp"] == T0 + 1_000
    update_id.assert_called_once_with(
        CONTACT,
        "ciao",
        True,
        T0 + 1_000,
        "echo-B",
        protocol="signal",
    )


def test_echo_out_of_order_picks_closest_idless(signal_backend: SignalBackend):
    """The nearest of several compatible id-less rows receives the echo id."""
    older = {**_outgoing(None, text="OK"), "timestamp": T0}
    closest = {**_outgoing(None, text="OK"), "timestamp": T0 + 4_000}
    signal_backend.cache[CONTACT] = [older, closest]

    with patch.object(signal_mod, "_update_message_id") as update_id:
        added = signal_backend.ingest_message(
            CONTACT, _outgoing("echo-B", text="OK"), T0 + 4_500
        )

    assert added is False
    assert len(signal_backend.cache[CONTACT]) == 2
    assert older["id"] is None
    assert closest["id"] == "echo-B"
    assert closest["timestamp"] == T0 + 4_000
    update_id.assert_called_once()


def test_outgoing_multi_attachment_same_id_not_collapsed(
    signal_backend: SignalBackend,
):
    """One Signal id may represent distinct outgoing attachment entries."""
    first = _outgoing(
        str(T0),
        text="File: first.bin",
        msg_type="attachment",
        attachment_id="attachment-A",
    )
    second = _outgoing(
        str(T0),
        text="File: second.bin",
        msg_type="attachment",
        attachment_id="attachment-B",
    )

    results = [
        signal_backend.ingest_message(CONTACT, first, T0, persist=False),
        signal_backend.ingest_message(CONTACT, second, T0, persist=False),
    ]

    assert results == [True, True]
    assert len(signal_backend.cache[CONTACT]) == 2
    assert {row["attachment_id"] for row in signal_backend.cache[CONTACT]} == {
        "attachment-A",
        "attachment-B",
    }


def test_last_ditch_idless_requires_same_attachment(
    signal_backend: SignalBackend,
):
    """The 5 s no-id fallback must not merge different attachments."""
    first = _outgoing(None, msg_type="image", attachment_id="photo-A")
    second = _outgoing(None, msg_type="image", attachment_id="photo-B")

    results = [
        signal_backend.ingest_message(CONTACT, first, T0, persist=False),
        signal_backend.ingest_message(CONTACT, second, T0 + 1_000, persist=False),
    ]

    assert results == [True, True]
    assert len(signal_backend.cache[CONTACT]) == 2
    assert [row["attachment_id"] for row in signal_backend.cache[CONTACT]] == [
        "photo-A",
        "photo-B",
    ]
