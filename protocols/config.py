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

import json
import os
import socket
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent


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
    """Return the env var if set, else the config.json value, else default."""
    env = os.environ.get(env_name)
    if env:
        return env
    cfg = _load_config()
    value = cfg.get(key, default)
    return value if value is not None else default


def _get_int(key: str, env_name: str, default: int) -> int:
    """Return an int setting: env var, else config.json, else *default*.

    Invalid values fall back to *default*.
    """
    raw = os.environ.get(env_name)
    if not raw:
        raw = _load_config().get(key, default)
    try:
        return int(raw)
    except (ValueError, TypeError):
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


# ─── Address book / picker configuration ────────────────────────────────────


def get_address_book_ttl_s() -> int:
    """Return the address-book cache TTL in seconds (default ``300``)."""
    return _get_int("address_book_ttl_s", "ADDRESS_BOOK_TTL_S", 300)


def get_wa_lid_cache_ttl_days() -> int:
    """Return the WhatsApp ``@lid``→number cache TTL in days (default ``30``)."""
    return _get_int("wa_lid_cache_ttl_days", "WA_LID_CACHE_TTL_DAYS", 30)


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
    return _get_int("thumbnail_max_lines", "THUMBNAIL_MAX_LINES", 12)


def thumbnail_max_cols() -> int:
    """Return the max thumbnail width in columns (default ``60``)."""
    return _get_int("thumbnail_max_cols", "THUMBNAIL_MAX_COLS", 60)


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
    ``0.0.0.0`` when exposing it over a VPN or tunnel.
    """
    value = _web_config().get("host", "127.0.0.1")
    if value is None:
        return "127.0.0.1"
    return str(value)


def web_token() -> str:
    """Return the web Bearer token from env or the ``web`` config section."""
    env_token = os.environ.get("SIGNAL_TUI_WEB_TOKEN")
    if env_token:
        return env_token
    value = _web_config().get("token", "")
    return str(value) if value is not None else ""
