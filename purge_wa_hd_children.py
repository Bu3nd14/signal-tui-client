#!/usr/bin/env python3
"""Rimuove dal cache SQLite i child HD dei "HD dual upload" WhatsApp.

Una foto/video HD arriva da WhatsApp come DUE messaggi (parent SD visibile +
child HD nascosto, ``associationType: HD_IMAGE_DUAL_UPLOAD``).  Prima del fix
in ``_event_from_message`` entrambi venivano salvati e la chat mostrava la
stessa foto due volte.

Il DB non conserva i metadati WAHA, quindi lo script:

1. seleziona le righe media WhatsApp che hanno un "gemello" (stessa chat,
   stessa direzione, stesso tipo/caption, Δts ≤ 3 s, msg_id diverso) —
   euristica che include anche album legittimi;
2. per ciascun msg_id interroga WAHA (senza scaricare il media) e tiene SOLO
   quelli che WAHA conferma come child HD (``_is_hd_child``);
3. dry-run di default; ``--apply`` crea un backup e cancella le righe.

Dopo ``--apply`` riavviare ``megachat.py serve``: il cache in memoria del
server viene ricaricato dal DB solo alla connessione.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from protocols import db
from protocols.whatsapp_events import _is_hd_child

PAIR_WINDOW_MS = 3000

#: (chat_id, msg_id) -> messaggio WAHA grezzo o None
FetchMessage = Callable[[str, str], dict | None]

_SUSPECTS_SQL = """
SELECT DISTINCT a.id, a.contact_number, a.msg_id, a.timestamp, a.is_mine,
       a.msg_type, a.attachment_info
FROM messages a
JOIN messages b
  ON b.protocol = 'whatsapp'
 AND b.contact_number = a.contact_number
 AND b.is_mine = a.is_mine
 AND b.msg_type = a.msg_type
 AND IFNULL(b.attachment_info, '') = IFNULL(a.attachment_info, '')
 AND b.id != a.id
 AND IFNULL(b.msg_id, '') != IFNULL(a.msg_id, '')
 AND ABS(b.timestamp - a.timestamp) <= ?
WHERE a.protocol = 'whatsapp'
  AND a.msg_type IN ('image', 'attachment')
  AND a.msg_id IS NOT NULL AND a.msg_id != ''
ORDER BY a.contact_number, a.timestamp
"""


def _chat_of(msg_id: str, fallback: str) -> str:
    """Chat JID incorporato in un id serializzato ``{bool}_{chat}_{hex}[...]``."""
    parts = msg_id.split("_")
    if len(parts) >= 3 and parts[0] in ("true", "false") and "@" in parts[1]:
        return parts[1]
    return fallback


def _waha_fetch() -> FetchMessage:
    from protocols.config import resolve_whatsapp_api_url
    from protocols.whatsapp_rest import WhatsAppRESTClient

    client = WhatsAppRESTClient(resolve_whatsapp_api_url())

    def fetch(chat_id: str, msg_id: str) -> dict | None:
        result = client._request(
            "GET",
            f"/api/{client.session_name}/chats/{quote(chat_id, safe='')}"
            f"/messages/{quote(msg_id, safe='')}?downloadMedia=false",
        )
        if client.last_status in (401, 403):
            # Senza autenticazione ogni verdetto sarebbe "keep": meglio fermarsi.
            raise SystemExit(
                "WAHA rejected the API key (HTTP "
                f"{client.last_status}): set WHATSAPP_API_KEY or WAHA_API_KEY in .env"
            )
        return result if isinstance(result, dict) else None

    return fetch


def _backup_database(db_file: Path) -> Path:
    epoch = int(time.time())
    backup_file = db_file.with_name(f"{db_file.name}.bak-{epoch}")
    while backup_file.exists():
        epoch += 1
        backup_file = db_file.with_name(f"{db_file.name}.bak-{epoch}")
    with (
        closing(sqlite3.connect(db_file)) as source,
        closing(sqlite3.connect(backup_file)) as backup,
        source,
        backup,
    ):
        source.backup(backup)
    return backup_file


def find_hd_children(db_file: Path, fetch: FetchMessage) -> tuple[set[str], int]:
    """Ritorna (msg_id confermati child HD, numero di sospetti esaminati)."""
    with closing(sqlite3.connect(db_file)) as connection:
        rows = connection.execute(_SUSPECTS_SQL, (PAIR_WINDOW_MS,)).fetchall()
    children: set[str] = set()
    checked: set[str] = set()
    for row_id, contact, msg_id, ts, is_mine, msg_type, info in rows:
        if msg_id in checked:
            continue
        checked.add(msg_id)
        raw = fetch(_chat_of(msg_id, contact), msg_id)
        verdict = "HD-CHILD" if raw is not None and _is_hd_child(raw) else "keep"
        if raw is None:
            verdict = "keep (not found on WAHA)"
        print(
            f"{verdict:<26} row={row_id} contact={contact!r} msg_id={msg_id!r} "
            f"ts={ts} mine={bool(is_mine)} type={msg_type} info={(info or '')[:40]!r}"
        )
        if verdict == "HD-CHILD":
            children.add(msg_id)
    return children, len(checked)


def purge(
    db_file: Path | str | None = None,
    *,
    apply: bool = False,
    fetch: FetchMessage | None = None,
) -> int:
    target = Path(db_file or db.DB_FILE)
    if not target.exists():
        print(f"DB not found: {target}")
        return 0
    children, checked = find_hd_children(target, fetch or _waha_fetch())
    if not apply:
        print(
            f"DRY-RUN: {checked} suspect(s), {len(children)} HD child(ren), "
            "0 row(s) deleted."
        )
        return 0
    if not children:
        print("Nothing to delete.")
        return 0
    backup_file = _backup_database(target)
    print(f"Backup created: {backup_file}")
    removed = 0
    with db._DB_LOCK, closing(sqlite3.connect(target)) as connection, connection:
        for msg_id in sorted(children):
            removed += connection.execute(
                "DELETE FROM messages WHERE protocol = 'whatsapp' AND msg_id = ?",
                (msg_id,),
            ).rowcount
    print(f"Removed {removed} row(s). Restart megachat to reload the cache.")
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--db", type=Path, default=db.DB_FILE)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    purge(args.db, apply=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
