"""
Fase 6 — Flusso UI di editing messaggi (integration, headless).

Copre il comportamento end-to-end del mixin ``EditMessageMixin`` e dei suoi
punti di contatto (``MessageWidget.EditRequested``, hook submit in ``tui/send.py``,
dispatch ``message_edit`` in ``tui/events.py``, barra ``#reply-bar`` in
``tui/unread_reply.py``).

Approccio per gli eventi ``message_edit`` in arrivo: le fixture mockano
``BackendManager``/``SignalBackend`` (vedi ``tests/conftest.py``).  Qui
configuriamo il mock ``signal_backend.apply_edit`` con un ``side_effect`` che
simula la mutazione backend (ritorno del dict ``info`` atteso dal contratto),
così il path UI ``_handle_edit_event`` è esercitato in modo deterministico e
senza I/O reale.  La persistenza SQLite reale di ``apply_edit`` è già coperta
da ``tests/test_edit_signal.py`` e ``tests/test_db_edit.py``: qui isoliamo il
solo strato UI.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from textual.containers import Vertical
from textual.widgets import Static

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from models import PROTOCOL_SIGNAL, ChatEvent
from ui_components import MessageTextArea, MessageWidget

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _own_message(**overrides) -> dict:
    """Un messaggio proprio di testo pre-seed (identità Signal: ts=1000)."""
    msg = {
        "id": "sig-1000",
        "text": "vecchio",
        "is_mine": True,
        "sender": "You",
        "timestamp": 1000,
        "msg_type": "text",
        "status": "sent",
        "read": True,
    }
    msg.update(overrides)
    return msg


def _edit_requested(**overrides):
    """Costruisce un ``EditRequested`` con i default del messaggio ``_own_message``."""
    kw = {
        "text": "vecchio",
        "timestamp": 1000,
        "sender": "You",
        "is_mine": True,
        "status": "sent",
        "message_id": "sig-1000",
    }
    kw.update(overrides)
    return MessageWidget.EditRequested(**kw)


def _select_and_render(app, contact, messages):
    """Seleziona il contatto, seeda la cache UI e monta la chat."""
    app.selected_contact = contact
    app._cache[contact.cache_key] = messages
    app._load_all_messages()


def _only_message_widget(app) -> MessageWidget:
    chat_log = app.query_one("#chat-log", Vertical)
    widgets = [w for w in chat_log.children if isinstance(w, MessageWidget)]
    assert len(widgets) == 1
    return widgets[0]


def _fake_apply_edit(
    contact_id, message_id, new_text, *, is_mine=None, edit_timestamp=None
):
    """Simula ``backend.apply_edit`` (mutazione + info) per il path UI."""
    return {
        "message_id": message_id,
        "timestamp": 1000,
        "old_text": "vecchio",
        "text": new_text,
        "is_mine": is_mine if is_mine is not None else False,
    }


# ─── Gate: richieste rifiutate ────────────────────────────────────────────────


@pytest.mark.integration
async def test_edit_gate_not_mine_rejected(app_for_test):
    """Edit su messaggio non proprio → rifiutato, stato invariato, status settato."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()

        app.on_message_widget_edit_requested(_edit_requested(is_mine=False))
        await pilot.pause()

        assert app._editing_message is None
        status_bar = app.query_one("#status-text", Static)
        assert status_bar.content == "❌ You can only edit your own messages"


@pytest.mark.integration
async def test_edit_gate_pending_rejected(app_for_test):
    """Edit su messaggio pending/failed → rifiutato."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()

        app.on_message_widget_edit_requested(_edit_requested(status="pending"))
        await pilot.pause()

        assert app._editing_message is None
        status_bar = app.query_one("#status-text", Static)
        assert status_bar.content == "❌ Message not sent yet — cannot edit"


@pytest.mark.integration
async def test_edit_gate_media_rejected(app_for_test):
    """Edit su messaggio media (msg_type != text) → rifiutato."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()
        contact = app.contacts[0]
        app.selected_contact = contact
        app._cache[contact.cache_key] = [_own_message(msg_type="image", text="[Image]")]

        app.on_message_widget_edit_requested(_edit_requested(text="[Image]"))
        await pilot.pause()

        assert app._editing_message is None
        status_bar = app.query_one("#status-text", Static)
        assert status_bar.content == "❌ Only text messages can be edited"


# ─── Apertura edit ────────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_edit_request_populates_state(app_for_test):
    """EditRequested su proprio testo → stato popolato, barra ✏️, input pre-caricato."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()
        contact = app.contacts[0]
        _select_and_render(app, contact, [_own_message()])
        await pilot.pause()

        app.on_message_widget_edit_requested(_edit_requested())
        await pilot.pause()

        editing = app._editing_message
        assert editing is not None
        assert editing["protocol"] == PROTOCOL_SIGNAL
        assert editing["contact_id"] == contact.id
        assert editing["cache_key"] == contact.cache_key
        assert editing["timestamp"] == 1000
        # Per Signal l'id reale del server ha precedenza (id-first).
        assert editing["message_id"] == "sig-1000"
        assert editing["old_text"] == "vecchio"

        reply_bar = app.query_one("#reply-bar")
        assert not reply_bar.has_class("reply-bar-hidden")
        reply_text = app.query_one("#reply-text", Static)
        assert "✏️" in reply_text.content
        assert "vecchio" in reply_text.content

        ta = app.query_one("#message-input", MessageTextArea)
        assert ta.text == "vecchio"
        assert ta.has_focus


# ─── Submit in modalità editing ───────────────────────────────────────────────


@pytest.mark.integration
async def test_submit_edit_updates_cache_widget_and_backend(app_for_test_with_mocks):
    """Submit ottimistico: cache, identità, widget, backend, input e barra."""
    app, signal_backend = app_for_test_with_mocks
    async with app.run_test() as pilot:
        await pilot.pause()
        contact = app.contacts[0]
        ck = contact.cache_key
        _select_and_render(app, contact, [_own_message()])
        await pilot.pause()

        app.on_message_widget_edit_requested(_edit_requested())
        await pilot.pause()

        ta = app.query_one("#message-input", MessageTextArea)
        ta.text = "nuovo"
        with (
            patch.object(app, "run_worker", side_effect=lambda work, **kw: work()),
            patch.object(
                app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)
            ),
        ):
            await pilot.press("enter")
        await pilot.pause()

        # Cache UI aggiornata.
        entry = app._cache[ck][0]
        assert entry["text"] == "nuovo"
        assert entry["edited"] is True

        # Identità: la nuova è presente, la vecchia assente.
        assert (PROTOCOL_SIGNAL, ck, 1000, "nuovo") in app._seen_message_ids
        assert (PROTOCOL_SIGNAL, ck, 1000, "vecchio") not in app._seen_message_ids
        assert (PROTOCOL_SIGNAL, ck, 1000, "nuovo") in app._shown_in_log
        assert (PROTOCOL_SIGNAL, ck, 1000, "vecchio") not in app._shown_in_log

        # Widget aggiornato in place.
        widget = _only_message_widget(app)
        assert widget._msg_text == "nuovo"
        assert widget._edited is True

        # Backend chiamato con (contact_id, message_id, new_text).
        signal_backend.edit_message_sync.assert_called_once_with(
            contact.id, "sig-1000", "nuovo"
        )

        # Input svuotato e barra nascosta.
        assert ta.text == ""
        reply_bar = app.query_one("#reply-bar")
        assert reply_bar.has_class("reply-bar-hidden")
        assert app._editing_message is None


@pytest.mark.integration
@pytest.mark.parametrize("failure", ["exception", "false"])
async def test_submit_edit_failure_rolls_back(app_for_test_with_mocks, failure):
    """Fallimento backend → rollback completo (cache + widget + identità) + status."""
    app, signal_backend = app_for_test_with_mocks
    if failure == "exception":
        signal_backend.edit_message_sync = MagicMock(side_effect=RuntimeError("boom"))
        expected_error = "boom"
    else:
        signal_backend.edit_message_sync = MagicMock(return_value=False)
        expected_error = "edit rejected by server"

    async with app.run_test() as pilot:
        await pilot.pause()
        contact = app.contacts[0]
        ck = contact.cache_key
        _select_and_render(app, contact, [_own_message()])
        await pilot.pause()

        app.on_message_widget_edit_requested(_edit_requested())
        await pilot.pause()

        ta = app.query_one("#message-input", MessageTextArea)
        ta.text = "nuovo"
        with (
            patch.object(app, "run_worker", side_effect=lambda work, **kw: work()),
            patch.object(
                app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)
            ),
        ):
            await pilot.press("enter")
        await pilot.pause()

        # Testo originale ovunque.
        entry = app._cache[ck][0]
        assert entry["text"] == "vecchio"
        assert entry["edited"] is False

        widget = _only_message_widget(app)
        assert widget._msg_text == "vecchio"
        assert widget._edited is False

        # Identità ripristinata.
        assert (PROTOCOL_SIGNAL, ck, 1000, "vecchio") in app._seen_message_ids
        assert (PROTOCOL_SIGNAL, ck, 1000, "nuovo") not in app._seen_message_ids

        status_bar = app.query_one("#status-text", Static)
        assert status_bar.content == f"❌ Edit failed: {expected_error}"


# ─── Eventi message_edit in arrivo ────────────────────────────────────────────


@pytest.mark.integration
async def test_incoming_edit_chat_open_updates_widget_no_new_bubble(
    app_for_test_with_mocks,
):
    """Edit in arrivo con chat aperta → widget aggiornato, nessuna bolla nuova."""
    app, signal_backend = app_for_test_with_mocks
    async with app.run_test() as pilot:
        await pilot.pause()
        contact = app.contacts[0]
        ck = contact.cache_key
        _select_and_render(
            app,
            contact,
            [
                {
                    "id": "sig-1000",
                    "text": "vecchio",
                    "is_mine": False,
                    "sender": "Mario",
                    "timestamp": 1000,
                    "msg_type": "text",
                    "status": "read",
                    "read": False,
                }
            ],
        )
        await pilot.pause()

        chat_log = app.query_one("#chat-log", Vertical)
        n_before = len(chat_log.children)
        app._unread_counts[ck] = 3  # pre-esistente: l'edit non deve toccarlo

        signal_backend.apply_edit = MagicMock(side_effect=_fake_apply_edit)

        event = ChatEvent(
            type="message_edit",
            protocol=PROTOCOL_SIGNAL,
            contact_id=contact.id,
            payload={
                "edit_message_id": "sig-1000",
                "text": "nuovo",
                "timestamp": 1000,
                "edit_timestamp": None,
                "is_mine": False,
                "sender": "Mario",
                "contact": contact,
                "msg_type": "text",
            },
        )

        with patch.object(
            app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)
        ):
            handled = app._handle_event(event)
        await pilot.pause()

        assert handled is True
        widget = _only_message_widget(app)
        assert widget._msg_text == "nuovo"
        assert widget._edited is True

        # Nessuna bolla nuova: il numero di figli è invariato.
        assert len(chat_log.children) == n_before
        # Nessun bump unread.
        assert app._unread_counts[ck] == 3
        # Cache UI specchiata.
        assert app._cache[ck][0]["text"] == "nuovo"
        assert app._cache[ck][0]["edited"] is True


@pytest.mark.integration
async def test_incoming_edit_chat_closed_updates_cache_only(
    app_for_test_with_mocks,
):
    """Edit in arrivo con altra chat aperta → cache aggiornata, nessun widget toccato."""
    app, signal_backend = app_for_test_with_mocks
    async with app.run_test() as pilot:
        await pilot.pause()
        contact_a = app.contacts[0]
        contact_b = app.contacts[1]
        app.selected_contact = contact_b
        ck_a = contact_a.cache_key
        app._cache[ck_a] = [
            {
                "id": "sig-1000",
                "text": "vecchio",
                "is_mine": False,
                "sender": "Mario",
                "timestamp": 1000,
                "msg_type": "text",
                "status": "read",
                "read": False,
            }
        ]
        await pilot.pause()

        chat_log = app.query_one("#chat-log", Vertical)
        n_before = len(chat_log.children)

        signal_backend.apply_edit = MagicMock(side_effect=_fake_apply_edit)

        event = ChatEvent(
            type="message_edit",
            protocol=PROTOCOL_SIGNAL,
            contact_id=contact_a.id,
            payload={
                "edit_message_id": "sig-1000",
                "text": "nuovo",
                "timestamp": 1000,
                "edit_timestamp": None,
                "is_mine": False,
                "sender": "Mario",
                "contact": contact_a,
                "msg_type": "text",
            },
        )

        with patch.object(
            app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)
        ):
            handled = app._handle_event(event)
        await pilot.pause()

        assert handled is True
        # Cache UI del contatto aggiornata.
        assert app._cache[ck_a][0]["text"] == "nuovo"
        assert app._cache[ck_a][0]["edited"] is True
        # Il backend è stato invocato con i parametri giusti (persistenza).
        signal_backend.apply_edit.assert_called_once_with(
            contact_a.id, "sig-1000", "nuovo", is_mine=False, edit_timestamp=None
        )
        # Nessun widget montato/toccato nella chat aperta.
        assert len(chat_log.children) == n_before


# ─── Mutua esclusione reply ↔ edit ────────────────────────────────────────────


@pytest.mark.integration
async def test_opening_edit_cancels_reply(app_for_test):
    """Aprire l'edit cancella un reply attivo."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()
        contact = app.contacts[0]
        _select_and_render(app, contact, [_own_message()])
        await pilot.pause()

        widget = _only_message_widget(app)

        # Apri un reply via click.
        await pilot.click(widget)
        await pilot.pause()
        assert app._reply_to is not None

        # Apri l'edit → il reply deve essere cancellato.
        app.on_message_widget_edit_requested(_edit_requested())
        await pilot.pause()

        assert app._editing_message is not None
        assert app._reply_to is None


@pytest.mark.integration
async def test_opening_reply_cancels_edit(app_for_test):
    """Aprire un reply (click) deve cancellare un edit attivo (mutua esclusione)."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()
        contact = app.contacts[0]
        _select_and_render(app, contact, [_own_message()])
        await pilot.pause()

        widget = _only_message_widget(app)

        # Apri l'edit.
        app.on_message_widget_edit_requested(_edit_requested())
        await pilot.pause()
        assert app._editing_message is not None

        # Apri un reply → deve cancellare l'edit.
        await pilot.click(widget)
        await pilot.pause()

        assert app._reply_to is not None
        assert app._editing_message is None


# ─── No duplicati dopo _refresh_chat ──────────────────────────────────────────


@pytest.mark.integration
async def test_refresh_chat_after_edit_no_duplicates(app_for_test):
    """Dopo un edit, ``_refresh_chat()`` non rimonta nulla di nuovo."""
    app = app_for_test
    async with app.run_test() as pilot:
        await pilot.pause()
        contact = app.contacts[0]
        _select_and_render(app, contact, [_own_message()])
        await pilot.pause()

        app.on_message_widget_edit_requested(_edit_requested())
        await pilot.pause()

        with (
            patch.object(app, "run_worker", side_effect=lambda work, **kw: work()),
            patch.object(
                app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)
            ),
        ):
            app._submit_edit("nuovo")
        await pilot.pause()

        chat_log = app.query_one("#chat-log", Vertical)
        n_before = len(chat_log.children)

        app._refresh_chat()
        await pilot.pause()

        assert len(chat_log.children) == n_before
        widget = _only_message_widget(app)
        assert widget._msg_text == "nuovo"


# ─── Doppio edit consecutivo ──────────────────────────────────────────────────


@pytest.mark.integration
async def test_double_edit_consecutive(app_for_test_with_mocks):
    """Due edit consecutivi sullo stesso messaggio funzionano."""
    app, signal_backend = app_for_test_with_mocks
    async with app.run_test() as pilot:
        await pilot.pause()
        contact = app.contacts[0]
        ck = contact.cache_key
        _select_and_render(app, contact, [_own_message()])
        await pilot.pause()

        # Primo edit.
        app.on_message_widget_edit_requested(_edit_requested(text="vecchio"))
        await pilot.pause()
        with (
            patch.object(app, "run_worker", side_effect=lambda work, **kw: work()),
            patch.object(
                app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)
            ),
        ):
            app._submit_edit("nuovo1")
        await pilot.pause()

        # Secondo edit.
        app.on_message_widget_edit_requested(_edit_requested(text="nuovo1"))
        await pilot.pause()
        with (
            patch.object(app, "run_worker", side_effect=lambda work, **kw: work()),
            patch.object(
                app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)
            ),
        ):
            app._submit_edit("nuovo2")
        await pilot.pause()

        entry = app._cache[ck][0]
        assert entry["text"] == "nuovo2"
        assert entry["edited"] is True

        widget = _only_message_widget(app)
        assert widget._msg_text == "nuovo2"

        assert signal_backend.edit_message_sync.call_args_list == [
            call(contact.id, "sig-1000", "nuovo1"),
            call(contact.id, "sig-1000", "nuovo2"),
        ]
