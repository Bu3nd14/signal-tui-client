"""Thumbnail generation and cache for images (Pillow).

One place for the per-path thumbnail locks and the cache pruning; the video
thumbnails (:mod:`server.video_thumbs`) share them.  Thumbnails live under
``$XDG_CACHE_HOME/signal-tui-client/web-thumbs/<protocol>`` and are named by a
digest of (path, mtime, size, width), so a changed source never serves a stale
thumbnail.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from pathlib import Path

from server.media import _is_thumbnail_candidate

logger = logging.getLogger(__name__)

THUMB_WIDTHS = {96, 240, 480}
_THUMB_CACHE_LIMIT = 500 * 1024 * 1024
_THUMB_LOCKS: dict[Path, threading.Lock] = {}
_THUMB_LOCKS_GUARD = threading.Lock()


def _web_thumb_dir(proto: str) -> Path:
    cache_home = Path(
        os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")
    ).expanduser()
    return cache_home / "signal-tui-client" / "web-thumbs" / proto


def _thumb_lock(path: Path) -> threading.Lock:
    with _THUMB_LOCKS_GUARD:
        return _THUMB_LOCKS.setdefault(path, threading.Lock())


def _prune_thumb_cache(cache_root: Path) -> None:
    try:
        files = [path for path in cache_root.rglob("*.jpg") if path.is_file()]
        entries = sorted(
            ((path.stat().st_mtime_ns, path.stat().st_size, path) for path in files),
            reverse=True,
        )
        total = sum(size for _, size, _ in entries)
        for _, size, path in reversed(entries):
            if total <= _THUMB_CACHE_LIMIT:
                break
            path.unlink(missing_ok=True)
            total -= size
    except OSError:
        logger.debug("Unable to prune web thumbnail cache", exc_info=True)


def _thumbnail(path: Path, proto: str, attachment_id: str, width: int) -> Path | None:
    if not _is_thumbnail_candidate(path, proto, attachment_id):
        return None

    from PIL import Image

    thumb_dir = _web_thumb_dir(proto)
    stat = path.stat()
    # La size entra nel digest: un overwrite che preserva mtime_ns ma cambia i
    # byte deve invalidare la thumbnail già su disco (coerente col token client
    # 'mtime_ns-size').
    digest = hashlib.sha1(
        f"{path}|{stat.st_mtime_ns}|{stat.st_size}|{width}".encode(),
        usedforsecurity=False,
    ).hexdigest()
    thumb = thumb_dir / f"{digest}.jpg"
    with _thumb_lock(thumb):
        if thumb.exists():
            try:
                thumb.touch()
            except OSError:
                pass
            return thumb
        tmp = thumb.with_suffix(".tmp")
        try:
            thumb_dir.mkdir(parents=True, exist_ok=True)
            with Image.open(path) as image:
                if image.format == "GIF" or (
                    image.format == "WEBP" and getattr(image, "is_animated", False)
                ):
                    return None
                image.draft("RGB", (width, width))
                with image.convert("RGB") as converted:
                    converted.thumbnail((width, width), Image.BILINEAR)
                    converted.save(tmp, "JPEG", quality=78, optimize=True)
            tmp.replace(thumb)
            _prune_thumb_cache(thumb_dir.parent)
            return thumb
        except (OSError, ValueError):
            tmp.unlink(missing_ok=True)
            logger.debug("Unable to generate web thumbnail for %s", path, exc_info=True)
            return None
