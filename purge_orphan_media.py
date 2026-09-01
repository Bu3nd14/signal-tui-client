#!/usr/bin/env python3
"""One-shot scan and purge of orphan media files.

Usage:
    python3 purge_orphan_media.py [--protocol PROTOCOL] [--execute | --dry-run]
                                  [--older-than-days N] [--grace-seconds S] [-v]

Exit codes: 0 success with orphans found, 1 no orphans or skipped directory,
2 unreadable database, 3 deletion errors in execute mode.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from protocols.db import DB_FILE
from protocols.media_prune import default_scopes, prune_media


def _nonnegative(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("deve essere >= 0")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--protocol",
        choices=("signal", "whatsapp", "telegram", "quote-thumbs"),
        action="append",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument("--older-than-days", type=_nonnegative, default=1.0)
    parser.add_argument("--grace-seconds", type=_nonnegative)
    parser.add_argument("-v", action="store_true", dest="verbose")
    return parser


def _db_readable(db_file: Path) -> bool:
    try:
        conn = sqlite3.connect(f"file:{db_file.resolve()}?mode=ro", uri=True)
        try:
            conn.execute(
                "SELECT protocol, attachment_id, quote_attachment_id, "
                "quote_attachment_path FROM messages LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return False
    return True


def _display_path(path: Path) -> str:
    try:
        return f"~/{path.expanduser().relative_to(Path.home())}"
    except ValueError:
        return str(path)


def _mb(size: int) -> float:
    return size / (1024 * 1024)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    db_file = Path(DB_FILE)
    if not db_file.exists():
        print(f"ABORT: DB non trovato: {db_file}")
        return 2
    if not _db_readable(db_file):
        print(f"ABORT: DB illeggibile: {db_file}")
        return 2

    scopes = default_scopes()
    if args.protocol:
        selected = set(args.protocol)
        scopes = [scope for scope in scopes if scope.label in selected]
    else:
        scopes = [scope for scope in scopes if scope.dir.exists()]

    dry_run = not args.execute
    grace_s = (
        args.grace_seconds
        if args.grace_seconds is not None
        else args.older_than_days * 86400
    )
    report = prune_media(
        db_file=db_file,
        scopes=scopes,
        grace_s=grace_s,
        dry_run=dry_run,
    )

    print(f"=== Orphan media scan ({'dry-run' if dry_run else 'execute'}) ===")
    for scope in report.scopes:
        print(f"{scope.scope:<12} dir={_display_path(scope.dir)}")
        if scope.skipped_dir_missing:
            print("  directory mancante (saltata)")
            continue
        candidate_bytes = scope.bytes_freed + sum(
            orphan.size for orphan in scope.orphans_dryrun
        )
        print(
            f"  files: {scope.files_scanned}  refs protetti: {scope.refs_collected}  "
            f"orfani: {scope.orphans_found}  (~{_mb(candidate_bytes):.1f} MB)"
        )
        if args.verbose:
            for orphan in scope.orphans_dryrun:
                print(f"    {orphan.path}  ({_mb(orphan.size):.2f} MB)")

    print(
        f"TOTALE ORFANI: {report.total_orphans()} (~{_mb(report.total_bytes()):.1f} MB)"
    )
    if args.execute:
        removed = sum(scope.orphans_deleted for scope in report.scopes)
        errors = sum(scope.errors for scope in report.scopes)
        print(
            f"Rimossi {removed} file, liberati {_mb(report.total_bytes()):.1f} MB "
            f"(errori: {errors})"
        )
        if report.total_orphans() and errors:
            return 3

    if any(scope.skipped_dir_missing for scope in report.scopes):
        return 1
    return 0 if report.total_orphans() else 1


if __name__ == "__main__":
    raise SystemExit(main())
