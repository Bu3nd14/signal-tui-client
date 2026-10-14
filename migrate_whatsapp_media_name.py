#!/usr/bin/env python3
"""Backfill nome/caption dei media WhatsApp non-image.

Bug: per i media WhatsApp non-image (document/video/audio/voice) i siti di
normalizzazione salvavano la caption utente in ``attachment_info`` (usato come
nome file da TUI/web) lasciando il ``text`` vuoto o sintetico.  Questo script
riallinea le righe storiche alla semantica canonica:

- la caption torna in ``text`` (se ``text`` è vuoto o è un'identità sintetica);
- ``attachment_info`` diventa il filename reale (basename dell'``attachment_id``)
  oppure un'etichetta generica per i mirror client-side ``sent-*``.

Default ``--dry-run``: apre il DB in sola lettura e NON crea backup.  Con
``--apply`` crea un backup ``<db>.bak-<epoch>`` e aggiorna in un'unica
transazione.  Idempotente (un rerun non aggiorna nulla).  Non tocca immagini,
gif, sticker né altri protocolli.

Ordine d'uso: dedup al boot -> poi migrate.  Lo script presuppone che la
manutenzione di avvio (``protocols.db.run_startup_maintenance``) abbia già fuso
le coppie gemelle; eseguirlo prima può far convergere righe che il boot
avrebbe poi scartato.

Uso:
    python3 migrate_whatsapp_media_name.py
    python3 migrate_whatsapp_media_name.py --apply
    python3 migrate_whatsapp_media_name.py --contact 391234567890@c.us --limit 100
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

from models import (
    is_caption_like,
    is_sent_mirror_attachment_id,
    is_whatsapp_synthetic_media_text,
)
from protocols.db import DB_FILE

_TARGET_SQL = (
    "SELECT id, text, attachment_id, attachment_info, media_kind, msg_type, "
    "content_type FROM messages WHERE protocol = 'whatsapp' "
    "AND (media_kind IN ('document', 'video', 'audio', 'voice') "
    "OR msg_type = 'attachment') "
    "AND attachment_info IS NOT NULL AND trim(attachment_info) <> '' "
    "AND (content_type IS NULL OR content_type NOT LIKE 'image/%')"
)

#: Etichetta generica per i mirror ``sent-*`` (il file locale non conserva il
#: nome originale).  Default ``File`` per kind sconosciuti.
_KIND_LABELS = {
    "document": "Documento",
    "video": "Video",
    "audio": "Audio",
    "voice": "Audio",
}


def _basename(attachment_id: str | None) -> str:
    """Basename dell'``attachment_id`` (rimuove query/fragment da un URL)."""
    if not attachment_id:
        return ""
    return Path(urlsplit(str(attachment_id)).path).name


def _new_info(attachment_id: str | None, kind: str | None) -> str:
    """Ritorna il nuovo ``attachment_info`` (filename reale o etichetta)."""
    if not attachment_id:
        return "File"
    if is_sent_mirror_attachment_id(attachment_id):
        return _KIND_LABELS.get(kind or "", "File")
    return _basename(attachment_id) or "File"


def _connect(db_file: Path, *, readonly: bool) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    return conn


def _backup(db_file: Path) -> Path | None:
    """Backup WAL-safe via la backup API; ``None`` se fallisce (abort)."""
    backup_file = Path(f"{db_file}.bak-{int(time.time())}")
    try:
        source = sqlite3.connect(db_file)
        try:
            destination = sqlite3.connect(backup_file)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
    except sqlite3.Error as exc:
        print(f"ABORT: backup fallito: {exc}")
        return None
    print(f"Backup creato: {backup_file}")
    return backup_file


def _log(action: str, row: sqlite3.Row, reason: str) -> None:
    print(f"{action} id={row['id']} {reason}")


def run(
    db_file: Path,
    *,
    apply: bool,
    contact: str | None,
    limit: int | None,
) -> int:
    if not db_file.exists():
        print(f"DB non trovato: {db_file}")
        return 1

    if apply and _backup(db_file) is None:
        return 1

    conn = _connect(db_file, readonly=not apply)
    updated = skipped = 0
    try:
        where = ""
        params: list = []
        if contact:
            where = " AND contact_number = ?"
            params.append(contact)
        sql = f"{_TARGET_SQL}{where} ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        targets = conn.execute(sql, params).fetchall()

        for row in targets:
            info = row["attachment_info"] or ""
            attachment_id = row["attachment_id"]
            basename = _basename(attachment_id)

            # Solo le righe con la caption finita per errore in info (una riga
            # già corretta ha info == basename ed è saltata qui).
            if not is_caption_like(info) or info == basename:
                _log("SKIP", row, "info-già-corretta")
                skipped += 1
                continue

            text = row["text"] or ""
            if not text.strip() or is_whatsapp_synthetic_media_text(
                text, attachment_id=attachment_id
            ):
                new_text = info
            else:
                new_text = row["text"]

            kind = row["media_kind"] or row["msg_type"]
            new_info = _new_info(attachment_id, kind)

            if new_text == (row["text"] or "") and new_info == info:
                _log("SKIP", row, "già-aggiornato")
                skipped += 1
                continue

            if apply:
                conn.execute(
                    "UPDATE messages SET text = ?, attachment_info = ? WHERE id = ?",
                    (new_text, new_info, row["id"]),
                )
            _log("UPDATE", row, f"text={new_text!r} info={new_info!r}")
            updated += 1

        if apply:
            conn.commit()
    finally:
        conn.close()

    print(f"{updated} updated / {skipped} skipped")
    if not apply:
        print("(dry-run: nessuna modifica scritta)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_FILE)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="scrive le modifiche (default: dry-run in sola lettura)",
    )
    parser.add_argument("--contact", type=str, default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    return run(
        args.db,
        apply=args.apply,
        contact=args.contact,
        limit=args.limit,
    )


if __name__ == "__main__":
    sys.exit(main())
