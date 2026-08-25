"""Regression tests for failed optimistic-message status handling."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from models import PROTOCOL_TELEGRAM
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
        same_text = MessageWidget("optimistic", timestamp=12_000, is_mine=True)
        target.set_status = MagicMock()
        same_text.set_status = MagicMock()
        handler = SimpleNamespace(
            chat_log=SimpleNamespace(children=[same_text, target])
        )

        EventHandlingMixin._update_message_widgets_status(
            handler,
            [{"timestamp": 12_000, "text": "optimistic", "status": "sent"}],
        )

        # Exact (timestamp, text) match wins for the same-text widget.
        same_text.set_status.assert_called_once_with("sent")
        # The other widget shares the text but differs in timestamp beyond the
        # 2000ms fuzzy window → NOT recolored.
        target.set_status.assert_not_called()

    def test_fuzzy_status_match_ignores_widget_with_different_text(self):
        target = MessageWidget("optimistic", timestamp=10_000, is_mine=True)
        other_text = MessageWidget("other", timestamp=10_001, is_mine=True)
        target.set_status = MagicMock()
        other_text.set_status = MagicMock()
        handler = SimpleNamespace(
            chat_log=SimpleNamespace(children=[other_text, target])
        )

        EventHandlingMixin._update_message_widgets_status(
            handler,
            [{"timestamp": 12_000, "text": "optimistic", "status": "sent"}],
        )

        # Fuzzy fallback is bound to the text: only the same-text widget within
        # ±2000ms is updated, never a nearby bubble with different text.
        target.set_status.assert_called_once_with("sent")
        other_text.set_status.assert_not_called()


class _ReplyHandler(UnreadReplyMixin):
    def __init__(self, children=()):
        self._download_mode = False
        self._reply_to = None
        self._retry_failed_message = MagicMock()
        self._update_reply_bar = MagicMock()
        self.chat_log = SimpleNamespace(children=children)
        self.input = MagicMock()
        self.query_one = MagicMock(return_value=self.input)
        # Il retry dal click gira in un worker thread (run_worker): nel test
        # esegui subito la lambda per verificare il comportamento.
        self.run_worker = MagicMock(
            side_effect=lambda fn, *a, **kw: fn() if callable(fn) else None
        )


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
        self.call_from_thread = MagicMock(
            side_effect=lambda fn, *a, **kw: fn(*a, **kw) if callable(fn) else None
        )


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

    def test_retry_keeps_legacy_telegram_reply_failed_without_message_id(self):
        contact = SimpleNamespace(
            cache_key="telegram:42", protocol=PROTOCOL_TELEGRAM, id="42"
        )
        handler = _SendHandler(
            contact,
            [
                {
                    "is_mine": True,
                    "timestamp": 1234,
                    "text": "retry me",
                    "status": "failed",
                    "quote_timestamp": 1000,
                }
            ],
        )

        handler._retry_failed_message(1234, "retry me")

        handler._status.assert_called_once_with(
            "❌ Cannot retry a Telegram reply; original message ID is unavailable", 0
        )
        handler._transition_outgoing_status.assert_not_called()
        handler.run_worker.assert_not_called()
        assert handler._cache[contact.cache_key][0]["status"] == "failed"

    def test_retry_telegram_reply_forwards_persisted_message_id_to_backend(self):
        contact = SimpleNamespace(
            cache_key="telegram:42", protocol=PROTOCOL_TELEGRAM, id="42"
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
                    "reply_to_message_id": "12",
                }
            ],
        )
        backend = MagicMock()
        backend.send_message_sync.return_value = None
        handler.manager = SimpleNamespace(get=MagicMock(return_value=backend))

        handler._retry_failed_message(1234, "retry me")
        worker = handler.run_worker.call_args.args[0]
        worker()

        backend.send_message_sync.assert_called_once_with(
            "42",
            "retry me",
            quote_timestamp=1000,
            quote_author="42",
            quote_message="original message",
            reply_to_message_id="12",
        )

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


def test_telegram_send_result_updates_cache_and_optimistic_widget_message_id():
    contact = SimpleNamespace(
        cache_key="telegram:42", protocol=PROTOCOL_TELEGRAM, id="42"
    )
    timestamp = 1234
    text = "telegram reply"
    bubble = MessageWidget(text, timestamp=timestamp, is_mine=True, status="pending")
    handler = _SendHandler(
        contact,
        [{"is_mine": True, "timestamp": timestamp, "text": text, "id": None}],
    )
    backend = MagicMock()
    backend.send_message_sync.return_value = "77"
    handler.manager = SimpleNamespace(get=MagicMock(return_value=backend))
    handler.chat_log = SimpleNamespace(children=[bubble])
    handler.call_from_thread = MagicMock(
        side_effect=lambda callback, *args: callback(*args)
    )
    update_message_id = MagicMock(wraps=handler._update_outgoing_message_id)
    handler._update_outgoing_message_id = update_message_id
    reply_data = {"text": "original", "timestamp": 1000, "message_id": "12"}

    handler._send_message_worker(
        text,
        timestamp,
        reply_data,
        protocol=PROTOCOL_TELEGRAM,
        contact_id=contact.id,
    )

    backend.ingest_message.assert_called_once()
    assert handler._cache[contact.cache_key][0]["id"] == "77"
    assert bubble._message_id == "77"
    update_message_id.assert_called_once_with(
        PROTOCOL_TELEGRAM, contact.id, timestamp, text, "77"
    )


def test_telegram_message_id_update_keeps_cache_when_optimistic_widget_is_gone():
    contact = SimpleNamespace(
        cache_key="telegram:42", protocol=PROTOCOL_TELEGRAM, id="42"
    )
    handler = _SendHandler(
        contact,
        [{"is_mine": True, "timestamp": 1234, "text": "sent", "id": None}],
    )
    handler.call_from_thread = MagicMock(
        side_effect=lambda callback, *args: callback(*args)
    )

    handler._update_outgoing_message_id(
        PROTOCOL_TELEGRAM, contact.id, 1234, "sent", "77"
    )

    assert handler._cache[contact.cache_key][0]["id"] == "77"


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
