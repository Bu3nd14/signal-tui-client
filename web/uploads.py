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
#: Hard cap on the total bytes of a single multi-attachment request.
_MAX_TOTAL_BYTES = 250 * 1024 * 1024
_MAX_BYTES_BY_KIND = {
    "image": MAX_UPLOAD_BYTES,
    "video": 100 * 1024 * 1024,
    "audio": 50 * 1024 * 1024,
    "document": 50 * 1024 * 1024,
}
UPLOAD_MAX_AGE_SECONDS = 60 * 60
_CHUNK_SIZE = 256 * 1024
#: Byte di header accumulati per il riconoscimento del container (e per
#: l'eventuale box-walk MP4/WebM): sufficiente per `ftyp`/`moov`/`Tracks`.
_SNIFF_BYTES = 4096

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
    "audio/webm": {".webm"},
    "audio/wav": {".wav"},
    "application/pdf": {".pdf"},
    "application/zip": {".zip", ".docx", ".xlsx", ".pptx"},
}

#: Varianti CodecID usate dagli elementi Tracks di Matroska/WebM.
_WEBM_VIDEO_CODECS = (b"V_VP8", b"V_VP9", b"V_AV1", b"V_MPEG4/ISO/AVC")
_WEBM_AUDIO_CODECS = (
    b"A_OPUS",
    b"A_VORBIS",
    b"A_AAC",
    b"A_MPEG/L3",
    b"A_FLAC",
    b"A_PCM",
)


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
    if len(header) >= 12 and header[4:8] in {b"ftyp", b"styp"}:
        brand = header[8:12].lower()
        if brand == b"qt  ":
            return "video/quicktime", "video"
        if brand == b"m4a ":
            return "audio/mp4", "audio"
        # R3: i container frammentati iOS usano brand `iso*`/`mp4*`/`m4a*`
        # (es. `iso5`, `iso6`, `mp42`): accettali come MP4, il raffinamento
        # `_mp4_has_video_track`/hint dichiarato decide audio vs video.
        if brand.startswith((b"iso", b"mp4", b"m4a")) or brand in {b"avc1", b"avc3"}:
            return "video/mp4", "video"
        # Brand ignoto: il container MP4 non è più affidabile → 400.
        return None
    if len(header) >= 8 and header[4:8] == b"moof":
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


def _sniff_webm_kind(header: bytes) -> str | None:
    """Return the WebM kind from the CodecID strings in the Tracks element.

    Any video codec wins over audio (a container with both is served as
    video); ``None`` means no codec was found in the sniffed header.
    """
    if any(codec in header for codec in _WEBM_VIDEO_CODECS):
        return "video"
    if any(codec in header for codec in _WEBM_AUDIO_CODECS):
        return "audio"
    return None


def _iter_boxes(data: bytes, start: int, end: int):
    """Yield ``(box_type, box_start, box_end)`` for the boxes in ``data``.

    Handles 32-bit sizes, ``size == 1`` (64-bit extended) and ``size == 0``
    (box extends to ``end``).
    """
    offset = start
    while offset + 8 <= end:
        size = int.from_bytes(data[offset : offset + 4], "big")
        box_type = data[offset + 4 : offset + 8]
        header_size = 8
        if size == 1:
            if offset + 16 > end:
                return
            size = int.from_bytes(data[offset + 8 : offset + 16], "big")
            header_size = 16
        elif size == 0:
            size = end - offset
        if size < header_size:
            return
        box_end = min(offset + size, end)
        yield box_type, offset, box_end
        if box_end <= offset:
            return
        offset = box_end


def _mp4_has_video_track(data: bytes) -> str | None:
    """Classify an MP4 container by walking ``moov`` → ``trak`` → ``mdia``.

    R1: any video track wins. Returns ``"video"`` if at least one ``hdlr`` is
    ``vide``, ``"audio"`` only if no video and at least one ``soun``, else
    ``None`` (nessuna prova).
    """
    found_video = False
    found_audio = False
    for box_type, box_start, box_end in _iter_boxes(data, 0, len(data)):
        if box_type != b"moov":
            continue
        for trak_type, trak_start, trak_end in _iter_boxes(
            data, box_start + 8, box_end
        ):
            if trak_type != b"trak":
                continue
            for mdia_type, mdia_start, mdia_end in _iter_boxes(
                data, trak_start + 8, trak_end
            ):
                if mdia_type != b"mdia":
                    continue
                for hdlr_type, hdlr_start, hdlr_end in _iter_boxes(
                    data, mdia_start + 8, mdia_end
                ):
                    if hdlr_type != b"hdlr" or hdlr_start + 20 > hdlr_end:
                        continue
                    handler = data[hdlr_start + 16 : hdlr_start + 20]
                    if handler == b"vide":
                        found_video = True
                    elif handler == b"soun":
                        found_audio = True
    if found_video:
        return "video"
    if found_audio:
        return "audio"
    return None


def _classify_upload(
    header: bytes,
    *,
    declared_content_type: str | None,
    filename: str | None,
) -> tuple[str, str]:
    """Return ``(mime_type, media_kind)`` for an upload header.

    Il mime dichiarato dal client è usato SOLO come hint (per i container
    audio ambigui) e mai come fonte di verità: magic bytes, box-walk e
    estensione coerente restano i criteri decisivi.
    """
    detected = _sniff_media(header)
    if detected is None:
        raise UploadValidationError(400)
    mime_type, media_kind = detected

    if mime_type == "video/webm":
        refined = _sniff_webm_kind(header)
        if refined == "video":
            return "video/webm", "video"
        if refined == "audio":
            return "audio/webm", "audio"
    elif mime_type == "video/mp4":
        refined = _mp4_has_video_track(header)
        if refined == "video":
            return "video/mp4", "video"
        if refined == "audio":
            return "audio/mp4", "audio"

    # Hint per-file: accettato solo per un container ambiguo SENZA prova di
    # traccia video e con estensione coerente (`.mp4` escluso di proposito).
    declared = (declared_content_type or "").lower().split(";", 1)[0].strip()
    if (
        declared in {"audio/webm", "audio/mp4"}
        and media_kind != "audio"
        and mime_type in {"video/webm", "video/mp4", "video/quicktime"}
    ):
        suffix = Path(filename or "").suffix.lower()
        coherent = (declared == "audio/webm" and suffix == ".webm") or (
            declared == "audio/mp4" and suffix == ".m4a"
        )
        if coherent:
            return declared, "audio"
    return mime_type, media_kind


def _store_upload_sync(upload: Any, *, max_bytes: int | None = None) -> StoredUpload:
    directory = ensure_upload_directory()
    temporary_path: Path | None = None
    total = 0
    header = bytearray()
    raw_filename = getattr(upload, "filename", None)
    declared_content_type = getattr(upload, "content_type", None)
    try:
        with tempfile.NamedTemporaryFile(
            dir=directory, prefix="upload-", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.chmod(temporary_path, 0o600)
            while chunk := upload.file.read(_CHUNK_SIZE):
                total += len(chunk)
                # Cross-file budget: the caller caps this file at what is
                # left of the request-wide total, so a batch exceeding the
                # total is rejected while reading, not after storing it all.
                if max_bytes is not None and total > max_bytes:
                    raise UploadValidationError(413)
                if total > max(_MAX_BYTES_BY_KIND.values()):
                    raise UploadValidationError(413)
                if len(header) < _SNIFF_BYTES:
                    header.extend(chunk[: _SNIFF_BYTES - len(header)])
                # Cap per-kind mentre si legge: l'header incompleto non è
                # classificabile, quindi il 400 viene rimandato a fine upload.
                try:
                    _, detected_kind = _classify_upload(
                        bytes(header),
                        declared_content_type=declared_content_type,
                        filename=raw_filename,
                    )
                except UploadValidationError:
                    detected_kind = None
                if detected_kind and total > _max_bytes_for_kind(detected_kind):
                    raise UploadValidationError(413)
                temporary.write(chunk)

        mime_type, media_kind = _classify_upload(
            bytes(header),
            declared_content_type=declared_content_type,
            filename=raw_filename,
        )
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


async def store_upload(upload: Any, *, max_bytes: int | None = None) -> StoredUpload:
    """Store *upload* and return its metadata.

    ``max_bytes`` optionally caps this single file (e.g. to what remains of
    the request-wide ``_MAX_TOTAL_BYTES`` budget): exceeding it aborts the
    read with a 413 ``UploadValidationError`` before the whole file is on
    disk.  ``None`` keeps the per-kind limits only.
    """
    try:
        return await asyncio.to_thread(_store_upload_sync, upload, max_bytes=max_bytes)
    finally:
        await upload.close()
