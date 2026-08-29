from __future__ import annotations

import sqlite3

from purge_test_rows import purge


def test_purge_backs_up_and_removes_only_known_whatsapp_rows(tmp_path):
    db_file = tmp_path / "messages.db"
    with sqlite3.connect(db_file) as connection:
        connection.execute(
            "CREATE TABLE messages (protocol TEXT, contact_number TEXT, text TEXT)"
        )
        connection.executemany(
            "INSERT INTO messages VALUES (?, ?, ?)",
            [
                ("whatsapp", "db@lid", "remove"),
                ("whatsapp", "unread@lid", "remove"),
                ("whatsapp", "3912345678@c.us", "remove"),
                ("whatsapp", "111@lid", "keep"),
                ("signal", "db@lid", "keep"),
            ],
        )

    assert purge(db_file) == 3

    backup = next(tmp_path.glob("messages.db.bak-*"))
    with sqlite3.connect(db_file) as connection:
        remaining = connection.execute(
            "SELECT protocol, contact_number FROM messages ORDER BY protocol"
        ).fetchall()
    with sqlite3.connect(backup) as connection:
        backed_up = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    assert remaining == [("signal", "db@lid"), ("whatsapp", "111@lid")]
    assert backed_up == 5
