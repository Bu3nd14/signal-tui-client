"""Validation and temporary storage for web image uploads."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
UPLOAD_MAX_AGE_SECONDS = 60 * 60
_CHUNK_SIZE = 256 * 1024

_EXTENSIONS = {
    "image/png": {".png"},
    "image/jpeg": {".jpg", ".jpeg"},
    "image/gif": {".gif"},
    "image/webp": {".webp"},
}


class UploadValidationError(ValueError):
    def __init__(self, status_code: int):
        super().__init__("Invalid image upload")
        self.status_code = status_code


@dataclass(frozen=True)
class StoredUpload:
    path: Path
    mime_type: str

    def cleanup(self) -> None:
        self.path.unlink(missing_ok=True)


def upload_directory() -> Path:
    import backend

    return Path(backend.CACHE_DIR) / "web-uploads"


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


def _image_type(header: bytes) -> str | None:
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    return None


def _store_upload_sync(upload: Any) -> StoredUpload:
    directory = ensure_upload_directory()
    temporary_path: Path | None = None
    total = 0
    header = bytearray()
    try:
        with tempfile.NamedTemporaryFile(
            dir=directory, prefix="upload-", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.chmod(temporary_path, 0o600)
            while chunk := upload.file.read(_CHUNK_SIZE):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise UploadValidationError(413)
                if len(header) < 16:
                    header.extend(chunk[: 16 - len(header)])
                temporary.write(chunk)

        mime_type = _image_type(bytes(header))
        if mime_type is None:
            raise UploadValidationError(400)
        suffix = Path(upload.filename or "").suffix.lower()
        if suffix not in _EXTENSIONS[mime_type]:
            raise UploadValidationError(400)
        final_path = temporary_path.with_suffix(suffix)
        temporary_path.replace(final_path)
        temporary_path = None
        return StoredUpload(final_path, mime_type)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


async def store_upload(upload: Any) -> StoredUpload:
    try:
        return await asyncio.to_thread(_store_upload_sync, upload)
    finally:
        await upload.close()
