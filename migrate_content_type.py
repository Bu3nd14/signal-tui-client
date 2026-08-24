#!/usr/bin/env python3
"""Backfill the ``content_type`` column for legacy media rows.

Bug #37 piano B (``docs/DESIGN_QUOTE_MEDIA_37_PLANB.md``) persists the quoted
attachment ``content_type`` (mime) so a Signal media reply can send
``quoteAttachments`` with a valid descriptor.  Rows persisted BEFORE the
``content_type`` column existed have ``content_type IS NULL`` and would degrade
to the V2 behaviour (no thumbnail).  This script fills the gap for existing
media: the mime is read from the actual attachment file on disk (signal-cli
downloads incoming media to ``SIGNAL_CLI_ATTACHMENTS_DIR``, filenamed by
attachment id), so the value is the *real* type of the file.

Usage:
    python3 migrate_content_type.py
    # optional overrides:
    #   python3 migrate_content_type.py --db /path/to/messages.db
    #   python3 migrate_content_type.py --attachments /path/to/attachments
"""

from __future__ import annotations

import argparse
import mimetypes
import sqlite3
import subprocess
import sys
from pathlib import Path

DEFAULT_DB = Path.home() / ".local" / "share" / "signal-tui-client" / "messages.db"
DEFAULT_ATTACHMENTS = Path.home() / ".local" / "share" / "signal-cli" / "attachments"


def _mime_of(path: Path) -> str | None:
    """Return the mime type of *path* (file magic first, then mimetypes)."""
    try:
        out = subprocess.run(
            ["file", "--mime-type", "-b", str(path)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        value = (out.stdout or "").strip()
        if value and "/" in value and value != "application/octet-stream":
            return value
    except (OSError, subprocess.SubprocessError):
        pass
    guess, _ = mimetypes.guess_type(path.name)
    return guess


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--attachments", default=str(DEFAULT_ATTACHMENTS))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db_file = Path(args.db)
    att_dir = Path(args.attachments)
    if not db_file.exists():
        print(f"DB non trovato: {db_file}")
        return 1
    if not att_dir.is_dir():
        print(f"Directory allegati non trovata: {att_dir}")
        return 2

    conn = sqlite3.connect(db_file)
    try:
        rows = conn.execute(
            """
            SELECT id, protocol, msg_type, attachment_id, timestamp
            FROM messages
            WHERE msg_type != 'text'
              AND content_type IS NULL
              AND attachment_id IS NOT NULL
              AND attachment_id != ''
            """
        ).fetchall()
    except sqlite3.OperationalError as exc:
        print(f"Colonna content_type non presente nel DB: {exc}")
        conn.close()
        return 3

    if not rows:
        print("Nessuna riga media senza content_type da backfillare.")
        conn.close()
        return 0

    updated = skipped_missing_file = skipped_no_mime = 0
    for row_id, protocol, msg_type, attachment_id, ts in rows:
        att_path = att_dir / attachment_id
        if not att_path.is_file():
            skipped_missing_file += 1
            continue
        mime = _mime_of(att_path)
        if not mime:
            skipped_no_mime += 1
            continue
        if not args.dry_run:
            conn.execute(
                "UPDATE messages SET content_type = ? WHERE id = ?",
                (mime, row_id),
            )
        updated += 1

    if not args.dry_run:
        conn.commit()

    print(f"Totale righe media legacy: {len(rows)}")
    print(f"  aggiornate con content_type: {updated}")
    print(f"  saltate (file allegato mancante): {skipped_missing_file}")
    print(f"  saltate (mime non rilevato): {skipped_no_mime}")
    if args.dry_run:
        print("(dry-run: nessuna modifica scritta)")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
