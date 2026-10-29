"""Request/response models of the server API (pydantic).

Field names and order are identical to the JSON the HTTP adapter has always
returned, so routes can send ``call_op`` results as they are.  Quirks of a
single transport (e.g. HTTP ``protocol`` vs ``proto``) never appear here.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
)

from models import DEFAULT_SIGNAL_DEVICE_NAME
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


class ServerConfigOut(BaseModel):
    """Settings a frontend needs to render (not secrets, not server wiring)."""

    thumbnail_max_lines: int
    thumbnail_max_cols: int
    #: Rendered results of the contact picker.
    picker_max_results: int = 50
    #: Protocol the picker prefers for a person on several, ``""`` = most recent.
    picker_preferred_backend: str = ""
    #: Lazy WhatsApp media on chat open (``WA_LAZY_MEDIA``): one switch for
    #: every frontend (web pending attachments, TUI lazy tokens).
    wa_lazy_media: bool = True


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


class OptimisticSend(BaseModel):
    """A frontend that shows its message before the server confirms it.

    Presence of this object (``MessagesSendRequest.optimistic``) selects the
    *optimistic* flavour of ``messages.send`` (text only): the core seeds a
    ``pending`` row in the chat cache, persists it BEFORE the network send,
    bumps the contact's recency, publishes the legacy ``message`` frame, sends
    through the contact's backend (no ``sent_mirror``: the pending row is the
    local mirror), then moves the row to ``sent`` / ``failed`` and attaches the
    server message id.  The frontend only draws its own bubble and reconciles
    with the result.  ``timestamp`` is the client's millisecond timestamp: the
    identity of the row until the echo arrives.  The other fields are display
    metadata stored on the row as given (quote text and quoted-media
    thumbnail); the quote the *wire* carries is the usual ``quote_*`` fields.

    ``resend`` re-sends a ``failed`` row: nothing is seeded, the row goes
    ``failed`` -> ``pending`` (``conflict`` when it is not failed) and the send
    is attempted again.
    """

    timestamp: StrictInt
    resend: StrictBool = False
    quote_text: StrictStr | None = None
    attachment_id: StrictStr | None = None
    content_type: StrictStr | None = None
    quote_attachment_id: StrictStr | None = None
    quote_attachment_path: StrictStr | None = None
    quote_content_type: StrictStr | None = None


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
    #: Retry transient backend errors with back-off (the web policy).  A caller
    #: that wants exactly one attempt (the TUI, whose user retries by clicking
    #: the failed bubble) passes ``False``.
    retry: StrictBool = True
    #: See :class:`OptimisticSend`.  Adapters must not forward it from
    #: untrusted request bodies unless they mean to expose it.
    optimistic: OptimisticSend | None = None


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


# -- device link --------------------------------------------------------------

LinkProtocol = Literal["signal", "whatsapp", "telegram"]


class LinkProtocolStatus(BaseModel):
    """One protocol as the link flow sees it (no network I/O to compute it).

    ``available``: the backend is configured/registered, so a link can start
    (WhatsApp: WAHA configured; Telegram: API credentials set).  ``account`` is
    the linked Signal number.  ``linked`` is ``None`` when it cannot be known
    without asking the remote service (WhatsApp).
    """

    available: bool
    account: str = ""
    linked: bool | None = None


class LinkStatusOut(BaseModel):
    protocols: dict[str, LinkProtocolStatus]


class LinkStartRequest(BaseModel):
    protocol: LinkProtocol
    #: Signal only: the number the user typed (picks the account when
    #: ``signal-cli`` lists several).
    phone: StrictStr = ""
    #: Signal only: the device name shown in the phone's linked-devices list.
    device_name: StrictStr = DEFAULT_SIGNAL_DEVICE_NAME


class LinkQrOut(BaseModel):
    """What to show for a pairing.

    ``url``: a link to render as a QR code (``sgnl://link...``, ``tg://login``);
    ``png``: a ready QR image (base64 in ``data``); ``text``: pre-rendered
    text shown as it is; ``info`` / ``error``: a message instead of a QR.
    """

    kind: Literal["url", "png", "text", "info", "error"]
    data: str


class LinkStartOut(BaseModel):
    qr: LinkQrOut


class LinkProtocolRequest(BaseModel):
    protocol: LinkProtocol


class LinkPollOut(BaseModel):
    """Progress of a started link.

    ``state``: ``waiting``; ``finalize`` (Signal: ``signal-cli`` exited
    successfully, call ``link.finalize``); ``done``.  ``needs_2fa`` (Telegram)
    is true once, when the account asks for its password.  ``qr`` is a
    refreshed code to show instead of the previous one.
    """

    state: Literal["waiting", "finalize", "done"]
    needs_2fa: bool = False
    qr: LinkQrOut | None = None


class LinkFinalizeRequest(BaseModel):
    protocol: Literal["signal"] = "signal"
    phone: StrictStr = ""


class LinkFinalizeOut(BaseModel):
    account: str


class Link2faRequest(BaseModel):
    password: StrictStr


class Link2faOut(BaseModel):
    ok: bool


class LinkReconnectRequest(BaseModel):
    protocols: list[LinkProtocol]


# -- download mode ------------------------------------------------------------


class DownloadServeAttachmentRequest(BaseModel):
    protocol: StrictStr
    attachment_id: StrictStr


class DownloadServeTextRequest(BaseModel):
    text: StrictStr
    #: Plain file name (no directories): it becomes the last URL segment.
    filename: StrictStr = "message.txt"


class DownloadOut(BaseModel):
    """URL on the temporary download server (port 10042)."""

    url: str


# -- reactions ----------------------------------------------------------------


class ReactionsSetRequest(BaseModel):
    protocol: StrictStr
    contact_id: StrictStr
    message_id: StrictStr
    emoji: StrictStr


class ReactionsSetOut(BaseModel):
    ok: bool
    reactions: list[dict[str, Any]]


# -- links / client log -------------------------------------------------------


class LinksPreviewRequest(BaseModel):
    url: str


class LinkPreviewOut(BaseModel):
    """Sanitized card of a page: the page's HTML never reaches a client."""

    url: str
    title: str
    description: str
    site_name: str
    has_image: bool


class ClientLogRequest(BaseModel):
    """A line a frontend wants in the server log (``source`` is set by the
    adapter, never taken from the client)."""

    message: Any = None
    source: str = "client"


class StatusOut(BaseModel):
    status: str


class ClientsOut(BaseModel):
    """``server.clients``: live client connections of the server.

    ``connections`` counts the Unix-socket connections, including the caller's
    own when it comes through the socket (``self_included``); ``web`` counts
    the browser WebSockets separately.  Both are 0 without that adapter.
    """

    connections: int
    self_included: bool
    web: int = 0


# -- transcription / OpenAI key -----------------------------------------------


class TranscribeSubmitRequest(BaseModel):
    """Both fields are checked by the operation ('Invalid request')."""

    protocol: Any = None
    attachment_id: Any = None


class TranscribeStatusRequest(BaseModel):
    protocol: Literal["signal", "whatsapp", "telegram"]
    attachment_id: str


class TranscribeConfigOut(BaseModel):
    enabled: bool


class OpenAIKeyRequest(BaseModel):
    """Checked by the operation ('Chiave non valida')."""

    api_key: Any = None


# -- gmail ----------------------------------------------------------------------


def _none_as_empty(value: Any) -> Any:
    return "" if value is None else value


#: A form-like text field: absent/``null`` means empty (the legacy multipart
#: handling); the operation validates the content.
FormText = Annotated[str, BeforeValidator(_none_as_empty)]


class GmailMessageRef(BaseModel):
    """One message of a folder (``folder`` ``None``: the configured one)."""

    uid: int
    folder: str | None = None


class GmailMessagesListRequest(BaseModel):
    folder: str | None = None
    offset: int = 0
    limit: int = 50
    refresh: StrictBool = False


class GmailMessagesBulkDeleteRequest(BaseModel):
    """``uids`` and ``folder`` are checked by the operation (own error texts)."""

    uids: Any = None
    folder: Any = None


class GmailAttachmentRequest(GmailMessageRef):
    index: int


class GmailMessagesSendRequest(BaseModel):
    """Compose and send a mail.  ``attachments`` are local files the adapter
    stored (any file type; the operation enforces the Gmail limits)."""

    to: FormText = ""
    cc: FormText = ""
    bcc: FormText = ""
    subject: FormText = ""
    text: FormText = ""
    reply_to_uid: int | None = None
    reply_to_folder: str | None = None
    #: Forward this message (its text and attachments are added server-side).
    forward_uid: int | None = None
    forward_folder: str | None = None
    attachments: list[AttachmentIn] = Field(default_factory=list)


class GmailMessagesSendCheckRequest(GmailMessagesSendRequest):
    """``gmail.messages.send.check``: a send without its files (not stored yet),
    like :class:`MessagesSendCheckRequest`."""

    attachment_count: int = Field(default=0, ge=0)
    slots_ok: bool = True


class GmailImageRequest(BaseModel):
    """``token``: the HMAC-signed value ``server.gmail.sign_image_url`` issued."""

    token: str


class GmailAttachmentOut(BlobOut):
    """A downloaded mail attachment: a temporary blob plus its (safe) name.

    The file is the caller's to delete once delivered; the core also prunes
    stale ones (see ``server.gmail.write_blob``).
    """

    filename: str
