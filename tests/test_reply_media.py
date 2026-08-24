"""Integration tests (headless) for the media reply flow (bug #37).

These exercise the ``UnreadReplyMixin.on_image_widget_reply_requested`` handler
against a real ``SignalTUI`` mounted via ``App.run_test()``: populating
``_reply_to`` (including the wire-faithful ``quote_wire_body``), showing the
media placeholder in the reply bar, toggle/cancel semantics, reply↔edit mutual
exclusion, and download-mode behaviour.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from textual.containers import Vertical
from textual.widgets import Static

from models import PROTOCOL_SIGNAL, PROTOCOL_WHATSAPP, ChatContact
from ui_components import ImageWidget


def _reply_requested(**overrides) -> ImageWidget.ReplyRequested:
    """Build a ``ReplyRequested`` event with the default metadata of an image."""
    kw = {
        "text": "🖼️ Immagine",
        "caption": None,
        "timestamp": 1000,
        "sender": "Mario",
        "is_mine": False,
        "message_id": "sig-img-1",
        "attachment_id": "att-1",
        "content_type": "image/png",
    }
    kw.update(overrides)
    return ImageWidget.ReplyRequested(**kw)


def _mount_image(app) -> None:
    """Mount a single ``ImageWidget`` (timestamp=1000) into the chat log."""
    contact = app.contacts[0]
    app.selected_contact = contact
    app._add_message(
        text="",
        is_mine=False,
        quote_text=None,
        msg_type="image",
        attachment_info="photo.jpg",
        attachment_id="att-1",
        timestamp=1000,
        sender="Mario",
        status="read",
        protocol=contact.protocol,
        message_id="sig-img-1",
    )


def _mounted_image(app) -> ImageWidget:
    chat_log = app.query_one("#chat-log", Vertical)
    widgets = [w for w in chat_log.children if isinstance(w, ImageWidget)]
    assert len(widgets) == 1
    return widgets[0]


@pytest.mark.integration
async def test_handler_populates_reply_to_from_image(app_for_test):
    """Alt+click su immagine → ``_reply_to`` popolato con quote_wire_body."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()
        _mount_image(app)
        await pilot.pause()

        app.on_image_widget_reply_requested(_reply_requested())
        await pilot.pause()

        assert app._reply_to is not None
        assert app._reply_to["text"] == "🖼️ Immagine"
        assert app._reply_to["quote_wire_body"] is None
        assert app._reply_to["timestamp"] == 1000
        assert app._reply_to["sender"] == "Mario"
        assert app._reply_to["is_mine"] is False
        assert app._reply_to["message_id"] == "sig-img-1"
        assert app._reply_to["attachment_id"] == "att-1"
        assert app._reply_to["content_type"] == "image/png"
        assert "attachment_info" not in app._reply_to
        assert app._reply_to["_widget"] is _mounted_image(app)


@pytest.mark.integration
async def test_handler_populates_quote_wire_body_from_caption(app_for_test):
    """La caption reale finisce in ``quote_wire_body`` (body di filo)."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()
        _mount_image(app)
        await pilot.pause()

        app.on_image_widget_reply_requested(
            _reply_requested(text="Che bella!", caption="Che bella!")
        )
        await pilot.pause()

        assert app._reply_to["text"] == "Che bella!"
        assert app._reply_to["quote_wire_body"] == "Che bella!"


@pytest.mark.integration
async def test_reply_bar_shows_media_placeholder(app_for_test):
    """La reply-bar mostra "↩️ Replying to: 🖼️ Immagine"."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()
        _mount_image(app)
        await pilot.pause()

        app.on_image_widget_reply_requested(_reply_requested())
        await pilot.pause()

        reply_bar = app.query_one("#reply-bar")
        reply_text = app.query_one("#reply-text", Static)
        assert not reply_bar.has_class("reply-bar-hidden")
        assert reply_text.content == "↩️ Replying to: 🖼️ Immagine"


@pytest.mark.integration
async def test_second_click_cancels_media_reply(app_for_test):
    """Secondo Alt+click sulla stessa immagine → annulla la reply."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()
        _mount_image(app)
        await pilot.pause()

        app.on_image_widget_reply_requested(_reply_requested())
        await pilot.pause()
        assert app._reply_to is not None

        app.on_image_widget_reply_requested(_reply_requested())
        await pilot.pause()

        assert app._reply_to is None
        reply_bar = app.query_one("#reply-bar")
        assert reply_bar.has_class("reply-bar-hidden")


@pytest.mark.integration
async def test_media_reply_cancels_active_edit(app_for_test):
    """Una reply media cancella un edit attivo (mutua esclusione reply↔edit)."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()
        _mount_image(app)
        await pilot.pause()
        app._editing_message = {"old_text": "vecchio", "_widget": None}

        app.on_image_widget_reply_requested(_reply_requested())
        await pilot.pause()

        assert app._editing_message is None
        assert app._reply_to is not None
        assert app._reply_to["text"] == "🖼️ Immagine"


@pytest.mark.integration
async def test_download_mode_image_serves_file_instead_of_reply(app_for_test):
    """In download mode, Alt+click serve il file invece di impostare la reply."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()
        _mount_image(app)
        await pilot.pause()
        app._download_mode = True

        with patch.object(app, "_start_download") as start_download:
            app.on_image_widget_reply_requested(_reply_requested())
            await pilot.pause()

        assert app._reply_to is None
        start_download.assert_called_once_with(
            text="🖼️ Immagine",
            attachment_id="att-1",
            timestamp=1000,
            protocol=PROTOCOL_SIGNAL,
        )


@pytest.mark.integration
async def test_download_mode_passes_protocol_for_non_signal(app_for_test):
    """In download mode il protocol del contatto selezionato viene propagato."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()
        _mount_image(app)
        await pilot.pause()
        app.selected_contact = ChatContact(
            id="391234567890@c.us",
            display_name="Pix",
            protocol=PROTOCOL_WHATSAPP,
        )
        app._download_mode = True

        with patch.object(app, "_start_download") as start_download:
            app.on_image_widget_reply_requested(_reply_requested())
            await pilot.pause()

        assert app._reply_to is None
        start_download.assert_called_once_with(
            text="🖼️ Immagine",
            attachment_id="att-1",
            timestamp=1000,
            protocol=PROTOCOL_WHATSAPP,
        )
