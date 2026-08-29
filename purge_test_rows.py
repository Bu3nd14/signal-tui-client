#!/usr/bin/env python3
"""Remove known regression-test WhatsApp rows from the local message database."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from backend import _DB_LOCK, DB_FILE

TEST_CONTACTS = ("db@lid", "unread@lid", "3912345678@c.us")


def purge(db_file: Path | None = None) -> int:
    db_file = Path(db_file or DB_FILE)
    if not db_file.exists():
        print("No cache DB found, nothing to purge.")
        return 0

    backup_file = db_file.with_name(f"{db_file.name}.bak-{int(time.time())}")
    with _DB_LOCK:
        with sqlite3.connect(db_file) as source, sqlite3.connect(backup_file) as backup:
            source.backup(backup)
        print(f"Backup created: {backup_file}")

        with sqlite3.connect(db_file) as connection:
            cursor = connection.execute(
                "DELETE FROM messages "
                "WHERE protocol = 'whatsapp' "
                "AND contact_number IN (?, ?, ?)",
                TEST_CONTACTS,
            )
            removed = cursor.rowcount

    print(f"Removed {removed} test row(s).")
    return removed


if __name__ == "__main__":
    purge()
