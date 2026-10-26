"""Blob operations: attachments, thumbnails, quote media (and avatars).

They return :class:`~server.models_api.BlobOut` handles to local files; see
:mod:`server.blobs` for the resolution and confinement rules.  Handlers are
sync and may block (disk, SQLite, Pillow, ffmpeg, a lazy download).
"""

from __future__ import annotations

from server import blobs
from server.api import ApiError, ChatAPI, operation
from server.models_api import (
    BlobOut,
    ContactsAvatarRequest,
    MediaAttachmentRequest,
    MediaQuotedAttachmentOut,
    MediaQuotedAttachmentRequest,
    MediaQuoteRequest,
    MediaThumbnailRequest,
)
from server.ops.common import PROTOCOLS


def _known(protocol: str) -> str:
    if protocol not in PROTOCOLS:
        raise ApiError("not_found", "Not Found")
    return protocol


@operation("media.attachment", request=MediaAttachmentRequest, response=BlobOut)
def media_attachment(api: ChatAPI, req: MediaAttachmentRequest) -> BlobOut:
    """Local file of an attachment (downloaded lazily when needed).

    ``not_found`` when the backend cannot produce it or the file is not inside
    the protocol's media root.
    """
    return blobs.attachment_blob(api.manager, _known(req.protocol), req.attachment_id)


@operation("media.thumbnail", request=MediaThumbnailRequest, response=BlobOut)
def media_thumbnail(api: ChatAPI, req: MediaThumbnailRequest) -> BlobOut:
    """JPEG thumbnail (width 96, 240 or 480) of an image or video attachment.

    Falls back to the original attachment for media that has no thumbnail and
    for any other width.
    ``unprocessable`` when a video thumbnail cannot be produced.
    """
    return blobs.thumbnail_blob(
        api.manager, _known(req.protocol), req.attachment_id, req.width
    )


@operation("media.quote", request=MediaQuoteRequest, response=BlobOut)
def media_quote(api: ChatAPI, req: MediaQuoteRequest) -> BlobOut:
    """Quote thumbnail image saved for a message row (by its database id)."""
    return blobs.quote_blob(_known(req.protocol), req.message_row_id, req.width)


@operation(
    "media.quoted_attachment",
    request=MediaQuotedAttachmentRequest,
    response=MediaQuotedAttachmentOut,
)
def media_quoted_attachment(
    api: ChatAPI, req: MediaQuotedAttachmentRequest
) -> MediaQuotedAttachmentOut:
    """Attachment id of the image a reply quotes (same chat), if any."""
    from server.media import quoted_attachment_id

    return MediaQuotedAttachmentOut(
        attachment_id=quoted_attachment_id(
            req.protocol,
            req.contact_key,
            quote_timestamp=req.quote_timestamp,
            reply_to_message_id=req.reply_to_message_id,
            quote_text=req.quote_text,
            content_type=req.content_type,
        )
    )


@operation("contacts.avatar", request=ContactsAvatarRequest, response=BlobOut)
def contacts_avatar(api: ChatAPI, req: ContactsAvatarRequest) -> BlobOut:
    """Profile picture of a contact (cached on disk); ``not_found`` if none."""
    _known(req.protocol)
    if not req.contact_id.strip():
        raise ApiError("invalid_request", "Invalid request")
    return blobs.avatar_blob(req.protocol, api.manager, req.contact_id)
