from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from purge_test_rows import (
    EXPECTED_MAX_COUNTS,
    TEST_ROWS,
    _backup_database,
    main,
    purge,
)

# Production schema of the ``messages`` table (see protocols/db.py migrations).
_MESSAGES_DDL = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_number TEXT NOT NULL,
    text TEXT,
    is_mine INTEGER NOT NULL DEFAULT 0,
    sender TEXT,
    timestamp INTEGER NOT NULL,
    quote_text TEXT,
    msg_type TEXT DEFAULT 'text',
    attachment_info TEXT,
    attachment_id TEXT,
    read INTEGER DEFAULT 0,
    status TEXT DEFAULT 'read',
    protocol TEXT NOT NULL DEFAULT 'signal',
    msg_id TEXT,
    quote_timestamp INTEGER,
    quote_author TEXT,
    reply_to_message_id TEXT,
    message_part_key TEXT,
    local_id TEXT,
    edited INTEGER NOT NULL DEFAULT 0,
    content_type TEXT,
    quote_attachment_id TEXT,
    quote_content_type TEXT,
    quote_attachment_path TEXT,
    media_kind TEXT,
    batch_id TEXT,
    batch_index INTEGER
)
"""

# Real regression-row values verified against the production DB.
_TELEGRAM_TIMESTAMP = 1786953045082
_TELEGRAM_MSG_ID = "77"


def _make_db(tmp_path: Path) -> Path:
    db_file = tmp_path / "messages.db"
    with sqlite3.connect(db_file) as connection:
        connection.execute(_MESSAGES_DDL)
    return db_file


def _insert(
    db_file: Path,
    protocol: str,
    contact: str,
    *,
    text: str = "",
    timestamp: int = 1,
    **extra,
) -> None:
    columns = {
        "protocol": protocol,
        "contact_number": contact,
        "text": text,
        "timestamp": timestamp,
        **extra,
    }
    with sqlite3.connect(db_file) as connection:
        connection.execute(
            f"INSERT INTO messages ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            list(columns.values()),
        )


def _count(db_file: Path) -> int:
    with sqlite3.connect(db_file) as connection:
        return connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


def _select(db_file: Path, expression: str) -> list:
    with sqlite3.connect(db_file) as connection:
        return connection.execute(
            f"SELECT {expression} FROM messages ORDER BY id"
        ).fetchall()


def _backups(tmp_path: Path) -> list[Path]:
    return list(tmp_path.glob("messages.db.bak-*"))


def test_purge_backs_up_and_removes_only_known_whatsapp_rows(tmp_path):
    db_file = _make_db(tmp_path)
    _insert(db_file, "whatsapp", "db@lid", text="remove")
    _insert(db_file, "whatsapp", "unread@lid", text="remove")
    _insert(db_file, "whatsapp", "3912345678@c.us", text="remove")
    _insert(db_file, "whatsapp", "111@lid", text="keep")
    _insert(db_file, "signal", "db@lid", text="keep")

    assert purge(db_file, apply=True) == 3

    backup = next(tmp_path.glob("messages.db.bak-*"))
    remaining = _select(db_file, "protocol, contact_number")
    with sqlite3.connect(backup) as connection:
        backed_up = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    assert remaining == [("whatsapp", "111@lid"), ("signal", "db@lid")]
    assert backed_up == 5


def test_purge_dry_run_does_not_delete(tmp_path):
    db_file = _make_db(tmp_path)
    for index in range(3):
        _insert(db_file, "signal", "42", batch_id="batch-1", batch_index=index)

    assert purge(db_file) == 3

    assert _count(db_file) == 3
    assert _backups(tmp_path) == []


def test_purge_apply_creates_backup_and_deletes(tmp_path):
    db_file = _make_db(tmp_path)
    for index in range(3):
        _insert(db_file, "signal", "42", batch_id="batch-1", batch_index=index)

    assert purge(db_file, apply=True) == 3

    backup = next(tmp_path.glob("messages.db.bak-*"))
    with sqlite3.connect(backup) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3
    assert _count(db_file) == 0


def test_purge_signal_batch_predicate(tmp_path):
    db_file = _make_db(tmp_path)
    for index in range(3):
        _insert(db_file, "signal", "42", batch_id="batch-1", batch_index=index)
    _insert(db_file, "signal", "42", text="real", timestamp=999)
    _insert(db_file, "signal", "42", batch_id="batch-2")

    assert purge(db_file, apply=True) == 3

    assert _select(db_file, "batch_id") == [(None,), ("batch-2",)]


def test_purge_telegram_timestamp_predicate(tmp_path):
    db_file = _make_db(tmp_path)
    _insert(
        db_file,
        "telegram",
        "42",
        text="reply",
        timestamp=_TELEGRAM_TIMESTAMP,
        msg_id=_TELEGRAM_MSG_ID,
    )
    _insert(db_file, "telegram", "42", text="reply", timestamp=1, msg_id="78")
    _insert(
        db_file,
        "telegram",
        "42",
        text="other",
        timestamp=_TELEGRAM_TIMESTAMP,
        msg_id="79",
    )

    assert purge(db_file, apply=True) == 1

    assert _select(db_file, "msg_id") == [("78",), ("79",)]


def test_purge_idempotent(tmp_path):
    db_file = _make_db(tmp_path)
    _insert(db_file, "signal", "42", batch_id="batch-1")
    _insert(
        db_file,
        "telegram",
        "42",
        text="reply",
        timestamp=_TELEGRAM_TIMESTAMP,
        msg_id=_TELEGRAM_MSG_ID,
    )

    assert purge(db_file, apply=True) == 2
    assert purge(db_file) == 0
    assert len(_backups(tmp_path)) == 1
    assert _count(db_file) == 0


def test_purge_abort_if_count_exceeds_expected(tmp_path):
    db_file = _make_db(tmp_path)
    for index in range(5):
        _insert(db_file, "signal", "42", batch_id="batch-1", batch_index=index)

    assert purge(db_file, apply=True) == -1

    assert _count(db_file) == 5
    assert _backups(tmp_path) == []


def test_test_rows_and_expected_counts_share_the_same_keys():
    assert set(TEST_ROWS) == set(EXPECTED_MAX_COUNTS)


def test_backup_database_avoids_existing_collision(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1000)
    db_file = _make_db(tmp_path)
    (tmp_path / "messages.db.bak-1000").write_text("occupied")

    backup = _backup_database(db_file)

    assert backup.name == "messages.db.bak-1001"
    assert backup.exists()


def test_purge_rolls_back_on_post_verify_mismatch(tmp_path):
    db_file = _make_db(tmp_path)
    _insert(db_file, "signal", "42", batch_id="batch-1")
    with sqlite3.connect(db_file) as connection:
        connection.execute(
            "CREATE TRIGGER reinsert_test_row AFTER DELETE ON messages "
            "WHEN OLD.batch_id = 'batch-1' BEGIN "
            "INSERT INTO messages (protocol, contact_number, timestamp, batch_id) "
            "VALUES ('signal', '42', 1, 'batch-1'); END"
        )

    assert purge(db_file, apply=True) == -1

    assert _count(db_file) == 1


def test_purge_aborts_on_invalid_backup(tmp_path, monkeypatch):
    import purge_test_rows

    db_file = _make_db(tmp_path)
    _insert(db_file, "signal", "42", batch_id="batch-1")
    fake_backup = tmp_path / "messages.db.bak-broken"
    fake_backup.write_text("this is not a sqlite database")
    monkeypatch.setattr(purge_test_rows, "_backup_database", lambda _db: fake_backup)

    assert purge(db_file, apply=True) == -1

    assert _count(db_file) == 1


def test_main_dry_run_returns_zero_without_backup(tmp_path):
    db_file = _make_db(tmp_path)
    _insert(db_file, "signal", "42", batch_id="batch-1")

    assert main(["--db", str(db_file)]) == 0

    assert _count(db_file) == 1
    assert _backups(tmp_path) == []


def test_main_apply_returns_zero_on_success(tmp_path):
    db_file = _make_db(tmp_path)
    _insert(db_file, "signal", "42", batch_id="batch-1")

    assert main(["--db", str(db_file), "--apply"]) == 0

    assert _count(db_file) == 0
    assert len(_backups(tmp_path)) == 1
