"""Message send operation: validation, idempotency, retry, backend calls."""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from pathlib import Path
from typing import Any

from server import messages as _domain
from server.api import ApiError, ChatAPI, operation
from server.models_api import AttachmentIn, MessagesSendRequest
from server.ops.common import PROTOCOLS
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


@operation("messages.send", request=MessagesSendRequest, response=dict[str, bool])
async def messages_send(api: ChatAPI, req: MessagesSendRequest) -> dict[str, bool]:
    """Send a text message, or attachments (the text captions the first one).

    Text sends are idempotent per ``(protocol, contact_id, client_msg_id)`` and
    retried with backoff on transient backend errors (``send_retry`` frames);
    attachment sends are neither deduplicated nor retried.  ``attachments`` are
    local files stored by the adapter, which also deletes them afterwards.
    Async: the backend calls run in worker threads and the retry back-off
    sleeps on the loop, so concurrent sends do not block each other.
    """
    protocol, contact_id, text = req.protocol, req.contact_id, req.text
    attachments = req.attachments
    if protocol not in PROTOCOLS:
        raise _invalid()
    if not isinstance(contact_id, str) or not contact_id.strip():
        raise _invalid()
    if not isinstance(text, str) or len(text) > _domain.MAX_TEXT_LENGTH:
        raise _invalid()
    if not attachments and not text.strip():
        raise _invalid()
    if len(attachments) > MAX_ATTACHMENTS:
        raise ApiError("invalid_request", "Too many attachments")
    if any(not a.path and a.rejected is None for a in attachments):
        raise _invalid()

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
    if isinstance(quote_message, str):
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
            raise _invalid()
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
    if protocol in {"whatsapp", "telegram"} and reply_to_message_id is not None:
        kwargs["reply_to_message_id"] = reply_to_message_id

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
    try:
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
            for attempt in range(1, SEND_RETRY_MAX_ATTEMPTS + 1):
                try:
                    result = await asyncio.to_thread(
                        manager.send_message_sync,
                        protocol,
                        contact_id,
                        text,
                        **kwargs,
                    )
                    if result is not None:
                        message_id = str(result)
                    succeeded = True
                    break
                except Exception as exc:
                    classification = classify_send_error(protocol, exc)
                    if classification == "terminal":
                        logger.exception(
                            "Terminal send error, no retry: protocol=%s contact=%s",
                            protocol,
                            contact_id,
                        )
                        break
                    if attempt == SEND_RETRY_MAX_ATTEMPTS:
                        logger.exception(
                            "Send failed after %d attempts: protocol=%s contact=%s",
                            SEND_RETRY_MAX_ATTEMPTS,
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
    except ApiError:
        raise
    except NotImplementedError:
        if attachments:
            raise ApiError("not_supported", "Attachment send not supported") from None
        logger.exception(
            "Web send failed: protocol=%s contact=%s", protocol, contact_id
        )
        raise ApiError("backend_error", "Message send failed") from None
    except Exception:
        logger.exception(
            "Web send failed: protocol=%s contact=%s", protocol, contact_id
        )
        raise ApiError("backend_error", "Message send failed") from None
    finally:
        # B3/N2: mark_sent solo su successo, release in tutti gli altri casi
        # (incluso il 502 dopo i 3 tentativi), sempre vincolato al token.
        if registry_token is not None and registry_key is not None:
            if succeeded:
                registry.mark_sent(
                    registry_key, registry_token, message_id, int(time.time())
                )
            else:
                registry.release(registry_key, registry_token)

    if not succeeded:
        raise ApiError("backend_error", "Message send failed")

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
