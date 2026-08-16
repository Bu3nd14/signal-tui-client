"""
Regression tests for the JSON→SQLite migration script (migrate_cache_sqlite.py).

Verifies that:
1. messages.json is migrated to messages.db correctly.
2. The old JSON file is backed up to messages.json.bak.
3. The original messages.json is removed after migration.
4. If no messages.json exists, migration is a no-op.
"""

from __future__ import annotations

import json
import runpy
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import migrate_cache_sqlite as mig


@pytest.fixture
def tmp_cache(tmp_path: Path):
    """Point the migration script at a temporary cache directory."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache_file = cache_dir / "messages.json"
    db_file = cache_dir / "messages.db"
    with (
        patch.object(mig, "CACHE_DIR", cache_dir),
        patch.object(mig, "CACHE_FILE", cache_file),
        patch.object(mig, "DB_FILE", db_file),
    ):
        yield cache_dir, cache_file, db_file


def _write_sample_json(cache_file: Path):
    """Write a sample messages.json."""
    cache_file.write_text(
        json.dumps(
            {
                "+391234567890": [
                    {
                        "text": "Ciao!",
                        "is_mine": False,
                        "sender": "Mario",
                        "timestamp": 1000,
                        "quote_text": None,
                        "msg_type": "text",
                        "attachment_info": None,
                        "attachment_id": None,
                        "read": False,
                        "status": "read",
                    },
                    {
                        "text": "Come stai?",
                        "is_mine": True,
                        "sender": "You",
                        "timestamp": 1001,
                        "quote_text": None,
                        "msg_type": "text",
                        "attachment_info": None,
                        "attachment_id": None,
                        "read": True,
                        "status": "sent",
                    },
                ],
                "+391111111111": [
                    {
                        "text": "Messaggio",
                        "is_mine": False,
                        "sender": "Luigi",
                        "timestamp": 1002,
                        "quote_text": None,
                        "msg_type": "text",
                        "attachment_info": None,
                        "attachment_id": None,
                        "read": False,
                        "status": "read",
                    },
                ],
            }
        )
    )


class TestMigrate:
    """🔄 Migrazione da JSON a SQLite."""

    def test_migrate_creates_db(self, tmp_cache):
        """La migrazione crea messages.db con i messaggi."""
        _, cache_file, db_file = tmp_cache
        _write_sample_json(cache_file)

        mig.migrate()

        assert db_file.exists()
        conn = sqlite3.connect(db_file)
        rows = conn.execute(
            "SELECT contact_number, text, is_mine, status FROM messages ORDER BY timestamp"
        ).fetchall()
        conn.close()
        assert len(rows) == 3
        assert rows[0][1] == "Ciao!"
        assert rows[0][2] == 0
        assert rows[1][1] == "Come stai?"
        assert rows[1][2] == 1
        assert rows[2][0] == "+391111111111"

    def test_migrate_backs_up_json(self, tmp_cache):
        """Il file JSON originale viene rinominato in .bak."""
        cache_dir, cache_file, _ = tmp_cache
        _write_sample_json(cache_file)

        mig.migrate()

        assert (cache_dir / "messages.json.bak").exists()
        assert not cache_file.exists()

    def test_migrate_no_json_is_noop(self, tmp_cache):
        """Se messages.json non esiste, la migrazione non fa nulla."""
        cache_dir, _, db_file = tmp_cache

        mig.migrate()

        assert not db_file.exists()
        assert not (cache_dir / "messages.json.bak").exists()

    def test_migrate_preserves_optional_fields(self, tmp_cache):
        """I campi opzionali (quote, attachment, msg_type) vengono salvati."""
        _, cache_file, db_file = tmp_cache
        cache_file.write_text(
            json.dumps(
                {
                    "+391234567890": [
                        {
                            "text": "img",
                            "is_mine": False,
                            "sender": "Mario",
                            "timestamp": 2000,
                            "quote_text": "citazione",
                            "msg_type": "image",
                            "attachment_info": "photo.jpg",
                            "attachment_id": "att-123",
                            "read": False,
                            "status": "read",
                        },
                    ]
                }
            )
        )

        mig.migrate()

        conn = sqlite3.connect(db_file)
        row = conn.execute(
            "SELECT quote_text, msg_type, attachment_info, attachment_id FROM messages"
        ).fetchone()
        conn.close()
        assert row[0] == "citazione"
        assert row[1] == "image"
        assert row[2] == "photo.jpg"
        assert row[3] == "att-123"


def test_main_guard(tmp_path: Path, monkeypatch):
    """Running the script directly (__main__) is a no-op without messages.json."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    runpy.run_path(PROJECT_ROOT / "migrate_cache_sqlite.py", run_name="__main__")
