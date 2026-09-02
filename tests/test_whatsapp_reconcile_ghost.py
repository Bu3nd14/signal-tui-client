from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from models import PROTOCOL_TELEGRAM, PROTOCOL_WHATSAPP
from protocols import db
from protocols.whatsapp import WhatsAppBackend

CID = "391234567890@c.us"
TS = 1_700_000_000_000


@pytest.fixture
def tmp_db(tmp_path: Path):
    db_file = tmp_path / "messages.db"
    with (
        patch.object(db, "DB_FILE", db_file),
        patch.object(db, "CACHE_DIR", tmp_path),
    ):
        yield db_file


def _backend() -> WhatsAppBackend:
    return WhatsAppBackend(api_url="http://api.test")


def _message(msg_id: str, text: str = "ciao") -> dict:
    return {
        "id": msg_id,
        "text": text,
        "is_mine": True,
        "sender": "You",
        "msg_type": "text",
        "attachment_id": None,
    }


def _rows(db_file: Path) -> list[tuple]:
    with sqlite3.connect(db_file) as conn:
        return conn.execute(
            "SELECT msg_id, text, status, edited FROM messages ORDER BY id"
        ).fetchall()


def test_reconcile_different_id_keeps_original_row(tmp_db):
    backend = _backend()
    assert backend.ingest_message(CID, _message("A"), TS)

    assert backend.ingest_message(
        CID, _message("B"), TS + 1000, reconcile=True
    ) is False

    assert len(backend.cache[CID]) == 1
    assert backend.cache[CID][0]["id"] == "A"
    assert [row[0] for row in _rows(tmp_db)] == ["A"]


def test_reconcile_exact_ids_keeps_repeated_real_sends(tmp_db):
    backend = _backend()
    assert backend.ingest_message(CID, _message("X", "OK"), TS)
    assert backend.ingest_message(CID, _message("Y", "OK"), TS + 1000)

    assert not backend.ingest_message(
        CID, _message("X", "OK"), TS, reconcile=True
    )
    assert not backend.ingest_message(
        CID, _message("Y", "OK"), TS + 1000, reconcile=True
    )

    assert [message["id"] for message in backend.cache[CID]] == ["X", "Y"]
    assert [row[0] for row in _rows(tmp_db)] == ["X", "Y"]


def test_reconcile_ambiguous_text_skips_unknown_id(tmp_db, caplog):
    backend = _backend()
    backend.ingest_message(CID, _message("X", "OK"), TS)
    backend.ingest_message(CID, _message("Y", "OK"), TS + 1000)

    with caplog.at_level(logging.WARNING, logger="protocols.whatsapp"):
        result = backend.ingest_message(
            CID, _message("Z", "OK"), TS + 2000, reconcile=True
        )

    assert result is False
    assert len(backend.cache[CID]) == 2
    assert len(_rows(tmp_db)) == 2
    assert "ambiguous outgoing text" in caplog.text


def test_reconcile_text_ignores_media_with_same_caption(tmp_db, caplog):
    backend = _backend()
    backend.ingest_message(
        CID,
        {
            **_message("media-id", "caption"),
            "msg_type": "video",
            "attachment_id": "sent-video.mp4",
        },
        TS,
    )
    backend.ingest_message(CID, _message("text-id", "caption"), TS + 1000)

    with caplog.at_level(logging.INFO, logger="protocols.whatsapp"):
        result = backend.ingest_message(
            CID,
            _message("rest-id", "caption"),
            TS + 2000,
            reconcile=True,
        )

    assert result is False
    assert [message["id"] for message in backend.cache[CID]] == [
        "media-id",
        "text-id",
    ]
    assert "kept original id=text-id" in caplog.text
    assert "ambiguous outgoing text" not in caplog.text


def test_default_ingest_keeps_distinct_ids(tmp_db):
    backend = _backend()
    backend.ingest_message(CID, _message("A"), TS)

    assert backend.ingest_message(CID, _message("B"), TS + 1000)
    assert [message["id"] for message in backend.cache[CID]] == ["A", "B"]
    assert len(_rows(tmp_db)) == 2


def test_whitespace_only_edit_is_noop_and_rollback_clears_edited(tmp_db):
    db._add_message_to_cache(
        CID,
        "hello world",
        True,
        "You",
        TS,
        protocol=PROTOCOL_WHATSAPP,
        msg_id="A",
    )
    backend = _backend()
    backend.cache = db._load_cache(protocol=PROTOCOL_WHATSAPP)

    assert backend._detect_edit(CID, "A", " hello\n world ", True, TS) is None
    assert backend.apply_edit(CID, "A", " hello\n world ") is None
    assert _rows(tmp_db)[0][3] == 0

    assert backend.apply_edit(CID, "A", "changed") is not None
    assert backend.apply_edit(CID, "A", "hello world", mark_edited=False) is not None
    assert backend.cache[CID][0]["edited"] is False
    assert _rows(tmp_db)[0][1:] == ("hello world", "sent", 0)


def test_boot_purge_removes_lower_rank_but_keeps_equal_rank(tmp_db, caplog):
    for msg_id, text, timestamp, status in (
        ("read-id", "ghost", TS, "read"),
        ("sent-id", "ghost", TS + 1000, "sent"),
        ("real-x", "OK", TS + 2000, "sent"),
        ("real-y", "OK", TS + 3000, "sent"),
    ):
        db._add_message_to_cache(
            CID,
            text,
            True,
            "You",
            timestamp,
            protocol=PROTOCOL_WHATSAPP,
            msg_id=msg_id,
            status=status,
        )

    with caplog.at_level(logging.WARNING, logger="protocols.db"):
        loaded = db._load_cache(protocol=PROTOCOL_WHATSAPP)

    assert [message["id"] for message in loaded[CID]] == [
        "read-id",
        "sent-id",
        "real-x",
        "real-y",
    ]
    assert [row[0] for row in _rows(tmp_db)] == [
        "read-id",
        "sent-id",
        "real-x",
        "real-y",
    ]
    detected = db._detect_ghost_outgoing_text()
    assert {(group["contact"], group["text"]) for group in detected} == {
        (CID, "ghost"),
        (CID, "OK"),
    }
    assert "run purge_ghost_outgoing.py --apply to remove" in caplog.text


def test_boot_detection_never_deletes_legitimate_divergent_rank_pair(tmp_db):
    for msg_id, timestamp, status in (
        ("first-ok", TS, "read"),
        ("second-ok", TS + 1000, "sent"),
    ):
        db._add_message_to_cache(
            CID,
            "OK",
            True,
            "You",
            timestamp,
            protocol=PROTOCOL_WHATSAPP,
            msg_id=msg_id,
            status=status,
        )

    db._load_cache(protocol=PROTOCOL_WHATSAPP)

    assert [row[0] for row in _rows(tmp_db)] == ["first-ok", "second-ok"]


def test_boot_purge_does_not_touch_telegram_pair(tmp_db):
    for msg_id, timestamp, status in (
        ("telegram-read", TS, "read"),
        ("telegram-sent", TS + 1000, "sent"),
    ):
        db._add_message_to_cache(
            CID,
            "same telegram text",
            True,
            "You",
            timestamp,
            protocol=PROTOCOL_TELEGRAM,
            msg_id=msg_id,
            status=status,
        )

    db._load_cache(protocol=PROTOCOL_TELEGRAM)
    assert db._detect_ghost_outgoing_text() == []
    assert [row[0] for row in _rows(tmp_db)] == ["telegram-read", "telegram-sent"]


def test_fetch_history_reconciles_remote_id_without_ghost(tmp_db):
    backend = _backend()
    backend._rest = MagicMock()
    backend._presence_subscribe_lazy = MagicMock()
    backend.ingest_message(CID, _message("local-id", "from web"), TS)
    backend._rest.list_messages.return_value = [
        {
            "id": "history-hex-id",
            "to": CID,
            "fromMe": True,
            "timestamp": TS // 1000 + 1,
            "body": "from web",
            "ack": 3,
        }
    ]

    backend.fetch_history(CID)

    assert len(backend.cache[CID]) == 1
    assert backend.cache[CID][0]["id"] == "local-id"
    assert [row[0] for row in _rows(tmp_db)] == ["local-id"]
