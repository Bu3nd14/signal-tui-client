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


# -- reactions ----------------------------------------------------------------


class ReactionsSetRequest(BaseModel):
    protocol: StrictStr
    contact_id: StrictStr
    message_id: StrictStr
    emoji: StrictStr


class ReactionsSetOut(BaseModel):
    ok: bool
    reactions: list[dict[str, Any]]
