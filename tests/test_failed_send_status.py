"""Regression tests for failed optimistic-message status handling."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tui.events import EventHandlingMixin
from tui.send import SendMixin
from tui.unread_reply import UnreadReplyMixin
from ui_components import MessageWidget


class TestMessageStatusWidgets:
    def test_exact_identity_match_does_not_update_timestamp_collision(self):
        same_timestamp = 1_700_000_000_000
        other = MessageWidget("other", timestamp=same_timestamp, is_mine=True)
        target = MessageWidget("target", timestamp=same_timestamp, is_mine=True)
        target.set_status = MagicMock()
        other.set_status = MagicMock()
        handler = SimpleNamespace(chat_log=SimpleNamespace(children=[other, target]))

        EventHandlingMixin._update_message_widgets_status(
            handler,
            [{"timestamp": same_timestamp, "text": "target", "status": "delivered"}],
        )

        target.set_status.assert_called_once_with("delivered")
        other.set_status.assert_not_called()

    def test_fuzzy_status_match_accepts_waha_timestamp_drift_within_two_seconds(self):
        target = MessageWidget("optimistic", timestamp=10_000, is_mine=True)
        outside_window = MessageWidget("old", timestamp=7_999, is_mine=True)
        target.set_status = MagicMock()
        outside_window.set_status = MagicMock()
        handler = SimpleNamespace(
            chat_log=SimpleNamespace(children=[outside_window, target])
        )

        EventHandlingMixin._update_message_widgets_status(
            handler,
            [{"timestamp": 12_000, "text": "backend copy", "status": "sent"}],
        )

        target.set_status.assert_called_once_with("sent")
        outside_window.set_status.assert_not_called()


class _ReplyHandler(UnreadReplyMixin):
    def __init__(self, children=()):
        self._download_mode = False
        self._reply_to = None
        self._retry_failed_message = MagicMock()
        self._update_reply_bar = MagicMock()
        self.chat_log = SimpleNamespace(children=children)
        self.input = MagicMock()
        self.query_one = MagicMock(return_value=self.input)


class TestFailedMessageClick:
    def test_clicking_own_failed_message_retries_without_entering_reply_flow(self):
        handler = _ReplyHandler()
        event = MessageWidget.MessageClicked(
            "retry me", 1234, "You", is_mine=True, status="failed"
        )

        handler.on_message_widget_message_clicked(event)

        handler._retry_failed_message.assert_called_once_with(1234, "retry me")
        handler._update_reply_bar.assert_not_called()
        assert handler._reply_to is None

    @pytest.mark.parametrize(
        ("is_mine", "status"),
        [(True, "sent"), (False, "failed")],
        ids=["own-sent", "other-failed"],
    )
    def test_non_retryable_message_click_keeps_normal_reply_flow(self, is_mine, status):
        widget = MessageWidget("reply to me", timestamp=5678, is_mine=is_mine)
        handler = _ReplyHandler([widget])
        event = MessageWidget.MessageClicked(
            "reply to me", 5678, "Mario", is_mine=is_mine, status=status
        )

        handler.on_message_widget_message_clicked(event)

        handler._retry_failed_message.assert_not_called()
        handler._update_reply_bar.assert_called_once_with()
        assert handler._reply_to == {
            "text": "reply to me",
            "timestamp": 5678,
            "sender": "Mario",
            "is_mine": is_mine,
            "_widget": widget,
        }
        assert widget._selected is True
        handler.input.focus.assert_called_once_with()


class _SendHandler(SendMixin):
    def __init__(self, selected_contact=None, messages=()):
        self.selected_contact = selected_contact
        self._cache = (
            {selected_contact.cache_key: list(messages)} if selected_contact else {}
        )
        self._status = MagicMock()
        self._transition_outgoing_status = MagicMock(return_value=True)
        self.run_worker = MagicMock()


class TestRetryGuards:
    def test_retry_returns_without_selected_contact(self):
        handler = _SendHandler()

        handler._retry_failed_message(1234, "retry me")

        handler._transition_outgoing_status.assert_not_called()
        handler.run_worker.assert_not_called()

    def test_retry_rejects_reloaded_reply_without_quote_timestamp(self):
        contact = SimpleNamespace(
            cache_key="signal:Mario", protocol="signal", id="Mario"
        )
        handler = _SendHandler(
            contact,
            [
                {
                    "is_mine": True,
                    "timestamp": 1234,
                    "text": "retry me",
                    "status": "failed",
                    "quote_text": "original message",
                }
            ],
        )

        handler._retry_failed_message(1234, "retry me")

        handler._status.assert_called_once_with(
            "❌ Cannot retry a reply after reload; quote metadata is unavailable", 0
        )
        handler._transition_outgoing_status.assert_not_called()
        handler.run_worker.assert_not_called()

    def test_retry_returns_when_message_is_not_failed(self):
        contact = SimpleNamespace(
            cache_key="signal:Mario", protocol="signal", id="Mario"
        )
        handler = _SendHandler(
            contact,
            [
                {
                    "is_mine": True,
                    "timestamp": 1234,
                    "text": "retry me",
                    "status": "sent",
                }
            ],
        )

        handler._retry_failed_message(1234, "retry me")

        handler._transition_outgoing_status.assert_not_called()
        handler.run_worker.assert_not_called()

    def test_retry_keeps_failed_message_when_status_transition_is_rejected(self):
        contact = SimpleNamespace(
            cache_key="signal:Mario", protocol="signal", id="Mario"
        )
        handler = _SendHandler(
            contact,
            [
                {
                    "is_mine": True,
                    "timestamp": 1234,
                    "text": "retry me",
                    "status": "failed",
                }
            ],
        )
        handler._transition_outgoing_status.return_value = False

        handler._retry_failed_message(1234, "retry me")

        handler._transition_outgoing_status.assert_called_once_with(
            "signal", "Mario", 1234, "retry me", "pending", ("failed",)
        )
        handler.run_worker.assert_not_called()

    def test_retry_preserves_quote_metadata_for_send_worker(self):
        contact = SimpleNamespace(
            cache_key="signal:Mario", protocol="signal", id="Mario"
        )
        handler = _SendHandler(
            contact,
            [
                {
                    "is_mine": True,
                    "timestamp": 1234,
                    "text": "retry me",
                    "status": "failed",
                    "quote_text": "original message",
                    "quote_timestamp": 1000,
                }
            ],
        )

        handler._retry_failed_message(1234, "retry me")

        handler.run_worker.assert_called_once()
        worker = handler.run_worker.call_args.args[0]
        handler._send_message_worker = MagicMock()
        worker()
        handler._send_message_worker.assert_called_once_with(
            "retry me",
            1234,
            {"text": "original message", "timestamp": 1000},
            protocol="signal",
            contact_id="Mario",
        )


def test_send_without_backend_marks_optimistic_message_as_failed():
    handler = _SendHandler()
    handler.manager = SimpleNamespace(get=MagicMock(return_value=None))
    handler.call_from_thread = MagicMock(
        side_effect=lambda callback, *args: callback(*args)
    )

    handler._send_message_worker(
        "offline", 4321, None, protocol="whatsapp", contact_id="Mario"
    )

    handler._transition_outgoing_status.assert_called_once_with(
        "whatsapp", "Mario", 4321, "offline", "failed", ("pending",)
    )
    handler._status.assert_called_once_with("❌ No backend for protocol: whatsapp", 0)
