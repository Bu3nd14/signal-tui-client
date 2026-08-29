"""Safety and shutdown regressions for the per-contact cache prune (bug #51)."""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

import backend
from backend import db
from backends import config
from tui.app import SignalTUI


@pytest.fixture
def prune_db(tmp_path, monkeypatch):
    """Create and select an isolated cache database."""
    db_file = tmp_path / "messages.db"
    monkeypatch.setattr(backend, "DB_FILE", db_file)
    monkeypatch.setattr(backend, "CACHE_DIR", tmp_path)
    db._init_db()
    return db_file


def _insert_rows(db_file, rows):
    """Insert compact message tuples directly, preserving deliberate duplicates."""
    with sqlite3.connect(db_file) as conn:
        conn.executemany(
            "INSERT INTO messages "
            "(protocol, contact_number, text, timestamp, status, msg_id, read) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def _rows(db_file, where="1=1", params=()):
    with sqlite3.connect(db_file) as conn:
        return conn.execute(
            f"SELECT rowid, protocol, contact_number, text, timestamp, status, "
            f"msg_id, read FROM messages WHERE {where} ORDER BY rowid",
            params,
        ).fetchall()


def _message(protocol, contact, index, *, timestamp=None, status="read", msg_id=None):
    return (
        protocol,
        contact,
        f"msg-{index}",
        index if timestamp is None else timestamp,
        status,
        f"id-{protocol}-{contact}-{index}" if msg_id is None else msg_id,
        1,
    )


class TestCachePruneSafety:
    def test_protected_rows_survive_outside_cap(self, prune_db):
        now = 2_000_000
        rows = [_message("signal", "A", i, timestamp=1_000_000 + i) for i in range(310)]
        rows += [
            ("signal", "A", "pending", now - 20, "pending", "pending-id", 0),
            ("signal", "A", "failed", now - 10, "failed", "failed-id", 0),
            ("signal", "A", "recent-idless", now - 1, "read", None, 0),
        ]
        _insert_rows(prune_db, rows)

        assert db._prune_cache(limit=300, now_ms=now) == 10

        remaining = _rows(prune_db)
        assert len(remaining) == 303
        assert {row[3] for row in remaining} >= {
            "pending",
            "failed",
            "recent-idless",
        }

    def test_old_idless_row_beyond_cap_is_pruned(self, prune_db):
        now = 2_000_000
        rows = [_message("signal", "A", i, timestamp=1_500_000 + i) for i in range(100)]
        rows.append(("signal", "A", "old-idless", now - 600_001, "read", None, 1))
        _insert_rows(prune_db, rows)

        assert db._prune_cache(limit=100, now_ms=now) == 1
        assert not _rows(prune_db, "text = ?", ("old-idless",))

    def test_cap_is_independent_per_protocol_and_contact(self, prune_db):
        rows = [_message("signal", "A", i) for i in range(350)]
        rows += [_message("telegram", "B", i) for i in range(120)]
        rows += [_message("whatsapp", "C", i) for i in range(350)]
        _insert_rows(prune_db, rows)

        assert db._prune_cache(limit=300, now_ms=1_000_000) == 100

        counts = {}
        for row in _rows(prune_db):
            counts[(row[1], row[2])] = counts.get((row[1], row[2]), 0) + 1
        assert counts == {
            ("signal", "A"): 300,
            ("telegram", "B"): 120,
            ("whatsapp", "C"): 300,
        }

    def test_equal_timestamps_keep_highest_rowids(self, prune_db):
        rows = [_message("signal", "A", i, timestamp=123_456) for i in range(105)]
        _insert_rows(prune_db, rows)
        original = _rows(prune_db)

        assert db._prune_cache(limit=100, now_ms=1_000_000) == 5

        kept_rowids = {row[0] for row in _rows(prune_db)}
        assert kept_rowids == {row[0] for row in original[-100:]}

    def test_default_limit_comes_from_environment(self, prune_db, monkeypatch):
        monkeypatch.setenv("MESSAGE_RETENTION_PER_CONTACT", "150")
        _insert_rows(prune_db, [_message("signal", "A", i) for i in range(160)])

        assert db._prune_cache(now_ms=1_000_000) == 10
        assert len(_rows(prune_db)) == 150

    def test_zero_disables_prune(self, prune_db, monkeypatch):
        monkeypatch.setenv("MESSAGE_RETENTION_PER_CONTACT", "0")
        _insert_rows(prune_db, [_message("signal", "A", i) for i in range(110)])

        assert db._prune_cache(now_ms=1_000_000) == 0
        assert len(_rows(prune_db)) == 110

    def test_limit_below_floor_is_clamped_with_warning(self, prune_db, caplog):
        _insert_rows(prune_db, [_message("signal", "A", i) for i in range(130)])

        with caplog.at_level("WARNING", logger="signal-tui"):
            assert db._prune_cache(limit=50, now_ms=1_000_000) == 30

        assert len(_rows(prune_db)) == 100
        assert "forzato a 100" in caplog.text

    def test_config_json_limit_is_used(self, prune_db, monkeypatch, tmp_path):
        monkeypatch.delenv("MESSAGE_RETENTION_PER_CONTACT", raising=False)
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        (tmp_path / "config.json").write_text(
            json.dumps({"message_retention_per_contact": 180})
        )
        _insert_rows(prune_db, [_message("signal", "A", i) for i in range(190)])

        assert db._prune_cache(now_ms=1_000_000) == 10
        assert len(_rows(prune_db)) == 180

    def test_invalid_environment_value_falls_back_to_300(
        self, prune_db, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.setenv("MESSAGE_RETENTION_PER_CONTACT", "not-a-number")
        (tmp_path / "config.json").write_text(
            json.dumps({"message_retention_per_contact": 180})
        )
        _insert_rows(prune_db, [_message("signal", "A", i) for i in range(310)])

        assert config.get_message_retention_per_contact() == 300
        assert db._prune_cache(now_ms=1_000_000) == 10
        assert len(_rows(prune_db)) == 300

    def test_config_precedence_env_then_json_then_dotenv(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.delenv("MESSAGE_RETENTION_PER_CONTACT", raising=False)
        (tmp_path / ".env").write_text("MESSAGE_RETENTION_PER_CONTACT=170\n")
        assert config.get_message_retention_per_contact() == 170

        (tmp_path / "config.json").write_text(
            json.dumps({"message_retention_per_contact": 180})
        )
        assert config.get_message_retention_per_contact() == 180

        monkeypatch.setenv("MESSAGE_RETENTION_PER_CONTACT", "150")
        assert config.get_message_retention_per_contact() == 150

    def test_dedup_refetch_preserves_count_and_read_state(self, prune_db):
        rows = [
            (
                "whatsapp",
                "C",
                f"msg-{i}",
                i,
                "read",
                f"wa-{i}",
                1,
            )
            for i in range(150)
        ]
        _insert_rows(prune_db, rows)
        assert db._prune_cache(limit=100, now_ms=1_000_000) == 50

        for i in range(100, 150):
            db._add_message_to_cache(
                "C",
                f"msg-{i}",
                False,
                "Alice",
                i,
                protocol="whatsapp",
                msg_id=f"wa-{i}",
            )

        remaining = _rows(prune_db)
        assert len(remaining) == 100
        assert all(row[7] == 1 for row in remaining)

    def test_vacuum_runs_only_if_rows_were_deleted(self, prune_db, monkeypatch):
        original_connect = db.sqlite3.connect
        statements = []

        def traced_connect(*args, **kwargs):
            conn = original_connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        monkeypatch.setattr(db.sqlite3, "connect", traced_connect)
        _insert_rows(prune_db, [_message("signal", "A", i) for i in range(100)])
        assert db._prune_cache(limit=100, now_ms=1_000_000) == 0
        assert not any(sql.strip().upper() == "VACUUM" for sql in statements)

        _insert_rows(prune_db, [_message("signal", "A", 100)])
        statements.clear()
        assert db._prune_cache(limit=100, now_ms=1_000_000) == 1
        assert any(sql.strip().upper() == "VACUUM" for sql in statements)

    def test_vacuum_failure_is_non_fatal(self, prune_db, monkeypatch):
        _insert_rows(prune_db, [_message("signal", "A", i) for i in range(101)])
        original_connect = db.sqlite3.connect

        class VacuumFailingConnection:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, *args):
                if sql.strip().upper() == "VACUUM":
                    raise sqlite3.OperationalError("simulated VACUUM failure")
                return self._conn.execute(sql, *args)

            def commit(self):
                return self._conn.commit()

            def close(self):
                return self._conn.close()

        monkeypatch.setattr(
            db.sqlite3,
            "connect",
            lambda *args, **kwargs: VacuumFailingConnection(
                original_connect(*args, **kwargs)
            ),
        )

        assert db._prune_cache(limit=100, now_ms=1_000_000) == 1
        with original_connect(prune_db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 100


class TestAppExitPrune:
    @pytest.mark.integration
    async def test_ctrl_q_dispatches_on_exit_app_and_prunes(self, app_for_test):
        with patch("backend._prune_cache") as prune:
            async with app_for_test.run_test() as pilot:
                await pilot.press("ctrl+q")
                await pilot.pause()

        prune.assert_called_once_with()

    def test_on_exit_app_invokes_prune_and_swallows_failure(self):
        app = object.__new__(SignalTUI)
        app._polling_active = True
        app._hires_executor = MagicMock()
        app._web_enabled = False
        app.telegram_backend = None

        with patch("backend._prune_cache") as prune:
            SignalTUI.on_exit_app(app)
        prune.assert_called_once_with()
        assert app._polling_active is False

        with patch("backend._prune_cache", side_effect=RuntimeError("boom")):
            SignalTUI.on_exit_app(app)  # best-effort: must not propagate
