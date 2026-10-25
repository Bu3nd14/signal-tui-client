"""Request/response models of the server API (pydantic).

Field names and order are identical to the JSON the HTTP adapter has always
returned, so routes can send ``call_op`` results as they are.  Quirks of a
single transport (e.g. HTTP ``protocol`` vs ``proto``) never appear here.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field, StrictBool, StrictStr


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


# -- reactions ----------------------------------------------------------------


class ReactionsSetRequest(BaseModel):
    protocol: StrictStr
    contact_id: StrictStr
    message_id: StrictStr
    emoji: StrictStr


class ReactionsSetOut(BaseModel):
    ok: bool
    reactions: list[dict[str, Any]]
