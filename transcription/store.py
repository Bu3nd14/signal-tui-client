from __future__ import annotations

import sqlite3
import time
from typing import Any

import protocols.db as backend
from protocols.db import _DB_LOCK, _init_db


def get(protocol: str, attachment_id: str) -> dict[str, Any] | None:
    _init_db()
    with _DB_LOCK:
        conn = sqlite3.connect(backend.DB_FILE)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT status, text, error, model, updated_at "
                "FROM transcriptions WHERE protocol = ? AND attachment_id = ?",
                (protocol, attachment_id),
            ).fetchone()
        finally:
            conn.close()
    return dict(row) if row is not None else None


def set(
    protocol: str,
    attachment_id: str,
    status: str,
    text: str | None = None,
    error: str | None = None,
    model: str | None = None,
) -> None:
    _init_db()
    with _DB_LOCK:
        conn = sqlite3.connect(backend.DB_FILE)
        try:
            conn.execute(
                """
                INSERT INTO transcriptions (
                    protocol, attachment_id, status, text, error, model, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(protocol, attachment_id) DO UPDATE SET
                    status = excluded.status,
                    text = excluded.text,
                    error = excluded.error,
                    model = excluded.model,
                    updated_at = excluded.updated_at
                """,
                (
                    protocol,
                    attachment_id,
                    status,
                    text,
                    error,
                    model,
                    time.time(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
