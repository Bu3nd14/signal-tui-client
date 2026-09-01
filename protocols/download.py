"""
Temporary HTTP download server and file-serving helpers for the Signal TUI Client.

Serves message text and Signal attachments via a persistent local HTTP server,
so remote terminal sessions can fetch them.  No Textual dependency.
"""

import http.server
import logging
import os
import socket
import socketserver
import threading
from pathlib import Path

from .db import CACHE_DIR
from .rpc import get_attachment_path

logger = logging.getLogger(__name__)

# ─── Download server (temporary HTTP) ───────────────────────────────────────

DOWNLOAD_PORT = 10042
_DOWNLOAD_SERVER: socketserver.TCPServer | None = None
_DOWNLOAD_URL_BASE: str | None = None
_TEMP_DOWNLOAD_DIR: Path | None = None


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
    except Exception as _e:
        logger.debug("Failed to determine local IP, using 127.0.0.1", exc_info=True)
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


def _serve_file_path(att_path: Path) -> str:
    """Serve a local file via the persistent HTTP server.

    The file is symlinked (or copied) into the temp download directory
    under its original name, so the URL preserves the filename.

    Returns the full download URL string.
    """
    url_base = _ensure_download_server()
    dl_dir = _get_temp_download_dir()
    _clean_download_dir()

    link_path = dl_dir / att_path.name
    try:
        link_path.symlink_to(att_path)
    except OSError:
        import shutil

        shutil.copy2(att_path, link_path)

    return f"{url_base}/{att_path.name}"


def serve_attachment_for_download(attachment_id: str) -> str:
    """Serve a Signal attachment file via the persistent HTTP server.

    Resolves *attachment_id* to a local file via Signal's attachment store,
    then symlinks/copies it into the temp download directory.

    Returns the full download URL, or an error message prefixed with ``ERROR:``.
    """
    att_path = get_attachment_path(attachment_id)
    if att_path is None:
        return f"ERROR: Attachment file not found on server (id={attachment_id})"

    return _serve_file_path(att_path)


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
