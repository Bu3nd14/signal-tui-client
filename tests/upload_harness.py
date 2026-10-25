"""Test helper: store a fake multipart upload and validate it, like /api/send."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from server.uploads import validate_upload
from web.uploads import _copy_upload_sync


@dataclass(frozen=True)
class StoredUpload:
    path: Path
    filename: str
    mime_type: str
    media_kind: str

    def cleanup(self) -> None:
        self.path.unlink(missing_ok=True)


def store_upload_sync(upload: Any, *, max_bytes: int | None = None) -> StoredUpload:
    """Transport copy (``web.uploads``) then domain validation (``server.uploads``)."""
    temp = _copy_upload_sync(upload, max_bytes=max_bytes)
    try:
        valid = validate_upload(temp.path, temp.filename, temp.content_type)
    except Exception:
        temp.cleanup()
        raise
    return StoredUpload(valid.path, valid.filename, valid.mime_type, valid.media_kind)
