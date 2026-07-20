"""
Backend for Signal TUI Client.
Handles communication with signal-cli (JSON-RPC over HTTP or subprocess),
message cache on disk, and the Contact data model.
No Textual dependency.
"""

import json
import os
import subprocess
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
):
    """Add a message to the cache.
    msg_type: "text", "image", "sticker", "attachment"
    attachment_info: additional details (filename, sticker emoji, etc.)
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
        "read": is_mine,  # our messages are already read
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

    def send_message(self, message: str, recipient: str) -> dict:
        """Send a message to a recipient."""
        params = {
            "message": message,
            "recipient": [recipient],
        }
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
