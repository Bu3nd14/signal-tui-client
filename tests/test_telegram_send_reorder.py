"""
Regression test — contratto desiderato: l'invio a un contatto Telegram deve
aggiornare SUBITO l'ultimo messaggio/timestamp e riordinare la lista chat.

Questo test fissa il contratto di regressione: ``on_input_submitted``
(``tui/send.py``) deve aggiornare subito ``ChatContact.last_message_ts`` e
riordinare la lista dopo il messaggio ottimistico, senza dipendere dall'echo del
backend (``ChatEvent`` → poll worker → ``_reorder_contact_list``).

Il test è volutamente SENZA echo / ChatEvent / poll e NON esegue il worker:
verifica lo stato SUBITO dopo ``on_input_submitted``.  Solo mock, nessun
servizio reale.
"""

from __future__ import annotations

import queue
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backends import BackendManager
from backends.telegram import TelegramBackend
from models import PROTOCOL_SIGNAL, PROTOCOL_TELEGRAM, ChatContact
from signal_tui import SignalTUI


def test_telegram_send_updates_last_ts_and_reorders_immediately():
    """📨 Subito dopo ``on_input_submitted`` (senza echo/poll e prima del worker)
    il contatto Telegram deve avere timestamp avanzato ed essere primo in lista."""
    import signal_tui as stui

    # ── App con manager fresco e backend Telegram (istanza __new__, no network) ──
    app = SignalTUI()
    app.manager = BackendManager()

    telegram = TelegramBackend.__new__(TelegramBackend)
    telegram.protocol = PROTOCOL_TELEGRAM
    telegram.contacts = []
    telegram.cache = {}
    telegram._events = queue.Queue()
    telegram._contacts_by_id = {}
    telegram._seen_msg_ids = set()
    app.manager.register(telegram)

    # ── Bruno (Signal) in cima, Anna (Telegram) più vecchia ──
    anna = ChatContact(id="111", display_name="Anna", protocol=PROTOCOL_TELEGRAM)
    anna.last_message_ts = 1000
    bruno = ChatContact(id="+2", display_name="Bruno", protocol=PROTOCOL_SIGNAL)
    bruno.last_message_ts = 2000
    app.contacts = [bruno, anna]
    app.selected_contact = anna

    # ── Neutralizza DOM/worker (worker SOLO schedulato, mai eseguito) ──
    app._is_completion_visible = MagicMock(return_value=False)
    app.query_one = MagicMock(side_effect=Exception("no DOM in test"))
    app._add_message = MagicMock()
    app._cancel_reply = MagicMock()
    app.run_worker = MagicMock()
    app._reply_to = None
    app._cache = {}

    event = MagicMock()
    event.value = "ciao"

    with (
        patch("backend._add_message_to_cache"),
        patch("backend._update_message_id"),
        patch.object(stui, "replace_emoji_aliases", side_effect=lambda x: x),
    ):
        app.on_input_submitted(event)

    # Sanity: il worker è stato solo schedulato (nessun echo, nessun poll).
    assert app.run_worker.call_count == 1

    # ── Contratto 1: timestamp dell'ultimo messaggio avanzato SUBITO ──
    sent_ts = app._cache[anna.cache_key][-1]["timestamp"]
    assert anna.last_message_ts == sent_ts, (
        f"atteso last_message_ts={sent_ts} subito dopo l'invio, "
        f"avuto {anna.last_message_ts}"
    )

    # ── Contratto 2: contatto Telegram primo in lista SUBITO ──
    assert app.contacts[0].id == "111", (
        f"atteso il contatto Telegram '111' in cima alla lista, "
        f"avuto {app.contacts[0].id!r} ({app.contacts[0].display_name!r})"
    )
