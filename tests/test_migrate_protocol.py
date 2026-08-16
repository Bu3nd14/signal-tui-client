"""
Regression tests for the multi-protocol cache migration (migrate_cache_protocol.py).

Verifies:
1. The ``protocol`` column is added to an existing DB with default 'signal'.
2. Already-migrated DBs are a no-op (idempotent).
3. The contact index is rebuilt with the protocol prefix.
"""

from __future__ import annotations

import runpy
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import migrate_cache_protocol as mig


def _make_legacy_db(db_file: Path) -> None:
    """Create a legacy messages table WITHOUT the protocol column."""
    conn = sqlite3.connect(db_file)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
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
    """)
    conn.execute(
        "INSERT INTO messages (contact_number, text, timestamp) "
        "VALUES ('+391234567890', 'ciao', 1000)"
    )
    conn.commit()
    conn.close()


def _table_columns(db_file: Path) -> set[str]:
    conn = sqlite3.connect(db_file)
    rows = conn.execute("PRAGMA table_info(messages)").fetchall()
    conn.close()
    return {row[1] for row in rows}


@pytest.fixture
def tmp_db(tmp_path: Path):
    db_file = tmp_path / "messages.db"
    with patch.object(mig, "DB_FILE", db_file):
        yield db_file


class TestMigrateProtocol:
    """🔄 Migrazione schema → colonna protocol."""

    def test_adds_protocol_column(self, tmp_db):
        _make_legacy_db(tmp_db)
        mig.migrate()
        cols = _table_columns(tmp_db)
        assert "protocol" in cols

    def test_defaults_to_signal(self, tmp_db):
        _make_legacy_db(tmp_db)
        mig.migrate()
        conn = sqlite3.connect(tmp_db)
        row = conn.execute("SELECT protocol FROM messages").fetchone()
        conn.close()
        assert row[0] == "signal"

    def test_idempotent(self, tmp_db):
        _make_legacy_db(tmp_db)
        mig.migrate()
        mig.migrate()  # second run must not error
        cols = _table_columns(tmp_db)
        assert "protocol" in cols

    def test_index_uses_protocol(self, tmp_db):
        _make_legacy_db(tmp_db)
        mig.migrate()
        conn = sqlite3.connect(tmp_db)
        idx = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_messages_contact'"
        ).fetchone()
        conn.close()
        assert idx is not None and "protocol" in idx[0]

    def test_no_db_is_noop(self, tmp_path):
        """Se il DB non esiste, la migrazione non solleva errori."""
        db_file = tmp_path / "nonexistent.db"
        with patch.object(mig, "DB_FILE", db_file):
            mig.migrate()  # must not raise


class TestMigrateProtocolNewDB:
    """🆕 La migrazione su un DB già con protocol (schema nuovo) è invariante."""

    def test_keeps_existing_protocol(self, tmp_db):
        conn = sqlite3.connect(tmp_db)
        conn.execute("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                protocol TEXT NOT NULL DEFAULT 'signal',
                contact_number TEXT NOT NULL,
                text TEXT,
                timestamp INTEGER NOT NULL,
                read INTEGER DEFAULT 0,
                status TEXT DEFAULT 'read'
            )
        """)
        conn.execute(
            "INSERT INTO messages (protocol, contact_number, text, timestamp) "
            "VALUES ('whatsapp', 'wa-1', 'ciao', 1)"
        )
        conn.commit()
        conn.close()

        mig.migrate()

        conn = sqlite3.connect(tmp_db)
        row = conn.execute("SELECT protocol FROM messages").fetchone()
        conn.close()
        assert row[0] == "whatsapp"  # preserved


class TestInitDBAutoMigrate:
    """🩹 _init_db() aggiorna automaticamente un DB legacy (self-healing)."""

    @pytest.fixture
    def tmp_backend_db(self, tmp_path: Path):
        """Point backend at a temp legacy DB for _init_db tests."""
        import backend as backend_mod

        db_file = tmp_path / "messages.db"
        with (
            patch.object(backend_mod, "DB_FILE", db_file),
            patch.object(backend_mod, "CACHE_DIR", tmp_path),
        ):
            _make_legacy_db(db_file)
            yield backend_mod, db_file

    def test_init_db_adds_protocol_column(self, tmp_backend_db):
        """_init_db migra la colonna protocol su un DB esistente."""
        backend_mod, db_file = tmp_backend_db
        backend_mod._init_db()
        cols = _table_columns(db_file)
        assert "protocol" in cols

    def test_init_db_defaults_existing_rows_to_signal(self, tmp_backend_db):
        """I messaggi esistenti diventano protocol='signal'."""
        backend_mod, db_file = tmp_backend_db
        backend_mod._init_db()
        conn = sqlite3.connect(db_file)
        row = conn.execute("SELECT protocol FROM messages").fetchone()
        conn.close()
        assert row[0] == "signal"

    def test_init_db_rebuilds_protocol_index(self, tmp_backend_db):
        """L'indice viene ricostruito con il prefisso protocol."""
        backend_mod, db_file = tmp_backend_db
        backend_mod._init_db()
        conn = sqlite3.connect(db_file)
        idx = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_messages_contact'"
        ).fetchone()
        conn.close()
        assert idx is not None and "protocol" in idx[0]

    def test_init_db_then_load_and_prune_no_error(self, tmp_backend_db):
        """Dopo _init_db, _load_cache/_prune_cache non sollevano più eccezioni."""
        backend_mod, _ = tmp_backend_db
        backend_mod._init_db()
        # These used to raise OperationalError: no such column: protocol.
        loaded = backend_mod._load_cache()
        assert "+391234567890" in loaded
        backend_mod._prune_cache()
        assert loaded["+391234567890"][0]["protocol"] == "signal"

    def test_init_db_idempotent(self, tmp_backend_db):
        """Chiamate ripetute di _init_db sono sicure."""
        backend_mod, _ = tmp_backend_db
        backend_mod._init_db()
        backend_mod._init_db()  # must not raise
        cols = _table_columns(backend_mod.DB_FILE)
        assert "protocol" in cols


def test_main_guard(tmp_path: Path, monkeypatch):
    """Running the script directly (__main__) is a no-op without a DB."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    runpy.run_path(PROJECT_ROOT / "migrate_cache_protocol.py", run_name="__main__")
