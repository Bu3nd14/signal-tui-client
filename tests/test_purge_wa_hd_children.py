from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

import purge_wa_hd_children as purge_mod
from protocols import db

CHAT = "111111111111111@lid"
PARENT = f"false_{CHAT}_3A00000000000000000A"
CHILD = f"false_{CHAT}_3A00000000000000000B"
ALBUM_A = "false_120363@g.us_AAA_1@lid"
ALBUM_B = "false_120363@g.us_BBB_1@lid"
TS = 1_793_531_968_000


@pytest.fixture
def tmp_db(tmp_path: Path):
    db_file = tmp_path / "messages.db"
    with (
        patch.object(db, "DB_FILE", db_file),
        patch.object(db, "CACHE_DIR", tmp_path),
    ):
        db._init_db()
        rows = [
            # coppia HD dual upload (stessa riga anche sotto la chiave @c.us)
            ("391234567890@c.us", PARENT, TS),
            ("391234567890@c.us", CHILD, TS + 1000),
            ("text-row", None, TS),
            # album legittimo: due foto diverse a 1 s
            ("120363@g.us", ALBUM_A, TS),
            ("120363@g.us", ALBUM_B, TS + 1000),
        ]
        with closing(sqlite3.connect(db_file)) as conn, conn:
            for contact, msg_id, ts in rows:
                conn.execute(
                    "INSERT INTO messages (protocol, contact_number, text, is_mine,"
                    " timestamp, msg_type, attachment_info, msg_id)"
                    " VALUES ('whatsapp', ?, '', 0, ?, ?, 'caption', ?)",
                    (contact, ts, "text" if msg_id is None else "image", msg_id),
                )
        yield db_file


def _fake_waha(chat_id: str, msg_id: str) -> dict | None:
    if msg_id == CHILD:
        assert chat_id == CHAT  # chat ricavata dall'id serializzato
        return {
            "id": msg_id,
            "_data": {
                "associationType": "HD_IMAGE_DUAL_UPLOAD",
                "pairedMediaType": "HD_IMAGE_CHILD",
            },
        }
    if msg_id == PARENT:
        return {"id": msg_id, "_data": {"pairedMediaType": "SD_IMAGE_PARENT"}}
    return {"id": msg_id, "_data": {}}


def _msg_ids(db_file: Path) -> list[str | None]:
    with closing(sqlite3.connect(db_file)) as conn:
        return [r[0] for r in conn.execute("SELECT msg_id FROM messages ORDER BY id")]


def test_dry_run_deletes_nothing(tmp_db, capsys):
    before = _msg_ids(tmp_db)
    assert purge_mod.purge(tmp_db, fetch=_fake_waha) == 0
    assert _msg_ids(tmp_db) == before
    out = capsys.readouterr().out
    assert "1 HD child(ren)" in out
    assert not list(tmp_db.parent.glob("*.bak-*"))


def test_apply_removes_only_confirmed_hd_child(tmp_db):
    assert purge_mod.purge(tmp_db, apply=True, fetch=_fake_waha) == 1
    assert _msg_ids(tmp_db) == [PARENT, None, ALBUM_A, ALBUM_B]
    assert len(list(tmp_db.parent.glob("messages.db.bak-*"))) == 1


def test_unreachable_waha_keeps_everything(tmp_db):
    assert purge_mod.purge(tmp_db, apply=True, fetch=lambda c, m: None) == 0
    assert len(_msg_ids(tmp_db)) == 5
