"""Attachment/media helpers shared by the server operations and the web routes.

Extension tables, the per-protocol media roots and the cache-busting
attachment version (``mtime_ns-size``) used by ``messages.page``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

_IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".heic",
    ".heif",
    ".tif",
    ".tiff",
}


_VIDEO_EXTENSIONS = {
    ".mp4",
    ".m4v",
    ".mov",
    ".webm",
    ".mkv",
    ".3gp",
    ".avi",
    ".mpg",
    ".mpeg",
}


def _allowed_media_root(manager: Any, proto: str) -> Path:
    from protocols.db import CACHE_DIR
    from protocols.rpc import SIGNAL_CLI_ATTACHMENTS_DIR

    if proto == "signal":
        return Path(SIGNAL_CLI_ATTACHMENTS_DIR).resolve()
    if proto == "whatsapp":
        instance = manager.get(proto)
        if instance is not None and hasattr(instance, "_ensure_media_dir"):
            return instance._ensure_media_dir().resolve()
        return (CACHE_DIR / "whatsapp-media").resolve()
    if proto == "telegram":
        try:
            from protocols.telegram import _media_dir

            return _media_dir().resolve()
        except ImportError:
            return (Path(tempfile.gettempdir()) / "telegram-media").resolve()
    raise ValueError(f"Unsupported protocol: {proto}")


def _attachment_version(
    manager, proto: str, root: Path, attachment_id: str
) -> str | None:
    """Versione cache-busting ('mtime_ns-size') dell'allegato già su disco.

    Solo fast-path locale (mai lazy download). root è pre-risolta una volta per
    richiesta. Ogni candidate è resolve()+is_relative_to(root)+is_file() prima
    dello stat: nessun leak di mtime/size di file arbitrari (traversal/symlink
    fuori root -> None).
    """
    if not attachment_id:
        return None
    bare = attachment_id.split("?", 1)[0]
    name = Path(bare).name or bare
    if proto == "signal":
        candidates = (root / name, root / bare)
    elif proto == "whatsapp":
        candidates = (root / name,)
    elif proto == "telegram":
        candidates = (Path(bare), root / name)
    else:
        return None
    root_resolved = root.resolve()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except (OSError, ValueError):
            continue
        if not resolved.is_relative_to(root_resolved):
            continue
        try:
            st = resolved.stat()
        except OSError:
            continue
        if resolved.is_file() and st.st_size >= 0:
            return f"{st.st_mtime_ns}-{st.st_size}"
    return None


def _attach_media_versions(manager, proto: str, messages: list[dict[str, Any]]) -> None:
    """Arricchisce attachment['version'] per il cache-busting; best-effort.

    root risolta una sola volta. Qualunque errore (incluso OSError/PermissionError
    dal mkdir di _ensure_media_dir per WhatsApp) degrada silenziosamente a
    'nessuna version': la lista messaggi non deve mai fallire per un cache-buster.
    """
    try:
        root = _allowed_media_root(manager, proto)
    except Exception:  # noqa: BLE001 — funzionalità accessoria, mai 500
        return
    memo: dict[str, str | None] = {}
    for message in messages:
        att = message.get("attachment")
        if not att or not att.get("attachment_id"):
            continue
        att_id = att["attachment_id"]
        if att_id not in memo:
            try:
                memo[att_id] = _attachment_version(manager, proto, root, att_id)
            except Exception:  # noqa: BLE001
                memo[att_id] = None
        version = memo[att_id]
        if version:
            att["version"] = version
