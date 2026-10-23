"""FastAPI router for the Gmail POC (``/api/gmail/*``).

Every blocking IMAP/SMTP operation runs through ``asyncio.to_thread`` so the
uvicorn event loop is never blocked.  Errors are mapped to generic HTTP
responses: credentials and upstream details never reach the client or logs.
"""

import asyncio
import imaplib
import logging
import smtplib
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from web import gmail

logger = logging.getLogger(__name__)


def _ascii_filename(filename: str) -> str:
    return filename.encode("ascii", errors="replace").decode("ascii").replace('"', "_")


async def _run(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a blocking Gmail call off-thread and map failures to HTTP errors."""
    from fastapi import HTTPException

    try:
        return await asyncio.to_thread(function, *args, **kwargs)
    except HTTPException:
        raise
    except gmail.GmailNotConfigured:
        raise HTTPException(status_code=503, detail="Gmail non configurato") from None
    except gmail.GmailValidationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    except gmail.GmailNotFound:
        raise HTTPException(status_code=404, detail="Risorsa non trovata") from None
    except gmail.GmailUpstreamError:
        logger.warning("Gmail upstream error (%s)", function.__name__)
        raise HTTPException(
            status_code=502, detail="Errore di comunicazione con Gmail"
        ) from None
    except (imaplib.IMAP4.error, smtplib.SMTPException, OSError):
        logger.warning("Gmail transport error (%s)", function.__name__)
        raise HTTPException(
            status_code=502, detail="Errore di comunicazione con Gmail"
        ) from None
    except Exception as exc:  # noqa: BLE001 - never leak upstream details
        logger.warning(
            "Gmail request failed (%s: %s)", function.__name__, type(exc).__name__
        )
        raise HTTPException(status_code=502, detail="Errore Gmail") from None


_IMAGE_PROXY_MAX_BYTES = 8 * 1024 * 1024


def create_gmail_image_router() -> Any:
    """Same-origin image proxy for email HTML.

    The URL is carried by an HMAC-signed token, so this endpoint is safe to
    mount outside the Bearer-protected ``/api`` namespace (``<img src>`` cannot
    send an Authorization header).  Fetching reuses the SSRF-safe resolver and
    raster sniffing from ``web.link_preview``.
    """
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import Response

    from web.link_preview import fetch_external_image

    router = APIRouter()

    @router.get("/gmail-image")
    async def image(token: str) -> Response:
        url = gmail.verify_image_token(token)
        if not url:
            raise HTTPException(status_code=404, detail="Immagine non trovata")
        result = await fetch_external_image(url, max_bytes=_IMAGE_PROXY_MAX_BYTES)
        if not result:
            raise HTTPException(status_code=502, detail="Immagine non disponibile")
        data, content_type = result
        return Response(
            content=data,
            media_type=content_type,
            headers={
                "Cache-Control": "no-store",
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
    def status() -> dict[str, Any]:
        return gmail.status_snapshot()

    @router.get("/messages")
    async def messages(
        folder: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        return await _run(
            gmail.fetch_message_list,
            folder=folder,
            offset=offset,
            limit=limit,
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
        folder = payload.get("folder")
        if folder is not None and not isinstance(folder, str):
            raise HTTPException(status_code=400, detail="Cartella non valida")
        return await _run(gmail.delete_messages, payload.get("uids"), folder=folder)

    @router.delete("/messages/{uid}")
    async def delete(uid: int, folder: str | None = None) -> dict[str, bool]:
        await _run(gmail.delete_message, uid, folder=folder)
        return {"ok": True, "trashed": True}

    @router.get("/messages/{uid}")
    async def message(uid: int, folder: str | None = None) -> dict[str, Any]:
        return await _run(gmail.fetch_message, uid, folder=folder)

    @router.get("/messages/{uid}/attachments/{index}")
    async def attachment(uid: int, index: int, folder: str | None = None) -> Response:
        download = await _run(gmail.fetch_attachment, uid, index, folder=folder)
        encoded = quote(download.filename, safe="")
        disposition = (
            f'attachment; filename="{_ascii_filename(download.filename)}"; '
            f"filename*=UTF-8''{encoded}"
        )
        return Response(
            content=download.data,
            media_type=download.content_type,
            headers={
                "Content-Disposition": disposition,
                "Content-Length": str(len(download.data)),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.post("/messages/{uid}/read")
    async def read(uid: int, folder: str | None = None) -> dict[str, bool]:
        await _run(gmail.mark_read, uid, folder=folder)
        return {"ok": True}

    @router.post("/send")
    async def send(request: Request) -> dict[str, Any]:
        try:
            form = await request.form(
                max_files=gmail._MAX_ATTACHMENTS + 1, max_fields=20
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

        reply_to_uid: int | None = None
        reply_raw = field("reply_to_uid")
        if reply_raw:
            try:
                reply_to_uid = int(reply_raw)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="reply_to_uid non valido"
                ) from None

        uploads = form.getlist("file[]")
        if len(uploads) > gmail._MAX_ATTACHMENTS:
            raise HTTPException(status_code=400, detail="Troppi allegati")
        if any(not hasattr(upload, "read") for upload in uploads):
            raise HTTPException(status_code=400, detail="Richiesta non valida")

        attachments: list[tuple[str, bytes]] = []
        total = 0
        for upload in uploads:
            data = await upload.read(gmail._MAX_ATTACHMENT_BYTES + 1)
            await upload.close()
            if len(data) > gmail._MAX_ATTACHMENT_BYTES:
                raise HTTPException(status_code=413, detail="Allegato troppo grande")
            total += len(data)
            if total > gmail._MAX_TOTAL_ATTACHMENT_BYTES:
                raise HTTPException(status_code=413, detail="Allegati troppo grandi")
            attachments.append((upload.filename or "attachment", data))

        message_id = await _run(
            gmail.send_email,
            to=field("to"),
            cc=field("cc"),
            bcc=field("bcc"),
            subject=field("subject"),
            text=field("text"),
            reply_to_uid=reply_to_uid,
            reply_to_folder=field("reply_to_folder") or None,
            attachments=attachments,
        )
        return {"ok": True, "message_id": message_id}

    return router
