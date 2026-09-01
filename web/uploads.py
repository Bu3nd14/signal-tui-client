"""Validation and temporary storage for web media uploads."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from filename_utils import sanitize_filename

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_MAX_BYTES_BY_KIND = {
    "image": MAX_UPLOAD_BYTES,
    "video": 100 * 1024 * 1024,
    "audio": 50 * 1024 * 1024,
    "document": 50 * 1024 * 1024,
}
UPLOAD_MAX_AGE_SECONDS = 60 * 60
_CHUNK_SIZE = 256 * 1024

_EXTENSIONS_BY_MIME = {
    "image/png": {".png"},
    "image/jpeg": {".jpg", ".jpeg"},
    "image/gif": {".gif"},
    "image/webp": {".webp"},
    "video/mp4": {".mp4", ".m4v"},
    "video/quicktime": {".mov"},
    "video/webm": {".webm"},
    "audio/mpeg": {".mp3"},
    "audio/ogg": {".ogg", ".opus"},
    "audio/mp4": {".m4a"},
    "audio/wav": {".wav"},
    "application/pdf": {".pdf"},
    "application/zip": {".zip", ".docx", ".xlsx", ".pptx"},
}


class UploadValidationError(ValueError):
    def __init__(self, status_code: int):
        super().__init__("Invalid media upload")
        self.status_code = status_code


@dataclass(frozen=True)
class StoredUpload:
    path: Path
    filename: str
    mime_type: str
    media_kind: str

    def cleanup(self) -> None:
        self.path.unlink(missing_ok=True)


def upload_directory() -> Path:
    import protocols.db as backend

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


def _sniff_media(header: bytes) -> tuple[str, str] | None:
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "image"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "image"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", "gif"
    if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp", "image"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        brand = header[8:12]
        if brand == b"qt  ":
            return "video/quicktime", "video"
        if brand == b"M4A ":
            return "audio/mp4", "audio"
        if brand.startswith(b"mp4") or brand in {b"isom", b"iso2", b"avc1"}:
            return "video/mp4", "video"
    if header.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm", "video"
    if header.startswith(b"OggS"):
        return "audio/ogg", "audio"
    if header.startswith((b"ID3", b"\xff\xfb")):
        return "audio/mpeg", "audio"
    if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return "audio/wav", "audio"
    if header.startswith(b"%PDF-"):
        return "application/pdf", "document"
    if header.startswith(b"PK\x03\x04"):
        return "application/zip", "document"
    return None


def _max_bytes_for_kind(media_kind: str) -> int:
    limit_kind = "image" if media_kind == "gif" else media_kind
    return _MAX_BYTES_BY_KIND[limit_kind]


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
                if total > max(_MAX_BYTES_BY_KIND.values()):
                    raise UploadValidationError(413)
                if len(header) < 32:
                    header.extend(chunk[: 32 - len(header)])
                detected = _sniff_media(bytes(header))
                if detected and total > _max_bytes_for_kind(detected[1]):
                    raise UploadValidationError(413)
                temporary.write(chunk)

        detected = _sniff_media(bytes(header))
        if detected is None:
            raise UploadValidationError(400)
        mime_type, media_kind = detected
        if total > _max_bytes_for_kind(media_kind):
            raise UploadValidationError(413)
        filename = sanitize_filename(upload.filename)
        suffix = Path(filename).suffix.lower()
        if suffix not in _EXTENSIONS_BY_MIME.get(mime_type, set()):
            raise UploadValidationError(400)
        final_path = temporary_path.with_suffix(suffix)
        temporary_path.replace(final_path)
        temporary_path = None
        return StoredUpload(final_path, filename, mime_type, media_kind)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


async def store_upload(upload: Any) -> StoredUpload:
    try:
        return await asyncio.to_thread(_store_upload_sync, upload)
    finally:
        await upload.close()
