"""One-shot maintenance scripts must close every SQLite connection they open.

``with closing(sqlite3.connect(...)) as conn, conn`` only commits/rolls back; it does not
close. These tests record each connection opened by a script and assert it is
closed once the script's entry point returns.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import migrate_reactions_cleanup
import migrate_wa_e2e_notice
import protocols.db as backend
import purge_ghost_outgoing
import purge_test_rows


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[sqlite3.Connection]:
    real_connect = sqlite3.connect
    conns: list[sqlite3.Connection] = []

    def recording_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conns.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", recording_connect)
    return conns


def _is_closed(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


def _assert_all_closed(conns: list[sqlite3.Connection]) -> None:
    assert conns, "the script opened no connection: the test proves nothing"
    assert all(_is_closed(c) for c in conns)


def test_migrate_reactions_cleanup_closes_connection(opened):
    backend._init_db()
    opened.clear()
    migrate_reactions_cleanup.cleanup(apply=True)
    _assert_all_closed(opened)


def test_migrate_wa_e2e_notice_closes_connection(opened):
    backend._init_db()
    opened.clear()
    migrate_wa_e2e_notice.cleanup(apply=True)
    _assert_all_closed(opened)


def test_purge_test_rows_dry_run_closes_connection(opened):
    backend._init_db()
    opened.clear()
    assert purge_test_rows.purge(backend.DB_FILE) == 0
    _assert_all_closed(opened)


def test_purge_test_rows_backup_closes_connections(opened, tmp_path: Path):
    backend._init_db()
    opened.clear()
    backup = purge_test_rows._backup_database(backend.DB_FILE)
    assert backup.exists()
    assert len(opened) == 2
    _assert_all_closed(opened)


def test_purge_ghost_outgoing_backup_closes_connections(opened):
    backend._init_db()
    opened.clear()
    backup = purge_ghost_outgoing._backup_database(backend.DB_FILE)
    assert backup.exists()
    assert len(opened) == 2
    _assert_all_closed(opened)
