"""Message history operations."""

from __future__ import annotations

from typing import Any

from server import messages as _domain
from server.api import ApiError, ChatAPI, operation
from server.models_api import (
    MessagesPageRequest,
    MessagesSnapshotRequest,
    MessagesWindowOut,
    MessagesWindowRequest,
)


# Response is ``list[dict[str, Any]]`` on purpose: rows carry optional keys
# (``system``, ``reactions``, ``is_group``, ``attachment.version``) and
# callers/tests feed partial rows through; a strict model would drop or reject
# them.  The wire JSON must equal what the legacy route produced.
@operation("messages.page", request=MessagesPageRequest, response=list[dict[str, Any]])
def messages_page(api: ChatAPI, req: MessagesPageRequest) -> list[dict[str, Any]]:
    """Messages of a chat, oldest first (optionally refreshed from the backend).

    ``since`` keeps messages with ``timestamp >= since``; ``limit`` keeps the
    newest ``limit``.  Blocking (SQLite, optional remote history fetch).
    """
    if req.limit is not None and not 1 <= req.limit <= _domain._MAX_MESSAGES_LIMIT:
        raise ApiError("invalid_request", "Invalid limit")
    return _domain.messages_page(
        api.manager,
        req.protocol,
        req.contact_id,
        refresh=req.refresh,
        limit=req.limit,
        since=req.since,
    )


@operation("messages.window", request=MessagesWindowRequest, response=MessagesWindowOut)
def messages_window(api: ChatAPI, req: MessagesWindowRequest) -> MessagesWindowOut:
    """One chat's canonical rows from the core's cache, optionally refreshed.

    Unlike ``messages.page`` (presentation rows from SQLite for the web) this
    returns the core's own cache entries, including rows not persisted yet,
    unioned over the WhatsApp storage keys.  Blocking when ``refresh`` is set.
    """
    rows, refreshed = _domain.messages_window(
        api.manager,
        req.protocol,
        req.contact_id,
        refresh=req.refresh,
        refresh_limit=req.refresh_limit,
    )
    return MessagesWindowOut(messages=rows, refreshed=refreshed)


@operation(
    "messages.snapshot",
    request=MessagesSnapshotRequest,
    response=dict[str, list[dict[str, Any]]],
)
def messages_snapshot(
    api: ChatAPI, req: MessagesSnapshotRequest
) -> dict[str, list[dict[str, Any]]]:
    """Every chat's canonical cache rows of a protocol, keyed by contact id."""
    return _domain.messages_snapshot(api.manager, req.protocol)
