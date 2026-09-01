from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import protocols.db as backend
from transcription import store


@pytest.fixture
def transcription_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_file = tmp_path / "protocols.db"
    monkeypatch.setattr(backend, "DB_FILE", db_file)
    monkeypatch.setattr(backend, "CACHE_DIR", tmp_path)
    return db_file


def test_get_missing_key_returns_none(transcription_db: Path):
    assert store.get("signal", "missing") is None


def test_set_then_get_returns_persisted_record(transcription_db: Path):
    store.set(
        "signal", "audio-1", status="ok", text="ciao", model="gpt-transcribe"
    )

    record = store.get("signal", "audio-1")

    assert record is not None
    assert record["status"] == "ok"
    assert record["text"] == "ciao"
    assert record["error"] is None
    assert record["model"] == "gpt-transcribe"
    assert isinstance(record["updated_at"], float)


def test_set_upserts_existing_key_without_duplicate(transcription_db: Path):
    store.set("telegram", "audio-2", status="pending")
    store.set(
        "telegram", "audio-2", status="failed", error="boom", model="whisper-1"
    )

    record = store.get("telegram", "audio-2")
    with sqlite3.connect(transcription_db) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM transcriptions "
            "WHERE protocol = ? AND attachment_id = ?",
            ("telegram", "audio-2"),
        ).fetchone()[0]

    assert count == 1
    assert record is not None
    assert record["status"] == "failed"
    assert record["error"] == "boom"
    assert record["model"] == "whisper-1"


def test_init_db_creates_transcriptions_table_at_schema_v5(transcription_db: Path):
    backend._init_db()

    with sqlite3.connect(transcription_db) as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='transcriptions'"
        ).fetchone()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(transcriptions)")
        }

    assert table == ("transcriptions",)
    assert version == 5
    assert columns == {
        "protocol",
        "attachment_id",
        "status",
        "text",
        "error",
        "model",
        "updated_at",
    }
