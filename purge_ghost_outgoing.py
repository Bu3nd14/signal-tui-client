#!/usr/bin/env python3
"""Review and explicitly remove suspicious WhatsApp outgoing text ghosts."""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

from protocols import db

STATUS_RANK = {
    "pending": 0,
    "failed": 0,
    "sent": 1,
    "delivered": 2,
    "read": 3,
}


def _backup_database(db_file: Path) -> Path:
    epoch = int(time.time())
    backup_file = db_file.with_name(f"{db_file.name}.bak-{epoch}")
    while backup_file.exists():
        epoch += 1
        backup_file = db_file.with_name(f"{db_file.name}.bak-{epoch}")
    with sqlite3.connect(db_file) as source, sqlite3.connect(backup_file) as backup:
        source.backup(backup)
    return backup_file


def _loser(group: dict, include_equal_rank: bool) -> tuple[dict | None, str]:
    first, second = group["rows"]
    first_rank = STATUS_RANK.get(first["status"], 0)
    second_rank = STATUS_RANK.get(second["status"], 0)
    if first_rank != second_rank:
        loser = first if first_rank < second_rank else second
        return loser, f"lower status rank ({min(first_rank, second_rank)})"
    if include_equal_rank:
        return max((first, second), key=lambda row: row["id"]), (
            f"equal status rank ({first_rank}); larger rowid"
        )
    return None, f"equal status rank ({first_rank})"


def purge(
    db_file: Path | str | None = None,
    *,
    apply: bool = False,
    include_equal_rank: bool = False,
    msg_id: str | None = None,
) -> int:
    target = Path(db_file or db.DB_FILE)
    if not target.exists():
        print(f"DB not found: {target}")
        return 0

    db.DB_FILE = target
    with db._DB_LOCK:
        groups = db._detect_ghost_outgoing_text()
        if msg_id is not None:
            groups = [
                group
                for group in groups
                if any(row["msg_id"] == msg_id for row in group["rows"])
            ]

        losers = []
        for group in groups:
            loser, reason = _loser(group, include_equal_rank)
            row_summary = ", ".join(
                f"id={row['id']} msg_id={row['msg_id']!r} "
                f"timestamp={row['timestamp']} status={row['status']!r}"
                for row in group["rows"]
            )
            if loser is None:
                print(
                    f"SKIP contact={group['contact']!r} text={group['text']!r} "
                    f"rows=[{row_summary}] reason={reason}"
                )
                continue
            losers.append(loser)
            print(
                f"CANDIDATE contact={group['contact']!r} text={group['text']!r} "
                f"rows=[{row_summary}] loser_id={loser['id']} "
                f"loser_msg_id={loser['msg_id']!r} reason={reason}"
            )

        if not apply:
            print(f"DRY-RUN: {len(losers)} candidate(s), 0 row(s) deleted.")
            return 0

        backup_file = _backup_database(target)
        print(f"Backup created: {backup_file}")
        removed = 0
        with sqlite3.connect(target) as connection:
            for loser in losers:
                removed += connection.execute(
                    "DELETE FROM messages WHERE id = ?", (loser["id"],)
                ).rowcount
        print(f"Removed {removed} row(s).")
        return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=db.DB_FILE)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--include-equal-rank", action="store_true")
    parser.add_argument("--msg-id")
    args = parser.parse_args(argv)
    purge(
        args.db,
        apply=args.apply,
        include_equal_rank=args.include_equal_rank,
        msg_id=args.msg_id,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
