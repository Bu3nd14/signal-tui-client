from __future__ import annotations

import sqlite3

import backend
import migrate_reactions_cleanup as migration


def _insert_message(
    connection: sqlite3.Connection,
    *,
    protocol: str,
    text: str | None = "",
    msg_type: str = "text",
    attachment_id: str | None = None,
    attachment_info: str | None = None,
    quote_text: str | None = None,
    edited: int = 0,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO messages (
            protocol, contact_number, text, timestamp, msg_type,
            attachment_id, attachment_info, quote_text, edited
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            protocol,
            f"{protocol}-contact",
            text,
            connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] + 1,
            msg_type,
            attachment_id,
            attachment_info,
            quote_text,
            edited,
        ),
    )
    return cursor.lastrowid


def _prepare_db() -> tuple[set[int], set[int]]:
    backend._init_db()
    with sqlite3.connect(backend.DB_FILE) as connection:
        candidates = {
            _insert_message(connection, protocol="signal"),
            _insert_message(connection, protocol="signal", text=None),
            _insert_message(connection, protocol="whatsapp"),
            _insert_message(connection, protocol="telegram", text=None),
        }
        preserved = {
            _insert_message(
                connection, protocol="signal", attachment_id="attachment-1"
            ),
            _insert_message(connection, protocol="whatsapp", attachment_info="media"),
            _insert_message(connection, protocol="telegram", quote_text="quoted"),
            _insert_message(connection, protocol="signal", edited=1),
            _insert_message(connection, protocol="telegram", msg_type="image"),
            _insert_message(connection, protocol="whatsapp", text="hello"),
        }
    return candidates, preserved


def _message_ids() -> set[int]:
    with sqlite3.connect(backend.DB_FILE) as connection:
        return {row[0] for row in connection.execute("SELECT id FROM messages")}


def test_dry_run_reports_candidates_without_deleting(capsys):
    candidates, preserved = _prepare_db()

    assert migration.main(["--dry-run"]) == 0

    output = capsys.readouterr().out
    assert "Righe candidate: 4" in output
    assert "signal: 2" in output
    assert "whatsapp: 1" in output
    assert "telegram: 1" in output
    assert "dry-run: nessuna modifica scritta" in output
    assert _message_ids() == candidates | preserved


def test_apply_removes_only_candidates_and_reports_protocol_counts(capsys):
    candidates, preserved = _prepare_db()

    assert migration.main(["--apply"]) == 0

    output = capsys.readouterr().out
    assert "Righe cancellate: 4" in output
    assert "signal: 2" in output
    assert "whatsapp: 1" in output
    assert "telegram: 1" in output
    assert _message_ids() == preserved
    assert not candidates & _message_ids()
