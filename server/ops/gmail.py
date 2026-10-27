"""Gmail operations: the IMAP/SMTP reader and sender (``server/gmail.py``).

Every operation but ``gmail.image`` and ``gmail.messages.get`` is sync and
blocks on the network: an adapter runs it in a worker thread
(``web.ops.call_op`` does).  ``gmail.messages.get`` is async only to start,
on the server loop, the background prefetch of the message's remote images.  The texts of
the errors are the ones the Gmail routes have always sent (they are Italian,
like the rest of the Gmail UI); credentials and upstream details never reach
an error message or a log line.

Mapping (``gmail`` exception -> code, message):

* not configured        -> ``backend_down`` "Gmail non configurato"
* validation            -> ``invalid_request`` (``too_large`` for 413), its text
* not found             -> ``not_found`` "Risorsa non trovata"
* upstream / transport  -> ``backend_error`` "Errore di comunicazione con Gmail"
* anything else         -> ``backend_error`` "Errore Gmail"

Blob operations (``gmail.attachment``, ``gmail.image``) return a *temporary*
file (see :func:`server.gmail.write_blob`) that the adapter deletes once it has
delivered it.
"""

from __future__ import annotations

import asyncio
import imaplib
import logging
import os
import re
import smtplib
import time
from collections import OrderedDict
from collections.abc import Callable
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from server import blobs, gmail_unread
from server import gmail as _gmail
from server.api import ApiError, ChatAPI, operation
from server.models_api import (
    AttachmentIn,
    BlobOut,
    EmptyRequest,
    GmailAttachmentOut,
    GmailAttachmentRequest,
    GmailImageRequest,
    GmailMessageRef,
    GmailMessagesBulkDeleteRequest,
    GmailMessagesListRequest,
    GmailMessagesSendCheckRequest,
    GmailMessagesSendRequest,
)

logger = logging.getLogger(__name__)

#: Largest image the proxy fetches (the image goes into an ``<img>``).
IMAGE_PROXY_MAX_BYTES = 8 * 1024 * 1024
#: Largest decoded image (width * height) the proxy accepts.
IMAGE_PROXY_MAX_PIXELS = 25_000_000

_COMM_ERROR = "Errore di comunicazione con Gmail"

# Remote images of an opened message are prefetched in the background as soon
# as its detail is built, so the browser's <img> requests (which only start
# after the HTML is rendered) find them ready.  Shared by both paths: a cache
# bounded by bytes and age, and one in-flight fetch per URL that late requests
# join instead of fetching again.  Only touched from the server event loop.
_REMOTE_PREFETCH_MAX = 30
_REMOTE_PREFETCH_CONCURRENCY = 4
_REMOTE_IMAGE_TTL_S = 15 * 60
_REMOTE_IMAGE_CACHE_MAX_BYTES = 48 * 1024 * 1024
_DATA_SRC_RE = re.compile(r'data-src="(gmail-image\?token=[^"]+)"')
_remote_cache: OrderedDict[str, tuple[float, bytes, str]] = OrderedDict()
_remote_cache_bytes = 0
_remote_inflight: dict[str, asyncio.Task[tuple[bytes, str] | None]] = {}
_background: set[asyncio.Task[None]] = set()


def _run(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call a blocking Gmail function; map its failures to :class:`ApiError`."""
    try:
        return function(*args, **kwargs)
    except ApiError:
        raise
    except _gmail.GmailNotConfigured:
        raise ApiError("backend_down", "Gmail non configurato") from None
    except _gmail.GmailValidationError as exc:
        code = "too_large" if exc.status_code == 413 else "invalid_request"
        raise ApiError(code, str(exc)) from None
    except _gmail.GmailNotFound:
        raise ApiError("not_found", "Risorsa non trovata") from None
    except _gmail.GmailUpstreamError:
        logger.warning("Gmail upstream error (%s)", function.__name__)
        raise ApiError("backend_error", _COMM_ERROR) from None
    except (imaplib.IMAP4.error, smtplib.SMTPException, OSError):
        logger.warning("Gmail transport error (%s)", function.__name__)
        raise ApiError("backend_error", _COMM_ERROR) from None
    except Exception as exc:  # noqa: BLE001 - never leak upstream details
        logger.warning(
            "Gmail request failed (%s: %s)", function.__name__, type(exc).__name__
        )
        raise ApiError("backend_error", "Errore Gmail") from None


@operation("gmail.status", request=EmptyRequest, response=dict[str, Any])
def gmail_status(api: ChatAPI, req: EmptyRequest) -> dict[str, Any]:
    """Configuration status and limits; contacts no server."""
    return _gmail.status_snapshot()


@operation(
    "gmail.messages.list", request=GmailMessagesListRequest, response=dict[str, Any]
)
def gmail_messages_list(api: ChatAPI, req: GmailMessagesListRequest) -> dict[str, Any]:
    """One window of message summaries (``refresh`` bypasses the short cache)."""
    return _run(
        _gmail.fetch_message_list,
        folder=req.folder,
        offset=req.offset,
        limit=req.limit,
        refresh=req.refresh,
    )


def _remote_cache_get(url: str) -> tuple[bytes, str] | None:
    global _remote_cache_bytes
    entry = _remote_cache.get(url)
    if entry is None:
        return None
    stored_at, data, content_type = entry
    if time.monotonic() - stored_at > _REMOTE_IMAGE_TTL_S:
        del _remote_cache[url]
        _remote_cache_bytes -= len(data)
        return None
    _remote_cache.move_to_end(url)
    return data, content_type


def _remote_cache_put(url: str, data: bytes, content_type: str) -> None:
    global _remote_cache_bytes
    previous = _remote_cache.pop(url, None)
    if previous is not None:
        _remote_cache_bytes -= len(previous[1])
    _remote_cache[url] = (time.monotonic(), data, content_type)
    _remote_cache_bytes += len(data)
    while _remote_cache_bytes > _REMOTE_IMAGE_CACHE_MAX_BYTES and _remote_cache:
        _, (_, evicted, _) = _remote_cache.popitem(last=False)
        _remote_cache_bytes -= len(evicted)


def clear_remote_image_cache() -> None:
    """Forget cached and in-flight remote images (tests, account changes)."""
    global _remote_cache_bytes
    _remote_cache.clear()
    _remote_cache_bytes = 0
    _remote_inflight.clear()


async def _fetch_and_store(url: str) -> tuple[bytes, str] | None:
    from server import link_preview

    try:
        result = await link_preview.fetch_external_image(
            url,
            max_bytes=IMAGE_PROXY_MAX_BYTES,
            max_pixels=IMAGE_PROXY_MAX_PIXELS,
        )
        if result:
            _remote_cache_put(url, *result)
        return result
    finally:
        if _remote_inflight.get(url) is asyncio.current_task():
            del _remote_inflight[url]


async def _remote_image(url: str) -> tuple[bytes, str] | None:
    """A remote image from the cache, the in-flight fetch, or a new fetch."""
    cached = _remote_cache_get(url)
    if cached is not None:
        return cached
    loop = asyncio.get_running_loop()
    task = _remote_inflight.get(url)
    if task is None or task.get_loop() is not loop:
        task = loop.create_task(_fetch_and_store(url))
        _remote_inflight[url] = task
    # A cancelled browser request must not cancel the fetch others share.
    return await asyncio.shield(task)


def _remote_image_urls(html: str) -> list[str]:
    """Distinct remote URLs (not inline parts) of the proxied images, in order."""
    urls: list[str] = []
    for source in _DATA_SRC_RE.findall(html):
        token = parse_qs(urlsplit(unescape(source)).query).get("token", [""])[0]
        url = _gmail.verify_image_token(token)
        if url and not url.startswith(_gmail._PART_IMAGE_PREFIX) and url not in urls:
            urls.append(url)
            if len(urls) >= _REMOTE_PREFETCH_MAX:
                break
    return urls


async def _prefetch_remote_images(urls: list[str]) -> None:
    semaphore = asyncio.Semaphore(_REMOTE_PREFETCH_CONCURRENCY)

    async def prefetch(url: str) -> None:
        async with semaphore:
            try:
                await _remote_image(url)
            except Exception:
                logger.debug("Gmail image prefetch failed", exc_info=True)

    await asyncio.gather(*(prefetch(url) for url in urls))


def _schedule_remote_prefetch(html: str) -> None:
    if os.environ.get("GMAIL_IMAGE_PREFETCH", "1").strip() == "0" or not html:
        return
    urls = _remote_image_urls(html)
    if not urls:
        return
    task = asyncio.get_running_loop().create_task(_prefetch_remote_images(urls))
    _background.add(task)
    task.add_done_callback(_background.discard)


@operation("gmail.messages.get", request=GmailMessageRef, response=dict[str, Any])
async def gmail_messages_get(api: ChatAPI, req: GmailMessageRef) -> dict[str, Any]:
    """The full message: sanitized HTML, text and attachment descriptors.

    Its remote images start downloading in the background right away.
    """
    detail = await asyncio.to_thread(
        _run, _gmail.fetch_message, req.uid, folder=req.folder
    )
    if isinstance(detail, dict) and isinstance(detail.get("html"), str):
        _schedule_remote_prefetch(detail["html"])
    return detail


@operation("gmail.messages.delete", request=GmailMessageRef, response=dict[str, bool])
def gmail_messages_delete(api: ChatAPI, req: GmailMessageRef) -> dict[str, bool]:
    """Move one message to Trash (``not_found`` when it no longer exists)."""
    _run(_gmail.delete_message, req.uid, folder=req.folder)
    gmail_unread.poke()
    return {"ok": True, "trashed": True}


@operation(
    "gmail.messages.bulk_delete",
    request=GmailMessagesBulkDeleteRequest,
    response=dict[str, Any],
)
def gmail_messages_bulk_delete(
    api: ChatAPI, req: GmailMessagesBulkDeleteRequest
) -> dict[str, Any]:
    """Move several messages to Trash; unknown UIDs are reported as missing."""
    if req.folder is not None and not isinstance(req.folder, str):
        raise ApiError("invalid_request", "Cartella non valida")
    result = _run(_gmail.delete_messages, req.uids, folder=req.folder)
    gmail_unread.poke()
    return result


@operation(
    "gmail.messages.mark_read", request=GmailMessageRef, response=dict[str, bool]
)
def gmail_messages_mark_read(api: ChatAPI, req: GmailMessageRef) -> dict[str, bool]:
    """Set the ``\\Seen`` flag."""
    _run(_gmail.mark_read, req.uid, folder=req.folder)
    gmail_unread.poke()
    return {"ok": True}


@operation(
    "gmail.messages.mark_unread", request=GmailMessageRef, response=dict[str, bool]
)
def gmail_messages_mark_unread(api: ChatAPI, req: GmailMessageRef) -> dict[str, bool]:
    """Clear the ``\\Seen`` flag (back to unread)."""
    _run(_gmail.mark_unread, req.uid, folder=req.folder)
    gmail_unread.poke()
    return {"ok": True}


@operation("gmail.messages.archive", request=GmailMessageRef, response=dict[str, bool])
def gmail_messages_archive(api: ChatAPI, req: GmailMessageRef) -> dict[str, bool]:
    """Archive an inbox message (remove the Inbox label; it stays in All Mail)."""
    _run(_gmail.archive_message, req.uid, folder=req.folder)
    gmail_unread.poke()
    return {"ok": True}


@operation("gmail.unread", request=EmptyRequest, response=dict[str, Any])
def gmail_unread_count(api: ChatAPI, req: EmptyRequest) -> dict[str, Any]:
    """Unread count among the 200 listed messages, for the tab dot and badge.

    The polling worker's last value when it has one, else one check
    (``fetch_unread_status``).  ``configured: false`` (and 0) when Gmail is
    not set up.
    """
    if not _gmail.status_snapshot()["configured"]:
        return {"configured": False, "unread": 0}
    latest = gmail_unread.latest()
    if latest is None:
        latest = _run(_gmail.fetch_unread_status)
    return {"configured": True, "unread": int(latest.get("unread") or 0)}


def _check_slots(attachment_count: int, slots_ok: bool) -> None:
    """The payload checks that need no stored file, in the legacy order."""
    if attachment_count > _gmail._MAX_ATTACHMENTS:
        raise ApiError("invalid_request", "Troppi allegati")
    if not slots_ok:
        raise ApiError("invalid_request", "Richiesta non valida")


@operation(
    "gmail.messages.send.check",
    request=GmailMessagesSendCheckRequest,
    response=dict[str, bool],
)
def gmail_messages_send_check(
    api: ChatAPI, req: GmailMessagesSendCheckRequest
) -> dict[str, bool]:
    """Validate a send before its attachments are stored; sends nothing.

    Raises what ``gmail.messages.send`` would for the same request before it
    looks at any file (too many attachments, a slot that is not a file)."""
    _check_slots(req.attachment_count, req.slots_ok)
    return {"ok": True}


def _read_attachments(attachments: list[AttachmentIn]) -> list[tuple[str, bytes]]:
    """The files' bytes, after checking every size without reading anything.

    Same precedence as the old route, which read them one by one: the first
    oversized file (or a slot the adapter had to refuse) wins, then the total.
    """
    total = 0
    sizes: list[int] = []
    for attachment in attachments:
        if attachment.rejected is not None:
            raise ApiError("too_large", "Allegato troppo grande")
        try:
            size = Path(attachment.path).stat().st_size
        except OSError:
            raise ApiError("invalid_request", "Richiesta non valida") from None
        if size > _gmail._MAX_ATTACHMENT_BYTES:
            raise ApiError("too_large", "Allegato troppo grande")
        total += size
        if total > _gmail._MAX_TOTAL_ATTACHMENT_BYTES:
            raise ApiError("too_large", "Allegati troppo grandi")
        sizes.append(size)
    files: list[tuple[str, bytes]] = []
    for attachment, size in zip(attachments, sizes, strict=True):
        try:
            # Read at most one byte more than seen: the file may have grown.
            with Path(attachment.path).open("rb") as handle:
                data = handle.read(size + 1)
        except OSError:
            raise ApiError("invalid_request", "Richiesta non valida") from None
        if len(data) > size:
            raise ApiError("too_large", "Allegato troppo grande")
        files.append((attachment.filename or "attachment", data))
    return files


@operation(
    "gmail.messages.send", request=GmailMessagesSendRequest, response=dict[str, Any]
)
def gmail_messages_send(api: ChatAPI, req: GmailMessagesSendRequest) -> dict[str, Any]:
    """Send a mail (a reply when ``reply_to_uid`` is given); returns its
    ``Message-ID``.  ``attachments`` are local files stored by the adapter,
    which also deletes them afterwards."""
    _check_slots(
        len(req.attachments), all(a.path or a.rejected for a in req.attachments)
    )
    files = _read_attachments(req.attachments)
    message_id = _run(
        _gmail.send_email,
        to=req.to,
        cc=req.cc,
        bcc=req.bcc,
        subject=req.subject,
        text=req.text,
        reply_to_uid=req.reply_to_uid,
        reply_to_folder=req.reply_to_folder or None,
        forward_uid=req.forward_uid,
        forward_folder=req.forward_folder or None,
        attachments=files,
    )
    return {"ok": True, "message_id": message_id}


@operation(
    "gmail.attachment", request=GmailAttachmentRequest, response=GmailAttachmentOut
)
def gmail_attachment(api: ChatAPI, req: GmailAttachmentRequest) -> GmailAttachmentOut:
    """One attachment of a message as a temporary blob (download limit kept)."""
    download = _run(_gmail.fetch_attachment, req.uid, req.index, folder=req.folder)
    path = _run(_gmail.write_blob, download.data)
    blob = blobs.blob_for(path, download.content_type)
    return GmailAttachmentOut(**blob.model_dump(), filename=download.filename)


async def _inline_part_image(payload: str) -> tuple[bytes, str] | None:
    """Bytes of a ``cid:`` image part, only if it really is a raster image.

    The type is sniffed from the bytes (never the declared Content-Type) and
    the proxy byte/pixel caps apply, as for remote images.
    """
    from server import link_preview

    part = _gmail.parse_part_image(payload)
    if part is None:
        raise ApiError("not_found", "Immagine non trovata")
    folder, uid, index = part
    # Normally prefetched with the message; IMAP only on a cache miss.
    data = _gmail.cached_inline_image(folder, uid, index)
    if data is None:
        download = await asyncio.to_thread(
            _run, _gmail.fetch_attachment, uid, index, folder=folder
        )
        data = download.data
    if not data or len(data) > IMAGE_PROXY_MAX_BYTES:
        return None
    content_type = link_preview._sniff_raster(data)
    if content_type is None:
        return None
    dimensions = link_preview._raster_dimensions(data, content_type)
    if dimensions and dimensions[0] * dimensions[1] > IMAGE_PROXY_MAX_PIXELS:
        return None
    return data, content_type


@operation("gmail.image", request=GmailImageRequest, response=BlobOut)
async def gmail_image(api: ChatAPI, req: GmailImageRequest) -> BlobOut:
    """A remote image of an email, through the HMAC-signed proxy token.

    Only URLs this server signed are fetched, with the SSRF-safe fetcher of
    the link previews (public addresses only, redirects re-validated, raster
    formats only).  ``not_found`` for a bad token, ``backend_error`` when the
    image cannot be fetched.  The result is a temporary blob.  Remote images
    come from the prefetch cache when the message's detail already started
    them.
    """
    url = _gmail.verify_image_token(req.token)
    if not url:
        raise ApiError("not_found", "Immagine non trovata")
    if url.startswith(_gmail._PART_IMAGE_PREFIX):
        result = await _inline_part_image(url)
    else:
        result = await _remote_image(url)
    if not result:
        raise ApiError("backend_error", "Immagine non disponibile")
    data, content_type = result
    try:
        path = await asyncio.to_thread(_gmail.write_blob, data)
    except OSError:
        logger.warning("Gmail image blob could not be stored")
        raise ApiError("backend_error", "Immagine non disponibile") from None
    return blobs.blob_for(path, content_type)
