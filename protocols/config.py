"""
Configuration for the WhatsApp backend.

Reads settings from environment variables or the project ``config.json``:

- ``WHATSAPP_API_URL``      — base URL of the Baileys-based HTTP API
                              (e.g. ``http://127.0.0.1:3000``).
- ``WHATSAPP_API_KEY``      — API key sent as the ``X-Api-Key`` header (auto-read
                              from the project ``.env`` ``WAHA_API_KEY`` if unset).
- ``WHATSAPP_SESSION_NAME`` — name of the WhatsApp session on the API
                              (default ``"default"``).
- ``WHATSAPP_MEDIA_DIR``    — local directory where the API stores downloaded
                              media files (used by ``get_attachment_path``).

All settings are optional: when the API URL is missing/empty the client is
considered disabled and the ``BackendManager`` simply skips the WhatsApp
backend without affecting the Signal TUI client.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import shlex
import socket
import subprocess
import threading
import time
from pathlib import Path

from models import DEFAULT_THUMBNAIL_MAX_COLS, DEFAULT_THUMBNAIL_MAX_LINES

PROJECT_DIR = Path(__file__).resolve().parent.parent
logger = logging.getLogger(__name__)


def _load_dotenv() -> dict:
    """Best-effort parse of the project ``.env`` (KEY=VALUE lines).

    Returns an empty dict if the file is missing or unreadable.  This lets the
    Python client reuse the ``WAHA_API_KEY`` that docker-compose already loads
    into the WAHA container via ``env_file``, without requiring the user to
    export it twice.
    """
    env_file = PROJECT_DIR / ".env"
    if not env_file.exists():
        return {}
    data: dict[str, str] = {}
    try:
        with env_file.open() as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                data[key.strip()] = value.strip()
    except OSError:
        return {}
    return data


def _load_config() -> dict:
    """Return the parsed ``config.json`` (or an empty dict)."""
    config_file = PROJECT_DIR / "config.json"
    if not config_file.exists():
        return {}
    try:
        with open(config_file) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _get(key: str, env_name: str, default: str = "") -> str:
    """Return the env var if set, else the config.json value, else default.

    Reads ONLY ``os.environ`` and ``config.json`` — the project ``.env`` is NOT
    parsed here (unlike ``_load_dotenv`` used by a few legacy settings).
    """
    env = os.environ.get(env_name)
    if env:
        return env
    cfg = _load_config()
    value = cfg.get(key, default)
    return value if value is not None else default


def _get_int(key: str, env_name: str, default: int) -> int:
    """Return an int setting: env var, else config.json, else *default*.

    Invalid values fall back to *default*.  This helper (like ``_get`` and
    ``_get_bool``) reads ONLY ``os.environ`` and ``config.json``: it does NOT
    parse the project ``.env``.  Export the variable in the Python process or
    put it in ``config.json``; ``WAHA_*`` variables live in the WAHA container
    (separate namespace) and are not visible here.
    """
    raw = os.environ.get(env_name)
    if not raw:
        raw = _load_config().get(key, default)
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def _get_bool(key: str, env_name: str, default: bool) -> bool:
    """Return a bool setting: env var, else config.json, else *default*.

    Truthy values are ``1``/``true``/``yes``/``on`` and falsy values are
    ``0``/``false``/``no``/``off`` (case-insensitive, trimmed); anything else
    (including unrecognised values) falls back to *default*.

    Reads ONLY ``os.environ`` and ``config.json`` (like ``_get``/
    ``_get_int``): the project ``.env`` is NOT parsed here.
    """
    raw = os.environ.get(env_name)
    if raw is None or str(raw).strip() == "":
        raw = _load_config().get(key, default)
    if str(raw).strip().lower() in ("1", "true", "yes", "on"):
        return True
    if str(raw).strip().lower() in ("0", "false", "no", "off"):
        return False
    return default


def get_whatsapp_api_url() -> str:
    """Return the configured WhatsApp API base URL, or ``""`` if not set."""
    return _get("whatsapp_api_url", "WHATSAPP_API_URL").strip().rstrip("/")


def get_whatsapp_api_port() -> int:
    """Return the host port of the local WAHA service (default ``3005``)."""
    raw = os.environ.get("WHATSAPP_API_PORT", "3005") or "3005"
    try:
        return int(raw)
    except ValueError:
        return 3005


def resolve_whatsapp_api_url() -> str:
    """Return the URL to use for WhatsApp.

    Prefers an explicitly configured ``WHATSAPP_API_URL``/``whatsapp_api_url``;
    otherwise falls back to the local WAHA default ``http://127.0.0.1:{port}``
    (port from ``WHATSAPP_API_PORT``, default 3005).
    """
    configured = get_whatsapp_api_url()
    if configured:
        return configured
    return f"http://127.0.0.1:{get_whatsapp_api_port()}"


def _local_waha_reachable(timeout: float = 1.0) -> bool:
    """Return True if a WhatsApp HTTP API is reachable on the local port."""
    try:
        with socket.create_connection(
            ("127.0.0.1", get_whatsapp_api_port()), timeout=timeout
        ):
            return True
    except OSError:
        return False


def get_whatsapp_api_key() -> str:
    """Return the WAHA API key used for the ``X-Api-Key`` header.

    Read from (in order):
    1. the ``WHATSAPP_API_KEY`` environment variable,
    2. the ``whatsapp_api_key`` field in ``config.json``,
    3. the ``WAHA_API_KEY`` line in the project ``.env`` file.

    Returns ``\"\"`` if none is configured.  WAHA REST returns ``401`` for any
    request without a valid key, so a non-empty value is required once the
    container has been started with ``docker compose up -d``.
    """
    env = os.environ.get("WHATSAPP_API_KEY")
    if env:
        return env.strip()
    cfg = _load_config()
    value = cfg.get("whatsapp_api_key")
    if value:
        return str(value).strip()
    return _load_dotenv().get("WAHA_API_KEY", "").strip()


def get_whatsapp_session_name() -> str:
    """Return the WhatsApp session name (default ``"default"``)."""
    value = _get("whatsapp_session_name", "WHATSAPP_SESSION_NAME", "default").strip()
    return value or "default"


def get_whatsapp_media_dir() -> str:
    """Return the local media download directory (may be empty)."""
    return _get("whatsapp_media_dir", "WHATSAPP_MEDIA_DIR").strip()


def whatsapp_enabled() -> bool:
    """Return True if the WhatsApp backend should be registered.

    Enables when an API URL is explicitly configured *or* a local WAHA
    service is already reachable on the default port (auto-detect).
    """
    if get_whatsapp_api_url():
        return True
    return _local_waha_reachable()


def get_whatsapp_webhook_port() -> int:
    """Return the client webhook listen port (default ``8088``).

    Read from the ``CLIENT_WEBHOOK_PORT`` environment variable (default 8088),
    the same value used by ``protocols.webhook.WEBHOOK_PORT`` and the WAHA webhook URL
    declared in ``docker-compose.yml``.
    """
    raw = os.environ.get("CLIENT_WEBHOOK_PORT", "8088") or "8088"
    try:
        return int(raw)
    except ValueError:
        return 8088


WHATSAPP_WEBHOOK_EVENTS = (
    "message",
    "message.any",
    "message.ack",
    "message.ack.group",
    "presence.update",
    "message.reaction",
    "chat.archive",
)


def get_whatsapp_webhook_url() -> str:
    """Return the URL WAHA must POST to for this client's webhook.

    Respects an explicitly configured ``WAHA_WEBHOOK_URL`` (consistent with
    docker-compose); otherwise builds the default using ``host.docker.internal``
    (the host gateway from inside the WAHA container) plus the configured
    ``CLIENT_WEBHOOK_PORT``: ``http://host.docker.internal:{port}/webhook``.
    """
    explicit = os.environ.get("WAHA_WEBHOOK_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    return f"http://host.docker.internal:{get_whatsapp_webhook_port()}/webhook"


# ─── Telegram configuration ────────────────────────────────────────────────


def get_telegram_api_id() -> int:
    """Read ``TELEGRAM_API_ID`` from env, config.json, or .env.

    Returns 0 if not configured, which effectively disables Telegram.
    """
    raw = os.environ.get("TELEGRAM_API_ID", "")
    if raw:
        try:
            return int(raw)
        except ValueError:
            return 0
    cfg = _load_config()
    val = cfg.get("telegram_api_id", 0)
    try:
        if int(val):
            return int(val)
    except (ValueError, TypeError):
        pass
    # Fallback: .env file (same pattern as WhatsApp config)
    dotenv = _load_dotenv()
    raw = dotenv.get("TELEGRAM_API_ID", "")
    try:
        return int(raw) if raw else 0
    except (ValueError, TypeError):
        return 0


def get_message_retention_per_contact() -> int:
    """Return the per-contact history cap (default ``300``; ``0`` disables it)."""
    raw = os.environ.get("MESSAGE_RETENTION_PER_CONTACT", "")
    if raw:
        try:
            return int(raw)
        except (ValueError, TypeError):
            return 300

    cfg = _load_config()
    if "message_retention_per_contact" in cfg:
        try:
            return int(cfg["message_retention_per_contact"])
        except (ValueError, TypeError):
            return 300

    raw = _load_dotenv().get("MESSAGE_RETENTION_PER_CONTACT", "")
    try:
        return int(raw) if raw else 300
    except (ValueError, TypeError):
        return 300


def get_telegram_api_hash() -> str:
    """Read ``TELEGRAM_API_HASH`` from env, config.json, or .env."""
    raw = os.environ.get("TELEGRAM_API_HASH", "")
    if raw:
        return raw.strip()
    cfg = _load_config()
    val = cfg.get("telegram_api_hash", "")
    if val:
        return str(val).strip()
    # Fallback: .env file
    dotenv = _load_dotenv()
    return dotenv.get("TELEGRAM_API_HASH", "").strip()


def get_telegram_session_path() -> Path:
    """Return the path where the Telethon ``.session`` file is stored.

    Uses ``XDG_DATA_HOME`` (default ``~/.local/share``) +
    ``signal-tui-client/telegram.session``.
    """
    data_dir = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return data_dir / "signal-tui-client" / "telegram.session"


def telegram_enabled() -> bool:
    """Return True if Telegram credentials are configured."""
    return get_telegram_api_id() != 0 and bool(get_telegram_api_hash())


def signal_enabled() -> bool:
    """Return whether the Signal backend should auto-connect at boot.

    Explicit ``SIGNAL_ENABLED`` (environment, ``config.json``
    ``signal_enabled`` or the project ``.env``) wins.  Otherwise Signal is
    enabled only when a user number is configured (``SIGNAL_USER_NUMBER`` or
    ``config.json`` ``user_number``): a WhatsApp/Telegram-only deployment then
    skips the signal-cli daemon startup (and its retry noise) without removing
    the always-registered Signal backend.  Use ``SIGNAL_ENABLED=0`` to disable
    auto-connect even when a number is configured.
    """
    explicit = os.environ.get("SIGNAL_ENABLED")
    if explicit is None:
        cfg = _load_config()
        if "signal_enabled" in cfg:
            explicit = cfg.get("signal_enabled")
    if explicit is None:
        explicit = _load_dotenv().get("SIGNAL_ENABLED")
    if explicit is not None and str(explicit).strip() != "":
        return str(explicit).strip().lower() in ("1", "true", "yes", "on")
    number = os.environ.get("SIGNAL_USER_NUMBER") or _load_config().get("user_number")
    return bool(str(number or "").strip())


# ─── Address book / picker configuration ────────────────────────────────────


def get_address_book_ttl_s() -> int:
    """Return the address-book cache TTL in seconds (default ``300``)."""
    return _get_int("address_book_ttl_s", "ADDRESS_BOOK_TTL_S", 300)


def get_dynamic_refresh_interval_s() -> int:
    """Return the dynamic address-book refresh interval in seconds (default ``300``)."""
    return _get_int("dynamic_refresh_interval_s", "DYNAMIC_REFRESH_INTERVAL_S", 300)


def get_dynamic_refresh_cooldown_s() -> int:
    """Return the dynamic address-book lazy-refresh cooldown in seconds (default ``60``)."""
    return _get_int("dynamic_refresh_cooldown_s", "DYNAMIC_REFRESH_COOLDOWN_S", 60)


def get_wa_lid_cache_ttl_days() -> int:
    """Return the WhatsApp ``@lid``→number cache TTL in days (default ``30``)."""
    return _get_int("wa_lid_cache_ttl_days", "WA_LID_CACHE_TTL_DAYS", 30)


def get_wa_lid_retry_cooldown_s() -> int:
    """Cooldown di ri-armo per lid non risolti (default 300s)."""
    return _get_int("wa_lid_retry_cooldown_s", "WA_LID_RETRY_COOLDOWN_S", 300)


def get_wa_resync_download_media() -> bool:
    """Download+resolve media during the startup bulk resync (default False).

    This is the functional rollback switch: ``true`` restores
    ``downloadMedia=true`` and the media resolver for the bulk fetch (S2
    disabled).  Reads ``WA_RESYNC_DOWNLOAD_MEDIA`` (case-insensitive, trimmed)
    or ``config.json`` ``wa_resync_download_media`` — never ``.env``.
    """
    return _get_bool("wa_resync_download_media", "WA_RESYNC_DOWNLOAD_MEDIA", False)


def get_wa_resync_max_chats() -> int:
    """Max chats fetched by the startup bulk resync (default ``20``).

    ``0`` (exactly zero) means unlimited.  Negative values fall back to the
    default ``20``.  Reads ``WA_RESYNC_MAX_CHATS`` or ``config.json``
    ``wa_resync_max_chats`` — never ``.env``.
    """
    value = _get_int("wa_resync_max_chats", "WA_RESYNC_MAX_CHATS", 20)
    if value < 0:
        return 20
    return value


def get_wa_resync_limit() -> int:
    """Per-chat message limit of the startup bulk resync (default ``30``).

    Values ``<= 0`` fall back to ``30``.  Reads ``WA_RESYNC_LIMIT`` or
    ``config.json`` ``wa_resync_limit`` — never ``.env``.
    """
    value = _get_int("wa_resync_limit", "WA_RESYNC_LIMIT", 30)
    return value if value > 0 else 30


def get_wa_resync_workers() -> int:
    """Concurrent fetch workers of the startup bulk resync (default ``2``).

    Values ``<= 0`` fall back to ``2``.  Reads ``WA_RESYNC_WORKERS`` or
    ``config.json`` ``wa_resync_workers`` — never ``.env``.
    """
    value = _get_int("wa_resync_workers", "WA_RESYNC_WORKERS", 2)
    return value if value > 0 else 2


def get_wa_session_guard() -> bool:
    """Whether the WAHA session guard runs (default ``True``).

    Reads ``WA_SESSION_GUARD`` (``0``/``false``/``no``/``off`` disable it) or
    ``config.json`` ``wa_session_guard`` — never ``.env``.
    """
    return _get_bool("wa_session_guard", "WA_SESSION_GUARD", True)


def get_wa_session_poll_s() -> int:
    """Session guard poll interval in seconds (default ``60``).

    Values ``<= 0`` fall back to ``60``.  Reads ``WA_SESSION_POLL_S`` or
    ``config.json`` ``wa_session_poll_s`` — never ``.env``.
    """
    value = _get_int("wa_session_poll_s", "WA_SESSION_POLL_S", 60)
    return value if value > 0 else 60


def get_wa_session_restart_timeout_s() -> int:
    """Max seconds to wait for a restarted session to leave STARTING (default ``120``).

    Values ``<= 0`` fall back to ``120``.  Reads ``WA_SESSION_RESTART_TIMEOUT_S``
    or ``config.json`` ``wa_session_restart_timeout_s`` — never ``.env``.
    """
    value = _get_int(
        "wa_session_restart_timeout_s", "WA_SESSION_RESTART_TIMEOUT_S", 120
    )
    return value if value > 0 else 120


def get_wa_session_max_restarts_per_hour() -> int:
    """Max automatic session restarts per hour (default ``3``).

    ``0`` (exactly zero) means never restart.  Negative values fall back to
    ``3``.  Reads ``WA_SESSION_MAX_RESTARTS_PER_HOUR`` or ``config.json``
    ``wa_session_max_restarts_per_hour`` — never ``.env``.
    """
    value = _get_int(
        "wa_session_max_restarts_per_hour", "WA_SESSION_MAX_RESTARTS_PER_HOUR", 3
    )
    return value if value >= 0 else 3


_WA_SESSION_BACKOFF_DEFAULT = [5, 30, 120, 300]


def get_wa_session_backoff_s() -> list[int]:
    """Backoff (seconds) between consecutive restarts (default ``5,30,120,300``).

    Comma-separated positive ints; if empty, any part is not an int or any
    value is ``<= 0`` the default is used.  Reads ``WA_SESSION_BACKOFF_S`` or
    ``config.json`` ``wa_session_backoff_s`` — never ``.env``.
    """
    raw = str(_get("wa_session_backoff_s", "WA_SESSION_BACKOFF_S", "")).strip()
    if not raw:
        return list(_WA_SESSION_BACKOFF_DEFAULT)
    try:
        values = [int(part.strip()) for part in raw.split(",")]
    except ValueError:
        return list(_WA_SESSION_BACKOFF_DEFAULT)
    if any(v <= 0 for v in values):
        return list(_WA_SESSION_BACKOFF_DEFAULT)
    return values


def get_wa_session_stable_reset_s() -> int:
    """Seconds of continuous WORKING before the restart backoff resets (default ``600``).

    Values ``<= 0`` fall back to ``600``.  Reads ``WA_SESSION_STABLE_RESET_S``
    or ``config.json`` ``wa_session_stable_reset_s`` — never ``.env``.
    """
    value = _get_int("wa_session_stable_reset_s", "WA_SESSION_STABLE_RESET_S", 600)
    return value if value > 0 else 600


def get_wa_session_failed_grace_s() -> int:
    """Seconds FAILED must persist before a restart (default ``10``).

    ``0`` is allowed (restart on first observation).  Negative values fall back
    to ``10``.  Reads ``WA_SESSION_FAILED_GRACE_S`` or ``config.json``
    ``wa_session_failed_grace_s`` — never ``.env``.
    """
    value = _get_int("wa_session_failed_grace_s", "WA_SESSION_FAILED_GRACE_S", 10)
    return value if value >= 0 else 10


def get_wa_session_unreachable_after() -> int:
    """Consecutive failed polls before WAHA is reported unreachable (default ``5``).

    Values ``<= 0`` fall back to ``5``.  Reads ``WA_SESSION_UNREACHABLE_AFTER``
    or ``config.json`` ``wa_session_unreachable_after`` — never ``.env``.
    """
    value = _get_int("wa_session_unreachable_after", "WA_SESSION_UNREACHABLE_AFTER", 5)
    return value if value > 0 else 5


def get_picker_max_results() -> int:
    """Return the max number of rendered picker results (default ``50``)."""
    return _get_int("picker_max_results", "PICKER_MAX_RESULTS", 50)


def get_picker_preferred_backend() -> str:
    """Return the preferred picker backend, or ``""`` (most recent) if unset."""
    return _get("picker_preferred_backend", "PICKER_PREFERRED_BACKEND").strip()


# ─── Image rendering configuration ──────────────────────────────────────────


def image_protocol() -> str:
    """Return the desired image protocol (default ``"auto"``).

    One of ``auto``, ``kitty``, ``catimg`` or ``off``, read from the
    ``IMAGE_PROTOCOL`` environment variable then the ``image_protocol`` key in
    ``config.json`` (same resolution order as the other getters in this file).
    """
    value = _get("image_protocol", "IMAGE_PROTOCOL", "auto").strip().lower()
    return value or "auto"


def thumbnail_max_lines() -> int:
    """Return the max thumbnail height in lines (default ``12``)."""
    return _get_int(
        "thumbnail_max_lines", "THUMBNAIL_MAX_LINES", DEFAULT_THUMBNAIL_MAX_LINES
    )


def thumbnail_max_cols() -> int:
    """Return the max thumbnail width in columns (default ``60``)."""
    return _get_int(
        "thumbnail_max_cols", "THUMBNAIL_MAX_COLS", DEFAULT_THUMBNAIL_MAX_COLS
    )


# ─── Optional web reader configuration ──────────────────────────────────────


def _web_config() -> dict:
    """Return the ``web`` config section, or an empty dict."""
    value = _load_config().get("web", {})
    return value if isinstance(value, dict) else {}


def web_enabled() -> bool:
    """Return whether the optional web reader is enabled (default off)."""
    value = _web_config().get("enabled", False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return value is True


def web_port() -> int:
    """Return the web reader port (default ``4242``)."""
    try:
        port = int(_web_config().get("port", 4242))
    except (TypeError, ValueError):
        return 4242
    return port if 1 <= port <= 65535 else 4242


def web_host() -> str:
    """Return the web reader bind host (default ``127.0.0.1``).

    ``127.0.0.1`` keeps the web server local-only; set ``web.host`` in the
    config (or use ``--web-host``) to bind on another interface, e.g.
    ``0.0.0.0`` when exposing it over a VPN or tunnel.  Several interfaces
    are a list (``["127.0.0.1", "192.168.1.10"]``) or a comma-separated
    string; either is returned comma-separated.
    """
    value = _web_config().get("host", "127.0.0.1")
    if value is None:
        return "127.0.0.1"
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value) or "127.0.0.1"
    return str(value)


def web_token() -> str:
    """Return the web Bearer token from env or the ``web`` config section."""
    env_token = os.environ.get("SIGNAL_TUI_WEB_TOKEN")
    if env_token:
        return env_token
    value = _web_config().get("token", "")
    return str(value) if value is not None else ""


# ─── Gmail configuration ───────────────────────────────────────────────────


def _get_gmail(key: str, env_name: str, default: str = "") -> str:
    """Resolve a Gmail setting: environment, config.json, then ``.env``.

    Whitespace-only values are treated as unset so an empty ``GMAIL_ADDRESS``
    line in ``.env`` does not shadow a value from another source.
    """
    env = os.environ.get(env_name)
    if env is not None and env.strip():
        return env.strip()
    cfg = _load_config().get(key)
    if cfg is not None and str(cfg).strip():
        return str(cfg).strip()
    dotenv = _load_dotenv().get(env_name)
    if dotenv is not None and dotenv.strip():
        return dotenv.strip()
    return default


def _get_gmail_int(key: str, env_name: str, default: int) -> int:
    """Return an integer Gmail setting, falling back to *default* if invalid."""
    raw = _get_gmail(key, env_name, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def get_gmail_address() -> str:
    """Return the Gmail address used as SMTP ``From`` and IMAP login."""
    return _get_gmail("gmail_address", "GMAIL_ADDRESS")


def get_gmail_app_password_file() -> str:
    """Return the configured secret file path (``GMAIL_APP_PASSWORD_FILE``), or ``""``."""
    return _get_gmail("gmail_app_password_file", "GMAIL_APP_PASSWORD_FILE")


def get_gmail_app_password_cmd() -> str:
    """Return the configured secret command (``GMAIL_APP_PASSWORD_CMD``), or ``""``."""
    return _get_gmail("gmail_app_password_cmd", "GMAIL_APP_PASSWORD_CMD")


def _gmail_secret_path(raw: str) -> Path:
    """Resolve a secret file path, relative paths live under the project."""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else PROJECT_DIR / path


_GMAIL_SECRET_CACHE_TTL_S = 300
_GMAIL_SECRET_CMD_LABEL = "gmail_secret_cmd"
_GMAIL_SECRET_FILE_LABEL = "gmail_secret_file"

# Only the output of ``GMAIL_APP_PASSWORD_CMD`` (TPM/GPG) is cached: file and
# plaintext sources are sub-millisecond and must stay fresh on every call.
_gmail_secret_cache: dict[str, tuple[float, str]] = {}
_gmail_secret_cache_gen = 0
_gmail_secret_cache_lock = threading.Lock()

#: Per-process key for the opaque cache key: keeps the digest keyed so the
#: raw command (which may embed a secret) never lands in the cache.
_SECRET_CACHE_HMAC_KEY = secrets.token_bytes(32)


def _now() -> float:
    """Indirection over ``time.monotonic`` so tests can freeze the clock."""
    return time.monotonic()


def _gmail_secret_cache_key(command: str) -> str:
    """Return the opaque cache key for a credential command.

    The raw command may contain a secret inline, so it never appears in the
    cache or in logs.  A keyed BLAKE2b digest keeps the key opaque without
    hashing password material with a fast password-hashing algorithm.
    """
    return hashlib.blake2b(
        command.encode("utf-8"), key=_SECRET_CACHE_HMAC_KEY, digest_size=32
    ).hexdigest()


def _prune_gmail_secret_cache_locked(now: float) -> None:
    expired = [
        key
        for key, (expires_at, _secret) in _gmail_secret_cache.items()
        if expires_at <= now
    ]
    for key in expired:
        del _gmail_secret_cache[key]


def _gmail_secret_cache_get(key: str) -> str | None:
    now = _now()
    with _gmail_secret_cache_lock:
        _prune_gmail_secret_cache_locked(now)
        entry = _gmail_secret_cache.get(key)
        return entry[1] if entry is not None else None


def _gmail_secret_cache_put(key: str, secret: str, gen0: int) -> None:
    now = _now()
    with _gmail_secret_cache_lock:
        _prune_gmail_secret_cache_locked(now)
        if _gmail_secret_cache_gen != gen0:
            return
        _gmail_secret_cache[key] = (now + _GMAIL_SECRET_CACHE_TTL_S, secret)


def clear_gmail_secret_cache() -> None:
    """Drop the cached command secret and invalidate in-flight puts."""
    global _gmail_secret_cache_gen
    with _gmail_secret_cache_lock:
        _gmail_secret_cache.clear()
        _gmail_secret_cache_gen += 1


def _gmail_secret_from_file(raw: str) -> str:
    try:
        return _gmail_secret_path(raw).read_text(encoding="utf-8").strip()
    except UnicodeDecodeError:
        logger.warning("Gmail secret file failed: UnicodeDecodeError")
        return ""
    except OSError:
        logger.warning("Gmail secret file failed: OSError")
        return ""


def _gmail_secret_from_command(command: str) -> str:
    try:
        argv = shlex.split(command)
    except ValueError:
        logger.warning("Gmail secret command failed: ValueError")
        return ""
    if not argv:
        logger.warning("Gmail secret command failed: empty command")
        return ""

    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("Gmail secret command failed: TimeoutExpired")
        return ""
    except OSError:
        logger.warning("Gmail secret command failed: OSError")
        return ""
    except UnicodeDecodeError:
        logger.warning("Gmail secret command failed: UnicodeDecodeError")
        return ""
    except ValueError:
        logger.warning("Gmail secret command failed: ValueError")
        return ""

    if result.returncode != 0:
        logger.warning("Gmail secret command failed: returncode")
        return ""
    secret = (result.stdout or "").strip()
    if not secret:
        logger.warning("Gmail secret command failed: empty output")
        return ""
    return secret


def _cached_command_secret(command: str) -> str:
    """Return the command output, caching only successful non-empty results."""
    key = _gmail_secret_cache_key(command)
    cached = _gmail_secret_cache_get(key)
    if cached is not None:
        return cached

    with _gmail_secret_cache_lock:
        gen0 = _gmail_secret_cache_gen

    secret = _gmail_secret_from_command(command)
    if secret:
        _gmail_secret_cache_put(key, secret, gen0)
    return secret


def get_gmail_app_password() -> str:
    """Return the Gmail App Password (never exposed in API responses/logs).

    Preferred sources, in order: ``GMAIL_APP_PASSWORD_FILE`` (a ``600`` file,
    e.g. outside the repo), ``GMAIL_APP_PASSWORD_CMD`` (credential helper such
    as ``gpg --decrypt`` or a keyring lookup), then the legacy plaintext
    ``GMAIL_APP_PASSWORD`` env/config entry.
    """
    file_path = get_gmail_app_password_file()
    if file_path:
        secret = _gmail_secret_from_file(file_path)
        if secret:
            return secret
    command = get_gmail_app_password_cmd()
    if command:
        secret = _cached_command_secret(command)
        if secret:
            return secret
    return _get_gmail("gmail_app_password", "GMAIL_APP_PASSWORD")


def get_gmail_imap_host() -> str:
    """Return the IMAP host (default ``imap.gmail.com``)."""
    return _get_gmail("gmail_imap_host", "GMAIL_IMAP_HOST", "imap.gmail.com")


def get_gmail_imap_port() -> int:
    """Return the IMAP SSL port (default ``993``)."""
    port = _get_gmail_int("gmail_imap_port", "GMAIL_IMAP_PORT", 993)
    return port if 1 <= port <= 65535 else 993


def get_gmail_smtp_host() -> str:
    """Return the SMTP host (default ``smtp.gmail.com``)."""
    return _get_gmail("gmail_smtp_host", "GMAIL_SMTP_HOST", "smtp.gmail.com")


def get_gmail_smtp_port() -> int:
    """Return the SMTP STARTTLS port (default ``587``)."""
    port = _get_gmail_int("gmail_smtp_port", "GMAIL_SMTP_PORT", 587)
    return port if 1 <= port <= 65535 else 587


def get_gmail_folder() -> str:
    """Return the default mailbox folder (default ``INBOX``)."""
    folder = _get_gmail("gmail_folder", "GMAIL_FOLDER", "INBOX")
    return folder or "INBOX"


def get_gmail_timeout_s() -> int:
    """Return the IMAP/SMTP socket timeout in seconds (default ``30``)."""
    timeout = _get_gmail_int("gmail_timeout_s", "GMAIL_TIMEOUT_S", 30)
    return timeout if timeout > 0 else 30


def gmail_enabled() -> bool:
    """Return True when both the address and the App Password are configured."""
    return bool(get_gmail_address() and get_gmail_app_password())
