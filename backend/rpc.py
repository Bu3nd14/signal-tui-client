"""
Signal CLI communication for the Signal TUI Client.

Wraps signal-cli via JSON-RPC over HTTP (daemon) with a subprocess fallback,
plus envelope parsing (typing / receipts), the ``Contact`` data model and
attachment-path resolution.  No Textual dependency.
"""

import json
import logging
import os
import subprocess
import urllib.request
from pathlib import Path

import backend as _backend

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

PROJECT_DIR = Path(__file__).resolve().parent.parent


_NOT_CONFIGURED_MSG = (
    "Signal phone number not configured.\n"
    "Set the SIGNAL_USER_NUMBER environment variable or create a config.json file:\n"
    '  echo \'{"user_number": "+1234567890"}\' > config.json'
)


def _get_user_number() -> str:
    """Read phone number from environment variable or config.json.

    Best-effort: returns ``""`` when not configured (does NOT raise).
    Use :func:`_require_user_number` at the point of use to get the
    canonical RuntimeError.
    """
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
    return ""


def _require_user_number() -> str:
    """Return the configured user number, or raise the canonical RuntimeError."""
    num = _get_user_number()
    if not num:
        raise RuntimeError(_NOT_CONFIGURED_MSG)
    return num


USER_NUMBER = _get_user_number()  # "" se non configurato, il numero altrimenti
DAEMON_HTTP_PORT = 8080
DAEMON_URL = f"http://127.0.0.1:{DAEMON_HTTP_PORT}/api/v1/rpc"
SSE_URL = f"http://127.0.0.1:{DAEMON_HTTP_PORT}/api/v1/events"
# Directory where signal-cli stores downloaded attachments
SIGNAL_CLI_ATTACHMENTS_DIR = (
    Path.home() / ".local" / "share" / "signal-cli" / "attachments"
)
# ─── Signal CLI ──────────────────────────────────────────────────────────────


def _find_signal_cli() -> Path | None:
    """Find the signal-cli executable in ./bin/, or ``None`` if absent.

    Non-raising: returns ``None`` when ./bin/ is missing or contains no
    signal-cli-*/bin/signal-cli executable.  Use :func:`find_signal_cli`
    at the point of use to get the canonical FileNotFoundError.
    """
    bin_dir = PROJECT_DIR / "bin"
    if not bin_dir.is_dir():  # evita il FileNotFoundError grezzo di iterdir()
        return None
    for d in bin_dir.iterdir():
        if d.is_dir() and d.name.startswith("signal-cli-"):
            exe = d / "bin" / "signal-cli"
            if exe.exists() and exe.stat().st_mode & 0o111:
                return exe
    return None


# Retro-compatibilità: Path se configurato, None se assente
# (prima l'import sollevava FileNotFoundError).
SIGNAL_CLI_PATH: Path | None = _find_signal_cli()


def find_signal_cli() -> Path:
    """Return the signal-cli path, or raise the canonical FileNotFoundError."""
    path = _find_signal_cli()
    if path is None:
        raise FileNotFoundError("signal-cli not found in ./bin/")
    return path


def _is_daemon_running() -> bool:
    """Check if the signal-cli daemon is already running."""
    try:
        rpc = _backend.SignalRPCClient()
        test = rpc._call("listContacts")
        return "result" in test
    except Exception as _e:
        logger.debug("Daemon check failed, assuming not running", exc_info=True)
        return False


def _run_subprocess(args: list[str]) -> str:
    """Run signal-cli via subprocess and return stdout.

    Risolve binario e numero utente al momento dell'uso (non all'import):
    FileNotFoundError / RuntimeError canonici se non configurati.
    """
    num = _require_user_number()  # prima il numero (stesso ordine dell'import attuale)
    cli = find_signal_cli()  # poi il binario
    result = subprocess.run(  # noqa: PLW1510 — return code checked explicitly below
        [str(cli), "-u", num] + args,
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
    edit_timestamp: int | None = None,
    quote_attachments: list[str] | None = None,
    attachments: list[str] | None = None,
) -> str:
    """Send a message via subprocess, optionally with a quote/reply or edit."""
    args = ["send", "-m", message, recipient]
    if quote_timestamp is not None:
        args.extend(["--quote-timestamp", str(quote_timestamp)])
    if quote_author is not None:
        args.extend(["--quote-author", quote_author])
    if quote_message is not None:
        args.extend(["--quote-message", quote_message])
    if edit_timestamp is not None:
        args.extend(["--edit-timestamp", str(edit_timestamp)])
    for qa in quote_attachments or []:
        args.extend(["--quote-attachment", qa])
    for attachment in attachments or []:
        args.extend(["--attachment", attachment])
    return _backend._run_subprocess(args)


# ─── Attachment helpers ─────────────────────────────────────────────────────


def get_attachment_path(attachment_id: str) -> Path | None:
    """Resolve a signal-cli attachment ID to a local file path.

    Returns the Path if the file exists and is readable, or None if the
    file is missing / inaccessible (safe fallback).
    """
    if not attachment_id:
        return None
    att_path = _backend.SIGNAL_CLI_ATTACHMENTS_DIR / attachment_id
    if not att_path.resolve().is_relative_to(
        _backend.SIGNAL_CLI_ATTACHMENTS_DIR.resolve()
    ):
        logger.warning("Rejected attachment path outside the attachments directory")
        return None
    if att_path.exists() and att_path.is_file():
        return att_path
    return None


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
            if msg.get("is_mine", False) and any(
                abs(ts - t) <= TOLERANCE_MS for t in timestamps
            ):
                old_status = msg.get("status", "sent")
                rank = {
                    "pending": 0,
                    "failed": 0,
                    "sent": 1,
                    "delivered": 2,
                    "read": 3,
                }
                if rank.get(new_status, 0) > rank.get(old_status, 0):
                    msg["status"] = new_status
                    updated_messages.append(msg)

    return updated_messages


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
        except Exception as e:  # noqa: BLE001
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
        edit_timestamp: int | None = None,
        quote_attachments: list[str] | None = None,
        attachments: list[str] | None = None,
    ) -> dict:
        """Send a message to a recipient, optionally with a quote/reply.

        Parameters
        ----------
        message:
            The message text to send.
        recipient:
            The recipient's phone number.
        timestamp:
            Client timestamp (ms) passed to signal-cli.  NOTE: signal-cli has
            no ``timestamp`` option for ``send`` and IGNORES this value — it
            assigns the real timestamp itself.  The real timestamp is available
            in ``result.timestamp`` of the JSON-RPC response (and on stdout in
            subprocess mode).  The parameter is retained for call-site symmetry,
            not because it has any effect on the send.
        quote_timestamp:
            Timestamp (ms) of the message being replied to.
        quote_author:
            Phone number of the original message's author.
        quote_message:
            Text of the original message being quoted.
        edit_timestamp:
            Timestamp (ms) of the original message to edit.  When provided,
            signal-cli edits that message instead of sending a new one.
        quote_attachments:
            List of quoted-attachment descriptors in the signal-cli
            ``contentType[:filename[:previewFile]]`` format, rendered on the
            wire as the ``quoteAttachments`` JSON-RPC parameter.
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
        if edit_timestamp is not None:
            params["editTimestamp"] = edit_timestamp
        if quote_attachments is not None:
            params["quoteAttachments"] = quote_attachments
        if attachments is not None:
            params["attachments"] = attachments
        return self._call("send", params)

    def receive(self) -> list[dict]:
        """Receive messages (legacy polling method, prefer listen_events for real-time)."""
        result = self._call("receive")
        if "error" in result:
            return []
        return result.get("result", [])

    def listen_events(self, user_number: str):
        """Connect to the signal-cli SSE endpoint and yield incoming envelopes.

        Opens a long-lived HTTP GET connection to ``/api/v1/events``
        (Server-Sent Events).  The daemon pushes new messages as they
        arrive; keep-alive ``:\\n`` comments are sent every 15 s.

        Each yielded value is a dict containing an ``envelope`` key,
        matching the same structure previously returned by ``receive()``.

        The connection is established with a 30-second socket timeout.
        If no data (keep-alive or event) is received within that window,
        the socket times out and the generator returns, allowing the
        caller to reconnect.

        Parameters
        ----------
        user_number:
            The signal-cli account phone number, used as the ``account``
            query parameter.
        """
        sse_url = SSE_URL + f"?account={user_number}"
        try:
            req = urllib.request.Request(sse_url, method="GET")
            with urllib.request.urlopen(req, timeout=30) as resp:
                current_event: dict[str, str] = {}
                for line_bytes in resp:
                    line = line_bytes.decode("utf-8").rstrip("\n").rstrip("\r")
                    if not line:
                        # Blank line → end of event
                        if "data" in current_event:
                            try:
                                data = json.loads(current_event["data"])
                                if isinstance(data, dict) and "envelope" in data:
                                    yield data
                            except json.JSONDecodeError:
                                pass
                        current_event = {}
                        continue
                    if line.startswith(":"):
                        # SSE comment / keep-alive — skip
                        continue
                    if ":" in line:
                        field, _, value = line.partition(":")
                        # Trim a single leading space per spec
                        value = value.removeprefix(" ")
                        if field not in current_event:
                            current_event[field] = value
                        else:
                            current_event[field] += "\n" + value
        except Exception as _e:
            # Connection closed, timeout, or error → caller will reconnect
            logger.debug("SSE listen ended", exc_info=True)
            return


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
