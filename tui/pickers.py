"""Emoji/contact pickers, device-link screen, emoji completion."""

import logging

from textual.widgets import Input

from contact_picker import ContactPickerScreen
from device_link_screen import DeviceLinkPickerScreen
from emoji_picker import (
    EmojiCompletionWidget,
    EmojiPickerScreen,
)
from models import (
    ChatContact,
)

logger = logging.getLogger("signal_tui")


class PickerMixin:
    def _is_emoji_picker_open(self) -> bool:
        """Check if the emoji picker modal screen is currently active."""
        return isinstance(self.screen, EmojiPickerScreen)

    def _open_emoji_picker(self) -> None:
        """Open the emoji picker modal."""

        def _on_emoji_selected(emoji_char: str | None) -> None:
            if emoji_char:
                # Insert the selected emoji into the message input
                msg_input = self.query_one("#message-input", Input)
                current = msg_input.value
                cursor = msg_input.cursor_position
                # Insert at cursor position
                new_value = current[:cursor] + emoji_char + current[cursor:]
                msg_input.value = new_value
                msg_input.cursor_position = cursor + len(emoji_char)
                msg_input.focus()
            # Refresh chat to show any messages that arrived while the picker was open
            self._refresh_chat()

        self.push_screen(EmojiPickerScreen(), _on_emoji_selected)

    def action_open_emoji_picker(self) -> None:
        """Action to open emoji picker (bound to Ctrl+E)."""
        self._open_emoji_picker()

    # ─── Contact picker ───────────────────────────────────────────────────────

    def _open_contact_picker(self) -> None:
        """Open the contact search picker modal."""

        def _on_contact_selected(contact: ChatContact | None) -> None:
            if contact:
                # Select the contact's chat (also highlights it in the left list).
                # _select_contact already reloads the full chat from cache, so
                # calling _refresh_chat() afterwards would re-add the same
                # messages (the load worker runs in a separate thread and may
                # not have populated _seen_timestamps yet, making _refresh_chat
                # add everything again → duplicated messages).
                self._select_contact(contact)
            else:
                # Picker dismissed without selecting: refresh to show any
                # messages that arrived while the picker was open.
                self._refresh_chat()

        self.push_screen(
            ContactPickerScreen(self._filtered_contacts()), _on_contact_selected
        )

    def action_open_contact_picker(self) -> None:
        """Action to open the contact picker (bound to Ctrl+S)."""
        self._open_contact_picker()

    # ─── Device link picker ────────────────────────────────────────────────────

    def _open_device_link(self) -> None:
        """Open the device link picker modal (Ctrl+L)."""
        screen = DeviceLinkPickerScreen(
            signal_number=self.signal_backend.user_number,
            has_whatsapp=self.whatsapp_backend is not None,
            has_telegram=self.telegram_backend is not None,
        )

        def _on_done(_: object) -> None:
            logger.info("LINK-DONE: callback fired")
            self._reconnect_touched_backends(screen._touched_protocols)

        self.push_screen(screen, _on_done)

    def action_open_device_link(self) -> None:
        """Action to open device link picker (bound to Ctrl+L)."""
        self._open_device_link()

    # ─── Emoji alias auto-completion ──────────────────────────────────────────

    def _is_completion_visible(self) -> bool:
        """Check if the emoji completion widget is currently visible."""
        try:
            completion = self.query_one("#emoji-completion", EmojiCompletionWidget)
            return completion.has_class("-visible")
        except Exception as _e:
            logger.debug("Emoji completion not found", exc_info=True)
            return False

    def on_input_changed(self, event: Input.Changed) -> None:
        """Handle input changes for emoji alias auto-completion."""
        if event.input.id != "message-input":
            return

        value = event.value
        # Check if the user is typing an emoji alias (starts with ':')
        if ":" in value:
            # Find the last ':' that starts an alias
            last_colon = value.rfind(":")
            if last_colon >= 0:
                # Check if there's a closing ':' after it
                rest = value[last_colon + 1 :]
                # If no space after the colon, it might be an incomplete alias
                if " " not in rest and "/" not in rest:
                    prefix = rest
                    # Try to show suggestions
                    completion = self.query_one(
                        "#emoji-completion", EmojiCompletionWidget
                    )
                    completion.show_suggestions(prefix)
                    return

        # Hide completion if no alias is being typed
        try:
            completion = self.query_one("#emoji-completion", EmojiCompletionWidget)
            completion.hide_suggestions()
        except Exception as _e:
            logger.debug("Failed to hide emoji completion", exc_info=True)

    def _insert_emoji_from_completion(self) -> None:
        """Replace the current :alias: with the selected emoji from completion."""
        try:
            completion = self.query_one("#emoji-completion", EmojiCompletionWidget)
        except Exception as _e:
            logger.debug("Emoji completion not found", exc_info=True)
            return

        if not completion.selected_emoji:
            return

        msg_input = self.query_one("#message-input", Input)
        value = msg_input.value
        last_colon = value.rfind(":")
        if last_colon < 0:
            return

        # Replace from the last ':' to the end with the emoji
        new_value = value[:last_colon] + completion.selected_emoji + " "
        msg_input.value = new_value
        msg_input.cursor_position = len(new_value)
        completion.hide_suggestions()
        msg_input.focus()

    def action_next_suggestion(self) -> None:
        """Ctrl+N: go to next emoji suggestion.
        Does nothing if the emoji picker is open (so Ctrl+N reaches the picker)."""
        if self._is_emoji_picker_open():
            return
        if self._is_completion_visible():
            try:
                completion = self.query_one("#emoji-completion", EmojiCompletionWidget)
                completion.select_next()
            except Exception as _e:
                logger.debug("Failed to advance suggestion", exc_info=True)

    def action_prev_suggestion(self) -> None:
        """Ctrl+P: go to previous emoji suggestion.
        Does nothing if the emoji picker is open (so Ctrl+P reaches the picker)."""
        if self._is_emoji_picker_open():
            return
        if self._is_completion_visible():
            try:
                completion = self.query_one("#emoji-completion", EmojiCompletionWidget)
                completion.select_prev()
            except Exception as _e:
                logger.debug("Failed to rewind suggestion", exc_info=True)
