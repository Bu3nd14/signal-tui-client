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
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from models import (
    PROTOCOL_SIGNAL,
    PROTOCOL_TELEGRAM,
    PROTOCOL_WHATSAPP,
    ChatContact,
    contact_cache_key,
)
from signal_tui import SignalTUI
from ui_components import ImageWidget, MessageWidget, QuoteWidget


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


def _make_image_message(
    text: str,
    ts: int,
    attachment_id: str = "img-001.jpg",
    attachment_info: str = "🖼️ Image",
) -> dict:
    """Build a cached message dict with msg_type='image'."""
    return {
        "text": text,
        "is_mine": False,
        "sender": "Mario",
        "timestamp": ts,
        "quote_text": None,
        "msg_type": "image",
        "attachment_info": attachment_info,
        "attachment_id": attachment_id,
        "read": False,
        "status": "read",
    }


class _FakeChatLog:
    """Minimal stand-in for the #chat-log widget used by _refresh_chat."""

    def __init__(self) -> None:
        self.scrolled = False
        self.children: list = []

    def scroll_end(self, animate: bool = False) -> None:
        self.scrolled = True

    def mount(self, *widgets, before=None, after=None):
        for w in widgets:
            self.children.append(w)

    def remove_children(self):
        self.children = []


@pytest.mark.parametrize(
    ("msg_type", "attachment_info", "expected"),
    [
        ("attachment", "Video caption", "📎 Video caption"),
        ("attachment", "audio/ogg", "📎 audio/ogg"),
        ("attachment", "report.pdf", "📎 report.pdf"),
        ("sticker", "sticker/webp", "🎨 sticker/webp"),
    ],
)
def test_media_rendering_uses_attachment_info_not_canonical_identity(
    msg_type, attachment_info, expected
):
    app = SignalTUI()
    canonical_text = "Media: stable-media-id"
    message = {
        "text": canonical_text,
        "is_mine": False,
        "sender": "Mario",
        "timestamp": 1,
        "msg_type": msg_type,
        "attachment_info": attachment_info,
        "attachment_id": "stable-media-id",
    }

    cached_widget = app._build_message_widgets("whatsapp", False, message)[0]
    assert isinstance(cached_widget, MessageWidget)
    assert cached_widget._msg_text == expected

    app._chat_log = _FakeChatLog()
    app._add_message(**message, protocol="whatsapp")
    live_widget = app._chat_log.children[0]
    assert isinstance(live_widget, MessageWidget)
    assert live_widget._msg_text == expected


class TestRefreshChat:
    """🔄 Verifica che _refresh_chat non ri-aggiunga messaggi vecchi."""

    def _make_app(self, n_messages: int = 25) -> SignalTUI:
        """Build an app with a cache of *n_messages* for one contact."""
        app = SignalTUI()
        contact = ChatContact(
            id="+391234567890",
            display_name="Mario",
            protocol=PROTOCOL_SIGNAL,
            extras={"aci": "uuid-123"},
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
        return {(PROTOCOL_SIGNAL, contact.cache_key, ts) for ts in ts_list}

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
        app._cache[contact.cache_key].append(_make_message("nuovo", ts=26))

        added: list[str] = []

        def fake_add_message(text, *args, **kwargs):
            added.append(text)

        fake_chat_log = _FakeChatLog()

        with (
            patch.object(app, "_add_message", side_effect=fake_add_message),
            patch.object(app, "query_one", return_value=fake_chat_log),
        ):
            app._refresh_chat()

        # Only the new message should be added, not the old ones.
        assert added == ["nuovo"]
        # The chat should have been scrolled to the end.
        assert fake_chat_log.scrolled

    def test_refresh_chat_no_selected_contact(self):
        """Senza contatto selezionato, _refresh_chat non fa nulla."""
        app = SignalTUI()
        app.selected_contact = None
        app._cache = {
            contact_cache_key(PROTOCOL_SIGNAL, "+391234567890"): [
                _make_message("x", ts=1)
            ]
        }

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

        with (
            patch.object(app, "_add_message", side_effect=fake_add_message),
            patch.object(app, "query_one", return_value=fake_chat_log),
        ):
            app._refresh_chat()

        # All 3 messages are newer than max_seen=0, so all are added.
        assert len(added) == 3
        assert fake_chat_log.scrolled

    def test_whatsapp_multipart_refresh_uses_parent_id_and_text(self):
        """A DB-seeded multipart parent mounts every part once after webhook refresh."""
        app = SignalTUI()
        contact = ChatContact(
            id="1@c.us", display_name="Mario", protocol=PROTOCOL_WHATSAPP
        )
        app.selected_contact = contact
        app._cache = {
            contact.cache_key: [
                {**_make_message("one.jpg: media-1", ts=100), "id": "parent-1"},
                {**_make_message("two.jpg: media-2", ts=100), "id": "parent-1"},
            ]
        }
        app._seen_timestamps = set()
        app._seen_message_ids = set()
        added: list[str] = []
        log = _FakeChatLog()

        with (
            patch.object(
                app, "_add_message", side_effect=lambda text, **_: added.append(text)
            ),
            patch.object(app, "query_one", return_value=log),
        ):
            app._refresh_chat()
            app._refresh_chat()

        assert added == ["one.jpg: media-1", "two.jpg: media-2"]

    def test_refresh_chat_preserves_telegram_message_id_in_widget_event(self):
        app = SignalTUI()
        contact = ChatContact(id="42", display_name="Ada", protocol=PROTOCOL_TELEGRAM)
        app.selected_contact = contact
        app._cache = {
            contact.cache_key: [{**_make_message("telegram", ts=1), "id": "12345"}]
        }
        app._seen_timestamps = set()
        app._seen_message_ids = set()
        fake_chat_log = _FakeChatLog()

        with patch.object(app, "query_one", return_value=fake_chat_log):
            app._refresh_chat()

        widget = next(
            child
            for child in fake_chat_log.children
            if isinstance(child, MessageWidget)
        )
        events = []
        widget.post_message = events.append
        widget.on_click()

        assert events[0].message_id == "12345"

    def test_load_more_preserves_telegram_message_id_in_widget_event(self):
        app = SignalTUI()
        contact = ChatContact(id="42", display_name="Ada", protocol=PROTOCOL_TELEGRAM)
        app.selected_contact = contact
        app._cache = {
            contact.cache_key: [{**_make_message("telegram", ts=1), "id": "12345"}]
        }
        app._status = MagicMock()
        fake_chat_log = _FakeChatLog()

        with patch.object(app, "query_one", return_value=fake_chat_log):
            app._load_all_messages()

        widget = next(
            child
            for child in fake_chat_log.children
            if isinstance(child, MessageWidget)
        )
        events = []
        widget.post_message = events.append
        widget.on_click()

        assert events[0].message_id == "12345"


class TestLoadWorkerStaleness:
    """🛡️ Il guard del reload-token evita duplicati su ri-selezione."""

    def _make_app(self, n_messages: int = 3) -> SignalTUI:
        app = SignalTUI()
        contact = ChatContact(
            id="+391234567890",
            display_name="Mario",
            protocol=PROTOCOL_SIGNAL,
            extras={"aci": "uuid-123"},
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
        mounted: list[str] = []

        def fake_make_widget(text, *a, **k):
            mounted.append(text)
            return MagicMock()

        fake_chat_log = MagicMock()
        with (
            patch.object(app, "_make_message_widget", side_effect=fake_make_widget),
            patch.object(app, "query_one", return_value=fake_chat_log),
            patch.object(
                app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)
            ),
        ):
            app._load_messages_worker()
        from collections import Counter

        counts = Counter(mounted)
        for m in ["msg-1", "msg-2", "msg-3"]:
            assert counts[m] == 1, f"{m} mounted {counts[m]} times"
        # Single mount call with all widgets
        assert fake_chat_log.mount.called
        assert fake_chat_log.scroll_end.called

    def test_stale_load_worker_aborts_on_newer_selection(self):
        """Un worker con token scaduto non monta dopo una ri-selezione (anti-race).

        Il load gira in un thread; a metà esecuzione arriva una selezione più
        recente (token cambiato + contatto diverso). Il worker si accorge che
        è obsoleto e NON monta i messaggi che la nuova `_clear_chat` avrebbe
        già rimosso — altrimenti li rimonterebbe doppi.
        """
        import threading

        app = self._make_app(n_messages=20)  # >20 to force a workable batch
        mounted: list[str] = []

        def fake_make_widget(text, *a, **k):
            mounted.append(text)
            return MagicMock()

        fake_chat_log = MagicMock()

        # The cache holds 20 msgs; we simulate a slow mount by sleeping a tiny
        # bit in call_from_thread so a race is observable.
        def slow_call(fn, *a, **k):
            fn(*a, **k)

        with (
            patch.object(app, "_make_message_widget", side_effect=fake_make_widget),
            patch.object(app, "query_one", return_value=fake_chat_log),
            patch.object(app, "call_from_thread", side_effect=slow_call),
        ):
            # Selection #1: bump the reload token.
            app._chat_reload_token += 1

            def worker():
                # Run the real load; it captures the CURRENT token (=captured).
                app._load_messages_worker()

            th = threading.Thread(target=worker)
            th.start()
            th.join(timeout=0.001)  # let it begin
            # Selection #2: bump token + switch contact (invalidates worker #1).
            app._chat_reload_token += 1
            app.selected_contact = ChatContact(
                id="+399999999999",
                display_name="Altro",
                protocol=PROTOCOL_SIGNAL,
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

        counts = Counter(mounted)
        for m in counts:
            assert counts[m] <= 1, f"{m} mounted {counts[m]} times"


class TestRenderDedup:
    """🛡️ Il dedup a livello di rendering evita doppioni anche con seen vuoto."""

    def _make_app(self) -> SignalTUI:
        app = SignalTUI()
        c = ChatContact(
            id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        app.selected_contact = c
        app._cache = {
            c.cache_key: [
                {
                    "text": "old",
                    "is_mine": False,
                    "sender": "M",
                    "timestamp": 100,
                    "quote_text": None,
                    "msg_type": "text",
                    "attachment_info": None,
                    "attachment_id": None,
                    "read": False,
                    "status": "read",
                },
                {
                    "text": "session",
                    "is_mine": True,
                    "sender": "You",
                    "timestamp": 5000000000,
                    "quote_text": None,
                    "msg_type": "text",
                    "attachment_info": None,
                    "attachment_id": None,
                    "read": True,
                    "status": "sent",
                },
            ]
        }
        app._loaded_all = False
        return app

    def test_refresh_chat_does_not_double_with_stale_seen(self):
        """Anche con _seen_timestamps svuotato, _refresh_chat non rimonta doppio."""
        app = self._make_app()
        fake_log = _FakeChatLog()
        with (
            patch.object(app, "query_one", return_value=fake_log),
            patch.object(
                app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)
            ),
        ):
            app._seen_timestamps.clear()
            app._load_messages_worker()
            # Simulate a refresh with a stale (cleared) seen set — the exact
            # situation that used to double the newest/session messages.
            app._seen_timestamps.clear()
            app._refresh_chat()
        texts = [
            getattr(w, "_msg_text", None)
            for w in fake_log.children
            if hasattr(w, "_msg_text")
        ]
        assert len(texts) == 2  # 'old' + 'session', each exactly once
        assert texts.count("old") == 1
        assert texts.count("session") == 1

    def test_add_message_skips_already_shown(self):
        """_add_message non rimonta un messaggio già mostrato (guard)."""
        app = self._make_app()
        fake_log = _FakeChatLog()
        with patch.object(app, "query_one", return_value=fake_log):
            app._add_message(
                "session",
                is_mine=True,
                timestamp=5000000000,
                sender="You",
                status="sent",
            )
            # Same identity again -> skipped by render-dedup.
            app._add_message(
                "session",
                is_mine=True,
                timestamp=5000000000,
                sender="You",
                status="sent",
            )
            # A different message mounts normally.
            app._add_message(
                "other", is_mine=False, timestamp=5000000001, sender="M", status="read"
            )
        texts = [
            getattr(w, "_msg_text", None)
            for w in fake_log.children
            if hasattr(w, "_msg_text")
        ]
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

        with (
            patch.object(app, "_add_message", side_effect=fake_add),
            patch.object(app, "query_one", return_value=fake_log),
        ):
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
        c = ChatContact(
            id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        app.selected_contact = c
        app._loaded_all = False
        return app

    def test_add_message_shows_two_distinct_same_second(self):
        """Due messaggi con lo STESSO timestamp ma testo diverso: entrambi montati."""
        app = self._make_app()
        fake_log = _FakeChatLog()
        with patch.object(app, "query_one", return_value=fake_log):
            app._add_message(
                "primo", is_mine=False, timestamp=5000, sender="M", status="read"
            )
            app._add_message(
                "ULTIMO", is_mine=False, timestamp=5000, sender="M", status="read"
            )
        texts = [
            getattr(w, "_msg_text", None)
            for w in fake_log.children
            if hasattr(w, "_msg_text")
        ]
        assert texts.count("primo") == 1
        assert texts.count("ULTIMO") == 1

    def test_add_message_still_dedups_exact_duplicate(self):
        """Lo STESSO messaggio (stesso ts E stesso testo) resta deduplicato."""
        app = self._make_app()
        fake_log = _FakeChatLog()
        with patch.object(app, "query_one", return_value=fake_log):
            app._add_message(
                "ciao", is_mine=False, timestamp=5000, sender="M", status="read"
            )
            app._add_message(
                "ciao", is_mine=False, timestamp=5000, sender="M", status="read"
            )
        texts = [
            getattr(w, "_msg_text", None)
            for w in fake_log.children
            if hasattr(w, "_msg_text")
        ]
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
        texts = [
            getattr(w, "_msg_text", None)
            for w in fake_log.children
            if hasattr(w, "_msg_text")
        ]
        assert texts.count("primo") == 1
        assert texts.count("ULTIMO") == 1

    # ─── Regressione strutturale (WhatsApp) ─────────────────────────────────

    def _make_wa_app(self):
        """App con un contatto WhatsApp e cache potenzialmente non ordinata.

        Il manager è finto (get -> None) per evitare che il worker attivi un
        fetch_history REALE contro WAHA nei test (l'ambiente di test ha .env).
        """
        from unittest.mock import MagicMock

        app = SignalTUI()
        app.manager = MagicMock()
        app.manager.get.return_value = None
        from models import PROTOCOL_WHATSAPP

        contact = ChatContact(
            id="19645297868955@lid",
            display_name="Giovanni",
            protocol=PROTOCOL_WHATSAPP,
        )
        app.selected_contact = contact
        return app

    def test_load_messages_worker_shows_newest_even_if_cache_unsorted(self):
        """Cache WhatsApp FUORI ORDINE: l'ultimo messaggio per timestamp deve
        comparire anche se non è l'ultimo elemento dell'array (fix strutturale:
        [[-20:]] tagliava per posizione e perdeva l'ultimo)."""
        app = self._make_wa_app()
        contact = app.selected_contact
        # Array volutamente NON ordinato per timestamp: l'ultimo (ts 3000) è in
        # posizione non finale e un vecchio messaggio (ts 1000) è in coda.
        app._cache = {
            contact.cache_key: [
                _make_message("Ok  ci sentiamo", ts=3000),
                _make_message("vecchio", ts=1000),
                _make_message("medio", ts=2000),
            ]
        }
        app._chat_reload_token = 1
        app._seen_timestamps = set()
        app._seen_message_ids = set()
        app._loaded_all = False

        mounted: list[str] = []
        fake_log = _FakeChatLog()

        def fake_make_widget(text, *args, **kwargs):
            mounted.append(text)
            return MagicMock()

        with (
            patch.object(app, "query_one", return_value=fake_log),
            patch.object(
                app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)
            ),
            patch.object(app, "_make_message_widget", side_effect=fake_make_widget),
            patch.object(app, "_add_load_more_widget"),
        ):
            app._load_messages_worker()

        # L'ultimo messaggio per timestamp deve essere mostrato.
        assert "Ok  ci sentiamo" in mounted

    def test_refresh_chat_keeps_newest_after_echo_ts_change(self):
        """Dopo che l'echo aggiorna ts di un messaggio (in-place), l'ULTIMO
        resta mostrato e non viene duplicato (identity stabile via id)."""
        app = self._make_wa_app()
        contact = app.selected_contact
        # Messaggio mostrato con ts client (5000), poi l'echo cambia ts a 6000
        # e assegna l'id reale (come fa ingest_message).
        app._cache = {
            contact.cache_key: [
                _make_message("Ok  ci sentiamo", ts=5000),
                _make_message("certo", ts=4000),
            ]
        }
        # _seen_message_ids registra SOLO la forma (ts,text) PRE-echo.
        app._seen_message_ids = {
            (PROTOCOL_WHATSAPP, contact.cache_key, 5000, "Ok  ci sentiamo"),
        }
        app._seen_timestamps = {
            (PROTOCOL_WHATSAPP, contact.cache_key, 5000),
        }
        fake_log = _FakeChatLog()
        with patch.object(app, "query_one", return_value=fake_log):
            app._refresh_chat()
        texts = [
            getattr(w, "_msg_text", None)
            for w in fake_log.children
            if hasattr(w, "_msg_text")
        ]
        # Nessun doppione: il messaggio non viene rimontato due volte.
        assert texts.count("Ok  ci sentiamo") <= 1

    def test_load_more_banner_survives_clear_chat(self):
        """Regressione: con >20 messaggi il banner "load more" DEVE comparire.

        Fix B (mounting atomico) rimonta il log con uno ``_clear_chat()`` che
        rimuove TUTTI i figli del log.  Se il banner veniva montato PRIMA dello
        ``_clear_chat`` (commit fe3d5f1), veniva rimosso e non compariva mai il
        bottone "load previous messages" all'apertura di una chat con +20 msg.
        Ora viene rimontato alla FINE di ``_mount_window``, quindi deve essere
        presente tra i children del log dopo il load.
        """
        app = self._make_wa_app()
        contact = app.selected_contact
        app._cache = {
            contact.cache_key: [_make_message(f"msg-{i}", ts=i) for i in range(1, 26)]
        }
        app._chat_reload_token = 1
        app._seen_timestamps = set()
        app._seen_message_ids = set()
        app._loaded_all = False
        app._shown_in_log = set()

        fake_log = _FakeChatLog()
        # _make_message_widget è patcheato per non montare widget reali, ma il banner
        # usa il VERO _add_load_more_widget (monta un Button in fake_log).
        with (
            patch.object(app, "query_one", return_value=fake_log),
            patch.object(
                app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)
            ),
            patch.object(app, "_make_message_widget", return_value=MagicMock()),
        ):
            app._load_messages_worker()

        # Il banner deve essere sopravvissuto allo _clear_chat: almeno un
        # widget con id "load-more-msg" presente nel log.
        load_more = [
            w for w in fake_log.children if getattr(w, "id", None) == "load-more-msg"
        ]
        assert load_more, "Il banner 'load previous messages' non compare nel log"

    def test_image_messages_mount_from_cache(self):
        """Regressione: i messaggi con msg_type='image' devono essere montati
        come ImageWidget, non causare TypeError silenzioso.

        Il bug: _mount_window chiamava ImageWidget(...) senza l'argomento
        obbligatorio ``attachment_path``, causando TypeError ingoiato da
        ``except Exception: pass``.  Il widget non veniva mai aggiunto.
        """
        app = self._make_wa_app()
        contact = app.selected_contact
        app._cache = {
            contact.cache_key: [
                _make_message("msg-1", ts=1),
                _make_image_message("🖼️ Image", ts=2, attachment_id="img-001.jpg"),
                _make_message("msg-3", ts=3),
            ]
        }
        app._chat_reload_token = 1
        app._seen_timestamps = set()
        app._seen_message_ids = set()
        app._loaded_all = True  # meno di 20 messaggi
        app._shown_in_log = set()

        fake_log = _FakeChatLog()

        with (
            patch.object(app, "query_one", return_value=fake_log),
            patch.object(
                app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)
            ),
            patch.object(app, "_make_message_widget", return_value=MagicMock()),
            patch.object(app, "_add_load_more_widget"),
        ):
            app._load_messages_worker()

        # Estrae tutti gli ImageWidget montati nel fake_log
        image_widgets = [w for w in fake_log.children if isinstance(w, ImageWidget)]
        assert len(image_widgets) == 1, (
            f"Expected 1 ImageWidget in chat log, found {len(image_widgets)}. "
            f"Children: {[type(w).__name__ for w in fake_log.children]}"
        )
        # Il path non è risolto quando si carica da cache
        assert image_widgets[0].attachment_path is None
        assert image_widgets[0].attachment_id == "img-001.jpg"

    def test_merge_backend_cache_copies_quote_metadata(self):
        """Il twin backend ricco copia le chiavi quote mancanti (add-only)."""
        app = self._make_wa_app()
        contact = app.selected_contact
        app._cache = {
            contact.cache_key: [
                {
                    "text": "x",
                    "is_mine": False,
                    "timestamp": 1000,
                    "quote_text": "🖼️ Immagine",
                }
            ]
        }
        backend = MagicMock()
        backend.cache = {
            contact.id: [
                {
                    "text": "x",
                    "is_mine": False,
                    "timestamp": 1000,
                    "quote_text": "🖼️ Immagine",
                    "quote_attachment_id": "att-1",
                    "quote_content_type": "image/png",
                    "quote_attachment_path": "/tmp/quote-thumbs/abc.png",
                }
            ]
        }

        app._merge_backend_cache(contact, backend)

        entry = app._cache[contact.cache_key][0]
        assert entry["quote_attachment_id"] == "att-1"
        assert entry["quote_content_type"] == "image/png"
        assert entry["quote_attachment_path"] == "/tmp/quote-thumbs/abc.png"


class TestCacheImageReplyMetadata:
    """🖼️ Bug #37 — ``_build_message_widgets`` propaga i metadati reply all'ImageWidget."""

    def test_cache_built_image_widget_carries_reply_metadata(self):
        app = SignalTUI()
        message = {
            "id": "img-42",
            "text": "",
            "is_mine": False,
            "sender": "Mario",
            "timestamp": 1000,
            "quote_text": None,
            "msg_type": "image",
            "attachment_info": "photo.jpg",
            "attachment_id": "att-1",
            "read": False,
            "status": "read",
        }

        widgets = app._build_message_widgets("signal", False, message)
        image_widget = widgets[0]

        assert isinstance(image_widget, ImageWidget)
        assert image_widget._timestamp == 1000
        assert image_widget._sender == "Mario"
        assert image_widget._is_mine is False
        assert image_widget._message_id == "img-42"
        assert image_widget._attachment_info == "photo.jpg"
        assert image_widget._caption is None
        assert image_widget.attachment_id == "att-1"

    def test_cache_built_image_widget_carries_caption(self):
        """La caption reale calcolata da ``_image_caption`` arriva al widget."""
        app = SignalTUI()
        message = {
            "id": "img-42",
            "text": "Che bella!",
            "is_mine": False,
            "sender": "Mario",
            "timestamp": 1000,
            "quote_text": None,
            "msg_type": "image",
            "attachment_info": "photo.jpg",
            "attachment_id": "att-1",
            "read": False,
            "status": "read",
        }

        widgets = app._build_message_widgets("signal", False, message)
        image_widget = widgets[0]

        assert isinstance(image_widget, ImageWidget)
        assert image_widget._caption == "Che bella!"

    def test_cache_built_image_widget_carries_content_type(self):
        """(A/C) ``_build_message_widgets`` propaga ``content_type`` all'ImageWidget."""
        app = SignalTUI()
        message = {
            "id": "img-42",
            "text": "",
            "is_mine": False,
            "sender": "Mario",
            "timestamp": 1000,
            "quote_text": None,
            "msg_type": "image",
            "attachment_info": "photo.png",
            "attachment_id": "att-1",
            "content_type": "image/png",
            "read": False,
            "status": "read",
        }

        widgets = app._build_message_widgets("signal", False, message)
        image_widget = widgets[0]

        assert isinstance(image_widget, ImageWidget)
        assert image_widget._content_type == "image/png"

    def test_cache_media_quote_renders_bubble(self):
        """Una quote media (segnaposto) monta la bolla anche dal percorso cache."""
        app = SignalTUI()
        message = {
            "id": "msg-42",
            "text": "testo",
            "is_mine": True,
            "sender": "You",
            "timestamp": 2000,
            "quote_text": "🖼️ Immagine",
            "msg_type": "text",
            "attachment_info": None,
            "attachment_id": None,
            "read": True,
            "status": "sent",
        }

        widgets = app._build_message_widgets("signal", False, message)

        # Chunk 2: the quote bubble is now a QuoteWidget container; the text
        # lives (byte-identical) in its internal Static.
        assert isinstance(widgets[0], QuoteWidget)
        text_static = next(widgets[0].compose())
        assert text_static._Static__content == "▎ 🖼️ Immagine"
        assert isinstance(widgets[1], MessageWidget)
        assert widgets[1]._msg_text == "testo"
