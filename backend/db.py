"""
Message cache persistence (SQLite) for the Signal TUI Client.

Stores messages per (protocol, contact) in a local SQLite database so chats
persist across sessions.  Handles schema migration, incremental inserts,
dedup, read receipts and unread counts.  No Textual dependency.
"""

import sqlite3
import threading
from pathlib import Path

import backend as _backend

CACHE_DIR = Path.home() / ".local" / "share" / "signal-tui-client"
CACHE_FILE = CACHE_DIR / "messages.json"
DB_FILE = CACHE_DIR / "messages.db"
CACHE_RETENTION_DAYS = 3

# Current schema version, persisted via ``PRAGMA user_version`` so the legacy
# migration below is skipped once the schema is known to be up to date.
_SCHEMA_VERSION = 1


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
    if _current_schema_version(conn) >= _SCHEMA_VERSION:
        return

    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}

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
                    read INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'read'
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
    """
    _init_db()
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
    protocol: str = "signal",
    msg_id: str | None = None,
):
    """Add a message to the SQLite cache (incremental INSERT).
    msg_type: "text", "image", "sticker", "attachment"
    attachment_info: additional details (filename, sticker emoji, etc.)
    attachment_id: signal-cli attachment UUID for resolving the file on disk.
    protocol: source protocol ("signal", "whatsapp", ...). Defaults to signal
        for backward compatibility.
    msg_id: stable per-message id (e.g. the Baileys WhatsApp message id).
        Persisting it lets the id-based dedup work across sessions.
    """
    _init_db()
    with _DB_LOCK:
        conn = sqlite3.connect(_backend.DB_FILE)
        try:
            conn.execute(
                """INSERT INTO messages
                   (protocol, contact_number, text, is_mine, sender, timestamp,
                    quote_text, msg_type, attachment_info, attachment_id,
                    read, status, msg_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    int(is_mine),
                    "sent" if is_mine else "read",
                    msg_id,
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
):
    """Attach a real message id to an existing (optimistic) row.

    When the echo of an optimistic send arrives with its real id, the row that
    was inserted optimistically (``msg_id IS NULL`` or the legacy ``msg_id = ''``
    used by the Telegram backend) is updated in place instead of inserting a
    duplicate.  Matching is by ``(protocol, contact_number, text, is_mine)`` on
    the id-less row.
    """
    _init_db()
    with _DB_LOCK:
        conn = sqlite3.connect(_backend.DB_FILE)
        try:
            conn.execute(
                "UPDATE messages SET msg_id = ?, timestamp = ? "
                "WHERE protocol = ? AND contact_number = ? AND text = ? "
                "AND is_mine = ? AND (msg_id IS NULL OR msg_id = '')",
                (msg_id, timestamp, protocol, contact_number, text, int(is_mine)),
            )
            conn.commit()
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
    timestamp: int, status: str, protocol: str, contact_number: str
):
    """Update a message status in SQLite, scoped per (protocol, contact, ts).

    A bare ``timestamp`` match would update messages of OTHER protocols or
    contacts sharing the same millisecond timestamp, so the update is always
    scoped by ``protocol`` and ``contact_number``.
    """
    _init_db()
    with _DB_LOCK:
        conn = sqlite3.connect(_backend.DB_FILE)
        try:
            conn.execute(
                "UPDATE messages SET status = ? "
                "WHERE protocol = ? AND contact_number = ? AND timestamp = ?",
                (status, protocol, contact_number, timestamp),
            )
            conn.commit()
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
