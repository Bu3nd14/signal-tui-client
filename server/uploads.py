"""Upload policy: which attachments are acceptable, and how they are classified.

Domain side of the upload split (design §3.4).  The HTTP adapter turns an
``UploadFile`` into a local temp file (``web.uploads``: directory, copying,
request-wide byte budget, cleanup); everything that decides whether the bytes
are an acceptable attachment lives here: the sniffed kind, the per-kind size
limits and the filename/extension coherence.  Operations receive file *paths*
and call :func:`validate_upload`.

This module must not import FastAPI, Starlette, Textual, ``tui`` or ``web``.
"""

from __future__ import annotations

import os
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from filename_utils import sanitize_filename

#: Attachments accepted by a single send.
MAX_ATTACHMENTS = 10
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
#: Gmail attachments are any file (not the media-kind policy above); the HTTP
#: adapter needs the limits to size its multipart parsing and temp copies.
GMAIL_MAX_ATTACHMENTS = 10
GMAIL_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
GMAIL_MAX_TOTAL_ATTACHMENT_BYTES = 25 * 1024 * 1024
_MAX_BYTES_BY_KIND = {
    "image": MAX_UPLOAD_BYTES,
    "video": 100 * 1024 * 1024,
    "audio": 50 * 1024 * 1024,
    "document": 50 * 1024 * 1024,
}
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
    """``status_code`` 413 (too large) or 400 (unsupported/incoherent media)."""

    def __init__(self, status_code: int):
        super().__init__("Invalid media upload")
        self.status_code = status_code


@dataclass(frozen=True)
class ValidatedUpload:
    """An accepted attachment: its local file plus what validation decided."""

    path: Path
    filename: str  # sanitized
    mime_type: str
    media_kind: str


#: Every extension an accepted upload may carry (transport uses it to name
#: its temp file; the real check is :func:`validate_upload`).
ALLOWED_EXTENSIONS = frozenset().union(*_EXTENSIONS_BY_MIME.values())


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


class UploadGuard:
    """Incremental size/kind limits applied while an upload is being copied.

    ``feed`` each chunk as it is read; it raises ``UploadValidationError(413)``
    as soon as a limit is exceeded, so oversized files never reach the disk in
    full.  ``max_bytes`` optionally caps this file (e.g. what remains of a
    request-wide budget).  Format problems (400) are not raised here: an
    incomplete header is not classifiable, :func:`validate_upload` decides.
    ``media_limits=False`` (mail attachments are any file) applies ``max_bytes``
    only.
    """

    def __init__(
        self,
        *,
        max_bytes: int | None = None,
        declared_content_type: str | None = None,
        filename: str | None = None,
        media_limits: bool = True,
    ) -> None:
        self._media_limits = media_limits
        self._max_bytes = max_bytes
        self._declared = declared_content_type
        self._filename = filename
        self._header = bytearray()
        self.total = 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        # Cross-file budget: the caller caps this file at what is left of the
        # request-wide total, so a batch exceeding the total is rejected while
        # reading, not after storing it all.
        if self._max_bytes is not None and self.total > self._max_bytes:
            raise UploadValidationError(413)
        if not self._media_limits:
            return  # not media (mail attachments): only the caller's cap applies
        if self.total > max(_MAX_BYTES_BY_KIND.values()):
            raise UploadValidationError(413)
        if len(self._header) < _SNIFF_BYTES:
            self._header.extend(chunk[: _SNIFF_BYTES - len(self._header)])
        # Cap per-kind mentre si legge: l'header incompleto non è
        # classificabile, quindi il 400 viene rimandato a fine upload.
        try:
            _, detected_kind = _classify_upload(
                bytes(self._header),
                declared_content_type=self._declared,
                filename=self._filename,
            )
        except UploadValidationError:
            return
        if self.total > _max_bytes_for_kind(detected_kind):
            raise UploadValidationError(413)


def validate_upload(
    path: Path, filename: str | None, content_type: str | None
) -> ValidatedUpload:
    """Classify and check the stored file at *path*.

    Raises ``UploadValidationError`` (413 over the kind limit, 400 for an
    unrecognised or incoherent file).  ``filename`` is the client's name (it is
    sanitized here); ``content_type`` is only a hint for ambiguous containers.
    """
    total = path.stat().st_size
    with path.open("rb") as handle:
        header = handle.read(_SNIFF_BYTES)
    mime_type, media_kind = _classify_upload(
        header, declared_content_type=content_type, filename=filename
    )
    if total > _max_bytes_for_kind(media_kind):
        raise UploadValidationError(413)
    clean_name = sanitize_filename(filename)
    suffix = Path(clean_name).suffix.lower()
    if suffix not in _EXTENSIONS_BY_MIME.get(mime_type, set()):
        raise UploadValidationError(400)
    return ValidatedUpload(path, clean_name, mime_type, media_kind)


def cache_directory() -> Path:
    """The cache directory (``protocols.db.CACHE_DIR``, read at call time, like
    the rest of the cache layout, so adapters never need to import ``protocols``
    for it)."""
    import protocols.db as backend

    return Path(backend.CACHE_DIR)


def upload_directory() -> Path:
    """Staging directory of adapter uploads (``<cache dir>/web-uploads``)."""
    return cache_directory() / "web-uploads"


# -- staging of local files (adapters whose clients name paths) -------------------

#: Total bytes one request may stage (disk-protection cap, like the HTTP one).
MAX_STAGED_TOTAL_BYTES = 250 * 1024 * 1024
#: Staged files older than this are removed by :func:`prune_upload_directory`.
STAGED_MAX_AGE_SECONDS = 60 * 60
_COPY_CHUNK = 256 * 1024


def stage_local_file(
    source: str,
    *,
    filename: str | None,
    content_type: str | None,
    max_bytes: int | None = None,
    media_limits: bool = True,
) -> Path:
    """Copy a client-named local file into the upload directory; return the copy.

    For adapters whose clients pass *paths* (the Unix socket, mc-7kp): the
    operation must only see files the server owns, never a path it was talked
    into.  *source* must be absolute; it is opened with ``O_NOFOLLOW`` (a
    symlink as last component is refused) and ``O_NONBLOCK`` (a FIFO cannot
    hang us), and the open descriptor must be a regular file; the copy is read
    from that descriptor, so nothing can be swapped in between check and read.
    :class:`UploadGuard` cuts the copy off mid-way when a size limit is hit
    (``max_bytes`` caps this file, e.g. what is left of a total budget).
    ``media_limits=False`` is for files that are not media (mail attachments):
    only ``max_bytes`` applies, not the per-kind media limits.

    Raises ``UploadValidationError`` (413 over a limit; 400 for a path that is
    not an acceptable local regular file) and ``OSError`` on storage failures.
    The caller deletes the returned file when done.
    """
    if not isinstance(source, str) or not source or not os.path.isabs(source):
        raise UploadValidationError(400)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(source, flags)
    except OSError:
        raise UploadValidationError(400) from None
    temporary_path: Path | None = None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UploadValidationError(400)
        directory = upload_directory()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        guard = UploadGuard(
            max_bytes=max_bytes,
            declared_content_type=content_type,
            filename=filename,
            media_limits=media_limits,
        )
        suffix = Path(sanitize_filename(filename)).suffix.lower()
        with tempfile.NamedTemporaryFile(
            dir=directory,
            prefix="upload-",
            suffix=suffix if suffix in ALLOWED_EXTENSIONS else "",
            delete=False,
        ) as out:
            temporary_path = Path(out.name)
            os.chmod(temporary_path, 0o600)
            while chunk := os.read(fd, _COPY_CHUNK):
                guard.feed(chunk)
                out.write(chunk)
        staged, temporary_path = temporary_path, None
        return staged
    finally:
        os.close(fd)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def prune_upload_directory(*, now: float | None = None) -> None:
    """Delete staged files (not symlinks) older than :data:`STAGED_MAX_AGE_SECONDS`
    (leftovers of a crashed server); a missing directory is fine."""
    cutoff = (time.time() if now is None else now) - STAGED_MAX_AGE_SECONDS
    try:
        entries = list(upload_directory().iterdir())
    except OSError:
        return
    for path in entries:
        try:
            if (
                path.is_file()
                and not path.is_symlink()
                and path.stat().st_mtime < cutoff
            ):
                path.unlink()
        except OSError:
            continue
