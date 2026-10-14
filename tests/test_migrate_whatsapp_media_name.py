"""Test per ``migrate_whatsapp_media_name.py`` (semantica canonica v2).

Copre: dry-run readonly senza backup, apply con backup + idempotenza,
caption → ``text`` + basename, URL con query, mirror ``sent-*`` → etichetta di
kind, id assente → ``File`` e immagini non toccate.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import migrate_whatsapp_media_name as migrate
from protocols import db

CONTACT = "391234567890@c.us"


def _bootstrap() -> Path:
    db._init_db()
    return db.DB_FILE


def _add(
    *,
    text: str,
    attachment_id: str | None,
    attachment_info: str | None,
    msg_type: str = "attachment",
    content_type: str | None = "application/pdf",
    media_kind: str | None = "document",
    is_mine: bool = False,
    ts: int = 1000,
    msg_id: str = "m1",
):
    db._add_message_to_cache(
        CONTACT,
        text,
        is_mine,
        "You" if is_mine else "M",
        ts,
        protocol="whatsapp",
        msg_type=msg_type,
        attachment_info=attachment_info,
        attachment_id=attachment_id,
        content_type=content_type,
        media_kind=media_kind,
        msg_id=msg_id,
    )


def _row(msg_id: str = "m1") -> tuple | None:
    with sqlite3.connect(db.DB_FILE) as conn:
        return conn.execute(
            "SELECT text, attachment_info FROM messages "
            "WHERE contact_number = ? AND msg_id = ?",
            (CONTACT, msg_id),
        ).fetchone()


def _backups(path: Path) -> list[Path]:
    return list(path.parent.glob("*.bak*"))


def test_dry_run_writes_nothing_and_creates_no_backup():
    path = _bootstrap()
    _add(
        text="",
        attachment_id="http://host/dir/report.pdf",
        attachment_info="ecco il report",
    )

    assert migrate.run(path, apply=False, contact=None, limit=None) == 0
    assert _row() == ("", "ecco il report")
    assert _backups(path) == []


def test_apply_moves_caption_to_text_and_uses_basename():
    path = _bootstrap()
    _add(
        text="",
        attachment_id="http://host/dir/report.pdf",
        attachment_info="ecco il report",
    )

    assert migrate.run(path, apply=True, contact=None, limit=None) == 0
    assert _row() == ("ecco il report", "report.pdf")
    assert len(_backups(path)) == 1

    # Idempotente: un rerun non cambia la riga.
    assert migrate.run(path, apply=True, contact=None, limit=None) == 0
    assert _row() == ("ecco il report", "report.pdf")


def test_synthetic_text_is_replaced_by_caption():
    path = _bootstrap()
    _add(
        text="Media: http://host/dir/doc",
        attachment_id="http://host/dir/doc",
        attachment_info="una didascalia",
    )

    assert migrate.run(path, apply=True, contact=None, limit=None) == 0
    assert _row() == ("una didascalia", "doc")


def test_url_with_query_uses_path_basename():
    path = _bootstrap()
    _add(
        text="",
        attachment_id="http://host/api/files/report.pdf?download=1#frag",
        attachment_info="una didascalia",
    )

    assert migrate.run(path, apply=True, contact=None, limit=None) == 0
    assert _row() == ("una didascalia", "report.pdf")


def test_real_text_is_preserved():
    path = _bootstrap()
    _add(
        text="testo reale",
        attachment_id="http://host/dir/report.pdf",
        attachment_info="una didascalia",
    )

    assert migrate.run(path, apply=True, contact=None, limit=None) == 0
    assert _row() == ("testo reale", "report.pdf")


@pytest.mark.parametrize(
    ("kind", "label"),
    [
        ("document", "Documento"),
        ("video", "Video"),
        ("audio", "Audio"),
        ("voice", "Audio"),
    ],
)
def test_sent_mirror_uses_kind_label(kind, label):
    path = _bootstrap()
    _add(
        text="",
        attachment_id="sent-abc.pdf",
        attachment_info="una didascalia",
        media_kind=kind,
    )

    assert migrate.run(path, apply=True, contact=None, limit=None) == 0
    assert _row() == ("una didascalia", label)


def test_missing_attachment_id_uses_file_label():
    path = _bootstrap()
    _add(text="", attachment_id=None, attachment_info="una didascalia")

    assert migrate.run(path, apply=True, contact=None, limit=None) == 0
    assert _row() == ("una didascalia", "File")


def test_image_row_untouched():
    path = _bootstrap()
    _add(
        text="",
        attachment_id="http://host/photo.jpg",
        attachment_info="una caption",
        msg_type="image",
        content_type="image/jpeg",
        media_kind="image",
    )

    assert migrate.run(path, apply=True, contact=None, limit=None) == 0
    assert _row() == ("", "una caption")
