#!/usr/bin/env python3
"""Turn legacy WhatsApp "security code changed" rows into system notices.

Before the fix, a WEBJS ``e2e_notification`` (subtype ``identity``) was stored
as a plain text message whose text is the affected contact's bare JID
(e.g. ``20508603076702@lid``).  This rewrites those rows as
``msg_type = 'system'`` with a readable notice.

The default mode is a dry run. Pass ``--apply`` to update the reported rows.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence

import protocols.db as backend
from models import MSG_TYPE_SYSTEM
from protocols.db import _DB_LOCK

NOTICE_TEXT = "Il codice di sicurezza di un contatto è cambiato."

# Text is exactly "<digits>@lid" / "<digits>@c.us": nobody types that alone.
_CANDIDATE_WHERE = """
protocol = 'whatsapp' AND msg_type = 'text' AND is_mine = 0
AND attachment_id IS NULL AND quote_text IS NULL
AND (text GLOB '[0-9][0-9][0-9][0-9][0-9]*@lid'
     OR text GLOB '[0-9][0-9][0-9][0-9][0-9]*@c.us')
AND ltrim(substr(text, 1, instr(text, '@') - 1), '0123456789') = ''
"""


def cleanup(*, apply: bool = False) -> tuple[int, int]:
    """Report candidate rows and optionally rewrite them as system notices."""
    db_file = backend.DB_FILE
    if not db_file.exists():
        raise FileNotFoundError(db_file)

    with _DB_LOCK, sqlite3.connect(db_file) as connection:
        if apply:
            connection.execute("BEGIN IMMEDIATE")
        (count,) = connection.execute(
            f"SELECT COUNT(*) FROM messages WHERE {_CANDIDATE_WHERE}"
        ).fetchone()
        updated = 0
        if apply:
            cursor = connection.execute(
                f"UPDATE messages SET msg_type = ?, text = ?, sender = '' "
                f"WHERE {_CANDIDATE_WHERE}",
                (MSG_TYPE_SYSTEM, NOTICE_TEXT),
            )
            updated = cursor.rowcount
    return int(count), updated


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="report candidates without changing them (default)",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="rewrite all reported rows as system notices",
    )
    args = parser.parse_args(argv)

    try:
        count, updated = cleanup(apply=args.apply)
    except FileNotFoundError as exc:
        print(f"DB non trovato: {exc.args[0]}")
        return 1
    except sqlite3.OperationalError as exc:
        print(f"Impossibile bonificare il DB: {exc}")
        return 2

    if args.apply:
        print(f"Righe aggiornate: {updated}")
    else:
        print(f"Righe candidate: {count}")
        print("(dry-run: nessuna modifica scritta; usare --apply per aggiornare)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
