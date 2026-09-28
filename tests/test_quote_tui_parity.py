"""Design v3 item (j) — parità TUI per la quote media.

Verifica che un media "openable" (voce/audio/video/documento) non emetta
``MessageClicked`` al click/Enter (apre il media), così la quote vocale non
entra nel flusso reply; e che il percorso di reply media Signal produca una
riga mirror con ``quote_text`` di display non vuoto.
"""

from __future__ import annotations

from types import SimpleNamespace

from protocols.signal import SignalBackend
from ui_components import MessageWidget


def _click(meta: bool = False):
    return SimpleNamespace(meta=meta)


def test_click_on_openable_voice_emits_media_open_not_message_clicked():
    widget = MessageWidget(
        text="[Voice]",
        timestamp=1,
        sender="Mario",
        is_mine=False,
        attachment_ref=("voice-att-1", "signal"),
    )
    posted = []
    widget.post_message = posted.append

    widget.on_click(_click())

    assert len(posted) == 1
    assert isinstance(posted[0], MessageWidget.MediaOpenRequested)
    assert not isinstance(posted[0], MessageWidget.MessageClicked)


def test_enter_on_openable_voice_emits_media_open_not_message_clicked():
    widget = MessageWidget(
        text="[Audio]",
        timestamp=1,
        sender="Mario",
        is_mine=False,
        attachment_ref=("audio-att-1", "signal"),
    )
    posted = []
    widget.post_message = posted.append

    widget.key_enter()

    assert len(posted) == 1
    assert isinstance(posted[0], MessageWidget.MediaOpenRequested)
    assert not isinstance(posted[0], MessageWidget.MessageClicked)


def test_click_on_text_message_still_emits_message_clicked():
    widget = MessageWidget(text="ciao", timestamp=1, sender="Mario", is_mine=False)
    posted = []
    widget.post_message = posted.append

    widget.on_click(_click())

    assert len(posted) == 1
    assert isinstance(posted[0], MessageWidget.MessageClicked)


def test_signal_enqueue_media_reply_builds_display_quote_text():
    """Fix A: il mirror Signal senza caption deriva il display dal descriptor."""
    backend = SignalBackend()
    backend.enqueue_sent_message(
        "42",
        "1787250931234",
        "answer",
        quote_message="",
        quote_timestamp=1787250930234,
        quote_author="42",
        quote_content_type="audio/ogg",
        quote_filename="voice.ogg",
    )
    event = backend.poll_once()[0]
    assert event.payload["quote_text"] == "voice.ogg — 🎵 Audio"


def test_signal_ingest_media_reply_row_keeps_display_quote_text():
    backend = SignalBackend()
    backend.ingest_message(
        "42",
        {
            "id": "1787250931234",
            "text": "answer",
            "is_mine": True,
            "sender": "You",
            "timestamp": 1787250931234,
            "quote_text": "voice.ogg — 🎵 Audio",
            "quote_timestamp": 1787250930234,
            "quote_author": "42",
            "quote_content_type": "audio/ogg",
            "msg_type": "text",
            "attachment_info": None,
            "attachment_id": None,
        },
        1787250931234,
        persist=False,
    )
    assert backend.cache["42"][0]["quote_text"] == "voice.ogg — 🎵 Audio"
