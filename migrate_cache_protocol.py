#!/usr/bin/env python3
"""Migrate an existing SQLite cache to the multi-protocol schema.

Adds a ``protocol`` column to the ``messages`` table (default ``'signal'``)
and updates the contact index so messages are namespaced by protocol.  The
migration is idempotent: running it on an already-migrated database is a no-op.

The application also runs this automatically at startup (see
``protocols.db._migrate_protocol_schema``), so this script is only needed for an
explicit/manual migration.

Usage:
    python3 migrate_cache_protocol.py
"""

import sqlite3
from pathlib import Path

from protocols.db import _migrate_protocol_schema

CACHE_DIR = Path.home() / ".local" / "share" / "signal-tui-client"
DB_FILE = CACHE_DIR / "messages.db"


def migrate() -> None:
    """Add the ``protocol`` column and rebuild the index, if needed."""
    if not DB_FILE.exists():
        print("No cache DB found, nothing to migrate.")
        return

    conn = sqlite3.connect(DB_FILE)
    try:
        _migrate_protocol_schema(conn)
        conn.commit()
        print("Migration complete: protocol column ensured + index rebuilt.")
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
