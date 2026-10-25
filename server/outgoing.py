"""Outgoing-message status transitions (core side of an optimistic send)."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)


def transition_outgoing_status(
    backend: Any,
    protocol: str,
    contact_id: str,
    timestamp: int,
    text: str,
    status: str,
    expected_statuses: tuple[str, ...],
) -> bool:
    """Advance one optimistic outgoing message in SQLite and the backend cache.

    The SQLite update is scoped by ``(protocol, contact, timestamp, text)`` and
    guarded by ``expected_statuses`` and the status rank (never a downgrade).
    The echo (often faster than the sender) may already have replaced the
    optimistic timestamp with the server's, so a miss is retried on the text
    (most recent outgoing row).  Returns False, changing nothing in the cache,
    when no row accepted the transition.
    """
    from protocols.db import (
        DB_FILE,
        _update_message_status,
        _update_message_status_by_text,
    )

    updated = _update_message_status(
        timestamp,
        status,
        protocol,
        contact_id,
        text=text,
        expected_statuses=expected_statuses,
    ) or _update_message_status_by_text(
        text,
        status,
        protocol,
        contact_id,
        expected_statuses=expected_statuses,
    )
    if not updated:
        # Diagnosis: how many rows match by text / by timestamp tells whether
        # the row was never persisted or raced with the echo.
        rows_text = rows_ts = -1
        try:
            conn = sqlite3.connect(DB_FILE)
            try:
                rows_text = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE protocol=? "
                    "AND contact_number=? AND text=? AND is_mine=1",
                    (protocol, contact_id, text),
                ).fetchone()[0]
                rows_ts = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE protocol=? "
                    "AND contact_number=? AND timestamp=? AND is_mine=1",
                    (protocol, contact_id, timestamp),
                ).fetchone()[0]
            finally:
                conn.close()
        except Exception as dbg:
            logger.debug("diagnostic row count failed", exc_info=dbg)
        logger.warning(
            "Outgoing status transition failed "
            "(protocol=%r, contact_id=%r, ts=%r, text=%r, status=%r, "
            "rows_by_text=%s, rows_by_ts=%s)",
            protocol,
            contact_id,
            timestamp,
            text[:80],
            status,
            rows_text,
            rows_ts,
        )
        return False
    for msg in getattr(backend, "cache", {}).get(contact_id, []):
        if (
            msg.get("is_mine")
            and msg.get("text") == text
            and msg.get("status") in expected_statuses
        ):
            msg["status"] = status
    return True
