#!/usr/bin/env python3
"""Remove legacy empty text-message rows created by reaction events.

The default mode is a dry run. Pass ``--apply`` to delete the reported rows.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence

import protocols.db as backend
from protocols.db import _DB_LOCK

PROTOCOLS = ("signal", "whatsapp", "telegram")
_CANDIDATE_WHERE = """
msg_type = 'text' AND (text = '' OR text IS NULL)
AND attachment_id IS NULL AND attachment_info IS NULL
AND quote_text IS NULL AND edited = 0
"""


def _candidate_counts(connection: sqlite3.Connection) -> dict[str, int]:
    rows = connection.execute(
        f"""
        SELECT protocol, COUNT(*)
        FROM messages
        WHERE {_CANDIDATE_WHERE}
        GROUP BY protocol
        """
    ).fetchall()
    return {str(protocol): int(count) for protocol, count in rows}


def cleanup(*, apply: bool = False) -> tuple[dict[str, int], int]:
    """Report cleanup candidates and optionally remove them."""
    db_file = backend.DB_FILE
    if not db_file.exists():
        raise FileNotFoundError(db_file)

    with _DB_LOCK, sqlite3.connect(db_file) as connection:
        if apply:
            connection.execute("BEGIN IMMEDIATE")
        counts = _candidate_counts(connection)
        removed = 0
        if apply:
            cursor = connection.execute(
                f"""
                DELETE FROM messages
                WHERE {_CANDIDATE_WHERE}
                """
            )
            removed = cursor.rowcount
    return counts, removed


def _print_report(counts: dict[str, int], *, apply: bool, removed: int) -> None:
    total = sum(counts.values())
    label = "Righe cancellate" if apply else "Righe candidate"
    print(f"{label}: {removed if apply else total}")
    for protocol in PROTOCOLS:
        print(f"  {protocol}: {counts.get(protocol, 0)}")
    for protocol in sorted(set(counts) - set(PROTOCOLS)):
        print(f"  {protocol}: {counts[protocol]}")
    if not apply:
        print("(dry-run: nessuna modifica scritta; usare --apply per cancellare)")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="report candidates without deleting them (default)",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="delete all reported legacy rows",
    )
    args = parser.parse_args(argv)

    try:
        counts, removed = cleanup(apply=args.apply)
    except FileNotFoundError as exc:
        print(f"DB non trovato: {exc.args[0]}")
        return 1
    except sqlite3.OperationalError as exc:
        print(f"Impossibile bonificare il DB: {exc}")
        return 2

    _print_report(counts, apply=args.apply, removed=removed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
