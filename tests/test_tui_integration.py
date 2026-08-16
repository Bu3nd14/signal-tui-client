"""Integration tests that launch the real TUI headless via ``App.run_test()``.

These tests are slower than the unit tests (Textual mounts the full widget
tree) and are marked ``integration`` so they can be excluded from fast runs
with ``-m "not integration"``.
"""

from unittest.mock import patch

import pytest
from textual.widgets import Input, ListView, Static

from emoji_picker import EmojiPickerScreen


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

        input_widget = app.query_one("#message-input", Input)
        input_widget.focus()
        input_widget.value = "Ciao!"

        # Run the send worker synchronously so the backend call is deterministic.
        with patch.object(app, "run_worker", side_effect=lambda work, **kwargs: work()):
            await pilot.press("enter")

        await pilot.pause()

        contact = app.contacts[0]
        signal_backend.ingest_message.assert_called_once()
        assert signal_backend.ingest_message.call_args.args[0] == contact.id
        assert signal_backend.ingest_message.call_args.args[1]["text"] == "Ciao!"

        signal_backend.send_message_sync.assert_called_once_with(
            contact.id,
            "Ciao!",
            quote_timestamp=None,
            quote_author=None,
            quote_message=None,
        )


@pytest.mark.integration
async def test_emoji_picker_opens(app_for_test):
    """Ctrl+E opens the EmojiPickerScreen modal."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()

        await pilot.press("ctrl+e")
        await pilot.pause()

        assert isinstance(app.screen, EmojiPickerScreen)
