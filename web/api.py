"""REST API for contacts, persisted messages, media, and message sending."""

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from server.media import (
    _IMAGE_EXTENSIONS,
    _VIDEO_EXTENSIONS,
    _allowed_media_root,
    _attach_media_versions,  # noqa: F401  (re-export)
    _attachment_version,  # noqa: F401  (re-export)
)
from server.messages import (
    _cross_key_dedup_rows,  # noqa: F401  (re-export)
    _group_sender_resolver,  # noqa: F401  (re-export)
    _messages,  # noqa: F401  (re-export; patch server.messages._messages)
    _quoted_media_by_filename,  # noqa: F401  (re-export)
)
from server.models_api import AttachmentIn
from server.ops.common import PROTOCOLS as _PROTOCOLS
from server.uploads import MAX_ATTACHMENTS, UploadValidationError
from web import uploads as uploads_module
from web.ops import call_op
from web.uploads import TempUpload, store_temp_upload

logger = logging.getLogger(__name__)

_MAX_ATTACHMENTS = MAX_ATTACHMENTS
_THUMB_WIDTHS = {96, 240, 480}
_THUMB_CACHE_LIMIT = 500 * 1024 * 1024
_THUMB_LOCKS: dict[Path, threading.Lock] = {}
_THUMB_LOCKS_GUARD = threading.Lock()

_AUDIO_MIME_BY_EXT: dict[str, str] = {
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".oga": "audio/ogg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
}


def _media_content_type(path: Path, *, prefer_audio: bool = False) -> str | None:
    """Return deterministic audio MIME types and guess all other types.

    ``.webm`` è ambiguo (video o audio): ``prefer_audio`` lo serve come
    ``audio/webm`` quando il messaggio lo classifica come audio.
    """
    ext = path.suffix.lower()
    if ext in _AUDIO_MIME_BY_EXT:
        return _AUDIO_MIME_BY_EXT[ext]
    if prefer_audio and ext == ".webm":
        return "audio/webm"
    return mimetypes.guess_type(path.name)[0]


def _effective_host(request: Any) -> str:
    """Return the browser-facing host, trusting proxies only for HTTPS origins."""
    forwarded = request.headers.get("x-forwarded-host")
    origin = request.headers.get("origin", "")
    forwarded_proto = request.headers.get("x-forwarded-proto")
    if (
        forwarded
        and urlsplit(origin).scheme.lower() == "https"
        and (
            not forwarded_proto
            or forwarded_proto.split(",", 1)[0].strip().lower() == "https"
        )
    ):
        return forwarded.split(",", 1)[0].strip().lower()
    return (request.headers.get("host") or "").lower()


def _web_thumb_dir(proto: str) -> Path:
    cache_home = Path(
        os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")
    ).expanduser()
    return cache_home / "signal-tui-client" / "web-thumbs" / proto


def _attachment_content_type(proto: str, attachment_id: str) -> str | None:
    import protocols.db as backend
    from protocols.db import _DB_LOCK

    with _DB_LOCK:
        try:
            connection = sqlite3.connect(backend.DB_FILE)
            try:
                row = connection.execute(
                    "SELECT content_type, msg_type FROM messages "
                    "WHERE protocol = ? AND attachment_id = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (proto, attachment_id),
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            return None
    if not row:
        return None
    return row[0] or ("image/*" if row[1] == "image" else None)


def _attachment_media_kind(proto: str, attachment_id: str) -> str | None:
    import protocols.db as backend
    from protocols.db import _DB_LOCK

    with _DB_LOCK:
        try:
            connection = sqlite3.connect(backend.DB_FILE)
            try:
                row = connection.execute(
                    "SELECT media_kind FROM messages "
                    "WHERE protocol = ? AND attachment_id = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (proto, attachment_id),
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            return None
    if not row:
        return None
    return row[0]


def _is_thumbnail_candidate(path: Path, proto: str, attachment_id: str) -> bool:
    content_type = (_attachment_content_type(proto, attachment_id) or "").lower()
    suffix = path.suffix.lower()
    is_image = content_type.startswith("image/") or suffix in _IMAGE_EXTENSIONS
    return (
        is_image
        and suffix not in {".gif", ".heic", ".heif"}
        and content_type
        not in {
            "image/gif",
            "image/heic",
            "image/heif",
        }
    )


def _is_video_candidate(path: Path, proto: str, attachment_id: str) -> bool:
    content_type = (_attachment_content_type(proto, attachment_id) or "").lower()
    # Un audio (es. voice webm/m4a) non deve mai passare per la pipeline
    # thumbnail video, anche quando l'estensione `.webm` è nella lista video.
    if content_type.startswith("audio/"):
        return False
    if not content_type and _attachment_media_kind(proto, attachment_id) == "audio":
        return False
    return content_type.startswith("video/") or path.suffix.lower() in _VIDEO_EXTENSIONS


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


async def _store_attachments(
    upload_files: list[Any], stored: list[TempUpload]
) -> list[AttachmentIn]:
    """Copy multipart files to temp files; return blob handles for the operation.

    Appends each temp file to *stored* (the caller deletes them).  A file over
    the request-wide byte budget is not an error here: its slot (and the rest)
    is marked ``rejected`` so the operation reports problems in file order.
    Files are not stored at all when there are too many (the operation answers
    "Too many attachments"); a field that is not a file gets an empty handle.
    """
    attachments: list[AttachmentIn] = []
    store = len(upload_files) <= MAX_ATTACHMENTS
    rejected = False
    total_stored_bytes = 0
    for upload_file in upload_files:
        if not hasattr(upload_file, "read"):
            attachments.append(AttachmentIn())
            continue
        if not store:
            attachments.append(AttachmentIn())
            continue
        if rejected:
            attachments.append(AttachmentIn(rejected="too_large"))
            continue
        try:
            # The request-wide byte cap is enforced WHILE each file is stored
            # (each file gets the remaining budget), so a chunked request
            # cannot write far beyond _MAX_TOTAL_BYTES before being rejected.
            temp_upload = await store_temp_upload(
                upload_file,
                max_bytes=uploads_module._MAX_TOTAL_BYTES - total_stored_bytes,
            )
        except UploadValidationError:
            rejected = True
            attachments.append(AttachmentIn(rejected="too_large"))
            continue
        stored.append(temp_upload)
        total_stored_bytes += temp_upload.size
        attachments.append(
            AttachmentIn(
                path=str(temp_upload.path),
                filename=temp_upload.filename,
                content_type=temp_upload.content_type,
            )
        )
    return attachments


def create_api_router() -> Any:
    """Build the FastAPI router without making FastAPI a core dependency."""
    from fastapi import APIRouter, HTTPException, Request
    from fastapi.responses import FileResponse, JSONResponse, Response
    from starlette.exceptions import HTTPException as StarletteHTTPException

    router = APIRouter(prefix="/api")

    @router.get("/emoji")
    async def emojis(request: Request) -> JSONResponse:
        return JSONResponse(
            content=await call_op(request, "emoji.list"),
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @router.get("/reactions")
    async def reactions(request: Request, protocol: str) -> dict[str, list[str]]:
        return await call_op(request, "reactions.available", {"protocol": protocol})

    @router.post("/send")
    async def send(request: Request) -> dict[str, bool]:
        """Transport only: CSRF/size checks, body parsing, uploads -> temp files.

        Everything else (validation, idempotency, retry, the backend calls and
        the ``/ws`` frames) is the ``messages.send`` operation.
        """
        origin = request.headers.get("origin")
        if origin:
            origin_host = urlsplit(origin).netloc.lower()
            request_host = _effective_host(request)
            if not origin_host or origin_host != request_host:
                raise HTTPException(status_code=403, detail="Forbidden")

        is_multipart = (
            request.headers.get("content-type", "")
            .lower()
            .startswith("multipart/form-data")
        )
        upload_files: list[Any] = []
        try:
            if is_multipart:
                _MAX_TOTAL_BYTES = uploads_module._MAX_TOTAL_BYTES
                content_length = request.headers.get("content-length")
                if (
                    content_length
                    and int(content_length) > _MAX_TOTAL_BYTES + 1024 * 1024
                ):
                    raise HTTPException(status_code=413, detail="Upload too large")
                try:
                    form = await request.form(
                        max_files=_MAX_ATTACHMENTS + 10, max_fields=20
                    )
                except StarletteHTTPException as exc:
                    # Normalize Starlette's early "Too many files" rejection
                    # without swallowing unrelated HTTP exceptions.  The
                    # starlette base class also covers fastapi.HTTPException.
                    if exc.status_code == 400 and str(exc.detail).startswith(
                        "Too many files"
                    ):
                        raise HTTPException(
                            status_code=400, detail="Too many attachments"
                        ) from None
                    raise
                payload = dict(form)
                upload_files = form.getlist("file")
                payload.pop("file", None)
            else:
                payload = await request.json()
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="Invalid request") from None
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid request")

        if is_multipart:
            # Form fields are strings: "" means absent, digits mean an int.  A
            # non-numeric value is passed through for the operation to reject.
            quote_timestamp = payload.get("quote_timestamp")
            if quote_timestamp == "":
                payload["quote_timestamp"] = None
            elif quote_timestamp is not None:
                try:
                    payload["quote_timestamp"] = int(quote_timestamp)
                except (TypeError, ValueError):
                    pass

        if upload_files:
            # Cheap rejections (400/404) before any upload touches the disk.
            await call_op(
                request,
                "messages.send.check",
                {
                    **payload,
                    "attachments": [],
                    "attachment_count": len(upload_files),
                    "slots_ok": all(hasattr(f, "read") for f in upload_files),
                },
            )

        stored: list[TempUpload] = []
        try:
            try:
                attachments = await _store_attachments(upload_files, stored)
            except Exception:
                logger.exception(
                    "Web upload store failed: protocol=%s contact=%s",
                    payload.get("protocol"),
                    payload.get("contact_id"),
                )
                raise HTTPException(
                    status_code=502, detail="Message send failed"
                ) from None
            # ``attachments`` are ours: never trust one from the request body.
            return await call_op(
                request, "messages.send", {**payload, "attachments": attachments}
            )
        finally:
            # Temp files live only for this request, whatever the outcome.
            for temp_upload in stored:
                temp_upload.cleanup()

    @router.get("/status")
    async def backend_status(request: Request) -> dict[str, bool]:
        return await call_op(request, "server.status")

    @router.get("/contacts")
    async def contacts(request: Request, q: str | None = None) -> list[dict[str, Any]]:
        return await call_op(request, "contacts.list", {"q": q})

    @router.get("/contacts/book")
    async def contacts_book(
        request: Request, q: str, force: bool = False
    ) -> list[dict[str, Any]]:
        return await call_op(request, "contacts.book", {"q": q, "force": force})

    @router.get("/contact-avatar")
    async def contact_avatar(request: Request, proto: str, contact_id: str) -> Any:
        if proto not in {"whatsapp", "signal", "telegram"}:
            raise HTTPException(status_code=404, detail="Not Found")
        if not contact_id.strip():
            raise HTTPException(status_code=400, detail="Invalid request")
        from web.avatar_cache import resolve_avatar

        result = await asyncio.to_thread(
            resolve_avatar, proto, request.app.state.manager, contact_id
        )
        if result is None:
            raise HTTPException(status_code=404, detail="No profile picture")
        data, content_type = result
        return Response(
            content=data,
            media_type=content_type,
            headers={"Cache-Control": "private, max-age=86400"},
        )

    @router.get("/link-preview")
    async def link_preview(url: str) -> Any:
        from web.link_preview import BlockedURLError, get_preview

        try:
            preview = await get_preview(url)
        except BlockedURLError:
            raise HTTPException(status_code=400, detail="URL not allowed") from None
        if preview is None:
            raise HTTPException(status_code=404, detail="No preview")
        return JSONResponse(
            content={
                "url": preview["url"],
                "title": preview["title"],
                "description": preview["description"],
                "site_name": preview["site_name"],
                "has_image": bool(
                    preview.get("image_url") or preview.get("local_image")
                ),
            },
            headers={"Cache-Control": "private, max-age=3600"},
        )

    @router.get("/link-preview/image")
    async def link_preview_image(url: str) -> Any:
        from web.link_preview import BlockedURLError, get_preview_image

        try:
            result = await get_preview_image(url)
        except BlockedURLError:
            raise HTTPException(status_code=400, detail="URL not allowed") from None
        if result is None:
            raise HTTPException(status_code=404, detail="No preview image")
        data, content_type = result
        return Response(
            content=data,
            media_type=content_type,
            headers={
                "Cache-Control": "private, max-age=86400",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.post("/log")
    def client_log(payload: dict[str, Any]) -> dict[str, str]:
        message = str(payload.get("message") or "")[:500]
        logger.info("[web-client] %s", message)
        return {"status": "ok"}

    @router.post("/transcribe")
    def transcribe(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        proto = payload.get("protocol")
        attachment_id = payload.get("attachment_id")
        if proto not in _PROTOCOLS or not isinstance(attachment_id, str):
            raise HTTPException(status_code=400, detail="Invalid request")
        attachment_id = attachment_id.strip()
        if not attachment_id:
            raise HTTPException(status_code=400, detail="Invalid request")

        service = getattr(request.app.state, "transcription_service", None)
        if service is None:
            raise HTTPException(status_code=503, detail="Trascrizione non configurata")
        current = service.status(proto, attachment_id)
        if current is not None and current["status"] == "ok":
            return {"status": "done", "text": current.get("text", "")}
        if current is not None and current["status"] == "pending":
            return {"status": "pending"}

        path = request.app.state.manager.get_attachment_path(proto, attachment_id)
        if path is None:
            raise HTTPException(status_code=404, detail="Allegato non trovato")
        service.submit(proto, attachment_id, path)
        return {"status": "pending"}

    @router.post("/config/openai-key")
    def save_openai_key(request: Request, payload: dict[str, Any]) -> dict[str, str]:
        api_key = payload.get("api_key")
        if not isinstance(api_key, str):
            raise HTTPException(status_code=400, detail="Chiave non valida")
        api_key = api_key.strip()
        if not api_key or not api_key.startswith("sk-"):
            raise HTTPException(status_code=400, detail="Chiave non valida")

        from protocols.config import PROJECT_DIR, _load_config

        config_path = PROJECT_DIR / "config.json"
        data = _load_config()
        data["openai_api_key"] = api_key
        try:
            with config_path.open("w", encoding="utf-8") as config_file:
                json.dump(data, config_file, indent=2)
                config_file.write("\n")
        except OSError:
            logger.exception("Impossibile salvare la configurazione OpenAI")
            raise HTTPException(
                status_code=500, detail="Errore di salvataggio"
            ) from None

        try:
            from web.server import _init_transcription_service

            _init_transcription_service(request.app)
        except Exception:
            logger.exception("Impossibile inizializzare la trascrizione OpenAI")
            raise HTTPException(
                status_code=500, detail="Errore di inizializzazione"
            ) from None
        return {"status": "ok"}

    @router.get("/transcribe/config")
    def transcription_config(request: Request) -> dict[str, bool]:
        service = getattr(request.app.state, "transcription_service", None)
        return {"enabled": service is not None}

    @router.get("/transcribe/{proto}/{attachment_id:path}")
    def transcription_status(
        request: Request,
        proto: Literal["signal", "whatsapp", "telegram"],
        attachment_id: str,
    ) -> dict[str, Any]:
        service = getattr(request.app.state, "transcription_service", None)
        if service is None:
            raise HTTPException(status_code=503, detail="Trascrizione non configurata")
        current = service.status(proto, attachment_id)
        return current if current is not None else {"status": "unknown"}

    @router.get("/messages")
    async def messages(
        request: Request,
        proto: Literal["signal", "whatsapp", "telegram"],
        contact_id: str,
        refresh: bool = False,
        limit: int | None = None,
        since: int | None = None,
    ) -> list[dict[str, Any]]:
        return await call_op(
            request,
            "messages.page",
            {
                "protocol": proto,
                "contact_id": contact_id,
                "refresh": refresh,
                "limit": limit,
                "since": since,
            },
        )

    @router.post("/messages/read")
    async def messages_read(
        request: Request, payload: dict[str, Any]
    ) -> dict[str, str]:
        return await call_op(request, "chats.mark_read", payload)

    @router.post("/chats/archive")
    async def chats_archive(
        request: Request, payload: dict[str, Any]
    ) -> dict[str, bool]:
        return await call_op(request, "chats.archive", payload)

    @router.post("/messages/edit")
    async def messages_edit(
        request: Request, payload: dict[str, Any]
    ) -> dict[str, bool]:
        return await call_op(request, "messages.edit", payload)

    @router.post("/messages/reaction")
    async def messages_reaction(
        request: Request, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await call_op(request, "reactions.set", payload)

    @router.get("/media/{proto}/{attachment_id:path}")
    def media(
        request: Request,
        proto: Literal["signal", "whatsapp", "telegram"],
        attachment_id: str,
        w: int | None = None,
    ) -> Any:
        manager = request.app.state.manager
        root = _allowed_media_root(manager, proto)
        path = None
        video_request = w in _THUMB_WIDTHS and _is_video_candidate(
            Path(attachment_id), proto, attachment_id
        )
        chunk_available = False
        if video_request and proto in {"whatsapp", "telegram"}:
            from web.video_thumbs import _chunk_video_thumbnail

            thumb, chunk_available = _chunk_video_thumbnail(
                manager, proto, attachment_id, w
            )
            if thumb is not None:
                return FileResponse(
                    thumb,
                    media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=31536000, immutable"},
                )
        try:
            resolved = manager.get_attachment_path(proto, attachment_id)
        except Exception:  # noqa: BLE001
            logger.warning(
                "web media 404 proto=%s attachment_id=%s resolved=%s",
                proto,
                attachment_id,
                path,
            )
            if chunk_available:
                raise HTTPException(
                    status_code=422, detail="Video thumbnail unavailable"
                ) from None
            raise HTTPException(status_code=404) from None
        path = Path(resolved).resolve() if resolved else None
        if path is None or not path.is_file() or not path.is_relative_to(root):
            logger.warning(
                "web media 404 proto=%s attachment_id=%s resolved=%s",
                proto,
                attachment_id,
                path,
            )
            if chunk_available:
                raise HTTPException(
                    status_code=422, detail="Video thumbnail unavailable"
                )
            raise HTTPException(status_code=404)
        logger.info(
            "web media ok proto=%s attachment_id=%s path=%s w=%s",
            proto,
            attachment_id,
            path,
            w,
        )
        if w in _THUMB_WIDTHS:
            thumb = _thumbnail(path, proto, attachment_id, w)
            if thumb is not None:
                return FileResponse(
                    thumb,
                    media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=31536000, immutable"},
                )
            if video_request or _is_video_candidate(path, proto, attachment_id):
                from web.video_thumbs import _video_thumbnail

                thumb = _video_thumbnail(path, proto, attachment_id, w)
                if thumb is None:
                    raise HTTPException(
                        status_code=422, detail="Video thumbnail unavailable"
                    )
                return FileResponse(
                    thumb,
                    media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=31536000, immutable"},
                )
        # R7: `.webm` è ambiguo; il media_kind persistito decide audio vs video.
        prefer_audio = (
            path.suffix.lower() == ".webm"
            and _attachment_media_kind(proto, attachment_id) == "audio"
        )
        media_type = _media_content_type(path, prefer_audio=prefer_audio)
        return FileResponse(
            path,
            media_type=media_type,
            headers={"Cache-Control": "private, max-age=86400"},
        )

    @router.get("/quote-media/{proto}/{message_row_id}")
    def quote_media(
        proto: Literal["signal", "whatsapp", "telegram"],
        message_row_id: int,
        w: int | None = None,
    ) -> Any:
        import protocols.db as backend
        from protocols.db import _DB_LOCK

        with _DB_LOCK:
            try:
                connection = sqlite3.connect(backend.DB_FILE)
                try:
                    row = connection.execute(
                        "SELECT quote_attachment_path FROM messages "
                        "WHERE id = ? AND protocol = ?",
                        (message_row_id, proto),
                    ).fetchone()
                finally:
                    connection.close()
            except sqlite3.Error:
                logger.exception(
                    "Web quote media lookup database error: proto=%s row_id=%s",
                    proto,
                    message_row_id,
                )
                raise HTTPException(status_code=500, detail="Database error") from None

        stored = row[0] if row else None
        root = (Path(backend.CACHE_DIR) / "quote-thumbs").resolve()
        path = Path(stored).resolve() if stored else None
        if path is None or not path.is_file() or path.parent != root:
            logger.warning(
                "web quote media 404 proto=%s row_id=%s resolved=%s",
                proto,
                message_row_id,
                path,
            )
            raise HTTPException(status_code=404)

        served_path = path
        cache_control = "private, max-age=86400"
        if w in _THUMB_WIDTHS:
            thumb = _thumbnail(path, proto, str(message_row_id), w)
            if thumb is not None:
                served_path = thumb
                cache_control = "private, max-age=31536000, immutable"
        media_type, _ = mimetypes.guess_type(served_path.name)
        return FileResponse(
            served_path,
            media_type=media_type,
            headers={"Cache-Control": cache_control},
        )

    return router
