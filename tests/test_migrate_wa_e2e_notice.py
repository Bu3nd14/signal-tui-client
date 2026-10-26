"""Test per ``migrate_wa_e2e_notice.py``.

Copre: dry-run readonly, apply, idempotenza, DB mancante, errore operativo,
filtri di selezione delle righe candidate e main guard.
"""

from __future__ import annotations

import runpy
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

import migrate_wa_e2e_notice as migrate
import protocols.db as backend

NOTICE = migrate.NOTICE_TEXT
LID = "20508603076702@lid"
CUS = "391234567890@c.us"


def _insert(
    *,
    text: str,
    protocol: str = "whatsapp",
    msg_type: str = "text",
    is_mine: int = 0,
    sender: str = "M",
    attachment_id: str | None = None,
    quote_text: str | None = None,
) -> int:
    backend._init_db()
    with closing(sqlite3.connect(backend.DB_FILE)) as connection, connection:
        cursor = connection.execute(
            """
            INSERT INTO messages (
                protocol, contact_number, text, is_mine, sender, timestamp,
                msg_type, attachment_id, quote_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                protocol,
                CUS,
                text,
                is_mine,
                sender,
                connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] + 1,
                msg_type,
                attachment_id,
                quote_text,
            ),
        )
        connection.commit()
    return cursor.lastrowid


def _system_rows() -> list[tuple[str, str]]:
    with closing(sqlite3.connect(backend.DB_FILE)) as connection, connection:
        return connection.execute(
            "SELECT text, sender FROM messages WHERE msg_type = 'system'"
        ).fetchall()


def _row(message_id: int) -> tuple[str, str]:
    with closing(sqlite3.connect(backend.DB_FILE)) as connection, connection:
        return connection.execute(
            "SELECT text, msg_type FROM messages WHERE id = ?", (message_id,)
        ).fetchone()


def test_dry_run_reports_candidates_without_writing():
    lid_id = _insert(text=LID)
    cus_id = _insert(text=CUS)
    other_id = _insert(text="ciao")

    assert migrate.cleanup(apply=False) == (2, 0)
    assert _system_rows() == []
    assert _row(lid_id) == (LID, "text")
    assert _row(cus_id) == (CUS, "text")
    assert _row(other_id) == ("ciao", "text")


def test_apply_rewrites_only_candidates():
    _insert(text=LID)
    _insert(text=CUS)
    other_id = _insert(text="ciao")

    assert migrate.cleanup(apply=True) == (2, 2)
    assert _system_rows() == [(NOTICE, ""), (NOTICE, "")]
    assert _row(other_id) == ("ciao", "text")


def test_apply_is_idempotent():
    _insert(text=LID)
    _insert(text=CUS)
    other_id = _insert(text="ciao")

    assert migrate.cleanup(apply=True) == (2, 2)
    assert migrate.cleanup(apply=True) == (0, 0)
    assert _system_rows() == [(NOTICE, ""), (NOTICE, "")]
    assert _row(other_id) == ("ciao", "text")


def test_missing_db_raises_file_not_found():
    with pytest.raises(FileNotFoundError):
        migrate.cleanup()


def test_missing_db_main_returns_1():
    assert migrate.main([]) == 1


def test_operational_error_main_returns_2():
    with closing(sqlite3.connect(backend.DB_FILE)) as connection, connection:
        connection.execute("CREATE TABLE dummy(x)")

    assert migrate.main(["--apply"]) == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"is_mine": 1},
        {"protocol": "signal"},
        {"msg_type": "image"},
        {"attachment_id": "att-1"},
        {"quote_text": "citazione"},
        {"text": "ciao"},
        {"text": "1234@lid"},
        {"text": "12345abc@lid"},
    ],
)
def test_selection_filters(overrides):
    params = {
        "text": LID,
        "protocol": "whatsapp",
        "msg_type": "text",
        "is_mine": 0,
        "attachment_id": None,
        "quote_text": None,
        **overrides,
    }
    _insert(**params)

    assert migrate.cleanup(apply=True) == (0, 0)

    with closing(sqlite3.connect(backend.DB_FILE)) as connection, connection:
        text, msg_type, sender = connection.execute(
            "SELECT text, msg_type, sender FROM messages"
        ).fetchone()
    assert text == params["text"]
    assert msg_type == params["msg_type"]
    assert sender == "M"


def test_cli_dry_run_default():
    _insert(text=LID)

    assert migrate.main([]) == 0
    assert _system_rows() == []


def test_cli_apply_prints_updated(capsys):
    _insert(text=LID)

    assert migrate.main(["--apply"]) == 0
    assert "Righe aggiornate: 1" in capsys.readouterr().out
    assert len(_system_rows()) == 1


def test_main_guard_missing_db_returns_1(monkeypatch):
    script = Path(migrate.__file__)
    monkeypatch.setattr(sys, "argv", [str(script)])

    with pytest.raises(SystemExit) as exc:
        runpy.run_path(script, run_name="__main__")

    assert exc.value.code == 1
