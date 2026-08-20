"""Integration tests that launch the real TUI headless via ``App.run_test()``.

These tests are slower than the unit tests (Textual mounts the full widget
tree) and are marked ``integration`` so they can be excluded from fast runs
with ``-m "not integration"``.
"""

from unittest.mock import patch

import pytest
from textual import events
from textual.containers import Vertical
from textual.widgets import Label, ListView, Static

from emoji_picker import EmojiCompletionWidget, EmojiPickerScreen, get_emoji_suggestions
from ui_components import MessageTextArea, MessageWidget


@pytest.mark.integration
async def test_app_launches(app_for_test):
    """Smoke test: the app mounts headless with the expected widgets."""
    async with app_for_test.run_test() as pilot:
        await pilot.pause()

        app = pilot.app
        assert app.screen is not None
        assert app.query_one("#contact-list") is not None
        assert app.query_one("#message-input") is not None
        assert app.query_one("#status-bar") is not None


@pytest.mark.integration
async def test_quit_via_ctrl_q(app_for_test):
    """Ctrl+Q triggers ``action_quit`` and stops the app."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.press("ctrl+q")
        await pilot.pause()

        assert app._exit is True
        assert app.return_code == 0


@pytest.mark.integration
async def test_contact_list_renders(app_for_test):
    """The contacts injected by the fixture are rendered in #contact-list."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()

        contact_list = app.query_one("#contact-list", ListView)
        labels = [item._label_text for item in contact_list.children]
        assert labels == ["📱 Mario", "📱 Luigi", "📱 Giulia"]


@pytest.mark.integration
async def test_select_contact(app_for_test):
    """Clicking a contact updates ``selected_contact``."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()

        contact_list = app.query_one("#contact-list", ListView)
        await pilot.click(contact_list.children[0])
        await pilot.pause()

        assert app.selected_contact is not None
        assert app.selected_contact.display_name == "Mario"
        assert app.selected_contact.id == "+391234567890"


@pytest.mark.integration
async def test_protocol_filter_cycle(app_for_test):
    """Ctrl+W cycles the protocol filter all -> signal -> whatsapp -> telegram -> all."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._protocol_filter == "all"

        await pilot.press("ctrl+w")
        assert app._protocol_filter == "signal"

        await pilot.press("ctrl+w")
        assert app._protocol_filter == "whatsapp"

        await pilot.press("ctrl+w")
        assert app._protocol_filter == "telegram"

        await pilot.press("ctrl+w")
        assert app._protocol_filter == "all"


@pytest.mark.integration
async def test_status_bar_shows(app_for_test):
    """The #status-bar widget exists and reflects ``_status`` updates."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()

        status_bar = app.query_one("#status-bar", Static)
        app._status("Ciao", 0)  # persistent: no auto-clear timer
        assert status_bar.content == "Ciao"


@pytest.mark.integration
async def test_send_message_mocked(app_for_test_with_mocks):
    """Typing + Enter calls the mocked backend with contact + text."""
    app, signal_backend = app_for_test_with_mocks
    async with app.run_test() as pilot:
        await pilot.pause()

        # Select the first contact directly (selection UX is covered separately).
        app.selected_contact = app.contacts[0]

        input_widget = app.query_one("#message-input", MessageTextArea)
        input_widget.focus()
        input_widget.text = "Ciao!"

        signal_backend.send_message_sync.return_value = "ts-1"

        # Run the send worker synchronously so the backend call is deterministic.
        # ``call_from_thread`` is stubbed to run inline: in production the worker
        # runs on a real thread, so the real Textual ``call_from_thread`` is the
        # correct path — here we only neutralize the inline-worker limitation.
        with (
            patch.object(app, "run_worker", side_effect=lambda work, **kwargs: work()),
            patch.object(
                app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)
            ),
        ):
            await pilot.press("enter")

        await pilot.pause()

        contact = app.contacts[0]
        # The optimistic seed (on_input_submitted, persist=False) plus the real-id
        # ingestion in the send worker: Signal now enters the "real id" path, so
        # ingest is called twice — first id-less, then with the real server id.
        assert signal_backend.ingest_message.call_count == 2
        assert signal_backend.ingest_message.call_args.args[0] == contact.id
        assert signal_backend.ingest_message.call_args.args[1]["text"] == "Ciao!"
        assert signal_backend.ingest_message.call_args.args[1]["id"] == "ts-1"

        signal_backend.send_message_sync.assert_called_once_with(
            contact.id,
            "Ciao!",
            quote_timestamp=None,
            quote_author=None,
            quote_message=None,
        )

        # The real server id is mirrored into the UI cache for later matching.
        assert app._cache[contact.cache_key][-1]["id"] == "ts-1"


@pytest.mark.integration
async def test_emoji_picker_opens(app_for_test):
    """Ctrl+E opens the EmojiPickerScreen modal."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()

        await pilot.press("ctrl+e")
        await pilot.pause()

        assert isinstance(app.screen, EmojiPickerScreen)


@pytest.mark.integration
async def test_chat_title_updates(app_for_test):
    """Selecting a contact updates #ChatTitle with the contact name."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()

        contact_list = app.query_one("#contact-list", ListView)
        await pilot.click(contact_list.children[0])
        await pilot.pause()

        chat_title = app.query_one("#ChatTitle", Label)
        assert chat_title.content == "📱 Chat - Mario"


@pytest.mark.integration
async def test_input_cleared_after_send(app_for_test):
    """After sending a message, #message-input is cleared."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()

        app.selected_contact = app.contacts[0]

        input_widget = app.query_one("#message-input", MessageTextArea)
        input_widget.focus()
        input_widget.text = "Ciao!"

        await pilot.press("enter")
        await pilot.pause()

        assert input_widget.text == ""


@pytest.mark.integration
async def test_message_text_area_paste_and_newline_shortcuts(app_for_test):
    """The message editor accepts multiline paste and explicit newline shortcuts."""
    async with app_for_test.run_test() as pilot:
        editor = app_for_test.query_one("#message-input", MessageTextArea)
        editor.focus()
        await editor._on_paste(events.Paste("one\r\ntwo\rthree"))
        assert editor.text == "one\ntwo\nthree"

        await pilot.press("ctrl+j")
        await pilot.press("shift+enter")
        assert editor.text == "one\ntwo\nthree\n\n"


@pytest.mark.integration
async def test_multiline_message_is_sent_and_normalized(app_for_test_with_mocks):
    """Enter sends multiline messages rather than inserting another newline."""
    app, signal_backend = app_for_test_with_mocks
    async with app.run_test() as pilot:
        app.selected_contact = app.contacts[0]
        editor = app.query_one("#message-input", MessageTextArea)
        editor.focus()
        editor.text = "first\r\nsecond\rthird"

        signal_backend.send_message_sync.return_value = "ts-1"

        with (
            patch.object(app, "run_worker", side_effect=lambda work, **kwargs: work()),
            patch.object(
                app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)
            ),
        ):
            await pilot.press("enter")

        assert (
            signal_backend.send_message_sync.call_args.args[1] == "first\nsecond\nthird"
        )
        assert editor.text == ""
        # Signal now propagates the real server id into the optimistic entry.
        assert app._cache[app.contacts[0].cache_key][-1]["id"] == "ts-1"


@pytest.mark.integration
async def test_emoji_completion_keeps_enter_priority_over_send(app_for_test_with_mocks):
    """Enter accepts an emoji completion before it submits the message."""
    app, signal_backend = app_for_test_with_mocks
    async with app.run_test() as pilot:
        app.selected_contact = app.contacts[0]
        editor = app.query_one("#message-input", MessageTextArea)
        editor.focus()
        editor.text = ":sm"
        editor.move_cursor((0, len(editor.text)))
        await pilot.press("i")
        await pilot.pause()

        completion = app.query_one("#emoji-completion", EmojiCompletionWidget)
        assert completion.has_class("-visible")
        await pilot.press("enter")

        assert editor.text == f"{get_emoji_suggestions('smi')[0][0]} "
        signal_backend.send_message_sync.assert_not_called()


@pytest.mark.integration
async def test_emoji_picker_close(app_for_test):
    """Escape dismisses the EmojiPickerScreen and returns to the main screen."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()

        await pilot.press("ctrl+e")
        await pilot.pause()
        assert isinstance(app.screen, EmojiPickerScreen)

        await pilot.press("escape")
        await pilot.pause()

        assert not isinstance(app.screen, EmojiPickerScreen)
        assert app.query_one("#contact-list") is not None


@pytest.mark.integration
async def test_empty_contact_list(app_for_test):
    """An empty contact list mounts without crashing and renders no rows."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()

        app.contacts = []
        app._render_contact_list([])
        await pilot.pause()

        contact_list = app.query_one("#contact-list", ListView)
        assert list(contact_list.children) == []
        assert app.query_one("#message-input") is not None


@pytest.mark.integration
async def test_message_render_in_chat_log(app_for_test):
    """Injected messages (in/out) are rendered in #chat-log."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()

        contact = app.contacts[0]
        app.selected_contact = contact
        app._cache[contact.cache_key] = [
            {
                "text": "Ciao Mario!",
                "is_mine": False,
                "sender": "Mario",
                "timestamp": 1000,
                "msg_type": "text",
                "status": "read",
            },
            {
                "text": "Ciao!",
                "is_mine": True,
                "sender": "You",
                "timestamp": 2000,
                "msg_type": "text",
                "status": "sent",
            },
        ]
        app._load_all_messages()
        await pilot.pause()

        chat_log = app.query_one("#chat-log", Vertical)
        messages = [w for w in chat_log.children if isinstance(w, MessageWidget)]
        assert [m._msg_text for m in messages] == ["Ciao Mario!", "Ciao!"]
        assert messages[0]._msg_is_mine is False
        assert messages[1]._msg_is_mine is True
        assert messages[1]._status == "sent"


@pytest.mark.integration
async def test_reply_bar_hidden_initially(app_for_test):
    """#reply-bar is hidden at startup, shows on reply, hides on cancel."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()

        reply_bar = app.query_one("#reply-bar")
        assert reply_bar.has_class("reply-bar-hidden")

        # Render one outgoing message so there is a clickable bubble.
        contact = app.contacts[0]
        app.selected_contact = contact
        app._cache[contact.cache_key] = [
            {
                "id": "telegram-message-42",
                "text": "Ciao",
                "is_mine": True,
                "sender": "You",
                "timestamp": 1000,
                "msg_type": "text",
            },
        ]
        app._load_all_messages()
        await pilot.pause()

        chat_log = app.query_one("#chat-log", Vertical)
        bubble = next(w for w in chat_log.children if isinstance(w, MessageWidget))
        await pilot.click(bubble)
        await pilot.pause()

        assert not reply_bar.has_class("reply-bar-hidden")
        assert app._reply_to is not None
        assert app._reply_to["message_id"] == "telegram-message-42"

        await pilot.click("#reply-cancel")
        await pilot.pause()

        assert reply_bar.has_class("reply-bar-hidden")
        assert app._reply_to is None
