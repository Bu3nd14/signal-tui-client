#!/usr/bin/env python3
"""Migrate messages.json cache to SQLite database.

This script is INDEPENDENT from ``migrate_cache_status.py`` (which deals with
cache status). It converts the legacy JSON cache (``messages.json``) into the
new SQLite database (``messages.db``) used by the backend.

Usage:
    python3 migrate_cache_sqlite.py
"""

import json
import sqlite3
from pathlib import Path

CACHE_DIR = Path.home() / ".local" / "share" / "signal-tui-client"
CACHE_FILE = CACHE_DIR / "messages.json"
DB_FILE = CACHE_DIR / "messages.db"


def _init_db(conn: sqlite3.Connection) -> None:
    """Create the messages table and index if they don't exist."""
    conn.execute("PRAGMA journal_mode=WAL")
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
        "CREATE INDEX IF NOT EXISTS idx_messages_contact "
        "ON messages(contact_number, timestamp)"
    )


def migrate() -> None:
    """Migrate messages.json to messages.db, then back up the JSON file."""
    if not CACHE_FILE.exists():
        print("No messages.json found, nothing to migrate.")
        return

    with open(CACHE_FILE) as f:
        cache = json.load(f)

    conn = sqlite3.connect(DB_FILE)
    try:
        _init_db(conn)

        count = 0
        for contact, messages in cache.items():
            for msg in messages:
                conn.execute(
                    """INSERT INTO messages
                       (contact_number, text, is_mine, sender, timestamp, quote_text,
                        msg_type, attachment_info, attachment_id, read, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        contact,
                        msg.get("text"),
                        int(msg.get("is_mine", False)),
                        msg.get("sender"),
                        msg.get("timestamp", 0),
                        msg.get("quote_text"),
                        msg.get("msg_type", "text"),
                        msg.get("attachment_info"),
                        msg.get("attachment_id"),
                        int(msg.get("read", True)),
                        msg.get("status", "read"),
                    ),
                )
                count += 1

        conn.commit()
    finally:
        conn.close()

    # Backup old file
    backup = CACHE_FILE.with_suffix(".json.bak")
    CACHE_FILE.rename(backup)
    print(f"Migrated {count} messages to {DB_FILE}")
    print(f"Old file backed up to {backup}")


if __name__ == "__main__":
    migrate()
