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


# ─── Message cache ──────────────────────────────────────────────────────────

def _ensure_cache_dir():
    """Create the cache directory if it doesn't exist."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _load_cache() -> dict[str, list[dict]]:
    """Load all messages from cache."""
    _ensure_cache_dir()
    if not CACHE_FILE.exists():
        return {}
    try:
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(data: dict[str, list[dict]]):
    """Save all messages to cache."""
    _ensure_cache_dir()
    with open(CACHE_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _get_cached_messages(contact_number: str) -> list[dict]:
    """Return cached messages for a contact."""
    cache = _load_cache()
    return cache.get(contact_number, [])


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
):
    """Add a message to the cache.
    msg_type: "text", "image", "sticker", "attachment"
    attachment_info: additional details (filename, sticker emoji, etc.)
    attachment_id: signal-cli attachment UUID for resolving the file on disk.
    """
    cache = _load_cache()
    if contact_number not in cache:
        cache[contact_number] = []
    cache[contact_number].append({
        "text": text,
        "is_mine": is_mine,
        "sender": sender,
        "timestamp": timestamp,
        "quote_text": quote_text,
        "msg_type": msg_type,
        "attachment_info": attachment_info,
        "attachment_id": attachment_id,
        "read": is_mine,  # our messages are already read
        "status": "sent" if is_mine else "read",
    })
    _save_cache(cache)
    _prune_cache()


def _prune_cache():
    """Remove messages older than CACHE_RETENTION_DAYS days
    and limit to 200 messages per contact."""
    cache = _load_cache()
    now_ms = int(time.time() * 1000)
    cutoff = now_ms - CACHE_RETENTION_DAYS * 24 * 60 * 60 * 1000
    modified = False

    for contact in list(cache.keys()):
        # Remove old messages
        before = len(cache[contact])
        cache[contact] = [m for m in cache[contact] if m.get("timestamp", 0) >= cutoff]
        after = len(cache[contact])
        if before != after:
            modified = True

        # Limit to 200 messages per contact
        if len(cache[contact]) > 200:
            cache[contact] = cache[contact][-200:]
            modified = True

        if not cache[contact]:
            del cache[contact]
            modified = True

    if modified:
        _write_cache(cache)


def _write_cache(data: dict[str, list[dict]]):
    """Write cache to disk (without calling _prune_cache)."""
    _ensure_cache_dir()
    with open(CACHE_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _mark_as_read(contact_number: str):
    """Mark all messages for a contact as read."""
    cache = _load_cache()
    if contact_number in cache:
        modified = False
        for msg in cache[contact_number]:
            if not msg.get("read", True):
                msg["read"] = True
                modified = True
        if modified:
            _save_cache(cache)


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
    if source in cache:
        for msg in cache[source]:
            ts = msg.get("timestamp", 0)
            if ts in timestamps and msg.get("is_mine", False):
                old_status = msg.get("status", "sent")
                # Only upgrade status: sent → delivered → read
                if (old_status == "sent" and new_status in ("delivered", "read")) or \
                   (old_status == "delivered" and new_status == "read"):
                    msg["status"] = new_status
                    updated_messages.append(msg)

    return updated_messages


def _count_unread() -> dict[str, int]:
    """Count unread messages per contact.
    Messages without 'read' field (old cache) are considered read."""
    cache = _load_cache()
    counts = {}
    for number, messages in cache.items():
        unread = sum(
            1 for m in messages
            if not m.get("is_mine") and not m.get("read", True)
        )
        if unread > 0:
            counts[number] = unread
    return counts


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
