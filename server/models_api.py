"""Request/response models of the server API (pydantic).

Field names and order are identical to the JSON the HTTP adapter has always
returned, so routes can send ``call_op`` results as they are.  Quirks of a
single transport (e.g. HTTP ``protocol`` vs ``proto``) never appear here.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field, StrictBool, StrictStr

from server.events_api import ContactWire


def _stripped(value: Any) -> str:
    """Coerce like the legacy routes: ``str(value or "").strip()``."""
    return str(value or "").strip()


#: A string field that tolerates ``None``/non-str input and strips whitespace.
Stripped = Annotated[str, BeforeValidator(_stripped)]


class EmptyRequest(BaseModel):
    """Request of operations without parameters."""


# -- meta ---------------------------------------------------------------------


class EmojiCategoryOut(BaseModel):
    category: str
    icon: str
    emojis: list[str]
    aliases: dict[str, str]


class ReactionsAvailableRequest(BaseModel):
    protocol: str


class ReactionsAvailableOut(BaseModel):
    emojis: list[str] = Field(default_factory=list)


# ``server.status`` returns ``dict[str, bool]`` (protocol -> connected).


# -- contacts -----------------------------------------------------------------


class ContactOut(BaseModel):
    id: str
    display_name: str
    protocol: str
    extras: dict[str, Any]
    last_message_ts: int
    unread: int


class ContactsListRequest(BaseModel):
    q: str | None = None


class ContactsBookRequest(BaseModel):
    q: str
    force: bool = False


class ContactsForProtocolRequest(BaseModel):
    protocol: Stripped


class ContactsRegisterRequest(BaseModel):
    """A contact the frontend opens (address book / picker): open-or-create."""

    id: Stripped
    display_name: str
    protocol: Stripped
    extras: dict[str, Any] = {}


class ContactsAddressBookRequest(BaseModel):
    protocols: list[str] | None = None
    force: bool = False


class ContactsAddressBookOut(BaseModel):
    """Full address book of the requested protocols + per-protocol failures."""

    contacts: list[ContactWire]
    errors: dict[str, str]


class ContactsCheckNumberRequest(BaseModel):
    phone: Stripped


class ContactsCheckNumberOut(BaseModel):
    exists: bool | None


class ContactsTouchRequest(BaseModel):
    protocol: Stripped
    contact_id: Stripped
    timestamp: int


# -- chats --------------------------------------------------------------------


class ChatsMarkReadRequest(BaseModel):
    protocol: Stripped
    contact_id: Stripped


class StatusOkOut(BaseModel):
    status: str


class ChatsArchiveRequest(BaseModel):
    protocol: Stripped
    contact_id: Stripped
    archived: StrictBool


class ChatsArchiveOut(BaseModel):
    archived: bool


# -- messages -----------------------------------------------------------------


class MessagesPageRequest(BaseModel):
    protocol: Literal["signal", "whatsapp", "telegram"]
    contact_id: str
    refresh: bool = False
    limit: int | None = None  # 1..MAX_MESSAGES_LIMIT, checked by the operation
    since: int | None = None


# ``messages.page`` returns ``list[dict[str, Any]]`` (see server/ops/messages.py).


class MessagesWindowRequest(BaseModel):
    protocol: Literal["signal", "whatsapp", "telegram"]
    contact_id: str
    refresh: bool = False  # download the remote history first
    refresh_limit: int = Field(default=50, ge=1, le=200)


class MessagesWindowOut(BaseModel):
    # Canonical cache rows (the TUI's message shape), oldest first as cached.
    messages: list[dict[str, Any]]
    # True when the history download ran and succeeded.
    refreshed: bool


class MessagesSnapshotRequest(BaseModel):
    protocol: Literal["signal", "whatsapp", "telegram"]


class MessagesEditRequest(BaseModel):
    # Strict str: a missing or non-string field is "Invalid request" (legacy).
    # Emptiness / protocol / length checks are done by the operation.
    protocol: StrictStr
    contact_id: StrictStr
    message_id: StrictStr
    new_text: StrictStr


class OkOut(BaseModel):
    ok: bool


class AttachmentIn(BaseModel):
    """A blob handle: a local file the adapter already stored for the operation.

    ``filename`` / ``content_type`` are the client's claims (the operation
    sanitizes the name and only uses the type as a hint).  ``rejected`` marks
    a slot the adapter could not store (so errors are still reported in the
    legacy order: earlier files are validated first); such a slot has no path.
    The operation validates the file itself (kind, size, extension).
    """

    path: str = ""
    filename: str | None = None
    content_type: str | None = None
    rejected: Literal["too_large"] | None = None


class MessagesSendRequest(BaseModel):
    # protocol / contact_id / text: any other type is "Invalid request" (legacy).
    # The remaining fields keep the legacy lenient handling (blank -> None, bad
    # types rejected by the operation *after* the attachment-count check), so
    # they are ``Any`` here.
    protocol: StrictStr | None = None
    contact_id: StrictStr | None = None
    text: StrictStr | None = None
    client_msg_id: Any = None
    quote_timestamp: Any = None
    quote_author: Any = None
    quote_message: Any = None
    reply_to_message_id: Any = None
    quote_content_type: Any = None
    quote_attachment_id: Any = None
    batch_id: Any = None
    attachments: list[AttachmentIn] = Field(default_factory=list)


class MessagesSendCheckRequest(MessagesSendRequest):
    """``messages.send.check``: a send without its files (they are not stored yet).

    ``attachment_count`` is the number of file slots the adapter will fill;
    ``slots_ok`` is False when one of them is not a file at all.
    """

    attachment_count: int = Field(default=0, ge=0)
    slots_ok: bool = True


# -- media / blobs --------------------------------------------------------------


class BlobOut(BaseModel):
    """A blob handle: a file on the server machine and what is known about it.

    Both adapters run next to the server, so the handle is a local ``path``:
    HTTP streams it, the TUI reads it.  Blob operations only return files they
    own or have confined to a media root.  ``version`` is the cache-busting
    token ``<mtime_ns>-<size>``; ``immutable`` is true when the file is
    content-addressed (a generated thumbnail) and may be cached forever.
    """

    path: str
    content_type: str | None = None
    size: int | None = None
    version: str | None = None
    immutable: bool = False


class MediaAttachmentRequest(BaseModel):
    protocol: str
    attachment_id: str


class MediaThumbnailRequest(MediaAttachmentRequest):
    width: int  # one of server.thumbnails.THUMB_WIDTHS, checked by the operation


class MediaQuoteRequest(BaseModel):
    protocol: str
    message_row_id: int
    width: int | None = None


class MediaQuotedAttachmentRequest(BaseModel):
    """The quoted message of a reply, as the reply knows it."""

    protocol: str
    contact_key: str
    quote_timestamp: int | None = None
    reply_to_message_id: str | None = None
    quote_text: str = ""
    content_type: str | None = None


class MediaQuotedAttachmentOut(BaseModel):
    attachment_id: str | None = None


class ContactsAvatarRequest(BaseModel):
    protocol: str
    contact_id: str


# -- reactions ----------------------------------------------------------------


class ReactionsSetRequest(BaseModel):
    protocol: StrictStr
    contact_id: StrictStr
    message_id: StrictStr
    emoji: StrictStr


class ReactionsSetOut(BaseModel):
    ok: bool
    reactions: list[dict[str, Any]]
