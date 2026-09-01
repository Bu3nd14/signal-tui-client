"""
Phase 1 (DB persistence) tests for message editing.

Covers the ``_update_message_text`` helper and the ``edited`` column
migration (schema v2 -> v3) described in DESIGN_EDIT_MESSAGES.md §5 and the
test plan in §7 (``tests/test_db_edit.py``).

Every test uses an isolated temporary SQLite DB (``protocols.db.DB_FILE`` and
``protocols.db.CACHE_DIR`` are patched), mirroring ``test_db_schema_versioning.py``;
the real DB in ``~/.local/share/signal-tui-client`` is never touched.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import protocols.db as backend_mod  # package: DB_FILE / CACHE_DIR are patched here

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _make_v2_db(db_file: Path) -> None:
    """Create a schema-v2 ``messages`` table (``user_version = 2``) WITHOUT the
    ``edited`` column — i.e. the state just before this feature shipped."""
    conn = sqlite3.connect(db_file)
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            protocol TEXT NOT NULL DEFAULT 'signal',
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
            msg_id TEXT,
            quote_timestamp INTEGER,
            quote_author TEXT,
            reply_to_message_id TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX idx_messages_contact "
        "ON messages(protocol, contact_number, timestamp)"
    )
    conn.execute(
        "INSERT INTO messages (protocol, contact_number, text, timestamp, is_mine) "
        "VALUES ('signal', '+391234567890', 'ciao', 1000, 1)"
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()


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


def _db_rows(db_file: Path) -> list[tuple]:
    """Raw ``(text, edited, protocol, contact_number, msg_id, timestamp)`` rows."""
    conn = sqlite3.connect(db_file)
    rows = conn.execute(
        "SELECT text, edited, protocol, contact_number, msg_id, timestamp "
        "FROM messages ORDER BY id"
    ).fetchall()
    conn.close()
    return rows


@pytest.fixture
def tmp_db(tmp_path: Path):
    """Point the backend at a temp DB/CACHE_DIR for the duration of a test."""
    db_file = tmp_path / "messages.db"
    with (
        patch.object(backend_mod, "DB_FILE", db_file),
        patch.object(backend_mod, "CACHE_DIR", tmp_path),
    ):
        yield db_file


# ─── _update_message_text ────────────────────────────────────────────────────


class TestUpdateMessageText:
    """✏️ ``_update_message_text`` — riscrittura in place del testo."""

    def test_update_by_msg_id(self, tmp_db):
        """Per ``msg_id``: testo aggiornato, ``edited=1``, ritorna True."""
        backend_mod._add_message_to_cache(
            "391234567890@c.us",
            "old",
            True,
            "You",
            1000,
            protocol="whatsapp",
            msg_id="WA-mid-1",
        )

        updated = backend_mod._update_message_text(
            "391234567890@c.us", "new", "whatsapp", msg_id="WA-mid-1"
        )

        assert updated is True
        msg = backend_mod._load_cache(protocol="whatsapp")["391234567890@c.us"][0]
        assert msg["text"] == "new"
        assert msg["edited"] is True
        assert msg["id"] == "WA-mid-1"  # identity untouched
        assert msg["timestamp"] == 1000  # timestamp untouched
        # raw DB carries the numeric 1 flag
        assert _db_rows(tmp_db)[0][0:2] == ("new", 1)

    def test_update_by_timestamp_with_old_text_guard(self, tmp_db):
        """``timestamp`` + ``old_text``: match solo sulla riga attesa."""
        backend_mod._add_message_to_cache(
            "+39", "aaa", True, "You", 1000, protocol="signal"
        )
        backend_mod._add_message_to_cache(
            "+39", "bbb", True, "You", 1000, protocol="signal"
        )

        updated = backend_mod._update_message_text(
            "+39", "nuovo", "signal", timestamp=1000, old_text="aaa"
        )

        assert updated is True
        texts = sorted(m["text"] for m in backend_mod._load_cache()["+39"])
        assert texts == ["bbb", "nuovo"]

    def test_wrong_old_text_returns_false_and_leaves_row_intact(self, tmp_db):
        """``old_text`` errato → False e riga intatta (timestamp path)."""
        backend_mod._add_message_to_cache(
            "+39", "original", True, "You", 1000, protocol="signal"
        )

        updated = backend_mod._update_message_text(
            "+39", "new", "signal", timestamp=1000, old_text="wrong"
        )

        assert updated is False
        row = _db_rows(tmp_db)[0]
        assert row[0] == "original"  # text unchanged
        assert row[1] == 0  # edited still 0

    def test_wrong_old_text_returns_false_on_msg_id_path(self, tmp_db):
        """``old_text`` errato → False anche sul path ``msg_id``."""
        backend_mod._add_message_to_cache(
            "391234567890@c.us",
            "original",
            True,
            "You",
            1000,
            protocol="whatsapp",
            msg_id="WA-mid-1",
        )

        updated = backend_mod._update_message_text(
            "391234567890@c.us",
            "new",
            "whatsapp",
            msg_id="WA-mid-1",
            old_text="wrong",
        )

        assert updated is False
        assert _db_rows(tmp_db)[0][0] == "original"

    def test_mark_edited_false_sets_edited_to_zero(self, tmp_db):
        """``mark_edited=False`` → ``edited=0`` (rollback path)."""
        backend_mod._add_message_to_cache(
            "+39", "old", True, "You", 1000, protocol="signal"
        )
        backend_mod._update_message_text(
            "+39", "new", "signal", timestamp=1000, old_text="old"
        )
        assert _db_rows(tmp_db)[0][1] == 1

        rolled_back = backend_mod._update_message_text(
            "+39", "old", "signal", timestamp=1000, old_text="new", mark_edited=False
        )

        assert rolled_back is True
        row = _db_rows(tmp_db)[0]
        assert row[0] == "old"
        assert row[1] == 0

    def test_no_key_returns_false(self, tmp_db):
        """Nessuna chiave (``msg_id=None, timestamp=None``) → False, nessun crash."""
        backend_mod._add_message_to_cache(
            "+39", "old", True, "You", 1000, protocol="signal"
        )

        assert backend_mod._update_message_text("+39", "new", "signal") is False
        # no accidental full-table update
        assert _db_rows(tmp_db)[0][0] == "old"

    def test_is_mine_guard(self, tmp_db):
        """``is_mine`` errato → False; giusto → True."""
        backend_mod._add_message_to_cache(
            "+39", "ciao", True, "You", 1000, protocol="signal"
        )

        assert (
            backend_mod._update_message_text(
                "+39", "new", "signal", timestamp=1000, is_mine=False
            )
            is False
        )
        assert (
            backend_mod._update_message_text(
                "+39", "new", "signal", timestamp=1000, is_mine=True
            )
            is True
        )

    def test_update_scoped_by_protocol(self, tmp_db):
        """Stesso (contact, ts) su due protocolli → aggiorna solo quello giusto."""
        backend_mod._add_message_to_cache(
            "+39", "ciao", True, "You", 1000, protocol="signal"
        )
        backend_mod._add_message_to_cache(
            "+39", "ciao", True, "You", 1000, protocol="whatsapp"
        )

        backend_mod._update_message_text("+39", "nuovo", "signal", timestamp=1000)

        loaded = backend_mod._load_cache()["+39"]
        by_protocol = {m["protocol"]: m["text"] for m in loaded}
        assert by_protocol == {"signal": "nuovo", "whatsapp": "ciao"}

    def test_update_scoped_by_contact(self, tmp_db):
        """Stesso (protocol, ts) su due contatti → aggiorna solo quello giusto."""
        backend_mod._add_message_to_cache(
            "+391", "ciao", True, "You", 1000, protocol="signal"
        )
        backend_mod._add_message_to_cache(
            "+392", "ciao", True, "You", 1000, protocol="signal"
        )

        backend_mod._update_message_text("+391", "nuovo", "signal", timestamp=1000)

        loaded = backend_mod._load_cache()
        assert loaded["+391"][0]["text"] == "nuovo"
        assert loaded["+392"][0]["text"] == "ciao"

    def test_update_by_msg_id_scoped_by_protocol_and_contact(self, tmp_db):
        """Stesso ``msg_id`` su (protocol, contact) diversi → aggiorna solo il giusto."""
        backend_mod._add_message_to_cache(
            "+391", "ciao", True, "You", 1000, protocol="signal", msg_id="mid-1"
        )
        backend_mod._add_message_to_cache(
            "+391", "ciao", True, "You", 1000, protocol="whatsapp", msg_id="mid-1"
        )
        backend_mod._add_message_to_cache(
            "+392", "ciao", True, "You", 1000, protocol="signal", msg_id="mid-1"
        )

        updated = backend_mod._update_message_text(
            "+391", "nuovo", "signal", msg_id="mid-1"
        )

        assert updated is True
        rows = _db_rows(tmp_db)
        assert ("nuovo", 1, "signal", "+391", "mid-1", 1000) in rows
        assert ("ciao", 0, "whatsapp", "+391", "mid-1", 1000) in rows
        assert ("ciao", 0, "signal", "+392", "mid-1", 1000) in rows
        assert len(rows) == 3

    def test_update_preserves_timestamp_and_id(self, tmp_db):
        """L'identità temporale (ts/id) non cambia mai con l'edit."""
        backend_mod._add_message_to_cache(
            "391234567890@c.us",
            "old",
            True,
            "You",
            123456,
            protocol="whatsapp",
            msg_id="WA-mid-9",
        )

        backend_mod._update_message_text(
            "391234567890@c.us", "new", "whatsapp", msg_id="WA-mid-9"
        )

        row = _db_rows(tmp_db)[0]
        assert row[2:6] == ("whatsapp", "391234567890@c.us", "WA-mid-9", 123456)


# ─── Migrazione schema v2 -> v3 (colonna ``edited``) ─────────────────────────


class TestEditedColumnMigration:
    """🛠️ Migrazione v2→v3: colonna ``edited`` aggiunta in modo idempotente."""

    def test_v2_to_v3_adds_edited_column(self, tmp_db):
        """DB legacy (v2) senza colonna → ``edited`` aggiunta, user_version=4."""
        _make_v2_db(tmp_db)
        assert _user_version(tmp_db) == 2
        assert "edited" not in _table_columns(tmp_db)

        backend_mod._init_db()

        assert _user_version(tmp_db) == 4
        assert "edited" in _table_columns(tmp_db)

    def test_v2_existing_rows_get_edited_zero(self, tmp_db):
        """Le righe esistenti ricevono ``edited=0`` (default)."""
        _make_v2_db(tmp_db)
        backend_mod._init_db()

        assert _db_rows(tmp_db) == [("ciao", 0, "signal", "+391234567890", None, 1000)]

    def test_migration_idempotent_double_init(self, tmp_db):
        """Doppia ``_init_db()`` non duplica la colonna né cambia user_version."""
        _make_v2_db(tmp_db)
        backend_mod._init_db()
        backend_mod._init_db()

        cols = _table_columns(tmp_db)
        assert cols.count("edited") == 1
        assert _user_version(tmp_db) == 4
        assert _db_rows(tmp_db) == [("ciao", 0, "signal", "+391234567890", None, 1000)]

    def test_v3_db_missing_edited_column_still_gets_column(self, tmp_db):
        """DB già ``user_version=3`` ma privo di ``edited`` → colonna comunque aggiunta."""
        _make_v2_db(tmp_db)
        conn = sqlite3.connect(tmp_db)
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
        conn.close()
        assert "edited" not in _table_columns(tmp_db)

        backend_mod._init_db()

        assert "edited" in _table_columns(tmp_db)
        assert _db_rows(tmp_db) == [("ciao", 0, "signal", "+391234567890", None, 1000)]

    def test_load_cache_exposes_edited_as_bool(self, tmp_db):
        """``_load_cache`` espone ``"edited"`` come ``bool``."""
        _make_v2_db(tmp_db)
        backend_mod._init_db()

        msg = backend_mod._load_cache(protocol="signal")["+391234567890"][0]
        assert msg["edited"] is False
        assert isinstance(msg["edited"], bool)

        backend_mod._update_message_text(
            "+391234567890", "nuovo", "signal", timestamp=1000, old_text="ciao"
        )
        msg = backend_mod._load_cache(protocol="signal")["+391234567890"][0]
        assert msg["edited"] is True
        assert isinstance(msg["edited"], bool)

    def test_fresh_rows_have_edited_false(self, tmp_db):
        """Un DB fresco (v3) crea righe con ``edited=0`` / ``edited=False``."""
        backend_mod._add_message_to_cache(
            "+39", "ciao", True, "You", 1000, protocol="signal"
        )

        assert _db_rows(tmp_db)[0][1] == 0
        assert backend_mod._load_cache()["+39"][0]["edited"] is False
