#!/usr/bin/env python3
"""Remove known regression-test rows from the local message database."""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from protocols.db import _DB_LOCK, DB_FILE

# SQL fragments pinning the known test rows. Constants only: predicate_extra
# must never embed external/user input.
_SIGNAL_BATCH = "batch_id = 'batch-1'"
# Timestamp and msg_id verified against the real row in the production DB.
_TELEGRAM_REPLY = "text = 'reply' AND timestamp = 1786953045082 AND msg_id = '77'"

# Tuple (protocol, contact_number, predicate_extra) — extend as needed.
TEST_ROWS = (
    # WhatsApp: test contacts (any row for these numbers is a test row).
    ("whatsapp", "db@lid", None),
    ("whatsapp", "unread@lid", None),
    ("whatsapp", "3912345678@c.us", None),
    # Signal: only the mirrored batch, not the whole '42' contact.
    ("signal", "42", _SIGNAL_BATCH),
    # Telegram: the single regression row.
    ("telegram", "42", _TELEGRAM_REPLY),
)

# Abort if the found count exceeds the expected maximum (None = never abort).
EXPECTED_MAX_COUNTS = {
    ("whatsapp", "db@lid", None): None,
    ("whatsapp", "unread@lid", None): None,
    ("whatsapp", "3912345678@c.us", None): None,
    ("signal", "42", _SIGNAL_BATCH): 3,
    ("telegram", "42", _TELEGRAM_REPLY): 1,
}


def _build_where(protocol: str, contact: str, extra: str | None) -> tuple[str, list]:
    """Build a parameterized WHERE clause for one test-row predicate."""
    where = "protocol = ? AND contact_number = ?"
    params: list = [protocol, contact]
    if extra:
        where += f" AND ({extra})"
    return where, params


def _backup_database(db_file: Path) -> Path:
    """WAL-safe backup via the online backup API, timestamped and collision-free."""
    timestamp = int(time.time())
    backup_file = db_file.with_name(f"{db_file.name}.bak-{timestamp}")
    while backup_file.exists():
        timestamp += 1
        backup_file = db_file.with_name(f"{db_file.name}.bak-{timestamp}")

    with (
        _DB_LOCK,
        sqlite3.connect(db_file) as source,
        sqlite3.connect(backup_file) as backup,
    ):
        source.execute("PRAGMA busy_timeout = 5000")
        source.backup(backup)
    return backup_file


def purge(db_file: Path | None = None, *, apply: bool = False) -> int:
    """Count (dry-run) or remove test rows, returning the affected count.

    Dry-run is the default: pass ``apply=True`` to back up and delete.
    ``-1`` signals an abort (count above expected, backup failure or mismatch).
    """
    target = Path(db_file or DB_FILE)
    if not target.exists():
        print(f"DB not found: {target}")
        return 0

    with sqlite3.connect(target) as connection:
        connection.execute("PRAGMA busy_timeout = 5000")
        counts: dict[tuple, int] = {}
        for protocol, contact, extra in TEST_ROWS:
            where, params = _build_where(protocol, contact, extra)
            count = connection.execute(
                f"SELECT COUNT(*) FROM messages WHERE {where}", params
            ).fetchone()[0]
            counts[(protocol, contact, extra)] = count
            if count:
                print(f"FOUND protocol={protocol} contact={contact} count={count}")

    for key, max_expected in EXPECTED_MAX_COUNTS.items():
        if max_expected is not None and counts.get(key, 0) > max_expected:
            print(
                f"ABORT: {key} count={counts.get(key)} > max_expected={max_expected}. "
                "Manual review required."
            )
            return -1

    total = sum(counts.values())
    if total == 0:
        print("No test rows found.")
        return 0

    if not apply:
        print(f"DRY-RUN: {total} test row(s) found, 0 deleted. Use --apply to remove.")
        return total

    backup_file = _backup_database(target)
    print(f"Backup created: {backup_file}")

    try:
        with sqlite3.connect(backup_file) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.Error as exc:
        print(f"ABORT: backup integrity check failed: {exc}")
        return -1
    if result != "ok":
        print(f"ABORT: backup integrity check failed: {result}")
        return -1

    removed = 0
    with _DB_LOCK, sqlite3.connect(target) as connection:
        connection.execute("PRAGMA busy_timeout = 5000")
        for protocol, contact, extra in TEST_ROWS:
            key = (protocol, contact, extra)
            where, params = _build_where(protocol, contact, extra)
            cursor = connection.execute(f"DELETE FROM messages WHERE {where}", params)
            if cursor.rowcount != counts[key]:
                print(
                    f"ERROR: {key} deleted {cursor.rowcount} row(s), "
                    f"expected {counts[key]}."
                )
                connection.rollback()
                return -1
            removed += cursor.rowcount

        remaining = 0
        for protocol, contact, extra in TEST_ROWS:
            where, params = _build_where(protocol, contact, extra)
            remaining += connection.execute(
                f"SELECT COUNT(*) FROM messages WHERE {where}", params
            ).fetchone()[0]
        if remaining:
            print(f"ERROR: {remaining} test row(s) still present after DELETE.")
            connection.rollback()
            return -1

    print(f"Removed {removed} row(s).")
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_FILE)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    rc = purge(args.db, apply=args.apply)
    return 0 if rc >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
