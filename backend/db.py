"""
Message cache persistence (SQLite) for the Signal TUI Client.

Stores messages per (protocol, contact) in a local SQLite database so chats
persist across sessions.  Handles schema migration, incremental inserts,
dedup, read receipts and unread counts.  No Textual dependency.
"""

import logging
import sqlite3
import threading
from pathlib import Path

import backend as _backend

logger = logging.getLogger(__name__)

CACHE_DIR = Path.home() / ".local" / "share" / "signal-tui-client"
CACHE_FILE = CACHE_DIR / "messages.json"
DB_FILE = CACHE_DIR / "messages.db"
CACHE_RETENTION_DAYS = 3

# Window (ms) entro cui un'entry id-less può essere considerata l'echo di un
# messaggio con id reale.  Condivisa tra ``_update_message_id`` (match mirato a
# UNA riga entro la finestra) e ``_dedup_messages_by_id`` (guardia difensiva che
# non cancella partizioni con timestamp divergenti oltre la finestra).
_ECHO_MATCH_WINDOW_MS = 600_000  # 10 minuti

# Current schema version, persisted via ``PRAGMA user_version`` so the legacy
# migration below is skipped once the schema is known to be up to date.
_SCHEMA_VERSION = 3


# ─── Message cache (SQLite) ─────────────────────────────────────────────────

# Lock to serialize concurrent SQLite writes (poll worker thread + UI thread).
_DB_LOCK = threading.RLock()


def _ensure_cache_dir():
    """Create the cache directory if it doesn't exist."""
    _backend.CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _current_schema_version(conn: sqlite3.Connection) -> int:
    """Read the schema version stored in ``PRAGMA user_version``."""
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _migrate_protocol_schema(conn: sqlite3.Connection) -> None:
    """Upgrade a legacy ``messages`` table to the multi-protocol schema.

    If the table already has a ``protocol`` column this is a no-op.  When the
    column is missing (an existing database created before the multi-protocol
    refactor), it is added with a ``DEFAULT 'signal'`` so every existing
    message is assigned to the Signal protocol.  The contact index is then
    rebuilt to include the protocol prefix.

    The migration is gated by ``PRAGMA user_version`` so the DROP/CREATE index
    churn runs only once per database, not on every write.

    Works on the connection passed in; the caller is responsible for
    committing / closing.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}

    # Track whether a message's text was edited in place, so the
    # " (modificato)" indicator survives a restart.  Ensured unconditionally
    # (not gated by the user_version check below): a DB can already carry
    # user_version == 3 from an earlier migration path while still lacking
    # this column, and ``_load_cache`` / ``_update_message_text`` rely on it.
    if "edited" not in columns:
        conn.execute(
            "ALTER TABLE messages ADD COLUMN edited INTEGER NOT NULL DEFAULT 0"
        )

    # Mime type of a media attachment (e.g. "image/png").  Persisted so a
    # Signal quote can rebuild its ``quoteAttachments`` thumbnail even after a
    # restart (bug #37, piano B).  Ensured unconditionally for the same reason
    # as ``edited``: a DB can carry user_version == 3 while still lacking it.
    if "content_type" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN content_type TEXT")

    # Quoted-media thumbnail metadata (DESIGN_QUOTE_THUMBNAIL, additive).  The
    # resolved path is deliberately NOT persisted: it is derived lazily in the
    # UI via ``get_attachment_path`` (transient local file).
    if "quote_attachment_id" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN quote_attachment_id TEXT")
    if "quote_content_type" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN quote_content_type TEXT")
    # Signal extracts the quoted thumbnail from the envelope and stores it under
    # a content-hash name in ``CACHE_DIR/quote-thumbs/`` (a persistent file), so
    # its path IS persisted — unlike the lazy Telegram/WhatsApp path.
    if "quote_attachment_path" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN quote_attachment_path TEXT")

    if _current_schema_version(conn) >= _SCHEMA_VERSION:
        return

    if "protocol" not in columns:
        conn.execute(
            "ALTER TABLE messages ADD COLUMN protocol TEXT NOT NULL DEFAULT 'signal'"
        )

    # The WhatsApp backend carries a stable per-message ``id`` (the Baileys
    # message id).  Persisting it lets the id-based dedup in
    # ``_message_already_cached`` work across sessions — without it, DB-seeded
    # cache entries have no id and distinct messages sharing the same second
    # AND text get merged/dropped (chats appear "behind" when opened).
    if "msg_id" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN msg_id TEXT")

    # Keep enough reply metadata to retry a failed message after a restart and,
    # in particular, retain Telegram's server message id for reply_to.
    if "quote_timestamp" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN quote_timestamp INTEGER")
    if "quote_author" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN quote_author TEXT")
    if "reply_to_message_id" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN reply_to_message_id TEXT")

    # Rebuild the index so it is namespaced by protocol.  Dropping and
    # re-creating is idempotent on both migrated and fresh tables.
    conn.execute("DROP INDEX IF EXISTS idx_messages_contact")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_contact "
        "ON messages(protocol, contact_number, timestamp)"
    )

    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")


def _init_db():
    """Create the SQLite database and schema if it doesn't exist.

    Also auto-migrates an existing (legacy) database that predates the
    multi-protocol schema by adding the ``protocol`` column, so old caches
    keep working without manual migration.
    """
    _ensure_cache_dir()
    with _DB_LOCK:
        conn = sqlite3.connect(_backend.DB_FILE)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    protocol TEXT NOT NULL DEFAULT 'signal',
                    contact_number TEXT NOT NULL,
                    text TEXT,
                    is_mine INTEGER NOT NULL DEFAULT 0,
                    sender TEXT,
                    timestamp INTEGER NOT NULL,
                    quote_text TEXT,
                    msg_type TEXT DEFAULT 'text',
                    attachment_info TEXT,
                    attachment_id TEXT,
                    content_type TEXT,
                    quote_attachment_id TEXT,
                    quote_attachment_path TEXT,
                    quote_content_type TEXT,
                    read INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'read',
                    msg_id TEXT,
                    quote_timestamp INTEGER,
                    quote_author TEXT,
                    reply_to_message_id TEXT,
                    edited INTEGER NOT NULL DEFAULT 0
                )
            """)
            # Upgrade a pre-existing legacy DB in place (idempotent).
            _migrate_protocol_schema(conn)
            conn.commit()
        finally:
            conn.close()


def _load_cache(protocol: str | None = None) -> dict[str, list[dict]]:
    """Load messages from SQLite into a dict {contact: [messages]}.

    When ``protocol`` is given, only messages of that protocol are returned
    (e.g. ``"whatsapp"``), so each backend seeds its in-memory cache with only
    its own messages.  ``None`` (default) loads everything, preserving the
    legacy behaviour.

    Also runs an idempotent cross-session dedup by ``msg_id`` so protocol
    backends do not re-ingest duplicates after a restart.
    """
    _init_db()
    _dedup_messages_by_id()
    with _DB_LOCK:
        conn = sqlite3.connect(_backend.DB_FILE)
        try:
            conn.row_factory = sqlite3.Row
            if protocol is None:
                rows = conn.execute(
                    "SELECT * FROM messages ORDER BY timestamp"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE protocol = ? ORDER BY timestamp",
                    (protocol,),
                ).fetchall()
        finally:
            conn.close()
    cache: dict[str, list[dict]] = {}
    for row in rows:
        contact = row["contact_number"]
        if contact not in cache:
            cache[contact] = []
        cache[contact].append(
            {
                "id": row["msg_id"],
                "text": row["text"],
                "is_mine": bool(row["is_mine"]),
                "sender": row["sender"],
                "timestamp": row["timestamp"],
                "quote_text": row["quote_text"],
                "msg_type": row["msg_type"],
                "attachment_info": row["attachment_info"],
                "attachment_id": row["attachment_id"],
                "content_type": row["content_type"],
                "quote_attachment_id": row["quote_attachment_id"],
                "quote_attachment_path": row["quote_attachment_path"],
                "quote_content_type": row["quote_content_type"],
                "quote_timestamp": row["quote_timestamp"],
                "quote_author": row["quote_author"],
                "reply_to_message_id": row["reply_to_message_id"],
                "edited": bool(row["edited"]),
                "read": bool(row["read"]),
                "status": row["status"],
                "protocol": row["protocol"],
            }
        )
    return cache


def _add_message_to_cache(
    contact_number: str,
    text: str,
    is_mine: bool,
    sender: str,
    timestamp: int,
    quote_text: str | None = None,
    msg_type: str = "text",
    attachment_info: str | None = None,
    attachment_id: str | None = None,
    content_type: str | None = None,
    protocol: str = "signal",
    msg_id: str | None = None,
    status: str | None = None,
    quote_timestamp: int | None = None,
    quote_author: str | None = None,
    reply_to_message_id: str | None = None,
    quote_attachment_id: str | None = None,
    quote_attachment_path: str | None = None,
    quote_content_type: str | None = None,
):
    """Add a message to the SQLite cache (incremental INSERT).
    msg_type: "text", "image", "sticker", "attachment"
    attachment_info: additional details (filename, sticker emoji, etc.)
    attachment_id: signal-cli attachment UUID for resolving the file on disk.
    content_type: mime type of a media attachment (e.g. "image/png"), persisted
        so a Signal quote can rebuild its ``quoteAttachments`` thumbnail.
    protocol: source protocol ("signal", "whatsapp", ...). Defaults to signal
        for backward compatibility.
    msg_id: stable per-message id (e.g. the Baileys WhatsApp message id).
        Persisting it lets the id-based dedup work across sessions.
    """
    _init_db()
    if quote_attachment_path is not None:
        quote_attachment_path = str(quote_attachment_path)
    with _DB_LOCK:
        conn = sqlite3.connect(_backend.DB_FILE)
        try:
            conn.execute(
                """INSERT INTO messages
                   (protocol, contact_number, text, is_mine, sender, timestamp,
                     quote_text, msg_type, attachment_info, attachment_id, content_type,
                     quote_attachment_id, quote_attachment_path, quote_content_type,
                      read, status, msg_id, quote_timestamp, quote_author, reply_to_message_id)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    protocol,
                    contact_number,
                    text,
                    int(is_mine),
                    sender,
                    timestamp,
                    quote_text,
                    msg_type,
                    attachment_info,
                    attachment_id,
                    content_type,
                    quote_attachment_id,
                    quote_attachment_path,
                    quote_content_type,
                    int(is_mine),
                    status or ("sent" if is_mine else "read"),
                    msg_id,
                    quote_timestamp,
                    quote_author,
                    reply_to_message_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def _update_message_id(
    contact_number: str,
    text: str,
    is_mine: bool,
    timestamp: int,
    msg_id: str,
    protocol: str = "signal",
) -> bool:
    """Attach a real message id to the single closest id-less optimistic row.

    When the echo of an optimistic send arrives with its real id, the row that
    was inserted optimistically (``msg_id IS NULL`` or the legacy ``msg_id = ''``
    used by the Telegram backend) is updated in place instead of inserting a
    duplicate.  Matching is by ``(protocol, contact_number, text, is_mine)`` on
    the id-less row, but restricted to the echo window
    (``_ECHO_MATCH_WINDOW_MS``) around ``timestamp`` and limited to a single
    row: the closest in time, with a deterministic tie-break on ``rowid``
    (mirrors the ordering used by ``_dedup_messages_by_id``).  This guarantees
    an id is never attached to two distinct rows sharing the same text (e.g.
    two failed retries), which ``_dedup_messages_by_id`` would otherwise merge
    at boot.

    Returns ``True`` when exactly one row was updated, ``False`` otherwise.
    """
    _init_db()
    with _DB_LOCK:
        conn = sqlite3.connect(_backend.DB_FILE)
        try:
            cursor = conn.execute(
                "UPDATE messages SET msg_id = ?, timestamp = ? "
                "WHERE id = ("
                "SELECT id FROM messages WHERE protocol = ? AND contact_number = ? "
                "AND text = ? AND is_mine = ? AND (msg_id IS NULL OR msg_id = '') "
                "AND ABS(timestamp - ?) <= ? "
                "ORDER BY ABS(timestamp - ?) ASC, rowid ASC LIMIT 1)",
                (
                    msg_id,
                    timestamp,
                    protocol,
                    contact_number,
                    text,
                    int(is_mine),
                    timestamp,
                    _ECHO_MATCH_WINDOW_MS,
                    timestamp,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def _prune_cache():
    """Remove messages older than CACHE_RETENTION_DAYS and limit to 200 per contact."""

    _init_db()
    with _DB_LOCK:
        conn = sqlite3.connect(_backend.DB_FILE)
        try:
            # Keep only the 200 most recent messages per contact; no time-based
            # pruning — WhatsApp re-downloads history from WAHA anyway, and
            # time-based deletion breaks the dedup cycle (old messages are
            # deleted from DB, then re-inserted as "new" with read=False).
            # conn.execute("DELETE FROM messages WHERE timestamp < ?", (cutoff,))
            # Limit to 200 per contact: delete messages beyond the 200 most recent
            conn.execute("""
                DELETE FROM messages WHERE id NOT IN (
                    SELECT id FROM (
                        SELECT id, ROW_NUMBER() OVER (
                            PARTITION BY protocol, contact_number
                            ORDER BY timestamp DESC
                        ) AS rn FROM messages
                    ) WHERE rn <= 200
                )
            """)
            conn.commit()
        finally:
            conn.close()


def _mark_as_read(contact_number: str, protocol: str = "signal"):
    """Mark all messages for a contact as read."""
    _init_db()
    with _DB_LOCK:
        conn = sqlite3.connect(_backend.DB_FILE)
        try:
            conn.execute(
                "UPDATE messages SET read = 1 WHERE contact_number = ? AND protocol = ?",
                (contact_number, protocol),
            )
            conn.commit()
        finally:
            conn.close()


def _dedup_messages() -> int:
    """Remove duplicate messages from the database.

    A duplicate is defined as the same (protocol, contact_number, timestamp,
    text, is_mine) tuple.  Only the first occurrence (lowest rowid) is kept.
    Returns the number of rows removed.
    """
    _init_db()
    with _DB_LOCK:
        conn = sqlite3.connect(_backend.DB_FILE)
        try:
            before = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            conn.execute("""
                DELETE FROM messages WHERE rowid NOT IN (
                    SELECT MIN(rowid) FROM messages
                    GROUP BY protocol, contact_number, timestamp, text, is_mine
                )
            """)
            conn.commit()
            after = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            return before - after
        finally:
            conn.close()


def _update_message_status(
    timestamp: int,
    status: str,
    protocol: str,
    contact_number: str,
    text: str | None = None,
    expected_statuses: tuple[str, ...] | None = None,
) -> bool:
    """Update a message status in SQLite, scoped per (protocol, contact, ts).

    A bare ``timestamp`` match would update messages of OTHER protocols or
    contacts sharing the same millisecond timestamp, so the update is always
    scoped by ``protocol`` and ``contact_number``.
    """
    _init_db()
    with _DB_LOCK:
        conn = sqlite3.connect(_backend.DB_FILE)
        try:
            where = "protocol = ? AND contact_number = ? AND timestamp = ?"
            params: list = [protocol, contact_number, timestamp]
            if text is not None:
                where += " AND text = ?"
                params.append(text)
            if expected_statuses:
                placeholders = ", ".join("?" for _ in expected_statuses)
                where += f" AND status IN ({placeholders})"
                params.extend(expected_statuses)
            cursor = conn.execute(
                "UPDATE messages SET status = ? WHERE "
                + where
                + " AND CASE status WHEN 'pending' THEN 0 WHEN 'failed' THEN 0 "
                "WHEN 'sent' THEN 1 WHEN 'delivered' THEN 2 WHEN 'read' THEN 3 ELSE 0 END "
                "<= CASE ? WHEN 'pending' THEN 0 WHEN 'failed' THEN 0 WHEN 'sent' THEN 1 "
                "WHEN 'delivered' THEN 2 WHEN 'read' THEN 3 ELSE 0 END",
                [status, *params, status],
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def _update_message_status_by_id(
    msg_id: str,
    status: str,
    protocol: str,
    contact_number: str | None = None,
) -> bool:
    """Update a message status by its stable ``msg_id``.

    Like ``_update_message_status`` but keyed by the per-message ``msg_id``
    instead of the optimistic timestamp.  Used by the Telegram backend when a
    server read/delivery receipt identifies messages by id.  The optional
    ``contact_number`` scopes the update further when the same id could belong
    to different chats.
    """
    _init_db()
    with _DB_LOCK:
        conn = sqlite3.connect(_backend.DB_FILE)
        try:
            where = "protocol = ? AND msg_id = ?"
            params: list = [protocol, msg_id]
            if contact_number is not None:
                where += " AND contact_number = ?"
                params.append(contact_number)
            cursor = conn.execute(
                "UPDATE messages SET status = ? WHERE "
                + where
                + " AND CASE status WHEN 'pending' THEN 0 WHEN 'failed' THEN 0 "
                "WHEN 'sent' THEN 1 WHEN 'delivered' THEN 2 WHEN 'read' THEN 3 ELSE 0 END "
                "<= CASE ? WHEN 'pending' THEN 0 WHEN 'failed' THEN 0 WHEN 'sent' THEN 1 "
                "WHEN 'delivered' THEN 2 WHEN 'read' THEN 3 ELSE 0 END",
                [status, *params, status],
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def _update_message_status_by_text(
    text: str,
    status: str,
    protocol: str,
    contact_number: str,
    expected_statuses: tuple[str, ...] | None = None,
) -> bool:
    """Update the most recent matching outgoing row by ``(protocol, contact, text)``.

    Fallback per la transizione pending→sent (bug bolla "grigia"): l'echo di
    WhatsApp/Telegram può sostituire il timestamp ottimistico del client con
    quello del server PRIMA che il worker esegua la transizione, quindi il
    match per ``timestamp`` di ``_update_message_status`` fallisce.  Qui la
    riga outgoing più recente con lo stesso testo viene aggiornata, con lo
    stesso rank guard (mai downgrade) e lo scoping per protocollo/contatto.
    """
    _init_db()
    with _DB_LOCK:
        conn = sqlite3.connect(_backend.DB_FILE)
        try:
            where = "protocol = ? AND contact_number = ? AND text = ? AND is_mine = 1"
            params: list = [protocol, contact_number, text]
            if expected_statuses:
                placeholders = ", ".join("?" for _ in expected_statuses)
                where += f" AND status IN ({placeholders})"
                params.extend(expected_statuses)
            cursor = conn.execute(
                "UPDATE messages SET status = ? WHERE id = ("
                "SELECT id FROM messages WHERE "
                + where
                + " ORDER BY timestamp DESC LIMIT 1) "
                "AND CASE status WHEN 'pending' THEN 0 WHEN 'failed' THEN 0 "
                "WHEN 'sent' THEN 1 WHEN 'delivered' THEN 2 WHEN 'read' THEN 3 ELSE 0 END "
                "<= CASE ? WHEN 'pending' THEN 0 WHEN 'failed' THEN 0 WHEN 'sent' THEN 1 "
                "WHEN 'delivered' THEN 2 WHEN 'read' THEN 3 ELSE 0 END",
                [status, *params, status],
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def _update_message_text(
    contact_number: str,
    new_text: str,
    protocol: str,
    msg_id: str | None = None,
    timestamp: int | None = None,
    old_text: str | None = None,
    is_mine: bool | None = None,
    mark_edited: bool = True,
) -> bool:
    """Rewrite the text of an existing row in place (edit of a message).

    Matching is by ``(protocol, contact_number, msg_id)`` when ``msg_id`` is
    given, otherwise ``(protocol, contact_number, timestamp)``.  The temporal
    identity (timestamp/id) never changes — only the text does.  ``old_text``
    and ``is_mine`` are optional defensive constraints added to the WHERE
    clause when provided.  ``mark_edited`` drives the ``edited`` column (the
    rollback path sets it back to 0).  Returns ``True`` when a row was
    updated, following the ``_update_message_status`` pattern.
    """
    _init_db()
    with _DB_LOCK:
        conn = sqlite3.connect(_backend.DB_FILE)
        try:
            if msg_id is not None:
                where = "protocol = ? AND contact_number = ? AND msg_id = ?"
                params: list = [protocol, contact_number, msg_id]
            elif timestamp is not None:
                where = "protocol = ? AND contact_number = ? AND timestamp = ?"
                params = [protocol, contact_number, timestamp]
            else:
                return False
            if old_text is not None:
                where += " AND text = ?"
                params.append(old_text)
            if is_mine is not None:
                where += " AND is_mine = ?"
                params.append(int(is_mine))
            cursor = conn.execute(
                f"UPDATE messages SET text = ?, edited = ? WHERE {where}",
                [new_text, 1 if mark_edited else 0, *params],
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def _dedup_messages_by_id() -> int:
    """Remove duplicate rows with the same ``(protocol, contact_number, msg_id, text)``.

    When duplicate rows exist (e.g. an optimistic client-side row plus the
    server-echo row fetched at startup), keep the one with the highest status
    rank so a ``read`` receipt is never lost in favour of a ``sent`` duplicate.
    ``text`` is part of the dedup key because some protocols (WhatsApp) split a
    single incoming message into multiple cached rows (one per attachment) that
    share the same ``msg_id`` but have different text.  Idempotent: running it
    twice removes no additional rows.

    Defensive guard: a partition whose timestamps span more than
    ``_ECHO_MATCH_WINDOW_MS`` is a signal that one id was (erroneously) attached
    to two distinct messages (e.g. two failed retries sharing the same text).
    Such partitions are never merged — a warning is logged with the partition
    key, row count and timestamp range, and all rows are kept.

    Returns the number of rows removed.
    """
    _init_db()
    with _DB_LOCK:
        conn = sqlite3.connect(_backend.DB_FILE)
        try:
            before = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            # Defensive: log partitions whose timestamps diverge beyond the echo
            # window — an id assigned to two distinct messages.  These must never
            # be merged, otherwise a legitimate row would be deleted at boot.
            divergent = conn.execute(
                "SELECT protocol, contact_number, msg_id, text, COUNT(*) AS cnt, "
                "MIN(timestamp) AS min_ts, MAX(timestamp) AS max_ts "
                "FROM messages WHERE msg_id IS NOT NULL AND msg_id != '' "
                "GROUP BY protocol, contact_number, msg_id, text "
                "HAVING MAX(timestamp) - MIN(timestamp) > ?",
                (_ECHO_MATCH_WINDOW_MS,),
            ).fetchall()
            for (
                protocol,
                contact_number,
                msg_id,
                text,
                cnt,
                min_ts,
                max_ts,
            ) in divergent:
                logger.warning(
                    "dedup skipped partition with divergent timestamps "
                    "(protocol=%r, contact_number=%r, msg_id=%r, text=%r, "
                    "rows=%d, min_ts=%d, max_ts=%d)",
                    protocol,
                    contact_number,
                    msg_id,
                    text,
                    cnt,
                    min_ts,
                    max_ts,
                )
            # CTE: for every (protocol, contact_number, msg_id, text) group,
            # order by status rank descending and rowid ascending, then delete
            # every row past the first one — but ONLY when the partition's
            # timestamp range fits within the echo window (see guard above).
            conn.execute(
                """
                WITH ranked AS (
                    SELECT
                        rowid,
                        ROW_NUMBER() OVER (
                            PARTITION BY protocol, contact_number, msg_id, text
                            ORDER BY
                                CASE status
                                WHEN 'pending' THEN 0 WHEN 'failed' THEN 0
                                WHEN 'sent' THEN 1 WHEN 'delivered' THEN 2
                                WHEN 'read' THEN 3 ELSE 0
                                END DESC,
                                rowid ASC
                        ) AS rn,
                        MIN(timestamp) OVER (
                            PARTITION BY protocol, contact_number, msg_id, text
                        ) AS min_ts,
                        MAX(timestamp) OVER (
                            PARTITION BY protocol, contact_number, msg_id, text
                        ) AS max_ts
                    FROM messages
                    WHERE msg_id IS NOT NULL AND msg_id != ''
                )
                DELETE FROM messages
                WHERE rowid IN (
                    SELECT rowid FROM ranked
                    WHERE rn > 1 AND (max_ts - min_ts) <= ?
                )
                """,
                (_ECHO_MATCH_WINDOW_MS,),
            )
            conn.commit()
            after = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            return before - after
        finally:
            conn.close()


def _count_unread() -> dict[str, int]:
    """Count unread messages per contact."""
    _init_db()
    with _DB_LOCK:
        conn = sqlite3.connect(_backend.DB_FILE)
        try:
            rows = conn.execute(
                "SELECT contact_number, COUNT(*) as cnt FROM messages "
                "WHERE is_mine = 0 AND read = 0 GROUP BY contact_number"
            ).fetchall()
        finally:
            conn.close()
    return {row[0]: row[1] for row in rows}
