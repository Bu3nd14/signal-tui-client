"""Message send operation: validation, idempotency, retry, backend calls."""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from server import messages as _domain
from server.api import ApiError, ChatAPI, operation
from server.domain import bump_recency
from server.models_api import (
    AttachmentIn,
    MessagesSendCheckRequest,
    MessagesSendRequest,
    OptimisticSend,
)
from server.ops.common import PROTOCOLS
from server.outgoing import transition_outgoing_status
from server.retry import (
    SEND_RETRY_MAX_ATTEMPTS,
    classify_send_error,
    compute_delay,
    safe_error_text,
)
from server.send_registry import build_fingerprint
from server.uploads import (
    MAX_ATTACHMENTS,
    UploadValidationError,
    ValidatedUpload,
    validate_upload,
)

logger = logging.getLogger(__name__)

_INVALID = "Invalid request"


def _invalid() -> ApiError:
    return ApiError("invalid_request", _INVALID)


async def _validate_attachments(
    attachments: list[AttachmentIn],
) -> list[ValidatedUpload]:
    """Validate the adapter-stored files in order; the first bad one wins."""
    validated: list[ValidatedUpload] = []
    for attachment in attachments:
        if attachment.rejected is not None:
            raise ApiError("too_large", "Upload too large")
        try:
            validated.append(
                await asyncio.to_thread(
                    validate_upload,
                    Path(attachment.path),
                    attachment.filename,
                    attachment.content_type,
                )
            )
        except UploadValidationError as exc:
            if exc.status_code == 413:
                raise ApiError("too_large", "Upload too large") from None
            raise ApiError("invalid_request", "Unsupported media type") from None
        except OSError:
            raise _invalid() from None
    return validated


@dataclass
class _Prepared:
    """A fully validated send request (everything the old route checked)."""

    protocol: str
    contact_id: str
    text: str
    client_msg_id: str
    kwargs: dict[str, Any]
    quote_timestamp: Any
    quote_author: Any
    quote_message: Any
    reply_to_message_id: Any
    quote_content_type: Any
    quote_attachment_id: Any
    backend: Any
    contact: Any


def _prepare(
    api: ChatAPI, req: MessagesSendRequest, attachment_count: int, slots_ok: bool
) -> _Prepared:
    """Cheap validation, in the legacy precedence order; no side effects.

    Payload checks (attachment count and slot sanity included), quote/reply
    fields, backend + contact lookup (404), Signal quote resolution.  Needs no
    stored file, so the HTTP adapter runs it (``messages.send.check``) before
    spending I/O on uploads.
    """
    protocol, contact_id, text = req.protocol, req.contact_id, req.text
    if protocol not in PROTOCOLS:
        raise _invalid()
    if not isinstance(contact_id, str) or not contact_id.strip():
        raise _invalid()
    if not isinstance(text, str) or len(text) > _domain.MAX_TEXT_LENGTH:
        raise _invalid()
    if not attachment_count and not text.strip():
        raise _invalid()
    if attachment_count > MAX_ATTACHMENTS:
        raise ApiError("invalid_request", "Too many attachments")
    if not slots_ok:
        raise _invalid()
    if req.optimistic is not None and attachment_count:
        raise _invalid()  # the optimistic flavour is text only

    # client_msg_id: opzionale con fallback server (client vecchi/tooling).
    client_msg_id_raw = req.client_msg_id
    if (
        isinstance(client_msg_id_raw, str)
        and client_msg_id_raw.strip()
        and len(client_msg_id_raw) <= 128
    ):
        client_msg_id = client_msg_id_raw.strip()
    else:
        client_msg_id = str(uuid.uuid4())

    quote_timestamp = req.quote_timestamp
    quote_author = req.quote_author
    quote_message = req.quote_message
    reply_to_message_id = req.reply_to_message_id
    quote_content_type = req.quote_content_type if protocol == "signal" else None
    quote_attachment_id = req.quote_attachment_id if protocol == "signal" else None
    if isinstance(quote_author, str):
        quote_author = quote_author.strip() or None
    if isinstance(quote_content_type, str):
        quote_content_type = quote_content_type.strip() or None
    if isinstance(quote_attachment_id, str):
        quote_attachment_id = quote_attachment_id.strip() or None
    # The optimistic caller mirrors the wire exactly (a media reply's empty
    # body must reach the quote as "", not be omitted); only the web's
    # free-form field is normalised.
    if isinstance(quote_message, str) and req.optimistic is None:
        stripped_quote_message = quote_message.strip()
        quote_message = (
            stripped_quote_message
            if stripped_quote_message or quote_content_type
            else None
        )
    if isinstance(reply_to_message_id, str):
        reply_to_message_id = reply_to_message_id.strip() or None
    if isinstance(quote_timestamp, bool) or (
        quote_timestamp is not None and not isinstance(quote_timestamp, int)
    ):
        raise _invalid()
    if any(
        value is not None and not isinstance(value, str)
        for value in (
            quote_author,
            quote_message,
            reply_to_message_id,
            quote_content_type,
            quote_attachment_id,
        )
    ):
        raise _invalid()
    is_reply = any(
        value is not None
        for value in (
            quote_timestamp,
            quote_author,
            quote_message,
            reply_to_message_id,
            quote_content_type,
            quote_attachment_id,
        )
    )
    if protocol == "telegram" and is_reply:
        try:
            if int(reply_to_message_id) <= 0:  # pyright: ignore[reportArgumentType]
                raise ValueError
        except (TypeError, ValueError):
            raise _invalid() from None
    if protocol == "whatsapp" and is_reply and not reply_to_message_id:
        raise _invalid()

    manager = api.manager
    backend = manager.get(protocol)
    if backend is None:
        raise ApiError("not_found", "Not Found")

    contact = backend.find_contact(contact_id)
    if contact is None:
        raise ApiError("not_found", "Not Found")

    quote_attachments = None
    if protocol == "signal" and quote_attachment_id is not None:
        contact_attachment_ids = {
            message["attachment"]["attachment_id"]
            for message in _domain._messages(protocol, contact_id)
            if message["attachment"] is not None
        }
        if quote_attachment_id not in contact_attachment_ids:
            raise _invalid()
    if protocol == "signal" and quote_content_type is not None:
        if quote_attachment_id is None:
            if req.optimistic is None:
                raise _invalid()
            # A reply to media whose file id is unknown: the quote keeps its
            # type but has no thumbnail (what the TUI always did).
            quote_attachments = [quote_content_type]
        else:
            resolved = manager.get_attachment_path(protocol, quote_attachment_id)
            path = Path(resolved) if resolved else None
            quote_attachments = (
                [f"{quote_content_type}:{path.name}:{path}"]
                if path
                else [quote_content_type]
            )

    kwargs: dict[str, Any] = {
        "quote_timestamp": quote_timestamp,
        "quote_author": quote_author,
        "quote_message": quote_message,
    }
    if protocol == "signal" and quote_attachments is not None:
        kwargs["quote_attachments"] = quote_attachments
    # Signal ignores the id; the optimistic caller keeps passing it on, as the
    # TUI always did.
    if (
        protocol in {"whatsapp", "telegram"} or req.optimistic is not None
    ) and reply_to_message_id is not None:
        kwargs["reply_to_message_id"] = reply_to_message_id

    return _Prepared(
        protocol=protocol,
        contact_id=contact_id,
        text=text,
        client_msg_id=client_msg_id,
        kwargs=kwargs,
        quote_timestamp=quote_timestamp,
        quote_author=quote_author,
        quote_message=quote_message,
        reply_to_message_id=reply_to_message_id,
        quote_content_type=quote_content_type,
        quote_attachment_id=quote_attachment_id,
        backend=backend,
        contact=contact,
    )


@operation(
    "messages.send.check", request=MessagesSendCheckRequest, response=dict[str, bool]
)
def messages_send_check(api: ChatAPI, req: MessagesSendCheckRequest) -> dict[str, bool]:
    """Validate a send before its attachments are stored; sends nothing.

    Raises exactly what ``messages.send`` would for the same request before it
    looks at any file.  Takes no registry claim: only text sends (no
    attachments, so nothing to store) are idempotent.
    """
    _prepare(api, req, req.attachment_count, req.slots_ok)
    return {"ok": True}


@operation("messages.send", request=MessagesSendRequest, response=dict[str, Any])
async def messages_send(api: ChatAPI, req: MessagesSendRequest) -> dict[str, Any]:
    """Send a text message, or attachments (the text captions the first one).

    Text sends are idempotent per ``(protocol, contact_id, client_msg_id)`` and
    retried with backoff on transient backend errors (``send_retry`` frames;
    ``retry=false`` makes it a single attempt); attachment sends are neither
    deduplicated nor retried.  ``attachments`` are local files stored by the
    adapter, which also deletes them afterwards.

    With ``optimistic`` (text only) the core also owns the sender's pending
    row: it seeds and persists it before the network send, bumps the contact's
    recency, then moves it to ``sent`` or ``failed`` and attaches the server
    id.  The result is ``{ok, status, status_applied, message_id}``; on failure
    the ``ApiError`` details carry ``status_applied`` and ``error``.

    Async: the backend calls run in worker threads and the retry back-off
    sleeps on the loop, so concurrent sends do not block each other.
    """
    attachments = req.attachments
    opt = req.optimistic
    p = _prepare(
        api,
        req,
        len(attachments),
        all(a.path or a.rejected is not None for a in attachments),
    )
    protocol, contact_id, text = p.protocol, p.contact_id, p.text
    client_msg_id, kwargs, manager = p.client_msg_id, p.kwargs, api.manager
    backend, contact = p.backend, p.contact
    quote_timestamp, quote_author = p.quote_timestamp, p.quote_author
    quote_message, reply_to_message_id = p.quote_message, p.reply_to_message_id
    quote_content_type = p.quote_content_type
    quote_attachment_id = p.quote_attachment_id

    # Contatto book-only (non ancora in self.contacts): registralo prima
    # del send così gli eventi successivi lo risolvono.  Il backend
    # WhatsApp crea da sé l'alias @lid → contatto @c.us.
    is_known = any(str(c.id) == contact.id for c in backend.contacts)
    if not is_known:
        contact.extras["ghost"] = True
        backend.register_contact(contact)
    elif contact.extras.get("ghost"):
        # Ghost book-only già registrato: ritenta l'alias @lid.  Il primo
        # invio può averlo saltato se la cache LID era vuota; la cache può
        # popolarsi dopo.  Idempotente (setdefault), zero rete.
        backend.register_contact(contact)

    # Idempotenza (solo ramo testo, N7): claim atomico scoped al contatto.
    registry = api.send_registry
    registry_key: tuple[str, str, str] | None = None
    registry_token: str | None = None
    if not attachments:
        registry_key = (protocol, contact_id, client_msg_id)
        fingerprint = build_fingerprint(
            protocol=protocol,
            contact_id=contact_id,
            text=text,
            quote_timestamp=quote_timestamp,
            quote_author=quote_author,
            quote_message=quote_message,
            reply_to_message_id=reply_to_message_id,
            quote_content_type=quote_content_type,
            quote_attachment_id=quote_attachment_id,
        )
        outcome, value = registry.claim_or_lookup(registry_key, fingerprint)
        if outcome == "sent":
            if value.get("fingerprint") != fingerprint:
                raise ApiError("unprocessable", "Client message id conflict")
            return {"ok": True, "duplicate": True}
        if outcome == "inflight":
            # Un altro handler sta inviando: retryable lato client.  Nessuna
            # mutazione del registry (N2).
            raise ApiError("conflict", "Send in progress")
        registry_token = value

    succeeded = False
    message_id: str | None = None
    failure: ApiError | None = None
    last_error: str | None = None
    # Optimistic flavour: True once the sender's row is ``pending`` (seeded or
    # re-armed), i.e. a failure must move it to ``failed``.
    row_pending = False
    try:
        if opt is not None:
            # A failure from here on (even while persisting) must not leave
            # the cached row pending; a refused re-arm leaves it as it was.
            row_pending = not opt.resend
            await _begin_optimistic(backend, p, opt, contact.cache_key, api)
            row_pending = True
        if attachments:
            # Sequential validation keeps error messages stable (the first
            # invalid file wins).
            uploads = await _validate_attachments(attachments)
            batch_id = req.batch_id
            if not isinstance(batch_id, str) or not batch_id.strip():
                batch_id = None
            await asyncio.to_thread(
                manager.send_attachments_sync,
                protocol,
                contact_id,
                [upload.path for upload in uploads],
                batch_id=batch_id,
                # Product default: the single text field captions the
                # first attachment only.
                captions=[text or None] + [None] * (len(uploads) - 1),
                mime_types=[upload.mime_type for upload in uploads],
                media_kinds=[upload.media_kind for upload in uploads],
                filenames=[upload.filename for upload in uploads],
                **kwargs,
            )
            succeeded = True
        else:
            # Ramo TESTO: retry con backoff esponenziale + jitter.
            # Nessun timeout aggiuntivo: i backend hanno timeout interni propri.
            rng = random.Random()
            max_attempts = SEND_RETRY_MAX_ATTEMPTS if req.retry else 1
            for attempt in range(1, max_attempts + 1):
                try:
                    started = time.perf_counter()
                    if opt is None:
                        result = await asyncio.to_thread(
                            manager.send_message_sync,
                            protocol,
                            contact_id,
                            text,
                            **kwargs,
                        )
                    else:
                        # Straight to the backend: no sent_mirror, the pending
                        # row already is the local copy.
                        result = await asyncio.to_thread(
                            backend.send_message_sync, contact_id, text, **kwargs
                        )
                    _log_send_duration(protocol, contact_id, started)
                    if result is not None:
                        message_id = str(result)
                    succeeded = True
                    break
                except Exception as exc:
                    last_error = safe_error_text(exc)
                    classification = classify_send_error(protocol, exc)
                    if classification == "terminal":
                        logger.exception(
                            "Terminal send error, no retry: protocol=%s contact=%s",
                            protocol,
                            contact_id,
                        )
                        break
                    if attempt == max_attempts:
                        logger.exception(
                            "Send failed after %d attempts: protocol=%s contact=%s",
                            max_attempts,
                            protocol,
                            contact_id,
                        )
                        break
                    delay = compute_delay(attempt, rng)
                    logger.info(
                        "Send retry %d/%d in %.1fs: protocol=%s contact=%s error=%s",
                        attempt + 1,
                        SEND_RETRY_MAX_ATTEMPTS,
                        delay,
                        protocol,
                        contact_id,
                        exc,
                    )
                    api.publish_web_push(
                        {
                            "type": "send_retry",
                            "payload": {
                                "client_msg_id": client_msg_id,
                                "protocol": protocol,
                                "contact_id": contact_id,
                                "attempt": attempt + 1,
                                "max_attempts": SEND_RETRY_MAX_ATTEMPTS,
                                "error": safe_error_text(exc),
                            },
                        }
                    )
                    await asyncio.sleep(delay)
    except ApiError as exc:
        failure = exc
    except NotImplementedError as exc:
        last_error = safe_error_text(exc)
        if attachments:
            failure = ApiError("not_supported", "Attachment send not supported")
        else:
            logger.exception(
                "Web send failed: protocol=%s contact=%s", protocol, contact_id
            )
            failure = ApiError("backend_error", "Message send failed")
    except Exception as exc:
        last_error = safe_error_text(exc)
        logger.exception(
            "Web send failed: protocol=%s contact=%s", protocol, contact_id
        )
        failure = ApiError("backend_error", "Message send failed")
    finally:
        # B3/N2: mark_sent solo su successo, release in tutti gli altri casi
        # (incluso il 502 dopo i 3 tentativi), sempre vincolato al token.
        if registry_token is not None and registry_key is not None:
            if succeeded and failure is None:
                registry.mark_sent(
                    registry_key, registry_token, message_id, int(time.time())
                )
            else:
                registry.release(registry_key, registry_token)

    if failure is None and not succeeded:
        failure = ApiError("backend_error", "Message send failed")
    if failure is not None:
        if opt is not None and row_pending:
            applied = await asyncio.to_thread(
                transition_outgoing_status,
                backend,
                protocol,
                contact_id,
                opt.timestamp,
                text,
                "failed",
                ("pending",),
            )
            failure.details = {"status_applied": applied, "error": last_error}
        raise failure from None

    if opt is not None:
        return await _finish_optimistic(backend, p, opt, message_id)

    api.publish_web_push(
        {
            "type": "message",
            "payload": {
                "protocol": protocol,
                "contact_id": contact_id,
                "is_mine": True,
            },
        }
    )
    return {"ok": True}


def _log_send_duration(protocol: str, contact_id: str, started: float) -> None:
    """The time a bubble stays grey is the backend send time: log it."""
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if elapsed_ms >= 1000.0:
        logger.warning(
            "send_message_sync slow: protocol=%s contact=%s %.0f ms",
            protocol,
            contact_id,
            elapsed_ms,
        )
    else:
        logger.debug(
            "send_message_sync took %.1f ms (protocol=%s)", elapsed_ms, protocol
        )


async def _begin_optimistic(
    backend: Any, p: _Prepared, opt: OptimisticSend, cache_key: str, api: ChatAPI
) -> None:
    """Seed + persist the pending row (or re-arm a failed one) before sending.

    A new send
    whose identity is already cached (same text within the dedup window) adds
    nothing and persists nothing, as before; the contact is promoted anyway.
    """
    protocol, contact_id, text, ts = p.protocol, p.contact_id, p.text, opt.timestamp
    bump_recency(backend, cache_key, ts)
    if opt.resend:
        rearmed = await asyncio.to_thread(
            transition_outgoing_status,
            backend,
            protocol,
            contact_id,
            ts,
            text,
            "pending",
            ("failed",),
        )
        if not rearmed:
            raise ApiError(
                "conflict",
                "Message is not in a failed state",
                details={"status_applied": False, "error": None},
            )
        return
    data = {
        "text": text,
        "is_mine": True,
        "sender": "You",
        "timestamp": ts,
        "quote_text": opt.quote_text,
        "msg_type": "text",
        "attachment_info": None,
        "attachment_id": opt.attachment_id,
        "content_type": opt.content_type,
        "status": "pending",
        "protocol": protocol,
        "quote_timestamp": p.quote_timestamp,
        "quote_author": p.quote_author,
        "reply_to_message_id": p.reply_to_message_id,
        # Quoted-media thumbnail metadata (display-only).
        "quote_attachment_id": opt.quote_attachment_id,
        "quote_attachment_path": opt.quote_attachment_path,
        "quote_content_type": opt.quote_content_type,
    }
    # The cache seed must land in the contact's OWN backend cache, or the
    # backend would not recognise the echo and queue it again (double bubble).
    added = await asyncio.to_thread(backend.ingest_message, contact_id, data, ts, False)
    logger.debug(
        "optimistic ingest: added=%s protocol=%s contact=%r ts=%s text=%r",
        added,
        protocol,
        contact_id,
        ts,
        text[:60],
    )
    if added is True:
        # SQLite write BEFORE the network send, so the echo always finds the
        # row to upgrade.
        await asyncio.to_thread(backend._persist_message, contact_id, data, ts)
        api.publish_web_push(
            {
                "type": "message",
                "payload": {
                    "id": data.get("id"),
                    "protocol": protocol,
                    "contact_id": contact_id,
                    "timestamp": ts,
                    "is_mine": True,
                },
            }
        )


async def _finish_optimistic(
    backend: Any, p: _Prepared, opt: OptimisticSend, message_id: str | None
) -> dict[str, Any]:
    """``pending`` -> ``sent`` and the server id onto the cached row."""
    protocol, contact_id, text, ts = p.protocol, p.contact_id, p.text, opt.timestamp
    applied = await asyncio.to_thread(
        transition_outgoing_status,
        backend,
        protocol,
        contact_id,
        ts,
        text,
        "sent",
        ("pending",),
    )
    attached: str | None = None
    if message_id:
        # Attach the real id to the optimistic row so the echo matches by id
        # and never rewrites the optimistic timestamp.
        try:
            await asyncio.to_thread(
                backend.ingest_message,
                contact_id,
                {
                    "id": message_id,
                    "text": text,
                    "is_mine": True,
                    "sender": "You",
                    "timestamp": ts,
                    "quote_text": p.quote_message,
                    "quote_timestamp": p.quote_timestamp,
                    "quote_author": p.quote_author,
                    "reply_to_message_id": p.reply_to_message_id,
                },
                ts,
            )
            attached = message_id
        except Exception:
            logger.exception(
                "Attaching the server id failed: protocol=%s contact=%s",
                protocol,
                contact_id,
            )
    return {
        "ok": True,
        "status": "sent",
        "status_applied": applied,
        "message_id": attached,
    }
