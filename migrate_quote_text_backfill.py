#!/usr/bin/env python3
"""Backfill ``quote_text`` per le reply media outgoing Signal.

Bug: una reply (con quote) a un MESSAGGIO VOCALE/media inviata dalla Web UI
perde la quote nel cache locale: la riga outgoing ha ``quote_timestamp``
valorizzato ma ``quote_text`` vuoto.  Il fix alla fonte popola il mirror; questo
script riempie le righe storiche derivando il display dal messaggio quotato
(stesso markup di ``media_quote_display``).

Default ``--dry-run``: apre il DB in sola lettura e NON crea backup.  Con
``--apply`` crea un backup ``<db>.bak-<epoch>`` e aggiorna in un'unica
transazione.  Idempotente (un rerun non aggiorna nulla).

Uso:
    python3 migrate_quote_text_backfill.py
    python3 migrate_quote_text_backfill.py --apply
    python3 migrate_quote_text_backfill.py --contact +391234567890 --limit 100
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from models import (
    MEDIA_QUOTE_PLACEHOLDERS,
    media_quote_display,
)
from protocols.db import DB_FILE

_TARGET_SQL = (
    "SELECT id, contact_number, timestamp, quote_timestamp, quote_text "
    "FROM messages WHERE protocol = 'signal' AND is_mine = 1 "
    "AND (quote_text IS NULL OR trim(quote_text) = '') "
    "AND quote_timestamp IS NOT NULL"
)

_PLACEHOLDERS = tuple(MEDIA_QUOTE_PLACEHOLDERS.values())
_MEDIA_EXTENSIONS = frozenset(
    {
        "jpg",
        "jpeg",
        "png",
        "gif",
        "webp",
        "bmp",
        "tiff",
        "tif",
        "heic",
        "heif",
        "mp4",
        "mov",
        "mkv",
        "webm",
        "avi",
        "mp3",
        "ogg",
        "opus",
        "aac",
        "m4a",
        "wav",
        "pdf",
    }
)


def _ext(component: str) -> bool:
    return Path(component).suffix.lower().lstrip(".") in _MEDIA_EXTENSIONS


def _is_synthetic(text, *, attachment_id=None) -> bool:
    """True se *text* è un'identità media (non una caption utente) — v3.2."""
    t = (text or "").strip()
    if not t:
        return True
    if t in _PLACEHOLDERS:
        return True
    # (a) prefisso "<placeholder>: <tail>"
    for p in _PLACEHOLDERS:
        if t.startswith(f"{p}:"):
            tail = t[len(p) + 1 :].strip()
            if not tail:
                return True
            if attachment_id and tail in (attachment_id, Path(attachment_id).name):
                return True
            if _ext(tail):
                return True
            return bool(attachment_id is None and " " not in tail)
    # (b) forma legacy "<label>: <filename>: <id>" per-componenti
    if attachment_id:
        for comp in t.split(":"):
            c = comp.strip()
            if c in (attachment_id, Path(attachment_id).name):
                return True
    # (c) composito "<prefix> — <placeholder>" conservativo
    if " — " in t:
        prefix, _, suffix = t.rpartition(" — ")
        if suffix in _PLACEHOLDERS:
            if not prefix.strip():
                return True
            if attachment_id and prefix.strip() in (
                attachment_id,
                Path(attachment_id).name,
            ):
                return True
            return bool(_ext(prefix.strip()))
    return False


def _synthetic_fallback(text: str, attachment_id) -> bool:
    """True se scatta il ramo ``attachment_id is None`` con tail a token unico.

    Nota N1 (audit): il fallback riconosce l'identità senza id; lo si segnala
    nel log con il motivo ``synthetic-fallback``.
    """
    if attachment_id is not None:
        return False
    t = (text or "").strip()
    for p in _PLACEHOLDERS:
        if t.startswith(f"{p}:"):
            tail = t[len(p) + 1 :].strip()
            return bool(tail) and " " not in tail
    return False


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
    print(
        f"{action} id={row['id']} contact={row['contact_number']} "
        f"quote_timestamp={row['quote_timestamp']} {reason}"
    )


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
            # 1. auto-quote: il timestamp quotato coincide col proprio.
            if row["quote_timestamp"] == row["timestamp"]:
                _log("SKIP", row, "auto-quote")
                skipped += 1
                continue

            # 2. risolvi il messaggio quotato: deve essere univoco.
            quoted = conn.execute(
                "SELECT msg_type, text, attachment_info, content_type, "
                "media_kind, attachment_id FROM messages "
                "WHERE protocol = 'signal' AND contact_number = ? AND timestamp = ?",
                (row["contact_number"], row["quote_timestamp"]),
            ).fetchall()
            if len(quoted) != 1:
                reason = "quoted-not-found" if not quoted else "quoted-ambiguous"
                _log("SKIP", row, reason)
                skipped += 1
                continue
            target = quoted[0]

            # 4. segnaposto display per identità sintetica, testo reale altrimenti.
            text = target["text"] or ""
            attachment_id = target["attachment_id"]
            note = (
                " synthetic-fallback"
                if _synthetic_fallback(text, attachment_id)
                else ""
            )
            if _is_synthetic(text, attachment_id=attachment_id):
                content_type = (target["content_type"] or "").strip()
                if not content_type:
                    _log("SKIP", row, f"synthetic-senza-content_type{note}")
                    skipped += 1
                    continue
                derived = media_quote_display(content_type, filename=attachment_id)
            else:
                derived = target["text"]

            # 5. aggiorna solo se la derivazione è non vuota e diversa.
            if not (derived or "").strip():
                _log("SKIP", row, f"derivazione-vuota{note}")
                skipped += 1
                continue
            if derived == (row["quote_text"] or ""):
                _log("SKIP", row, f"già-aggiornato{note}")
                skipped += 1
                continue
            if apply:
                conn.execute(
                    "UPDATE messages SET quote_text = ? WHERE id = ?",
                    (derived, row["id"]),
                )
            _log("UPDATE", row, f"derived={derived!r}{note}")
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
