"""
Regression tests for Fix 1 — schema versioning via ``PRAGMA user_version``.

Before the fix, ``_init_db()`` called ``_migrate_protocol_schema()`` on every
invocation, which performed ``DROP INDEX`` + ``CREATE INDEX`` on every single
write (index churn on the UI thread).  The fix gates the migration behind
``PRAGMA user_version`` so the DROP/CREATE runs exactly once per database.

These tests verify:
  T1a: a legacy DB is migrated to ``user_version == 4`` and the SECOND
       ``_init_db()`` call does NOT run DROP/CREATE INDEX.
  T1b: after init the index is ``(protocol, contact_number, timestamp)`` and
       the ``messages`` table carries ``protocol`` and ``msg_id``.
  T1c: ``_migrate_protocol_schema`` on an already-versioned connection is a
       no-op that does not touch ``sqlite_master``.
  T1d: on an already-migrated, populated DB ``_init_db()`` stays fast and does
       not grow with the number of rows.
"""

from __future__ import annotations

import sqlite3
import statistics
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import protocols.db as backend_mod  # package: DB_FILE / CACHE_DIR are patched here
import protocols.db as db_mod  # the db module under test


def _make_legacy_db(db_file: Path) -> None:
    """Create a legacy ``messages`` table WITHOUT protocol/msg_id columns."""
    conn = sqlite3.connect(db_file)
    conn.execute(
        """
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
            status TEXT DEFAULT 'read'
        )
        """
    )
    conn.execute(
        "CREATE INDEX idx_messages_contact ON messages(contact_number, timestamp)"
    )
    conn.execute(
        "INSERT INTO messages (contact_number, text, timestamp) "
        "VALUES ('+391234567890', 'ciao', 1000)"
    )
    conn.commit()
    conn.close()


class _SpyConnection:
    """Proxy around a real sqlite3 connection that records executed SQL."""

    def __init__(self, conn: sqlite3.Connection, statements: list[str]):
        self._conn = conn
        self._statements = statements

    def execute(self, sql: str, *params):
        self._statements.append(sql)
        return self._conn.execute(sql, *params)

    def commit(self):
        return self._conn.commit()

    def close(self):
        return self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _user_version(db_file: Path) -> int:
    conn = sqlite3.connect(db_file)
    value = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    return value


def _table_columns(db_file: Path) -> list[str]:
    conn = sqlite3.connect(db_file)
    cols = [row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()]
    conn.close()
    return cols


def _index_columns(db_file: Path) -> list[str]:
    conn = sqlite3.connect(db_file)
    cols = [
        row[2]
        for row in conn.execute("PRAGMA index_info(idx_messages_contact)").fetchall()
    ]
    conn.close()
    return cols


@pytest.fixture
def tmp_db(tmp_path: Path):
    """Point the backend at a temp DB/CACHE_DIR for the duration of a test."""
    db_file = tmp_path / "messages.db"
    with (
        patch.object(backend_mod, "DB_FILE", db_file),
        patch.object(backend_mod, "CACHE_DIR", tmp_path),
    ):
        yield db_file


class TestSchemaVersioning:
    """🧱 T1a-d — versioning dello schema via ``PRAGMA user_version``."""

    def test_init_db_migrates_legacy_and_sets_user_version(self, tmp_db):
        """(a) ``_init_db()`` su DB legacy imposta ``user_version == 4``."""
        _make_legacy_db(tmp_db)
        assert _user_version(tmp_db) == 0  # legacy DB is unversioned

        backend_mod._init_db()

        assert _user_version(tmp_db) == 4
        assert "protocol" in _table_columns(tmp_db)
        assert "msg_id" in _table_columns(tmp_db)
        assert "reply_to_message_id" in _table_columns(tmp_db)
        assert "media_kind" in _table_columns(tmp_db)

    def test_second_init_does_not_churn_index(self, tmp_db):
        """(a) la SECONDA ``_init_db()`` NON esegue DROP/CREATE INDEX."""
        _make_legacy_db(tmp_db)
        backend_mod._init_db()  # first call migrates

        statements: list[str] = []
        real_connect = db_mod.sqlite3.connect

        def spy_connect(path, *args, **kwargs):
            return _SpyConnection(real_connect(path, *args, **kwargs), statements)

        with patch.object(db_mod.sqlite3, "connect", side_effect=spy_connect):
            backend_mod._init_db()

        churn = [
            s
            for s in statements
            if "DROP INDEX" in s.upper() or "CREATE INDEX" in s.upper()
        ]
        assert churn == [], f"index churn on second init: {churn}"

    def test_init_db_builds_protocol_index_and_columns(self, tmp_db):
        """(b) dopo init: indice (protocol, contact_number, timestamp) + colonne."""
        _make_legacy_db(tmp_db)
        backend_mod._init_db()

        assert _index_columns(tmp_db) == ["protocol", "contact_number", "timestamp"]
        cols = _table_columns(tmp_db)
        assert "protocol" in cols
        assert "msg_id" in cols
        assert "media_kind" in cols

    def test_migrate_on_versioned_connection_is_noop(self, tmp_db):
        """(c) Una migrazione già a v4 non modifica nuovamente lo schema."""
        backend_mod._init_db()  # fresh, already versioned

        conn = sqlite3.connect(tmp_db)
        before = conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()
        statements: list[str] = []
        spy = _SpyConnection(conn, statements)
        backend_mod._migrate_protocol_schema(spy)
        after = conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()
        conn.close()

        assert before == after  # sqlite_master unchanged
        churn = [
            s
            for s in statements
            if "DROP INDEX" in s.upper()
            or "CREATE INDEX" in s.upper()
            or "ALTER TABLE" in s.upper()
        ]
        assert churn == [], f"schema mutation on versioned connection: {churn}"

    def test_v4_connection_still_ensures_media_kind_column(self, tmp_db):
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                edited INTEGER NOT NULL DEFAULT 0,
                content_type TEXT,
                quote_attachment_id TEXT,
                quote_content_type TEXT,
                quote_attachment_path TEXT
            )
            """
        )
        conn.execute("PRAGMA user_version = 4")
        statements: list[str] = []

        db_mod._migrate_protocol_schema(_SpyConnection(conn, statements))

        assert "media_kind" in {
            row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        assert any("ADD COLUMN media_kind TEXT" in sql for sql in statements)
        conn.close()

    def test_init_db_fast_on_populated_migrated_db(self, tmp_db):
        """(d) su DB popolato già migrato ``_init_db()`` resta veloce (<5ms mediana)."""
        backend_mod._init_db()
        conn = sqlite3.connect(tmp_db)
        conn.executemany(
            "INSERT INTO messages (protocol, contact_number, text, timestamp) "
            "VALUES (?, ?, ?, ?)",
            [("signal", "+39", f"msg-{i}", i) for i in range(5000)],
        )
        conn.commit()
        conn.close()

        durations_ms: list[float] = []
        for _ in range(15):
            start = time.perf_counter()
            backend_mod._init_db()
            durations_ms.append((time.perf_counter() - start) * 1000)

        # The migration gate must skip the index rebuild regardless of size.
        assert statistics.median(durations_ms) < 5.0, (
            f"_init_db median {statistics.median(durations_ms):.3f}ms >= 5ms"
        )

    def test_reply_relationship_survives_cache_reload(self, tmp_db):
        backend_mod._add_message_to_cache(
            "42",
            "reply",
            True,
            "You",
            100,
            protocol="telegram",
            msg_id="99",
            quote_text="original",
            quote_timestamp=90,
            quote_author="42",
            reply_to_message_id="12",
        )
        message = backend_mod._load_cache(protocol="telegram")["42"][0]
        assert message["reply_to_message_id"] == "12"
        assert message["quote_timestamp"] == 90
