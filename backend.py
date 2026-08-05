"""
Backend for Signal TUI Client.
Handles communication with signal-cli (JSON-RPC over HTTP or subprocess),
message cache on disk, and the Contact data model.
No Textual dependency.
"""

import http.server
import json
import os
import socket
import socketserver
import sqlite3
import subprocess
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional



# ─── Constants ────────────────────────────────────────────────────────────────

PROJECT_DIR = Path(__file__).parent


def _get_user_number() -> str:
    """Read phone number from environment variable or config.json."""
    num = os.environ.get("SIGNAL_USER_NUMBER")
    if num:
        return num
    config_file = PROJECT_DIR / "config.json"
    if config_file.exists():
        try:
            with open(config_file) as f:
                cfg = json.load(f)
                num = cfg.get("user_number", "")
                if num:
                    return num
        except (json.JSONDecodeError, OSError):
            pass
    raise RuntimeError(
        "Signal phone number not configured.\n"
        "Set the SIGNAL_USER_NUMBER environment variable or create a config.json file:\n"
        '  echo \'{"user_number": "+1234567890"}\' > config.json'
    )


USER_NUMBER = _get_user_number()
DAEMON_HTTP_PORT = 8080
DAEMON_URL = f"http://127.0.0.1:{DAEMON_HTTP_PORT}/api/v1/rpc"
CACHE_DIR = Path.home() / ".local" / "share" / "signal-tui-client"
CACHE_FILE = CACHE_DIR / "messages.json"
DB_FILE = CACHE_DIR / "messages.db"
CACHE_RETENTION_DAYS = 3


# Directory where signal-cli stores downloaded attachments
SIGNAL_CLI_ATTACHMENTS_DIR = Path.home() / ".local" / "share" / "signal-cli" / "attachments"


# ─── Signal CLI ──────────────────────────────────────────────────────────────

def _find_signal_cli() -> Path:
    """Find the signal-cli executable in the ./bin/ directory of the project."""
    bin_dir = PROJECT_DIR / "bin"
    for d in bin_dir.iterdir():
        if d.is_dir() and d.name.startswith("signal-cli-"):
            exe = d / "bin" / "signal-cli"
            if exe.exists() and exe.stat().st_mode & 0o111:
                return exe
    raise FileNotFoundError("signal-cli not found in ./bin/")


SIGNAL_CLI_PATH = _find_signal_cli()


def find_signal_cli() -> Path:
    """Public utility function to find signal-cli."""
    return _find_signal_cli()


def _is_daemon_running() -> bool:
    """Check if the signal-cli daemon is already running."""
    try:
        rpc = SignalRPCClient()
        test = rpc._call("listContacts")
        return "result" in test
    except Exception:
        return False


def _run_subprocess(args: list[str]) -> str:
    """Run signal-cli via subprocess and return stdout."""
    result = subprocess.run(
        [str(SIGNAL_CLI_PATH), "-u", USER_NUMBER] + args,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"signal-cli error (code {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def _send_subprocess(
    message: str,
    recipient: str,
    quote_timestamp: int | None = None,
    quote_author: str | None = None,
    quote_message: str | None = None,
) -> str:
    """Send a message via subprocess, optionally with a quote/reply."""
    args = ["send", "-m", message, recipient]
    if quote_timestamp is not None:
        args.extend(["--quote-timestamp", str(quote_timestamp)])
    if quote_author is not None:
        args.extend(["--quote-author", quote_author])
    if quote_message is not None:
        args.extend(["--quote-message", quote_message])
    return _run_subprocess(args)


# ─── Attachment helpers ─────────────────────────────────────────────────────

def get_attachment_path(attachment_id: str) -> Optional[Path]:
    """Resolve a signal-cli attachment ID to a local file path.

    Returns the Path if the file exists and is readable, or None if the
    file is missing / inaccessible (safe fallback).
    """
    if not attachment_id:
        return None
    att_path = SIGNAL_CLI_ATTACHMENTS_DIR / attachment_id
    if att_path.exists() and att_path.is_file():
        return att_path
    return None


# ─── Message cache (SQLite) ─────────────────────────────────────────────────

# Lock to serialize concurrent SQLite writes (poll worker thread + UI thread).
_DB_LOCK = threading.RLock()


def _ensure_cache_dir():
    """Create the cache directory if it doesn't exist."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _migrate_protocol_schema(conn: sqlite3.Connection) -> None:
    """Upgrade a legacy ``messages`` table to the multi-protocol schema.

    If the table already has a ``protocol`` column this is a no-op.  When the
    column is missing (an existing database created before the multi-protocol
    refactor), it is added with a ``DEFAULT 'signal'`` so every existing
    message is assigned to the Signal protocol.  The contact index is then
    rebuilt to include the protocol prefix.

    Works on the connection passed in; the caller is responsible for
    committing / closing.
    """
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



def _init_db():
    """Create the SQLite database and schema if it doesn't exist.

    Also auto-migrates an existing (legacy) database that predates the
    multi-protocol schema by adding the ``protocol`` column, so old caches
    keep working without manual migration.
    """
    _ensure_cache_dir()
    with _DB_LOCK:
        conn = sqlite3.connect(DB_FILE)
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
        conn = sqlite3.connect(DB_FILE)
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
        cache[contact].append({
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
        })
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
        conn = sqlite3.connect(DB_FILE)
        try:
            conn.execute(
                """INSERT INTO messages
                   (protocol, contact_number, text, is_mine, sender, timestamp,
                    quote_text, msg_type, attachment_info, attachment_id,
                    read, status, msg_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (protocol, contact_number, text, int(is_mine), sender, timestamp,
                 quote_text, msg_type, attachment_info, attachment_id,
                 int(is_mine),
                 "sent" if is_mine else "read",
                 msg_id),
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

    When the echo of an optimistic send arrives with its real WhatsApp id, the
    row that was inserted optimistically (``msg_id IS NULL``) is updated in
    place instead of inserting a duplicate.  Matching is by
    ``(protocol, contact_number, text, is_mine)`` on the id-less row.
    """
    _init_db()
    with _DB_LOCK:
        conn = sqlite3.connect(DB_FILE)
        try:
            conn.execute(
                "UPDATE messages SET msg_id = ?, timestamp = ? "
                "WHERE protocol = ? AND contact_number = ? AND text = ? "
                "AND is_mine = ? AND msg_id IS NULL",
                (msg_id, timestamp, protocol, contact_number, text, int(is_mine)),
            )
            conn.commit()
        finally:
            conn.close()


def _prune_cache():
    """Remove messages older than CACHE_RETENTION_DAYS and limit to 200 per contact."""

    _init_db()
    with _DB_LOCK:
        conn = sqlite3.connect(DB_FILE)
        try:
            now_ms = int(time.time() * 1000)
            cutoff = now_ms - CACHE_RETENTION_DAYS * 24 * 60 * 60 * 1000
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
        conn = sqlite3.connect(DB_FILE)
        try:
            conn.execute(
                "UPDATE messages SET read = 1 WHERE contact_number = ? AND protocol = ?",
                (contact_number, protocol),
            )
            conn.commit()
        finally:
            conn.close()


def _update_message_status(timestamp: int, status: str):
    """Update the status of a message in SQLite by timestamp."""
    _init_db()
    with _DB_LOCK:
        conn = sqlite3.connect(DB_FILE)
        try:
            conn.execute(
                "UPDATE messages SET status = ? WHERE timestamp = ?",
                (status, timestamp),
            )
            conn.commit()
        finally:
            conn.close()



def _process_typing(envelope: dict) -> tuple[str, str] | None:
    """Extract typing-indicator data from an envelope.

    Typing indicators arrive as envelopes with a ``typingMessage`` field
    (from signal-cli's JSON-RPC daemon).  The envelope has the form::

        {
            "source": "+39...",
            "sourceNumber": "+39...",
            "sourceUuid": "...",
            "timestamp": 1234567890000,
            "typingMessage": {
                "action": "STARTED",   # or "STOPPED"
                "timestamp": 1234567890000
            }
        }

    Parameters
    ----------
    envelope:
        The full envelope dict from signal-cli.

    Returns
    -------
    tuple[str, str] | None
        A ``(contact_number, action)`` tuple where ``action`` is either
        ``"STARTED"`` or ``"STOPPED"``, or ``None`` if the envelope is not
        a typing indicator.
    """
    typing = envelope.get("typingMessage")
    if not typing:
        return None

    action = typing.get("action", "")
    if action not in ("STARTED", "STOPPED"):
        return None

    source = envelope.get("sourceNumber", "") or envelope.get("source", "")
    if not source:
        return None

    return (source, action)


def _process_receipt(envelope: dict, cache: dict) -> list[dict]:

    """Process a receiptMessage envelope and update message statuses in cache.

    Receipt messages contain delivery and read receipts for messages we sent.
    The envelope has the form:
    {
        "source": "+39...",
        "sourceNumber": "+39...",
        "sourceUuid": "...",
        "timestamp": 1234567890000,
        "receiptMessage": {
            "isDelivery": true,
            "isRead": false,
            "timestamps": [1234567890000, ...]
        }
    }

    Parameters
    ----------
    envelope:
        The full envelope dict from signal-cli.
    cache:
        The current message cache (mutated in-place).

    Returns
    -------
    list[dict]
        A list of updated message dicts (for UI refresh).
    """
    receipt = envelope.get("receiptMessage", {})
    timestamps = receipt.get("timestamps", [])
    source = envelope.get("sourceNumber", "") or envelope.get("source", "")

    if not timestamps or not source:
        return []

    updated_messages = []

    # Determine the new status based on receipt type.
    # signal-cli uses boolean fields: isDelivery, isRead, isViewed
    is_delivery = receipt.get("isDelivery", False)
    is_read = receipt.get("isRead", False)

    if is_read:
        new_status = "read"
    elif is_delivery:
        new_status = "delivered"
    else:
        return []

    # Update messages in cache for this contact
    # Usa fuzzy match con tolleranza di 1 secondo perché signal-cli a volte
    # modifica leggermente il timestamp che passiamo in send().
    TOLERANCE_MS = 1000
    if source in cache:
        for msg in cache[source]:
            ts = msg.get("timestamp", 0)
            if msg.get("is_mine", False) and any(abs(ts - t) <= TOLERANCE_MS for t in timestamps):
                old_status = msg.get("status", "sent")
                # Only upgrade status: sent → delivered → read
                if (old_status == "sent" and new_status in ("delivered", "read")) or \
                   (old_status == "delivered" and new_status == "read"):
                    msg["status"] = new_status
                    updated_messages.append(msg)

    return updated_messages


def _count_unread() -> dict[str, int]:
    """Count unread messages per contact."""
    _init_db()
    with _DB_LOCK:
        conn = sqlite3.connect(DB_FILE)
        try:
            rows = conn.execute(
                "SELECT contact_number, COUNT(*) as cnt FROM messages "
                "WHERE is_mine = 0 AND read = 0 GROUP BY contact_number"
            ).fetchall()
        finally:
            conn.close()
    return {row[0]: row[1] for row in rows}



# ─── JSON-RPC Client via HTTP ────────────────────────────────────────────────

class SignalRPCClient:
    """JSON-RPC client for communicating with signal-cli daemon over HTTP."""

    def __init__(self, url: str = DAEMON_URL):
        self.url = url
        self._req_id = 0

    def _call(self, method: str, params: dict | None = None) -> dict:
        """Execute a JSON-RPC call and return the result."""
        self._req_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._req_id,
            "method": method,
            "params": params or {},
        }
        data = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            self.url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                response_data = resp.read().decode("utf-8")
                return json.loads(response_data)
        except Exception as e:
            return {"error": str(e)}

    def list_contacts(self) -> list[dict]:
        """Fetch the contact list."""
        result = self._call("listContacts")
        if "error" in result:
            return []
        return result.get("result", [])

    def send_message(
        self,
        message: str,
        recipient: str,
        timestamp: int | None = None,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_message: str | None = None,
    ) -> dict:
        """Send a message to a recipient, optionally with a quote/reply.

        Parameters
        ----------
        message:
            The message text to send.
        recipient:
            The recipient's phone number.
        timestamp:
            Explicit timestamp (ms) to use as the message ID.
            If provided, signal-cli will use this timestamp instead of
            generating one, ensuring the receiptMessage timestamps match.
        quote_timestamp:
            Timestamp (ms) of the message being replied to.
        quote_author:
            Phone number of the original message's author.
        quote_message:
            Text of the original message being quoted.
        """
        params: dict = {
            "message": message,
            "recipient": [recipient],
        }
        if timestamp is not None:
            params["timestamp"] = timestamp
        if quote_timestamp is not None:
            params["quoteTimestamp"] = quote_timestamp
        if quote_author is not None:
            params["quoteAuthor"] = quote_author
        if quote_message is not None:
            params["quoteMessage"] = quote_message
        return self._call("send", params)

    def receive(self) -> list[dict]:
        """Receive messages."""
        result = self._call("receive")
        if "error" in result:
            return []
        return result.get("result", [])


# ─── Data model ──────────────────────────────────────────────────────────────

class Contact:
    """Represents a Signal contact."""

    def __init__(self, number: str, name: str = "", aci: str = ""):
        self.number = number
        self.name = name if name else number
        self.aci = aci

    @property
    def display_name(self) -> str:
        return self.name if self.name else self.number


# ─── Download server (temporary HTTP) ───────────────────────────────────────

DOWNLOAD_PORT = 10042
_DOWNLOAD_SERVER: Optional[socketserver.TCPServer] = None
_DOWNLOAD_URL_BASE: Optional[str] = None
_TEMP_DOWNLOAD_DIR: Optional[Path] = None


def _get_temp_download_dir() -> Path:
    """Get or create a temporary directory for serving download files."""
    global _TEMP_DOWNLOAD_DIR
    if _TEMP_DOWNLOAD_DIR is None:
        _TEMP_DOWNLOAD_DIR = CACHE_DIR / "downloads"
        _TEMP_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return _TEMP_DOWNLOAD_DIR


def get_local_ip() -> str:
    """Try to determine the local IP address reachable from the SSH client.

    Priority:
    1. Parse SSH_CONNECTION env var (set by SSH) for the server's IP.
    2. Connect to a dummy socket to learn which interface is used.
    """
    ssh_conn = os.environ.get("SSH_CONNECTION", "")
    if ssh_conn:
        parts = ssh_conn.strip().split()
        if len(parts) >= 3:
            # SSH_CONNECTION = "client_ip client_port server_ip server_port"
            return parts[2]  # server IP
    # Fallback: create a UDP socket to a non-routable address to learn our IP
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


class _DownloadHTTPHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP handler that serves files from the temp download directory.

    The server stays alive permanently; the file content is updated
    by overwriting ``download`` (or symlink) in the temp directory.
    """

    def __init__(self, *args, **kwargs):
        dl_dir = _get_temp_download_dir()
        super().__init__(*args, directory=str(dl_dir), **kwargs)

    def log_message(self, format: str, *args) -> None:
        """Suppress default HTTP log output."""
        pass


def _ensure_download_server() -> str:
    """Start the persistent download server if not already running.

    Returns the URL base (e.g. ``http://1.2.3.4:10042``).
    """
    global _DOWNLOAD_SERVER, _DOWNLOAD_URL_BASE

    if _DOWNLOAD_SERVER is not None:
        # Server already running — return the existing URL base
        assert _DOWNLOAD_URL_BASE is not None
        return _DOWNLOAD_URL_BASE

    ip = get_local_ip()
    socketserver.TCPServer.allow_reuse_address = True

    _DOWNLOAD_SERVER = socketserver.TCPServer(
        ("0.0.0.0", DOWNLOAD_PORT), _DownloadHTTPHandler
    )
    _DOWNLOAD_URL_BASE = f"http://{ip}:{DOWNLOAD_PORT}"

    t = threading.Thread(target=_DOWNLOAD_SERVER.serve_forever, daemon=True)
    t.start()

    return _DOWNLOAD_URL_BASE


# ─── WAHA webhook server (event-driven / push) ──────────────────────────────
# WAHA Core invia i messaggi in ingresso al client tramite webhook (POST a
# ``/webhook``) invece di un polling ``GET /api/messages``.  Questo server HTTP
# in thread daemon riceve quei pacchetti e li consegna al backend WhatsApp via
# ``handle_webhook``, che li normalizza e li accoda per la TUI.  Risponde subito
# ``200`` a WAHA per confermare la ricezione (evita retry e saturazione).

WEBHOOK_PORT = int(os.environ.get("CLIENT_WEBHOOK_PORT", "8088") or "8088")
_WEBHOOK_SERVER = None  # type: Optional[socketserver.TCPServer]


class _WebhookHTTPHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler that receives WAHA webhook POSTs on ``/webhook``.

    WAHA pushes ``{"event": "message", "session": "...", "payload": {...}}``.
    The handler extracts the JSON body, forwards it to the registered WhatsApp
    backend, and ALWAYS replies ``200`` (application/json) so WAHA doesn't
    retry.  Errors inside the handler are never fatal and never block the ack.
    """

    target = None  # set lazily by ensure_webhook_server()

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/webhook"):
            return self._wh_response(404)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                data = json.loads(raw or b"{}")
            except (json.JSONDecodeError, ValueError):
                return self._wh_response(400)
        except Exception:
            return self._wh_response(400)
        # Forward to the WhatsApp backend (never raises into the handler).
        try:
            target = self.target
            if target is not None and isinstance(data, dict):
                target(data)
        except Exception:
            pass
        return self._wh_response(200)

    def _wh_response(self, code: int) -> None:
        body = json.dumps({"ok": code == 200}).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except Exception:
            pass

    def log_message(self, format: str, *args) -> None:
        """Suppress default HTTP log output."""
        pass


def ensure_webhook_server(backend) -> int:
    """Start the persistent WAHA webhook server (idempotent) and bind *backend*.

    Registers ``backend.handle_webhook`` as the target for incoming webhook
    POSTs, starts the ``socketserver`` in a daemon thread if not running yet,
    and returns the client webhook port.  Safe to call multiple times (re-binds
    the current backend without restarting the socket).
    """
    global _WEBHOOK_SERVER

    if backend is not None and hasattr(backend, "handle_webhook"):
        _WebhookHTTPHandler.target = backend.handle_webhook

    if _WEBHOOK_SERVER is not None:
        return WEBHOOK_PORT

    socketserver.TCPServer.allow_reuse_address = True
    try:
        _WEBHOOK_SERVER = socketserver.TCPServer(
            ("0.0.0.0", WEBHOOK_PORT), _WebhookHTTPHandler
        )
    except OSError:
        # Porta già in uso o non bindabile: non blocchiamo l'avvio.  Il fallback
        # è che il webhook non venga consegnato (best-effort).
        return 0
    t = threading.Thread(target=_WEBHOOK_SERVER.serve_forever, daemon=True)
    t.start()
    return WEBHOOK_PORT


def _clean_download_dir(keep: str | None = None) -> None:
    """Remove all files in the temp download directory except *keep*."""
    dl_dir = _get_temp_download_dir()
    for child in dl_dir.iterdir():
        if keep is not None and child.name == keep:
            continue
        try:
            if child.is_symlink() or child.is_file():
                child.unlink()
            elif child.is_dir():
                import shutil
                shutil.rmtree(child)
        except OSError:
            pass


def serve_attachment_for_download(attachment_id: str) -> str:
    """Serve an attachment file via the persistent HTTP server.

    The file is symlinked (or copied) into the temp download directory
    under its original name, so the URL preserves the filename and
    extension (e.g. ``http://ip:10042/photo.jpg``).

    Parameters
    ----------
    attachment_id:
        The signal-cli attachment UUID.

    Returns
    -------
    str
        The full download URL, or an error message prefixed with ``ERROR:``.
    """
    att_path = get_attachment_path(attachment_id)
    if att_path is None:
        return f"ERROR: Attachment file not found on server (id={attachment_id})"

    url_base = _ensure_download_server()

    # Remove previous files, then place the new one with its original name
    dl_dir = _get_temp_download_dir()
    _clean_download_dir()

    link_path = dl_dir / att_path.name
    try:
        link_path.symlink_to(att_path)
    except OSError:
        # Symlink may fail; copy instead
        import shutil
        shutil.copy2(att_path, link_path)

    return f"{url_base}/{att_path.name}"


def serve_text_as_file(text: str, filename: str = "message.txt") -> str:
    """Write text to a temporary file and serve it via the persistent HTTP server.

    The file is written under the given ``filename``, so the URL preserves
    the name (e.g. ``http://ip:10042/signal-message-12345.txt``).

    Parameters
    ----------
    text:
        The message text to save.
    filename:
        The filename to use (default ``message.txt``).

    Returns
    -------
    str
        The full download URL, or an error message prefixed with ``ERROR:``.
    """
    url_base = _ensure_download_server()

    # Remove previous files, then write the new one
    dl_dir = _get_temp_download_dir()
    _clean_download_dir()

    file_path = dl_dir / filename
    try:
        file_path.write_text(text, encoding="utf-8")
    except OSError as e:
        return f"ERROR: Cannot write temp file: {e}"

    return f"{url_base}/{filename}"
