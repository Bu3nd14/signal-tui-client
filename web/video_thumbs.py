from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_VIDEO_THUMB_SEMAPHORE = threading.BoundedSemaphore(2)
_VIDEO_THUMB_ACQUIRE_TIMEOUT = 20
_FFMPEG_TIMEOUT = 15
_ffmpeg_unavailable_warned = False


def _mp4_has_moov(data: bytes) -> bool:
    offset = 0
    length = len(data)
    while offset + 8 <= length:
        size = int.from_bytes(data[offset : offset + 4], "big")
        box_type = data[offset + 4 : offset + 8]
        header_size = 8
        if size == 1:
            if offset + 16 > length:
                return False
            size = int.from_bytes(data[offset + 8 : offset + 16], "big")
            header_size = 16
        elif size == 0:
            size = length - offset

        if size < header_size:
            return False
        if box_type == b"moov":
            return True
        if size > length - offset:
            return False
        offset += size
    return False


@lru_cache(maxsize=1)
def _ffmpeg_executable() -> str | None:
    global _ffmpeg_unavailable_warned

    executable = shutil.which("ffmpeg")
    if executable is None and not _ffmpeg_unavailable_warned:
        logger.warning("ffmpeg unavailable; video thumbnails are disabled")
        _ffmpeg_unavailable_warned = True
    return executable


def _video_thumbnail(
    path: Path | None,
    proto: str,
    attachment_id: str,
    width: int,
) -> Path | None:
    if path is None or not path.is_file():
        return None

    from PIL import Image

    from web.api import _prune_thumb_cache, _thumb_lock, _web_thumb_dir

    try:
        digest = hashlib.sha1(
            f"{path}|{path.stat().st_mtime_ns}|{width}".encode(),
            usedforsecurity=False,
        ).hexdigest()
    except OSError:
        return None

    thumb_dir = _web_thumb_dir(proto)
    thumb = thumb_dir / f"{digest}.jpg"
    with _thumb_lock(thumb):
        if thumb.exists():
            try:
                thumb.touch()
            except OSError:
                pass
            return thumb

        executable = _ffmpeg_executable()
        if executable is None:
            return None
        if not _VIDEO_THUMB_SEMAPHORE.acquire(timeout=_VIDEO_THUMB_ACQUIRE_TIMEOUT):
            return None

        frame_path: Path | None = None
        output_path = thumb.with_suffix(".tmp")
        try:
            thumb_dir.mkdir(parents=True, exist_ok=True)
            descriptor, frame_name = tempfile.mkstemp(
                prefix="video-frame-", suffix=".jpg", dir=thumb_dir
            )
            os.close(descriptor)
            frame_path = Path(frame_name)

            completed = subprocess.run(
                [
                    executable,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    "0",
                    "-i",
                    str(path),
                    "-map",
                    "0:v:0",
                    "-frames:v",
                    "1",
                    "-f",
                    "image2",
                    "-vcodec",
                    "mjpeg",
                    "-y",
                    str(frame_path),
                ],
                capture_output=True,
                check=False,
                timeout=_FFMPEG_TIMEOUT,
            )
            if (
                completed.returncode != 0
                or not frame_path.is_file()
                or frame_path.stat().st_size == 0
            ):
                return None

            with Image.open(frame_path) as image:
                image.draft("RGB", (width, width))
                with image.convert("RGB") as converted:
                    converted.thumbnail((width, width), Image.BILINEAR)
                    converted.save(output_path, "JPEG", quality=78, optimize=True)
            output_path.replace(thumb)
            _prune_thumb_cache(thumb_dir.parent)
            return thumb
        except (OSError, ValueError, subprocess.SubprocessError):
            logger.debug(
                "Unable to generate video thumbnail for %s", path, exc_info=True
            )
            return None
        finally:
            _VIDEO_THUMB_SEMAPHORE.release()
            output_path.unlink(missing_ok=True)
            if frame_path is not None:
                frame_path.unlink(missing_ok=True)
