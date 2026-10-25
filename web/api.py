"""REST API for contacts, persisted messages, media, and message sending."""

import asyncio
import logging
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from server.media import (
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
from server.uploads import MAX_ATTACHMENTS, UploadValidationError
from web import uploads as uploads_module
from web.ops import call_op
from web.uploads import TempUpload, store_temp_upload

logger = logging.getLogger(__name__)

_MAX_ATTACHMENTS = MAX_ATTACHMENTS


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

    def _blob_response(blob: dict[str, Any]) -> Any:
        """Stream the file of a blob handle with its cache policy."""
        cache_control = (
            "private, max-age=31536000, immutable"
            if blob["immutable"]
            else "private, max-age=86400"
        )
        return FileResponse(
            blob["path"],
            media_type=blob["content_type"],
            headers={"Cache-Control": cache_control},
        )

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
        # Operation-level knobs of local frontends (the TUI's optimistic send,
        # its single-attempt policy): never settable from a web request.
        payload.pop("optimistic", None)
        payload.pop("retry", None)

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
        blob = await call_op(
            request, "contacts.avatar", {"protocol": proto, "contact_id": contact_id}
        )
        # Small file: sent as a plain body (no ETag/Range), as it always was.
        data = await asyncio.to_thread(Path(blob["path"]).read_bytes)
        return Response(
            content=data,
            media_type=blob["content_type"],
            headers={"Cache-Control": "private, max-age=86400"},
        )

    @router.get("/link-preview")
    async def link_preview(request: Request, url: str) -> Any:
        preview = await call_op(request, "links.preview", {"url": url})
        return JSONResponse(
            content=preview,
            headers={"Cache-Control": "private, max-age=3600"},
        )

    @router.get("/link-preview/image")
    async def link_preview_image(request: Request, url: str) -> Any:
        blob = await call_op(request, "links.preview_image", {"url": url})
        # Small file: a plain body, with the type the operation sniffed.
        data = await asyncio.to_thread(Path(blob["path"]).read_bytes)
        return Response(
            content=data,
            media_type=blob["content_type"],
            headers={
                "Cache-Control": "private, max-age=86400",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.post("/log")
    async def client_log(request: Request, payload: dict[str, Any]) -> Any:
        return await call_op(
            request,
            "server.client_log",
            {"message": payload.get("message"), "source": "web-client"},
        )

    @router.post("/transcribe")
    async def transcribe(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        return await call_op(
            request,
            "transcribe.submit",
            {
                "protocol": payload.get("protocol"),
                "attachment_id": payload.get("attachment_id"),
            },
        )

    @router.post("/config/openai-key")
    async def save_openai_key(
        request: Request, payload: dict[str, Any]
    ) -> dict[str, str]:
        return await call_op(
            request, "config.set_openai_key", {"api_key": payload.get("api_key")}
        )

    @router.get("/transcribe/config")
    async def transcription_config(request: Request) -> dict[str, bool]:
        return await call_op(request, "transcribe.config")

    @router.get("/transcribe/{proto}/{attachment_id:path}")
    async def transcription_status(
        request: Request,
        proto: Literal["signal", "whatsapp", "telegram"],
        attachment_id: str,
    ) -> dict[str, Any]:
        return await call_op(
            request,
            "transcribe.status",
            {"protocol": proto, "attachment_id": attachment_id},
        )

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
    async def media(
        request: Request,
        proto: Literal["signal", "whatsapp", "telegram"],
        attachment_id: str,
        w: int | None = None,
    ) -> Any:
        data: dict[str, Any] = {"protocol": proto, "attachment_id": attachment_id}
        if w is None:
            blob = await call_op(request, "media.attachment", data)
        else:
            blob = await call_op(request, "media.thumbnail", {**data, "width": w})
        return _blob_response(blob)

    @router.get("/quote-media/{proto}/{message_row_id}")
    async def quote_media(
        request: Request,
        proto: Literal["signal", "whatsapp", "telegram"],
        message_row_id: int,
        w: int | None = None,
    ) -> Any:
        blob = await call_op(
            request,
            "media.quote",
            {"protocol": proto, "message_row_id": message_row_id, "width": w},
        )
        return _blob_response(blob)

    return router
