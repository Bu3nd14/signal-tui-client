"""TUI side of the blob operations: attachment ids to local files."""

from __future__ import annotations

import logging
from pathlib import Path

from server.client import ApiError, ChatClient
from tui.client_timeouts import call_op

logger = logging.getLogger(__name__)


def resolve_attachment_path(
    client: ChatClient, protocol: str, attachment_id: str
) -> Path | None:
    """Local file of an attachment through ``media.attachment``, or ``None``.

    ``None`` when the server cannot produce it (``not_found``: missing,
    failed download, outside the media root; any other API error, a transport
    error included, is logged and treated the same: a missing attachment must
    never break the chat view).  May block (lazy download, a remote server):
    call it from a worker thread, never from the UI thread.
    """
    try:
        blob = call_op(
            client,
            "media.attachment",
            {"protocol": protocol, "attachment_id": attachment_id},
        )
    except ApiError as exc:
        if exc.code != "not_found":
            logger.warning(
                "media.attachment failed (protocol=%s id=%r): %s",
                protocol,
                attachment_id,
                exc,
            )
        return None
    return Path(blob["path"])
