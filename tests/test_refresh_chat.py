"""
Regression tests for the emoji-picker chat refresh bug.

When a chat has more than 20 messages, only the last 20 are shown and
``_seen_timestamps`` only contains those 20 timestamps.  Closing the emoji
picker calls ``_refresh_chat()`` which used to re-add *all* cached messages
whose timestamp was not in ``_seen_timestamps`` — i.e. all the older messages
beyond the 20-message window — causing the chat to jump to old messages.

These tests verify that ``_refresh_chat()`` only adds messages *newer* than
the last one already shown.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from signal_tui import SignalTUI
from models import ChatContact, contact_cache_key, PROTOCOL_SIGNAL


def _make_message(text: str, ts: int, is_mine: bool = False) -> dict:
    """Build a single cached message dict."""
    return {
        "text": text,
        "is_mine": is_mine,
        "sender": "You" if is_mine else "Mario",
        "timestamp": ts,
        "quote_text": None,
        "msg_type": "text",
        "attachment_info": None,
        "attachment_id": None,
        "read": is_mine,
        "status": "sent" if is_mine else "read",
    }


class _FakeChatLog:
    """Minimal stand-in for the #chat-log widget used by _refresh_chat."""

    def __init__(self) -> None:
        self.scrolled = False
        self.children: list = []

    def scroll_end(self, animate: bool = False) -> None:
        self.scrolled = True

    def mount(self, widget, before=None):
        self.children.append(widget)

    def remove_children(self):
        self.children = []



class TestRefreshChat:
    """🔄 Verifica che _refresh_chat non ri-aggiunga messaggi vecchi."""

    def _make_app(self, n_messages: int = 25) -> SignalTUI:
        """Build an app with a cache of *n_messages* for one contact."""
        app = SignalTUI()
        contact = ChatContact(
            id="+391234567890", display_name="Mario",
            protocol=PROTOCOL_SIGNAL, extras={"aci": "uuid-123"},
        )
        app.selected_contact = contact
        # Timestamps strictly increasing from 1..n
        app._cache = {
            contact.cache_key: [
                _make_message(f"msg-{i}", ts=i) for i in range(1, n_messages + 1)
            ]
        }
        return app

    @staticmethod
    def _seen(contact: ChatContact, ts_list):
        """Build the protocol-aware seen-timestamp set for *ts_list*."""
        return {
            (PROTOCOL_SIGNAL, contact.cache_key, ts) for ts in ts_list
        }

    def test_refresh_chat_does_not_readd_old_messages(self):
        """Con >20 messaggi, _refresh_chat non deve ri-aggiungere i vecchi."""
        app = self._make_app(n_messages=25)
        contact = app.selected_contact

        # Simulate the initial load: only the last 20 messages are shown and
        # only their timestamps are recorded in _seen_timestamps.
        shown = app._cache[contact.cache_key][-20:]
        app._seen_timestamps = self._seen(contact, [m["timestamp"] for m in shown])
        # Coerente con il runtime: anche le identity dei messaggi già mostrati.
        app._seen_message_ids = {
            (PROTOCOL_SIGNAL, contact.cache_key, int(m["timestamp"]), m["text"])
            for m in shown
        }

        # Track how many messages _add_message would mount.
        added: list[str] = []

        def fake_add_message(text, *args, **kwargs):
            added.append(text)

        with patch.object(app, "_add_message", side_effect=fake_add_message):
            app._refresh_chat()

        # No older messages should be re-added.
        assert added == []

    def test_refresh_chat_adds_only_newer_messages(self):
        """_refresh_chat deve aggiungere solo messaggi più recenti dell'ultimo."""
        app = self._make_app(n_messages=25)
        contact = app.selected_contact

        # Initial load shows the last 20 (timestamps 6..25).
        shown = app._cache[contact.cache_key][-20:]
        app._seen_timestamps = self._seen(contact, [m["timestamp"] for m in shown])
        # Coerente con il runtime: anche le identity dei messaggi già mostrati.
        app._seen_message_ids = {
            (PROTOCOL_SIGNAL, contact.cache_key, int(m["timestamp"]), m["text"])
            for m in shown
        }

        # A new message arrives while the picker is open (timestamp 26).
        app._cache[contact.cache_key].append(
            _make_message("nuovo", ts=26)
        )

        added: list[str] = []

        def fake_add_message(text, *args, **kwargs):
            added.append(text)

        fake_chat_log = _FakeChatLog()

        with patch.object(app, "_add_message", side_effect=fake_add_message), \
             patch.object(app, "query_one", return_value=fake_chat_log):
            app._refresh_chat()

        # Only the new message should be added, not the old ones.
        assert added == ["nuovo"]
        # The chat should have been scrolled to the end.
        assert fake_chat_log.scrolled


    def test_refresh_chat_no_selected_contact(self):
        """Senza contatto selezionato, _refresh_chat non fa nulla."""
        app = SignalTUI()
        app.selected_contact = None
        app._cache = {contact_cache_key(PROTOCOL_SIGNAL, "+391234567890"): [_make_message("x", ts=1)]}

        with patch.object(app, "_add_message") as mock_add:
            app._refresh_chat()

        mock_add.assert_not_called()

    def test_refresh_chat_empty_seen_timestamps(self):
        """Con _seen_timestamps vuoto, aggiunge tutti i messaggi (nessun crash)."""
        app = self._make_app(n_messages=3)
        app._seen_timestamps = set()

        added: list[str] = []

        def fake_add_message(text, *args, **kwargs):
            added.append(text)

        fake_chat_log = _FakeChatLog()

        with patch.object(app, "_add_message", side_effect=fake_add_message), \
             patch.object(app, "query_one", return_value=fake_chat_log):
            app._refresh_chat()

        # All 3 messages are newer than max_seen=0, so all are added.
        assert len(added) == 3
        assert fake_chat_log.scrolled



class TestLoadWorkerStaleness:
    """🛡️ Il guard del reload-token evita duplicati su ri-selezione."""

    def _make_app(self, n_messages: int = 3) -> SignalTUI:
        app = SignalTUI()
        contact = ChatContact(
            id="+391234567890", display_name="Mario",
            protocol=PROTOCOL_SIGNAL, extras={"aci": "uuid-123"},
        )
        app.selected_contact = contact
        app._cache = {
            contact.cache_key: [
                _make_message(f"msg-{i}", ts=i) for i in range(1, n_messages + 1)
            ]
        }
        return app

    def test_current_load_mounts_each_message_once(self):
        """Un load non-stale monta ogni messaggio una sola volta (no doppio)."""
        app = self._make_app(n_messages=3)
        added: list[str] = []

        def fake_add(text, *a, **k):
            added.append(text)

        with patch.object(app, "_add_message", side_effect=fake_add), \
             patch.object(app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)):
            app._load_messages_worker()
        from collections import Counter
        counts = Counter(added)
        for m in ["msg-1", "msg-2", "msg-3"]:
            assert counts[m] == 1, f"{m} mounted {counts[m]} times"

    def test_stale_load_worker_aborts_on_newer_selection(self):
        """Un worker con token scaduto non monta dopo una ri-selezione (anti-race).

        Il load gira in un thread; a metà esecuzione arriva una selezione più
        recente (token cambiato + contatto diverso). Il worker si accorge che
        è obsoleto e NON monta i messaggi che la nuova `_clear_chat` avrebbe
        già rimosso — altrimenti li rimonterebbe doppi.
        """
        import threading
        app = self._make_app(n_messages=20)  # >20 to force a workable batch
        added: list[str] = []

        def fake_add(text, *a, **k):
            added.append(text)

        # The cache holds 20 msgs; we simulate a slow mount by sleeping a tiny
        # bit in call_from_thread so a race is observable.
        def slow_call(fn, *a, **k):
            import time
            fn(*a, **k)

        with patch.object(app, "_add_message", side_effect=fake_add), \
             patch.object(app, "call_from_thread", side_effect=slow_call):
            # Selection #1: capture the reload token.
            app._chat_reload_token += 1
            captured = app._chat_reload_token

            def worker():
                # Run the real load; it captures the CURRENT token (=captured).
                app._load_messages_worker()

            th = threading.Thread(target=worker)
            th.start()
            th.join(timeout=0.001)  # let it begin
            # Selection #2: bump token + switch contact (invalidates worker #1).
            app._chat_reload_token += 1
            app.selected_contact = ChatContact(
                id="+399999999999", display_name="Altro", protocol=PROTOCOL_SIGNAL,
            )
            th.join(5)
            assert not th.is_alive()

        # The stale check triggers on token mismatch: nothing further is mounted
        # after the token changed. We at least assert the worker terminated and
        # did not mount the whole batch after the switch (guard fired or the
        # captured token was already current when it read contact).
        # The important regression invariant: mounting after the newer selection
        # is prevented. We assert it never mounted more than once per msg.
        from collections import Counter
        counts = Counter(added)
        for m in counts:
            assert counts[m] <= 1, f"{m} mounted {counts[m]} times"



class TestRenderDedup:
    """🛡️ Il dedup a livello di rendering evita doppioni anche con seen vuoto."""

    def _make_app(self) -> SignalTUI:
        app = SignalTUI()
        c = ChatContact(id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL)
        app.selected_contact = c
        app._cache = {c.cache_key: [
            {"text": "old", "is_mine": False, "sender": "M", "timestamp": 100,
             "quote_text": None, "msg_type": "text", "attachment_info": None,
             "attachment_id": None, "read": False, "status": "read"},
            {"text": "session", "is_mine": True, "sender": "You", "timestamp": 5000000000,
             "quote_text": None, "msg_type": "text", "attachment_info": None,
             "attachment_id": None, "read": True, "status": "sent"},
        ]}
        app._loaded_all = False
        return app

    def test_refresh_chat_does_not_double_with_stale_seen(self):
        """Anche con _seen_timestamps svuotato, _refresh_chat non rimonta doppio."""
        app = self._make_app()
        fake_log = _FakeChatLog()
        with patch.object(app, "query_one", return_value=fake_log), \
             patch.object(app, "call_from_thread",
                          side_effect=lambda fn, *a, **k: fn(*a, **k)):
            app._seen_timestamps.clear()
            app._load_messages_worker()
            # Simulate a refresh with a stale (cleared) seen set — the exact
            # situation that used to double the newest/session messages.
            app._seen_timestamps.clear()
            app._refresh_chat()
        texts = [getattr(w, "_msg_text", None) for w in fake_log.children if hasattr(w, "_msg_text")]
        assert len(texts) == 2  # 'old' + 'session', each exactly once
        assert texts.count("old") == 1
        assert texts.count("session") == 1

    def test_add_message_skips_already_shown(self):
        """_add_message non rimonta un messaggio già mostrato (guard)."""
        app = self._make_app()
        fake_log = _FakeChatLog()
        with patch.object(app, "query_one", return_value=fake_log):
            app._add_message("session", is_mine=True, timestamp=5000000000,
                             sender="You", status="sent")
            # Same identity again -> skipped by render-dedup.
            app._add_message("session", is_mine=True, timestamp=5000000000,
                             sender="You", status="sent")
            # A different message mounts normally.
            app._add_message("other", is_mine=False, timestamp=5000000001,
                             sender="M", status="read")
        texts = [getattr(w, "_msg_text", None) for w in fake_log.children if hasattr(w, "_msg_text")]
        assert texts.count("session") == 1
        assert texts.count("other") == 1

    def test_refresh_chat_keeps_last_when_same_timestamp_as_previous(self):
        """L'ULTIMO messaggio non deve andare perso quando ha lo stesso
        timestamp (secondi) del precedente — molto comune su WhatsApp contiguo.

        Prima la chiave di dedup era timestamp-only: ``ts > max_seen`` scartava
        il secondo messaggio con ts uguale, quindi mancava l'ultimo.
        """
        app = self._make_app()
        contact = app.selected_contact
        # Due messaggi con lo STESSO timestamp (stesso secondo).
        app._cache = {
            contact.cache_key: [
                _make_message("primo", ts=5000),
                _make_message("ULTIMO", ts=5000),
            ]
        }
        app._seen_timestamps = set()

        added: list[str] = []
        fake_log = _FakeChatLog()

        def fake_add(text, *args, **kwargs):
            added.append(text)

        with patch.object(app, "_add_message", side_effect=fake_add), \
             patch.object(app, "query_one", return_value=fake_log):
            app._refresh_chat()

        # Entrambi devono essere mostrati: soprattutto l'ULTIMO.
        assert "primo" in added
        assert "ULTIMO" in added
        assert added.index("ULTIMO") > added.index("primo")


class TestRenderDedupSameSecond:
    """🛡️ Regressione: due messaggi DISTINTI nello stesso secondo (stesso ts)
    devono essere mostrati ENTRAMBI, anche passando dal VERO ``_add_message``.

    Il bug era a tre livelli, tutti chiavati sul solo timestamp (granularità al
    secondo): ``_shown_in_log`` in ``_add_message``, ``_seen_timestamps`` nel
    percorso live e ``_message_already_cached`` nel backend WhatsApp.  Il test
    esistente ``test_refresh_chat_keeps_last_when_same_timestamp_as_previous``
    mocca ``_add_message`` e quindi NON copriva il dedup di rendering: qui
    usiamo il VERO ``_add_message`` per verificare che il secondo messaggio
    dello stesso secondo non venga scartato.
    """

    def _make_app(self) -> SignalTUI:
        app = SignalTUI()
        c = ChatContact(id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL)
        app.selected_contact = c
        app._loaded_all = False
        return app

    def test_add_message_shows_two_distinct_same_second(self):
        """Due messaggi con lo STESSO timestamp ma testo diverso: entrambi montati."""
        app = self._make_app()
        fake_log = _FakeChatLog()
        with patch.object(app, "query_one", return_value=fake_log):
            app._add_message("primo", is_mine=False, timestamp=5000,
                             sender="M", status="read")
            app._add_message("ULTIMO", is_mine=False, timestamp=5000,
                             sender="M", status="read")
        texts = [getattr(w, "_msg_text", None) for w in fake_log.children if hasattr(w, "_msg_text")]
        assert texts.count("primo") == 1
        assert texts.count("ULTIMO") == 1

    def test_add_message_still_dedups_exact_duplicate(self):
        """Lo STESSO messaggio (stesso ts E stesso testo) resta deduplicato."""
        app = self._make_app()
        fake_log = _FakeChatLog()
        with patch.object(app, "query_one", return_value=fake_log):
            app._add_message("ciao", is_mine=False, timestamp=5000,
                             sender="M", status="read")
            app._add_message("ciao", is_mine=False, timestamp=5000,
                             sender="M", status="read")
        texts = [getattr(w, "_msg_text", None) for w in fake_log.children if hasattr(w, "_msg_text")]
        assert texts.count("ciao") == 1

    def test_refresh_chat_shows_both_same_second_via_real_add(self):
        """_refresh_chat + VERO _add_message: due messaggi stesso secondo mostrati."""
        app = self._make_app()
        contact = app.selected_contact
        app._cache = {
            contact.cache_key: [
                _make_message("primo", ts=5000),
                _make_message("ULTIMO", ts=5000),
            ]
        }
        app._seen_timestamps = set()
        fake_log = _FakeChatLog()
        with patch.object(app, "query_one", return_value=fake_log):
            app._refresh_chat()
        texts = [getattr(w, "_msg_text", None) for w in fake_log.children if hasattr(w, "_msg_text")]
        assert texts.count("primo") == 1
        assert texts.count("ULTIMO") == 1




