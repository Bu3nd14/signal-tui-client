"""Message sending (input submit + send worker)."""

import logging
import sys
import time
import uuid
from typing import Any

from textual.widgets import Input

from emoji_picker import EmojiCompletionWidget
from emoji_picker import replace_emoji_aliases as _replace_emoji_aliases
from models import (
    PROTOCOL_SIGNAL,
    PROTOCOL_TELEGRAM,
    PROTOCOL_WHATSAPP,
    is_media_quote_placeholder,
)
from server.api import ApiError
from ui_components import MessageTextArea

logger = logging.getLogger(__name__)


def _quote_content_type(reply_data: dict) -> str | None:
    """Return the persisted quoted-attachment mime type, or ``None``.

    ``None`` is returned for legacy rows (pre-migration, ``content_type IS
    NULL``): the reply degrades to the V2 behaviour — no ``quoteAttachments``,
    so the quote stays correct and typed but without a thumbnail.  Deriving the
    mime from ``msg_type`` was considered and rejected (design §9): the mapping
    is unreliable (Signal video/audio are ``msg_type="attachment"`` → would
    yield ``application/octet-stream``) and guessing from the file extension is
    fragile, so the persisted value is the only trusted source.
    """
    return (reply_data.get("content_type") or "").strip() or None


def _resolve_emoji_replacer():
    """Return the alias replacer, patchable via ``signal_tui.replace_emoji_aliases``.

    Resolved lazily (instead of importing ``signal_tui`` at module level) to
    avoid a circular import: ``tui.send`` → ``signal_tui`` → ``tui.app``.
    Tests patch ``signal_tui.replace_emoji_aliases``, so we read it from the
    already-imported ``signal_tui`` module when available.
    """
    stui = sys.modules.get("signal_tui")
    if stui is not None:
        return stui.replace_emoji_aliases
    return _replace_emoji_aliases


class SendMixin:
    def on_message_text_area_submitted(self, event: MessageTextArea.Submitted) -> None:
        """Send a message when the user presses Enter.
        Also converts any :emoji: aliases in the message.
        If emoji completion is visible, insert the selected emoji instead."""
        # If emoji completion is visible, insert the selected emoji
        if self._is_completion_visible():
            self._insert_emoji_from_completion()
            return

        if not self.selected_contact:
            self._status("❌ Select a contact first!")
            return

        # Hide completion if visible
        try:
            completion = self.query_one("#emoji-completion", EmojiCompletionWidget)
            completion.hide_suggestions()
        except Exception as _e:
            logger.debug("Failed to hide emoji completion", exc_info=True)

        # Convert emoji aliases (e.g. :smile: → 😊)
        message = _resolve_emoji_replacer()(
            event.value.replace("\r\n", "\n").replace("\r", "\n").strip()
        )

        if not message:
            return

        if self._editing_message is not None:
            self._submit_edit(message)
            event.text_area.text = ""
            return

        contact = self.selected_contact
        contact_id = contact.id
        protocol = contact.protocol
        cache_key = contact.cache_key
        ts = int(time.time() * 1000)

        # Capture reply data before clearing it
        reply_data = self._reply_to
        quote_text = reply_data.get("text") if reply_data else None
        reply_to_message_id = reply_data.get("message_id") if reply_data else None

        # Telegram replies must use the original server message id.  A timestamp
        # is not a Telegram message id; refusing here prevents an optimistic
        # normal-message bubble from being created for an impossible reply.
        if reply_data and protocol == PROTOCOL_TELEGRAM:
            try:
                if (
                    isinstance(reply_to_message_id, bool)
                    or int(reply_to_message_id) <= 0
                ):
                    raise ValueError
            except (TypeError, ValueError):
                self._status(
                    "❌ Cannot reply: the original Telegram message ID is unavailable",
                    0,
                )
                return

        # WhatsApp quotes are applied server-side via the Baileys ``reply_to``
        # id only (WAHA ignores the ``quote_*`` params).  Without that id the
        # reply would be dropped or attached to the wrong target, so refuse
        # before creating an optimistic bubble.
        if reply_data and protocol == PROTOCOL_WHATSAPP and not reply_to_message_id:
            self._status(
                "❌ Cannot reply: the original WhatsApp message ID is unavailable",
                0,
            )
            return

        # The core seeds, persists and sends the row (``messages.send`` in
        # its optimistic flavour) from the worker; here only the view's own
        # copy of the bubble is created, instantly.
        quote_attachment_id = (
            reply_data.get("quote_attachment_id") if reply_data else None
        )
        quote_attachment_path = (
            reply_data.get("quote_attachment_path") if reply_data else None
        )
        quote_content_type = (
            reply_data.get("quote_content_type") if reply_data else None
        )
        attachment_id = reply_data.get("attachment_id") if reply_data else None
        content_type = reply_data.get("content_type") if reply_data else None
        quote_timestamp = reply_data.get("timestamp") if reply_data else None
        quote_author = contact_id if reply_data else None

        # Update in-memory cache for UI
        if cache_key not in self._cache:
            self._cache[cache_key] = []
        self._cache[cache_key].append(
            {
                "text": message,
                "is_mine": True,
                "sender": "You",
                "timestamp": ts,
                "quote_text": quote_text,
                "msg_type": "text",
                "attachment_info": None,
                "attachment_id": attachment_id,
                "content_type": content_type,
                "read": True,
                "status": "pending",
                "quote_timestamp": quote_timestamp,
                "quote_author": quote_author,
                "reply_to_message_id": reply_to_message_id,
                "quote_attachment_id": quote_attachment_id,
                "quote_attachment_path": quote_attachment_path,
                "quote_content_type": quote_content_type,
            }
        )

        self._promote_contact_after_send(contact, ts)

        # Show the message in the UI immediately (with quote if replying)

        self._add_message(
            message,
            is_mine=True,
            quote_text=quote_text,
            quote_attachment_id=quote_attachment_id,
            quote_attachment_path=quote_attachment_path,
            quote_content_type=quote_content_type,
            timestamp=ts,
            sender="You",
            status="pending",
            message_id=None,
        )
        self._seen_timestamps.add((protocol, cache_key, ts))
        self._seen_message_ids.add((protocol, cache_key, int(ts), message))

        event.text_area.text = ""

        # Cancel the reply highlight
        self._cancel_reply()

        self.run_worker(
            lambda msg=message, ts=ts, rdata=reply_data, protocol=protocol, contact_id=contact_id: (
                self._send_message_worker(
                    msg,
                    ts,
                    rdata,
                    protocol=protocol,
                    contact_id=contact_id,
                )
            ),
            exclusive=False,
            thread=True,
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Keep synthetic legacy submissions working without handling real Inputs."""
        if isinstance(event, Input.Submitted):
            return
        self.on_message_text_area_submitted(event)  # type: ignore[arg-type]

    def _build_send_request(
        self,
        message: str,
        timestamp: int,
        reply_data: dict | None,
        *,
        protocol: str,
        contact_id: str,
        resend: bool,
    ) -> dict[str, Any]:
        """The ``messages.send`` request of one optimistic send (JSON data)."""

        def _text(value: Any) -> str | None:
            return None if value is None else str(value)

        quote_message = reply_data.get("text") if reply_data else None
        reply_to_message_id = reply_data.get("message_id") if reply_data else None
        request: dict[str, Any] = {
            "protocol": protocol,
            "contact_id": contact_id,
            "text": message,
            "client_msg_id": uuid.uuid4().hex,
            # The user retries a failed bubble by clicking it: one attempt.
            "retry": False,
            # quote_author MUST be a contact id, not a display name: the
            # contact we were chatting with when the reply was submitted.
            "quote_timestamp": (
                int(reply_data["timestamp"])
                if reply_data and reply_data.get("timestamp") is not None
                else None
            ),
            "quote_author": contact_id if reply_data else None,
            "reply_to_message_id": _text(reply_to_message_id),
            "optimistic": {
                "timestamp": timestamp,
                "resend": resend,
                "quote_text": _text(quote_message),
                "attachment_id": _text(
                    reply_data.get("attachment_id") if reply_data else None
                ),
                "content_type": _text(
                    reply_data.get("content_type") if reply_data else None
                ),
                # Quoted-media thumbnail metadata (display-only).
                "quote_attachment_id": _text(
                    reply_data.get("quote_attachment_id") if reply_data else None
                ),
                "quote_attachment_path": _text(
                    reply_data.get("quote_attachment_path") if reply_data else None
                ),
                "quote_content_type": _text(
                    reply_data.get("quote_content_type") if reply_data else None
                ),
            },
        }
        # Wire-faithful quote body (R1/R2): a media reply carries the explicit
        # ``quote_wire_body`` key (real caption or None), so the Signal quote on
        # the wire uses that body (caption or "") — never the display
        # placeholder and never omitted.  Text replies never set the key, so
        # their text is used unchanged.  WA/TG ignore ``quote_message`` anyway.
        is_signal_media_reply = (
            reply_data is not None
            and "quote_wire_body" in reply_data
            and protocol == PROTOCOL_SIGNAL
        )
        if is_signal_media_reply:
            assert reply_data is not None
            quote_message = reply_data["quote_wire_body"] or ""
        request["quote_message"] = _text(quote_message)
        # Signal media reply: the core builds the ``quoteAttachments``
        # descriptor from the quoted attachment so the recipient renders the
        # thumbnail (bug #37, piano B); without a mime type (legacy rows) the
        # quote stays typed-less and thumbnail-less (V2 behaviour).
        if is_signal_media_reply:
            assert reply_data is not None
            content_type = _quote_content_type(reply_data)
            if content_type:
                request["quote_content_type"] = content_type
                request["quote_attachment_id"] = _text(reply_data.get("attachment_id"))
        return request

    def _send_message_worker(
        self,
        message: str,
        timestamp: int,
        reply_data: dict | None = None,
        *,
        protocol: str,
        contact_id: str,
        resend: bool = False,
    ):
        """Send one optimistic message through ``messages.send`` (worker thread).

        The core seeds and persists the pending row, sends it and moves it to
        ``sent`` / ``failed`` (see ``OptimisticSend``); this method applies the
        outcome to the view's own copies (UI cache, bubble).

        Parameters
        ----------
        message:
            The message text to send.
        timestamp:
            The client-generated timestamp (ms): the row's identity until the
            echo arrives, so receipt timestamps match.
        reply_data:
            If provided, the message is sent as a quote/reply.
        protocol, contact_id:
            The conversation selected when the message was submitted.
        resend:
            Re-send a ``failed`` row instead of seeding a new one.
        """
        request = self._build_send_request(
            message,
            timestamp,
            reply_data,
            protocol=protocol,
            contact_id=contact_id,
            resend=resend,
        )
        logger.debug(
            "worker send start: resend=%s protocol=%s contact=%r ts=%s text=%r",
            resend,
            protocol,
            contact_id,
            timestamp,
            message[:60],
        )
        try:
            result = self.client.call("messages.send", request)
        except Exception as e:
            # The cause goes to the log and the (permanent) status bar.
            logger.exception(
                "messages.send failed (protocol=%r contact=%r ts=%s text=%r)",
                protocol,
                contact_id,
                timestamp,
                message[:60],
            )
            # Whatever the core managed to store, the bubble must not stay
            # pending (nor, after a refused re-send, look like it is retrying).
            self._apply_outgoing_status(
                protocol, contact_id, timestamp, message, "failed", ("pending",)
            )
            details = e.details if isinstance(e, ApiError) else None
            error = details.get("error") if isinstance(details, dict) else None
            self.call_from_thread(self._status, f"❌ Send error: {error or e}", 0)
            return

        if result.get("status_applied"):
            self._apply_outgoing_status(
                protocol, contact_id, timestamp, message, "sent", ("pending",)
            )
        # The core attached the real server id to its row so the echo matches
        # by id and never rewrites the timestamp: mirror it into the view.
        message_id = result.get("message_id")
        if message_id:
            self._update_outgoing_message_id(
                protocol, contact_id, timestamp, message, str(message_id)
            )

    def _apply_outgoing_status(
        self,
        protocol: str,
        contact_id: str,
        timestamp: int,
        text: str,
        status: str,
        expected_statuses: tuple[str, ...],
    ) -> None:
        """Advance one optimistic message in the view: UI cache and bubble.

        The core has already moved its own row (SQLite + chat cache, rank
        guard included); ``expected_statuses`` keeps a receipt that arrived
        first from being overwritten here.
        """
        from models import contact_cache_key

        for msg in self._cache.get(contact_cache_key(protocol, contact_id), []):
            if (
                msg.get("is_mine")
                and msg.get("text") == text
                and msg.get("status") in expected_statuses
            ):
                msg["status"] = status
        self.call_from_thread(
            self._update_message_widgets_status,
            [{"timestamp": timestamp, "text": text, "status": status}],
        )

    def _update_outgoing_message_id(
        self,
        protocol: str,
        contact_id: str,
        timestamp: int,
        text: str,
        message_id: str,
    ) -> None:
        """Synchronize the real server id into the UI cache and widget.

        The send result carries the server-assigned message id, which must be
        mirrored into the UI cache so a later receipt/echo can be matched by id
        (and never rewrite the optimistic timestamp).
        """
        from models import contact_cache_key

        for msg in self._cache.get(contact_cache_key(protocol, contact_id), []):
            if (
                msg.get("is_mine")
                and msg.get("timestamp") == timestamp
                and msg.get("text") == text
            ):
                msg["id"] = message_id
        self.call_from_thread(
            self._update_outgoing_message_widget_id, timestamp, text, message_id
        )

    def _update_outgoing_message_widget_id(
        self, timestamp: int, text: str, message_id: str
    ) -> None:
        """Update the mounted optimistic bubble on Textual's UI thread."""
        try:
            for child in self.chat_log.children:
                if (
                    getattr(child, "_msg_is_mine", False)
                    and getattr(child, "_msg_timestamp", None) == timestamp
                    and getattr(child, "_msg_text", None) == text
                ):
                    child._message_id = message_id
        except Exception:
            logger.debug("Unable to update optimistic message widget id", exc_info=True)

    def _retry_failed_message(self, timestamp: int, text: str) -> None:
        """Retry a failed optimistic message without creating another row or bubble."""
        contact = self.selected_contact
        if contact is None:
            logger.warning(
                "retry aborted: selected_contact is None (ts=%s text=%r)",
                timestamp,
                text[:40],
            )
            return
        cache_entries = self._cache.get(contact.cache_key, [])
        message = next(
            (
                item
                for item in cache_entries
                if item.get("is_mine")
                and item.get("timestamp") == timestamp
                and item.get("text") == text
                and item.get("status") == "failed"
            ),
            None,
        )
        if message is None:
            logger.warning(
                "retry aborted: entry not found in UI cache (contact=%r cache_key=%r "
                "n_entries=%d ts=%s text=%r; statuses=%r)",
                contact.id,
                contact.cache_key,
                len(cache_entries),
                timestamp,
                text[:40],
                [m.get("status") for m in cache_entries if m.get("is_mine")][:10],
            )
            return
        if (
            contact.protocol == PROTOCOL_TELEGRAM
            and message.get("reply_to_message_id") is None
            and (
                message.get("quote_timestamp") is not None
                or message.get("quote_author") is not None
            )
        ):
            self.call_from_thread(
                self._status,
                "❌ Cannot retry a Telegram reply; original message ID is unavailable",
                0,
            )
            return
        if message.get("quote_text") and message.get("quote_timestamp") is None:
            self.call_from_thread(
                self._status,
                "❌ Cannot retry a reply after reload; quote metadata is unavailable",
                0,
            )
            return
        # Bubble back to pending at once; the core re-arms its own row (and
        # refuses when that row is no longer failed) as part of the send.
        self._apply_outgoing_status(
            contact.protocol, contact.id, timestamp, text, "pending", ("failed",)
        )
        logger.debug(
            "retry started: protocol=%r contact=%r ts=%s text=%r",
            contact.protocol,
            contact.id,
            timestamp,
            text[:40],
        )
        reply_data = None
        if message.get("quote_text"):
            reply_data = {
                "text": message["quote_text"],
                "timestamp": message["quote_timestamp"],
            }
            if message.get("reply_to_message_id") is not None:
                reply_data["message_id"] = message["reply_to_message_id"]
            # A media reply carries the quoted attachment metadata on its own
            # row (persisted at submit time).  Reconstruct it so the retry
            # rebuilds ``quoteAttachments`` exactly like the live send.  The
            # media marker is the presence of that metadata — NOT the display
            # placeholder — so a captioned media reply (whose persisted
            # ``quote_text`` is the real caption) is reconstructed too.
            is_media_reply = (
                message.get("content_type") is not None
                or message.get("attachment_id") is not None
            )
            if is_media_reply:
                reply_data["content_type"] = message.get("content_type")
                reply_data["attachment_id"] = message.get("attachment_id")
                # R2-bis: a media reply without caption has only the display
                # placeholder persisted; reconstruct a faithful empty body so
                # the Signal quote is never the placeholder.  For captioned
                # media the wire body is the caption itself (``quote_text``).
                if is_media_quote_placeholder(message["quote_text"]):
                    reply_data["quote_wire_body"] = ""
                else:
                    reply_data["quote_wire_body"] = message["quote_text"]
            elif is_media_quote_placeholder(message["quote_text"]):
                # Legacy captionless media row (pre-piano-B, no metadata):
                # keep the wire body empty (R2-bis) but there is nothing to
                # rebuild the thumbnail from → V2 behaviour.
                reply_data["quote_wire_body"] = ""
        self.run_worker(
            lambda: self._send_message_worker(
                text,
                timestamp,
                reply_data,
                protocol=contact.protocol,
                contact_id=contact.id,
                resend=True,
            ),
            exclusive=False,
            thread=True,
        )
