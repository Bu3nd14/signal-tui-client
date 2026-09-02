from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

import purge_ghost_outgoing as cli
from models import PROTOCOL_WHATSAPP
from protocols import db

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


def _add_pair(
    text: str,
    first_id: str,
    first_status: str,
    second_id: str,
    second_status: str,
    timestamp: int = TS,
) -> None:
    for msg_id, status, offset in (
        (first_id, first_status, 0),
        (second_id, second_status, 1000),
    ):
        db._add_message_to_cache(
            CID,
            text,
            True,
            "You",
            timestamp + offset,
            protocol=PROTOCOL_WHATSAPP,
            msg_id=msg_id,
            status=status,
        )


def _rows(db_file: Path) -> list[tuple]:
    with sqlite3.connect(db_file) as connection:
        return connection.execute(
            "SELECT id, msg_id, text, status FROM messages ORDER BY id"
        ).fetchall()


def test_dry_run_reports_candidate_without_deleting(tmp_db, capsys):
    _add_pair("ghost", "54798", "read", "54841", "sent")

    assert cli.purge(tmp_db) == 0

    output = capsys.readouterr().out
    assert "CANDIDATE" in output
    assert "loser_msg_id='54841'" in output
    assert "DRY-RUN: 1 candidate(s), 0 row(s) deleted" in output
    assert [row[1] for row in _rows(tmp_db)] == ["54798", "54841"]


def test_apply_rank_rules_and_optional_equal_rank(tmp_db, capsys):
    _add_pair("ghost", "54798", "read", "54841", "sent")
    _add_pair("OK", "equal-a", "sent", "equal-b", "sent", TS + 10_000)

    assert cli.purge(tmp_db, apply=True) == 1
    assert [row[1] for row in _rows(tmp_db)] == ["54798", "equal-a", "equal-b"]
    assert "SKIP" in capsys.readouterr().out

    assert cli.purge(tmp_db, apply=True, include_equal_rank=True) == 1
    assert [row[1] for row in _rows(tmp_db)] == ["54798", "equal-a"]


def test_apply_creates_backup_before_mutation(tmp_db):
    _add_pair("ghost", "read-id", "read", "sent-id", "sent")

    assert cli.purge(tmp_db, apply=True) == 1

    backups = list(tmp_db.parent.glob(f"{tmp_db.name}.bak-*"))
    assert len(backups) == 1
    assert [row[1] for row in _rows(backups[0])] == ["read-id", "sent-id"]
    assert [row[1] for row in _rows(tmp_db)] == ["read-id"]


def test_msg_id_applies_only_to_targeted_pair(tmp_db):
    _add_pair("first", "first-read", "read", "first-sent", "sent")
    _add_pair(
        "second",
        "second-read",
        "read",
        "second-sent",
        "sent",
        TS + 10_000,
    )

    assert cli.purge(tmp_db, apply=True, msg_id="second-sent") == 1

    assert [row[1] for row in _rows(tmp_db)] == [
        "first-read",
        "first-sent",
        "second-read",
    ]
