"""Transport side of media uploads: temp storage for the HTTP adapter.

The adapter copies each multipart ``UploadFile`` into a private temp file in
the upload directory (enforcing the request-wide byte budget while copying)
and hands the *paths* to the ``messages.send`` operation, which validates them
with :mod:`server.uploads` (sniffed kind, per-kind limits, extension
coherence).  The adapter deletes the temp files when the request ends,
whatever the outcome.

Decision (mc-mbm.9): copying bytes, the directory, its age-based pruning and
the request-wide total cap (``_MAX_TOTAL_BYTES``, a disk-protection limit of
this transport) are transport; *which* files are acceptable attachments is
domain and lives in ``server/uploads.py``.  The per-kind size limits are
domain but are also enforced mid-copy through ``server.uploads.UploadGuard`` so
an oversized file is cut off early rather than stored in full.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from filename_utils import sanitize_filename
from server.uploads import (
    ALLOWED_EXTENSIONS,
    UploadGuard,
    UploadValidationError,  # noqa: F401  (re-export for callers/tests)
    upload_directory,
)

#: Hard cap on the total bytes of a single multi-attachment request.
_MAX_TOTAL_BYTES = 250 * 1024 * 1024
UPLOAD_MAX_AGE_SECONDS = 60 * 60
_CHUNK_SIZE = 256 * 1024


@dataclass(frozen=True)
class TempUpload:
    """A multipart file copied to the upload directory (not yet validated)."""

    path: Path
    filename: str | None  # as sent by the client
    content_type: str | None  # as declared by the client

    @property
    def size(self) -> int:
        return self.path.stat().st_size

    def cleanup(self) -> None:
        self.path.unlink(missing_ok=True)


def ensure_upload_directory() -> Path:
    directory = upload_directory()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def prepare_upload_directory(*, now: float | None = None) -> Path:
    directory = ensure_upload_directory()
    cutoff = (time.time() if now is None else now) - UPLOAD_MAX_AGE_SECONDS
    for path in directory.iterdir():
        try:
            if (
                path.is_file()
                and not path.is_symlink()
                and path.stat().st_mtime < cutoff
            ):
                path.unlink()
        except OSError:
            continue
    return directory


def _temp_suffix(raw_filename: str | None) -> str:
    """Extension for the temp file: the client's, only if it is an allowed one."""
    suffix = Path(sanitize_filename(raw_filename)).suffix.lower()
    return suffix if suffix in ALLOWED_EXTENSIONS else ""


def _copy_upload_sync(upload: Any, *, max_bytes: int | None = None) -> TempUpload:
    """Copy *upload* to a private temp file, applying the size guard mid-copy."""
    directory = ensure_upload_directory()
    temporary_path: Path | None = None
    raw_filename = getattr(upload, "filename", None)
    declared_content_type = getattr(upload, "content_type", None)
    guard = UploadGuard(
        max_bytes=max_bytes,
        declared_content_type=declared_content_type,
        filename=raw_filename,
    )
    try:
        with tempfile.NamedTemporaryFile(
            dir=directory,
            prefix="upload-",
            suffix=_temp_suffix(raw_filename),
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.chmod(temporary_path, 0o600)
            while chunk := upload.file.read(_CHUNK_SIZE):
                guard.feed(chunk)
                temporary.write(chunk)
        stored = TempUpload(temporary_path, raw_filename, declared_content_type)
        temporary_path = None
        return stored
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


async def store_temp_upload(upload: Any, *, max_bytes: int | None = None) -> TempUpload:
    """Copy *upload* to a temp file; closes the upload.

    ``max_bytes`` caps this single file (e.g. what remains of the request-wide
    ``_MAX_TOTAL_BYTES`` budget): exceeding it aborts the copy with a 413
    ``UploadValidationError`` before the whole file is on disk.
    """
    try:
        return await asyncio.to_thread(_copy_upload_sync, upload, max_bytes=max_bytes)
    finally:
        await upload.close()
