"""Unread badges and reply bar management."""

import logging

from textual.containers import Horizontal
from textual.widgets import Input, Static

from ui_components import (
    MessageWidget,
)

logger = logging.getLogger(__name__)


class UnreadReplyMixin:
    def _recompute_unread(self, contact_cache_key_value: str | None = None) -> bool:
        """Ricalcola i conteggi unread in ``self._unread_counts`` SOLO nei dati.

        Non tocca i widget: è il passo \"dati\" del flush di fine batch, così il
        render della lista avviene una volta sola (e mai a scapito della chat).
        Ritorna ``True`` se almeno un conteggio è cambiato.
        """
        if not self.contacts:
            return False

        def _count_unread(messages: list[dict]) -> int:
            """Conta i messaggi non letti, con dedup per (timestamp, text)."""
            seen: set[tuple[int, str]] = set()
            count = 0
            for m in messages:
                if m.get("is_mine"):
                    continue
                if m.get("read", True):
                    continue
                identity = (int(m.get("timestamp") or 0), m.get("text", ""))
                if identity not in seen:
                    seen.add(identity)
                    count += 1
            return count

        changed = False

        if contact_cache_key_value is not None:
            # Incrementale: solo il contatto indicato (O(M)).
            messages = self._cache.get(contact_cache_key_value, [])
            unread = _count_unread(messages)
            old = self._unread_counts.get(contact_cache_key_value, 0)
            if unread != old:
                self._unread_counts[contact_cache_key_value] = unread
                changed = True
        else:
            # Full: tutti i contatti (startup / ricalcolo globale).
            for contact in self.contacts:
                messages = self._cache.get(contact.cache_key, [])
                unread = _count_unread(messages)
                old = self._unread_counts.get(contact.cache_key, 0)
                if unread != old:
                    self._unread_counts[contact.cache_key] = unread
                    changed = True

        return changed

    def _update_unread_badges(self, contact_cache_key_value: str | None = None):
        """Check the in-memory cache and update *N badges on contacts.
        If counts change, re-sort the list and rebuild it.

        Parameters
        ----------
        contact_cache_key_value:
            If provided (a ``contact_cache_key`` string), only recompute the
            unread count for this single contact (O(M) instead of O(N×M)).
            If None, recompute for all contacts (startup / list load).
        """
        if not self.contacts:
            return

        if not self._recompute_unread(contact_cache_key_value):
            return

        # Re-sort and rebuild the list
        self._sort_contacts()
        self._render_contact_list(list(self.contacts))

    # ─── Reply-to (quote) handling ───────────────────────────────────────────

    def _update_reply_bar(self):
        """Show or hide the reply bar based on ``self._reply_to``."""
        bar = self.query_one("#reply-bar", Horizontal)
        text_widget = self.query_one("#reply-text", Static)
        if self._reply_to:
            reply_text = self._reply_to.get("text", "")
            # Truncate long messages for display
            if len(reply_text) > 60:
                reply_text = reply_text[:57] + "..."
            text_widget.update(f"↩️ Replying to: {reply_text}")
            bar.remove_class("reply-bar-hidden")
            bar.styles.display = "block"
        else:
            text_widget.update("")
            bar.add_class("reply-bar-hidden")
            bar.styles.display = "none"

    def _cancel_reply(self):
        """Cancel the current reply selection."""
        # Deselect the previously selected widget
        if self._reply_to is not None:
            prev_widget = self._reply_to.get("_widget")
            if prev_widget is not None:
                try:
                    prev_widget.set_selected(False)
                except Exception as _e:
                    logger.debug(
                        "Failed to deselect previous reply widget", exc_info=True
                    )
        self._reply_to = None
        self._update_reply_bar()

    def on_message_widget_message_clicked(self, event: MessageWidget.MessageClicked):
        """Handle ``MessageClicked`` from a ``MessageWidget``.

        If download mode is active, serve the message text as a .txt file
        for download.  Otherwise toggles reply selection on the clicked
        message.
        """
        if self._download_mode:
            # In download mode: serve the message text as a downloadable file
            self._start_download(
                text=event.text,
                attachment_id=None,
                timestamp=event.timestamp,
            )
            return

        if event.is_mine and event.status == "failed":
            self._retry_failed_message(event.timestamp, event.text)
            return

        # If clicking the same message, cancel the reply
        if (
            self._reply_to is not None
            and self._reply_to.get("timestamp") == event.timestamp
        ):
            self._cancel_reply()
            return

        # Deselect the previously selected widget
        if self._reply_to is not None:
            prev_widget = self._reply_to.get("_widget")
            if prev_widget is not None:
                try:
                    prev_widget.set_selected(False)
                except Exception as _e:
                    logger.debug(
                        "Failed to deselect previous reply widget", exc_info=True
                    )

        # Store the new reply target
        self._reply_to = {
            "text": event.text,
            "timestamp": event.timestamp,
            "sender": event.sender,
            "is_mine": event.is_mine,
        }

        # Highlight the clicked widget (find it by timestamp in the chat log)
        chat_log = self.chat_log
        for child in chat_log.children:
            if (
                isinstance(child, MessageWidget)
                and child._msg_timestamp == event.timestamp
            ):
                child.set_selected(True)
                self._reply_to["_widget"] = child
                break

        self._update_reply_bar()

        # Return focus to the message input so the user can start typing
        # the reply immediately.
        try:
            self.query_one("#message-input", Input).focus()
        except Exception as _e:
            logger.debug("Failed to focus message input", exc_info=True)
