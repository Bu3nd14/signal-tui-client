"""Server-level and static-data operations."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from server.api import ChatAPI, operation
from server.models_api import (
    ClientLogRequest,
    EmojiCategoryOut,
    EmptyRequest,
    ReactionsAvailableOut,
    ReactionsAvailableRequest,
    ServerConfigOut,
    StatusOut,
)
from server.ops.common import PROTOCOL_ORDER

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _emoji_categories() -> list[dict[str, Any]]:
    import emoji

    from emoji_data import PREDEFINED_CATEGORIES

    aliases = {
        char: data.get("en", "").strip(":")
        for char, data in emoji.EMOJI_DATA.items()
        if data.get("en")
    }
    return [
        {
            "category": name,
            "icon": icon,
            "emojis": list(chars),
            "aliases": {char: aliases.get(char, "") for char in chars},
        }
        for name, icon, chars in PREDEFINED_CATEGORIES
    ]


@operation("emoji.list", request=EmptyRequest, response=list[EmojiCategoryOut])
def emoji_list(api: ChatAPI, req: EmptyRequest) -> list[dict[str, Any]]:
    """Emoji picker categories with their shortcode aliases."""
    return _emoji_categories()


@operation(
    "reactions.available",
    request=ReactionsAvailableRequest,
    response=ReactionsAvailableOut,
)
def reactions_available(
    api: ChatAPI, req: ReactionsAvailableRequest
) -> ReactionsAvailableOut:
    """Reaction emojis the protocol allows (Telegram only; others: none)."""
    if req.protocol != "telegram":
        return ReactionsAvailableOut()
    try:
        backend = api.manager.get(req.protocol)
        if backend is None or not hasattr(backend, "get_available_reactions"):
            return ReactionsAvailableOut()
        emojis = backend.get_available_reactions()
    except Exception:
        logger.debug("Telegram available reactions request failed", exc_info=True)
        return ReactionsAvailableOut()
    return ReactionsAvailableOut(emojis=emojis if isinstance(emojis, list) else [])


@operation("server.status", request=EmptyRequest, response=dict[str, bool])
def server_status(api: ChatAPI, req: EmptyRequest) -> dict[str, bool]:
    """Real per-protocol connection state (not inferred from contacts).

    A protocol with zero chats yet is still connected; a registered but
    not-yet-connected backend is not.  Missing/unregistered protocols
    (e.g. Telegram without credentials configured) report ``False``.
    """
    manager = api.manager
    return {
        proto: bool(getattr(manager.get(proto), "is_connected", False))
        for proto in PROTOCOL_ORDER
    }


@operation("server.config", request=EmptyRequest, response=ServerConfigOut)
def server_config(api: ChatAPI, req: EmptyRequest) -> ServerConfigOut:
    """Client-relevant settings (image thumbnail size limits, picker options)."""
    from protocols.config import (
        get_picker_max_results,
        get_picker_preferred_backend,
        thumbnail_max_cols,
        thumbnail_max_lines,
    )

    return ServerConfigOut(
        thumbnail_max_lines=thumbnail_max_lines(),
        thumbnail_max_cols=thumbnail_max_cols(),
        picker_max_results=get_picker_max_results(),
        picker_preferred_backend=get_picker_preferred_backend(),
    )


@operation("server.client_log", request=ClientLogRequest, response=StatusOut)
def server_client_log(api: ChatAPI, req: ClientLogRequest) -> StatusOut:
    """Write a line from a frontend (truncated to 500 characters) to the log."""
    logger.info("[%s] %s", req.source, str(req.message or "")[:500])
    return StatusOut(status="ok")
