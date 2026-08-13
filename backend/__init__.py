"""
Backend package for the Signal TUI Client.

Handles communication with signal-cli (JSON-RPC over HTTP or subprocess),
the SQLite message cache, the temporary download HTTP server, the WAHA
webhook server and the ``Contact`` data model.  No Textual dependency.

This package re-exports the full public (and historical private) API that used
to live in the single ``backend.py`` module, so ``from backend import X`` and
``import backend; backend.X`` keep working unchanged.
"""

from .rpc import (
    PROJECT_DIR,
    USER_NUMBER,
    DAEMON_HTTP_PORT,
    DAEMON_URL,
    SSE_URL,
    SIGNAL_CLI_ATTACHMENTS_DIR,
    SIGNAL_CLI_PATH,
    SignalRPCClient,
    Contact,
    _get_user_number,
    _find_signal_cli,
    find_signal_cli,
    _is_daemon_running,
    _run_subprocess,
    _send_subprocess,
    get_attachment_path,
    _process_typing,
    _process_receipt,
)
from .db import (
    CACHE_DIR,
    CACHE_FILE,
    DB_FILE,
    CACHE_RETENTION_DAYS,
    _DB_LOCK,
    _ensure_cache_dir,
    _migrate_protocol_schema,
    _init_db,
    _load_cache,
    _add_message_to_cache,
    _update_message_id,
    _prune_cache,
    _mark_as_read,
    _dedup_messages,
    _update_message_status,
    _count_unread,
)
from .download import (
    DOWNLOAD_PORT,
    _DOWNLOAD_SERVER,
    _DOWNLOAD_URL_BASE,
    _TEMP_DOWNLOAD_DIR,
    _get_temp_download_dir,
    get_local_ip,
    _DownloadHTTPHandler,
    _ensure_download_server,
    _clean_download_dir,
    _serve_file_path,
    serve_attachment_for_download,
    serve_text_as_file,
)
from .webhook import (
    WEBHOOK_PORT,
    _WEBHOOK_SERVER,
    _WebhookHTTPHandler,
    ensure_webhook_server,
)

__all__ = [
    # rpc
    "PROJECT_DIR",
    "USER_NUMBER",
    "DAEMON_HTTP_PORT",
    "DAEMON_URL",
    "SSE_URL",
    "SIGNAL_CLI_ATTACHMENTS_DIR",
    "SIGNAL_CLI_PATH",
    "SignalRPCClient",
    "Contact",
    "_get_user_number",
    "_find_signal_cli",
    "find_signal_cli",
    "_is_daemon_running",
    "_run_subprocess",
    "_send_subprocess",
    "get_attachment_path",
    "_process_typing",
    "_process_receipt",
    # db
    "CACHE_DIR",
    "CACHE_FILE",
    "DB_FILE",
    "CACHE_RETENTION_DAYS",
    "_DB_LOCK",
    "_ensure_cache_dir",
    "_migrate_protocol_schema",
    "_init_db",
    "_load_cache",
    "_add_message_to_cache",
    "_update_message_id",
    "_prune_cache",
    "_mark_as_read",
    "_dedup_messages",
    "_update_message_status",
    "_count_unread",
    # download
    "DOWNLOAD_PORT",
    "_DOWNLOAD_SERVER",
    "_DOWNLOAD_URL_BASE",
    "_TEMP_DOWNLOAD_DIR",
    "_get_temp_download_dir",
    "get_local_ip",
    "_DownloadHTTPHandler",
    "_ensure_download_server",
    "_clean_download_dir",
    "_serve_file_path",
    "serve_attachment_for_download",
    "serve_text_as_file",
    # webhook
    "WEBHOOK_PORT",
    "_WEBHOOK_SERVER",
    "_WebhookHTTPHandler",
    "ensure_webhook_server",
]
