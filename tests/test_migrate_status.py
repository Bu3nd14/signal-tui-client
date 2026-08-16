"""
Regression tests for migrate_cache_status.py.

Verifies that the offline migration adds ``"status": "read"`` only to sent
messages (``is_mine=True``) that lack the field, leaving received messages
and already-migrated messages untouched.
"""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import migrate_cache_status as mig


@pytest.fixture
def tmp_cache_file(tmp_path: Path):
    """Point the migration script at a temporary cache file."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache_file = cache_dir / "messages.json"
    with (
        patch.object(mig, "CACHE_DIR", cache_dir),
        patch.object(mig, "CACHE_FILE", cache_file),
    ):
        yield cache_dir, cache_file


class TestMigrateStatus:
    """➕ Migrazione status: aggiunge "read" ai messaggi inviati senza status."""

    def test_no_cache_file_noop(self, tmp_path: Path):
        """A missing cache file makes main() return without raising."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        cache_file = cache_dir / "missing.json"
        with (
            patch.object(mig, "CACHE_DIR", cache_dir),
            patch.object(mig, "CACHE_FILE", cache_file),
        ):
            mig.main()  # must not raise

    def test_adds_status_to_sent_messages(self, tmp_cache_file):
        """A sent message without status gets ``"status": "read"``."""
        _, cache_file = tmp_cache_file
        cache_file.write_text(
            json.dumps(
                {
                    "+391234567890": [
                        {
                            "text": "ciao",
                            "is_mine": True,
                            "sender": "You",
                            "timestamp": 1000,
                        },
                    ],
                }
            )
        )

        mig.main()

        data = json.loads(cache_file.read_text())
        assert data["+391234567890"][0]["status"] == "read"

    def test_received_messages_not_migrated(self, tmp_cache_file):
        """Received messages without status are left untouched."""
        _, cache_file = tmp_cache_file
        cache_file.write_text(
            json.dumps(
                {
                    "+391234567890": [
                        {
                            "text": "sent",
                            "is_mine": True,
                            "sender": "You",
                            "timestamp": 1000,
                        },
                        {
                            "text": "received",
                            "is_mine": False,
                            "sender": "Mario",
                            "timestamp": 1001,
                        },
                    ],
                }
            )
        )

        mig.main()

        data = json.loads(cache_file.read_text())
        msgs = data["+391234567890"]
        assert msgs[0]["status"] == "read"
        assert "status" not in msgs[1]

    def test_message_with_status_untouched(self, tmp_cache_file):
        """A message that already has a status is left as-is (idempotent)."""
        _, cache_file = tmp_cache_file
        cache_file.write_text(
            json.dumps(
                {
                    "+391234567890": [
                        {
                            "text": "sent",
                            "is_mine": True,
                            "sender": "You",
                            "timestamp": 1000,
                            "status": "sent",
                        },
                    ],
                }
            )
        )

        mig.main()

        data = json.loads(cache_file.read_text())
        assert data["+391234567890"][0]["status"] == "sent"

    def test_all_already_migrated_noop(self, tmp_cache_file):
        """When nothing needs migration the file is not rewritten."""
        _, cache_file = tmp_cache_file
        original = {
            "+391234567890": [
                {
                    "text": "a",
                    "is_mine": True,
                    "sender": "You",
                    "timestamp": 1,
                    "status": "read",
                },
            ],
            "+391111111111": [
                {
                    "text": "b",
                    "is_mine": False,
                    "sender": "Mario",
                    "timestamp": 2,
                    "status": "read",
                },
            ],
        }
        cache_file.write_text(json.dumps(original))

        mig.main()

        assert json.loads(cache_file.read_text()) == original


def test_main_guard(tmp_path: Path, monkeypatch):
    """Running the script directly (__main__) is a no-op without a cache file."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    runpy.run_path(PROJECT_ROOT / "migrate_cache_status.py", run_name="__main__")
