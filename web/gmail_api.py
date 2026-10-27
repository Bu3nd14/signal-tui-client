"""FastAPI router for the Gmail reader (``/api/gmail/*`` and ``/gmail-image``).

A thin HTTP translation of the ``gmail.*`` operations (``server/ops/gmail.py``):
routes parse the request, call :func:`web.ops.call_op` and shape the response.
Everything about Gmail itself (IMAP/SMTP, validation, error texts, limits,
the image-proxy HMAC and SSRF protections) lives in the core.

Transport duties kept here: parsing the multipart ``/send`` body, copying its
files to temp files (``web.uploads``), turning blob results into responses
(and deleting those temporary files afterwards).
"""

import asyncio
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

from server.models_api import AttachmentIn
from server.uploads import (
    GMAIL_MAX_ATTACHMENT_BYTES,
    GMAIL_MAX_ATTACHMENTS,
    GMAIL_MAX_TOTAL_ATTACHMENT_BYTES,
    UploadValidationError,
)
from web.ops import call_op
from web.uploads import TempUpload, store_temp_upload

logger = logging.getLogger(__name__)


def _ascii_filename(filename: str) -> str:
    return filename.encode("ascii", errors="replace").decode("ascii").replace('"', "_")


async def _consume_blob(blob: dict[str, Any]) -> bytes:
    """Read a temporary blob an operation produced, and delete it."""
    path = Path(blob["path"])
    try:
        return await asyncio.to_thread(path.read_bytes)
    finally:
        path.unlink(missing_ok=True)


_GMAIL_IMAGE_MAX_PIXELS = 25_000_000
_GMAIL_IMAGE_CONCURRENCY = 4


def create_gmail_image_router() -> Any:
    """Same-origin image proxy for email HTML.

    The URL is carried by an HMAC-signed token, so this endpoint is safe to
    mount outside the Bearer-protected ``/api`` namespace (``<img src>`` cannot
    send an Authorization header).  The ``gmail.image`` operation verifies the
    token and fetches with the SSRF-safe resolver and raster sniffing of the
    link previews.

    The semaphore caps concurrent Gmail image fetches per app; with multiple
    uvicorn workers the aggregate concurrency is the cap times the number of
    workers.  The per-image byte and pixel caps bound each fetch; the
    per-message aggregate budget (50 images) is documented as a residual risk
    in ``docs/DESIGN_GMAIL_IMAGES_DEFAULT.md``.
    """
    from fastapi import APIRouter, Request
    from fastapi.responses import Response

    router = APIRouter()
    image_semaphore = asyncio.Semaphore(_GMAIL_IMAGE_CONCURRENCY)

    @router.get("/gmail-image")
    async def image(request: Request, token: str) -> Response:
        async with image_semaphore:
            blob = await call_op(request, "gmail.image", {"token": token})
        data = await _consume_blob(blob)
        return Response(
            content=data,
            media_type=blob["content_type"],
            headers={
                # A remote image's token is stable for its URL: the browser
                # reuses it when the message is reopened (fewer fetches and
                # fewer tracking pings).  "private" keeps shared caches, such
                # as a tunnel or reverse proxy, from storing it.
                "Cache-Control": "private, max-age=3600",
                "Content-Security-Policy": "default-src 'none'",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return router


def create_gmail_router() -> Any:
    """Build the Gmail router without importing FastAPI at module import time."""
    from fastapi import APIRouter, HTTPException, Request
    from fastapi.responses import Response
    from starlette.exceptions import HTTPException as StarletteHTTPException

    router = APIRouter(prefix="/api/gmail")

    @router.get("/status")
    async def status(request: Request) -> dict[str, Any]:
        return await call_op(request, "gmail.status")

    @router.get("/unread")
    async def unread(request: Request) -> dict[str, Any]:
        return await call_op(request, "gmail.unread")

    @router.get("/messages")
    async def messages(
        request: Request,
        folder: str | None = None,
        offset: int = 0,
        limit: int = 50,
        refresh: bool = False,
    ) -> dict[str, Any]:
        return await call_op(
            request,
            "gmail.messages.list",
            {"folder": folder, "offset": offset, "limit": limit, "refresh": refresh},
        )

    @router.post("/messages/bulk-delete")
    async def bulk_delete(request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - malformed JSON body
            raise HTTPException(
                status_code=400, detail="Richiesta non valida"
            ) from None
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Richiesta non valida")
        return await call_op(
            request,
            "gmail.messages.bulk_delete",
            {"uids": payload.get("uids"), "folder": payload.get("folder")},
        )

    @router.delete("/messages/{uid}")
    async def delete(
        request: Request, uid: int, folder: str | None = None
    ) -> dict[str, bool]:
        return await call_op(
            request, "gmail.messages.delete", {"uid": uid, "folder": folder}
        )

    @router.get("/messages/{uid}")
    async def message(
        request: Request, uid: int, folder: str | None = None
    ) -> dict[str, Any]:
        return await call_op(
            request, "gmail.messages.get", {"uid": uid, "folder": folder}
        )

    @router.get("/messages/{uid}/attachments/{index}")
    async def attachment(
        request: Request, uid: int, index: int, folder: str | None = None
    ) -> Response:
        blob = await call_op(
            request,
            "gmail.attachment",
            {"uid": uid, "index": index, "folder": folder},
        )
        data = await _consume_blob(blob)
        filename = blob["filename"]
        encoded = quote(filename, safe="")
        disposition = (
            f'attachment; filename="{_ascii_filename(filename)}"; '
            f"filename*=UTF-8''{encoded}"
        )
        return Response(
            content=data,
            media_type=blob["content_type"],
            headers={
                "Content-Disposition": disposition,
                "Content-Length": str(len(data)),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.post("/messages/{uid}/read")
    async def read(
        request: Request, uid: int, folder: str | None = None
    ) -> dict[str, bool]:
        return await call_op(
            request, "gmail.messages.mark_read", {"uid": uid, "folder": folder}
        )

    @router.post("/messages/{uid}/unread")
    async def unread_message(
        request: Request, uid: int, folder: str | None = None
    ) -> dict[str, bool]:
        return await call_op(
            request, "gmail.messages.mark_unread", {"uid": uid, "folder": folder}
        )

    @router.post("/messages/{uid}/archive")
    async def archive(
        request: Request, uid: int, folder: str | None = None
    ) -> dict[str, bool]:
        return await call_op(
            request, "gmail.messages.archive", {"uid": uid, "folder": folder}
        )

    @router.post("/send")
    async def send(request: Request) -> dict[str, Any]:
        try:
            form = await request.form(
                max_files=GMAIL_MAX_ATTACHMENTS + 1, max_fields=20
            )
        except StarletteHTTPException as exc:
            status_code = exc.status_code if exc.status_code in (400, 413) else 400
            raise HTTPException(
                status_code=status_code, detail="Richiesta non valida"
            ) from None
        except Exception:  # noqa: BLE001 - malformed multipart body
            raise HTTPException(
                status_code=400, detail="Richiesta non valida"
            ) from None

        def field(name: str) -> str:
            value = form.get(name)
            return value if isinstance(value, str) else ""

        def optional_uid(name: str) -> int | None:
            raw = field(name)
            if not raw:
                return None
            try:
                return int(raw)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail=f"{name} non valido"
                ) from None

        payload: dict[str, Any] = {
            "to": field("to"),
            "cc": field("cc"),
            "bcc": field("bcc"),
            "subject": field("subject"),
            "text": field("text"),
            "reply_to_uid": optional_uid("reply_to_uid"),
            "reply_to_folder": field("reply_to_folder") or None,
            "forward_uid": optional_uid("forward_uid"),
            "forward_folder": field("forward_folder") or None,
        }
        uploads = form.getlist("file[]")
        if uploads:
            # Cheap rejections (400) before any upload touches the disk.
            await call_op(
                request,
                "gmail.messages.send.check",
                {
                    **payload,
                    "attachments": [],
                    "attachment_count": len(uploads),
                    "slots_ok": all(hasattr(upload, "read") for upload in uploads),
                },
            )

        stored: list[TempUpload] = []
        try:
            try:
                attachments = await _store_attachments(uploads, stored)
            except Exception:
                logger.exception("Gmail upload store failed")
                raise HTTPException(status_code=502, detail="Errore Gmail") from None
            # ``attachments`` are ours: this route takes no files from a body.
            return await call_op(
                request, "gmail.messages.send", {**payload, "attachments": attachments}
            )
        finally:
            # Temp files live only for this request, whatever the outcome.
            for temp_upload in stored:
                temp_upload.cleanup()

    return router


async def _store_attachments(
    uploads: list[Any], stored: list[TempUpload]
) -> list[dict[str, Any]]:
    """Copy each upload to a temp file; hand the *paths* to the operation.

    Transport keeps the disk bounded: a file over the per-file limit is cut off
    mid-copy and becomes a ``rejected`` slot (the operation reports it in order,
    after earlier files), and no further file is copied once the total limit is
    passed.  The operation re-checks every size on the stored copies.
    """
    slots: list[AttachmentIn] = []
    total = 0
    done = 0
    for upload in uploads:
        done += 1
        try:
            temp = await store_temp_upload(
                upload, max_bytes=GMAIL_MAX_ATTACHMENT_BYTES, media_limits=False
            )
        except UploadValidationError:
            slots.append(AttachmentIn(rejected="too_large"))
            break
        stored.append(temp)
        slots.append(
            AttachmentIn(
                path=str(temp.path),
                filename=temp.filename or "attachment",
                content_type=temp.content_type,
            )
        )
        total += temp.size
        if total > GMAIL_MAX_TOTAL_ATTACHMENT_BYTES:
            break
    for unread in uploads[done:]:
        await unread.close()
    return [slot.model_dump() for slot in slots]
