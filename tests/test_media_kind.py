"""Tests for normalized media classification and schema backfill."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from backend.db import _migrate_protocol_schema
from backends.signal import SignalBackend
from backends.telegram import _tg_media_kind
from models import media_kind_from_mime, msg_type_for_media_kind


@pytest.mark.parametrize(
    ("mime", "hints", "expected"),
    [
        (None, {}, None),
        ("", {}, None),
        (" IMAGE/GIF; charset=binary ", {}, "gif"),
        ("image/png", {}, "image"),
        ("video/mp4", {}, "video"),
        ("audio/ogg", {}, "audio"),
        ("audio/ogg", {"is_voice": True}, "voice"),
        ("video/mp4", {"is_gif": True}, "gif"),
        ("application/pdf", {}, "document"),
    ],
)
def test_media_kind_from_mime(mime, hints, expected):
    assert media_kind_from_mime(mime, **hints) == expected


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("image", "image"),
        ("gif", "image"),
        ("sticker", "sticker"),
        ("video", "attachment"),
        ("voice", "attachment"),
        ("audio", "attachment"),
        ("document", "attachment"),
    ],
)
def test_msg_type_for_media_kind(kind, expected):
    assert msg_type_for_media_kind(kind) == expected


@pytest.mark.parametrize(
    ("attachment", "expected_kind", "expected_type"),
    [
        (
            {"id": "voice", "contentType": "audio/ogg", "voiceNote": True},
            "voice",
            "attachment",
        ),
        ({"id": "gif", "contentType": "image/gif"}, "gif", "image"),
    ],
)
def test_signal_attachment_media_kind(attachment, expected_kind, expected_type):
    backend = SignalBackend.__new__(SignalBackend)
    messages = backend._extract_message_data(
        {
            "sourceNumber": "+39000",
            "dataMessage": {"attachments": [attachment]},
        }
    )

    assert messages[0]["media_kind"] == expected_kind
    assert messages[0]["msg_type"] == expected_type


class DocumentAttributeFilename:
    def __init__(self, file_name="file.bin"):
        self.file_name = file_name


class DocumentAttributeSticker:
    pass


class DocumentAttributeAnimated:
    pass


class DocumentAttributeVideo:
    pass


class DocumentAttributeVoice:
    pass


class DocumentAttributeAudio:
    def __init__(self, *, voice=False):
        self.voice = voice


@pytest.mark.parametrize(
    ("attributes", "mime", "expected"),
    [
        ([DocumentAttributeSticker()], "image/webp", "sticker"),
        ([DocumentAttributeVideo()], "video/mp4", "video"),
        ([DocumentAttributeVoice()], "audio/ogg", "voice"),
        ([DocumentAttributeAudio(voice=True)], "audio/ogg", "voice"),
        ([DocumentAttributeAudio()], "audio/mpeg", "audio"),
        ([DocumentAttributeAnimated()], "video/mp4", "gif"),
        ([DocumentAttributeFilename("report.pdf")], "application/pdf", "document"),
    ],
)
def test_telegram_document_attribute_media_kind(attributes, mime, expected):
    msg = SimpleNamespace(
        photo=None,
        document=SimpleNamespace(mime_type=mime, attributes=attributes),
    )

    kind, filename, actual_mime = _tg_media_kind(msg)

    assert kind == expected
    assert actual_mime == mime
    if expected == "document":
        assert filename == "report.pdf"


def test_media_kind_backfill_is_idempotent():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            protocol TEXT,
            contact_number TEXT,
            timestamp INTEGER,
            msg_type TEXT,
            attachment_info TEXT,
            attachment_id TEXT,
            content_type TEXT,
            media_kind TEXT,
            edited INTEGER DEFAULT 0,
            quote_attachment_id TEXT,
            quote_content_type TEXT,
            quote_attachment_path TEXT,
            msg_id TEXT,
            quote_timestamp INTEGER,
            quote_author TEXT,
            reply_to_message_id TEXT
        )
        """
    )
    rows = [
        ("sticker", None, None, None),
        ("image", None, None, "image/gif"),
        ("image", None, None, "image/jpeg"),
        ("attachment", None, None, "video/mp4"),
        ("attachment", None, None, "audio/ogg"),
        ("attachment", "🎬 Video", None, None),
        ("attachment", None, "recording.opus", None),
        (
            "attachment",
            "audio/ogg; codecs=opus",
            "https://example.test/voice-note.oga",
            None,
        ),
        ("attachment", None, "photo.heic", None),
        ("attachment", "unknown", "blob", None),
    ]
    connection.executemany(
        "INSERT INTO messages (msg_type, attachment_info, attachment_id, content_type) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )
    connection.execute("PRAGMA user_version = 3")

    _migrate_protocol_schema(connection)
    first = connection.execute("SELECT media_kind FROM messages ORDER BY id").fetchall()
    connection.execute("PRAGMA user_version = 3")
    _migrate_protocol_schema(connection)
    second = connection.execute(
        "SELECT media_kind FROM messages ORDER BY id"
    ).fetchall()

    assert first == second
    assert [row[0] for row in first] == [
        "sticker",
        "gif",
        "image",
        "video",
        "audio",
        "video",
        "audio",
        "audio",
        "image",
        "document",
    ]


def test_media_kind_correction_reclassifies_migrated_audio_documents():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE messages (media_kind TEXT, attachment_id TEXT, "
        "attachment_info TEXT, edited INTEGER DEFAULT 0, content_type TEXT, "
        "quote_attachment_id TEXT, quote_content_type TEXT, "
        "quote_attachment_path TEXT)"
    )
    connection.executemany(
        "INSERT INTO messages VALUES (?, ?, ?, 0, NULL, NULL, NULL, NULL)",
        [
            ("document", "old-voice.oga", None),
            ("document", "opaque", "audio/ogg; codecs=opus"),
            ("document", "report.pdf", "application/pdf"),
        ],
    )
    connection.execute("PRAGMA user_version = 4")

    _migrate_protocol_schema(connection)
    first = connection.execute("SELECT media_kind FROM messages").fetchall()
    _migrate_protocol_schema(connection)
    second = connection.execute("SELECT media_kind FROM messages").fetchall()

    assert first == second == [("audio",), ("audio",), ("document",)]


def test_media_kind_correction_backfills_null_audio_on_v4_database():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE messages (msg_type TEXT, media_kind TEXT, "
        "attachment_id TEXT, attachment_info TEXT, edited INTEGER DEFAULT 0, "
        "content_type TEXT, quote_attachment_id TEXT, quote_content_type TEXT, "
        "quote_attachment_path TEXT)"
    )
    connection.execute(
        "INSERT INTO messages "
        "(msg_type, attachment_info, attachment_id, media_kind) "
        "VALUES ('attachment', 'audio/ogg; codecs=opus', "
        "'http://localhost:3000/api/files/default/false_x.oga', NULL)"
    )
    connection.execute("PRAGMA user_version = 4")

    _migrate_protocol_schema(connection)
    first = connection.execute("SELECT media_kind FROM messages").fetchone()[0]
    _migrate_protocol_schema(connection)
    second = connection.execute("SELECT media_kind FROM messages").fetchone()[0]

    assert first == second == "audio"
