"""Message sending (input submit + send worker)."""

import logging
import sys
import time

from textual.widgets import Input

from emoji_picker import EmojiCompletionWidget
from emoji_picker import replace_emoji_aliases as _replace_emoji_aliases
from models import (
    PROTOCOL_TELEGRAM,
)

logger = logging.getLogger(__name__)


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
    def on_input_submitted(self, event: Input.Submitted):
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
        message = _resolve_emoji_replacer()(event.value.strip())

        if not message:
            return

        contact = self.selected_contact
        contact_id = contact.id
        cache_key = contact.cache_key
        ts = int(time.time() * 1000)

        # Capture reply data before clearing it
        reply_data = self._reply_to
        quote_text = reply_data.get("text") if reply_data else None

        # Save to SQLite (incremental INSERT), protocol-aware.
        data = {
            "text": message,
            "is_mine": True,
            "sender": "You",
            "timestamp": ts,
            "quote_text": quote_text,
            "msg_type": "text",
            "attachment_info": None,
            "attachment_id": None,
        }
        # Ingerisci l'ottimista nel backend CORRETTO del contatto (non hardcoded
        # su signal_backend): per WhatsApp deve finire nel WhatsAppBackend.cache,
        # altrimenti il polling dei messaggi non lo riconosce e ri-accoderebbe
        # l'echo -> messaggio mostrato DOPPIO (che si sistemava solo rientrando).
        ingest_backend = self.manager.get(contact.protocol)
        if ingest_backend is None:
            ingest_backend = self.signal_backend
        added = ingest_backend.ingest_message(contact_id, data, ts, persist=False)

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
                "attachment_id": None,
                "read": True,
                "status": "sent",
            }
        )

        self._promote_contact_after_send(contact, ts)

        # Show the message in the UI immediately (with quote if replying)

        self._add_message(
            message,
            is_mine=True,
            quote_text=quote_text,
            timestamp=ts,
            sender="You",
            status="sent",
        )
        self._seen_timestamps.add((contact.protocol, cache_key, ts))
        self._seen_message_ids.add((contact.protocol, cache_key, int(ts), message))

        event.input.value = ""

        # Cancel the reply highlight
        self._cancel_reply()

        self.run_worker(
            lambda msg=message, ts=ts, rdata=reply_data, persist=((ingest_backend, contact_id, data, ts) if added else None): (
                self._send_message_worker(msg, ts, rdata, persist=persist)
            ),
            exclusive=False,
            thread=True,
        )

    def _send_message_worker(
        self,
        message: str,
        timestamp: int,
        reply_data: dict | None = None,
        persist: tuple | None = None,
    ):
        """Send a message via the active backend's send path.

        Parameters
        ----------
        message:
            The message text to send.
        timestamp:
            The client-generated timestamp (ms) used as the message ID.
            Passed to the backend so that receipt timestamps match.
        reply_data:
            If provided, the message is sent as a quote/reply.
        persist:
            Optional ``(backend, contact_id, data, ts)`` payload.  When given,
            the optimistic row is persisted to SQLite here (worker thread),
            BEFORE the network send, so the echo always finds the row to
            upgrade via ``_update_message_id``.
        """
        if persist is not None:
            backend, contact_id, data, ts = persist
            backend._persist_message(contact_id, data, ts)

        if not self.selected_contact:
            return

        contact = self.selected_contact

        # Extract quote parameters from reply_data
        # quote_author MUST be a contact id, not a display name.
        # We always use the selected contact's id because we are
        # replying to the person we are chatting with.
        quote_timestamp = reply_data.get("timestamp") if reply_data else None
        quote_author = contact.id if reply_data else None
        quote_message = reply_data.get("text") if reply_data else None

        # Send synchronously through the selected contact's backend.  This is
        # a sync call running in a worker thread; it is NOT an async coroutine
        # that needs awaiting, which would otherwise be silently dropped.
        backend = self.manager.get(contact.protocol)
        if backend is None:
            self.call_from_thread(
                self._status, f"❌ No backend for protocol: {contact.protocol}", 0
            )
            return

        try:
            result = backend.send_message_sync(
                contact.id,
                message,
                quote_timestamp=quote_timestamp,
                quote_author=quote_author,
                quote_message=quote_message,
            )
            # For Telegram, ingest the real message id to upgrade the optimistic entry
            if contact.protocol == PROTOCOL_TELEGRAM and result:
                ingest_backend = self.manager.get(contact.protocol)
                if ingest_backend is not None:
                    ingest_backend.ingest_message(
                        contact.id,
                        {
                            "id": result,
                            "text": message,
                            "is_mine": True,
                            "sender": "You",
                            "timestamp": int(time.time() * 1000),
                        },
                        int(time.time() * 1000),
                    )
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self._status, f"❌ Send error: {e}", 0)
