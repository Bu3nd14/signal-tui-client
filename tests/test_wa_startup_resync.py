"""
Regression tests per il re-sync all'avvio dello storico WhatsApp e per lo
script one-shot ``purge_whatsapp_cache.py``.

Copre ``WhatsAppBackend.resync_history`` (unread ∪ chat con messaggi nel DB) e
il purge fail-safe (WAHA offline => nessuna cancellazione; backup creato prima).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backends.whatsapp import WhatsAppBackend
from signal_tui import SignalTUI


def _make_backend(api_url: str = "http://api.test") -> WhatsAppBackend:
    backend = WhatsAppBackend(api_url=api_url, media_dir="")
    backend._rest = MagicMock()
    backend._connected = True
    backend._chats_last_refresh = 0.0
    backend._CHATS_REFRESH_INTERVAL = 15.0
    return backend


# ─── _resync_wa_history (integrazione UI) ────────────────────────────────────


class TestUIResync:
    def _make_app(self) -> SignalTUI:
        app = SignalTUI()
        app.manager = MagicMock()
        return app

    def test_calls_resync_when_connected(self):
        app = self._make_app()
        backend = _make_backend()
        app.whatsapp_backend = backend
        backend._rest._request.return_value = []
        backend.cache = {"db@lid": [{"id": "a"}]}
        backend._rest.list_messages.return_value = []
        n = app._resync_wa_history()
        assert n == 1

    def test_does_not_resync_when_disconnected(self):
        app = self._make_app()
        backend = _make_backend()
        backend._connected = False
        app.whatsapp_backend = backend
        n = app._resync_wa_history()
        assert n == 0
        backend._rest.list_messages.assert_not_called()

    def test_no_backend_returns_zero(self):
        app = self._make_app()
        app.whatsapp_backend = None
        assert app._resync_wa_history() == 0

    def test_backend_error_is_swallowed(self):
        app = self._make_app()
        backend = _make_backend()
        app.whatsapp_backend = backend

        def boom():
            raise RuntimeError("boom")

        backend.resync_history = boom
        assert app._resync_wa_history() == 0


# ─── resync_history (backend WhatsApp) ────────────────────────────────────────


class TestResyncHistory:
    def test_targets_are_unread_union_of_db_cached_chats(self):
        """Unione: chat non lette + chat già con messaggi nel DB vengono scaricate."""
        backend = _make_backend()
        # chat con messaggi nel DB (chiavi del cache seminato da connect_sync)
        backend.cache = {"db@lid": [{"id": "a", "text": "x", "timestamp": 1}]}
        # unread dichiarate da WAHA (aggiunte pure se non in cache)
        now = 1_700_000_000
        backend._active_chats = {"unread@lid": (3, now), "read@lid": (0, now)}

        # un solo GET /chats (nessun throttling/polling) -> /chats
        backend._rest._request.return_value = [
            {"id": "unread@lid", "isGroup": False, "unreadCount": 3, "timestamp": now},
        ]

        def fake_list(cid, limit=1):
            return [
                {
                    "id": f"m_{cid}",
                    "key": {"id": f"m_{cid}"},
                    "from": cid,
                    "fromMe": False,
                    "body": "ciao",
                    "timestamp": now,
                }
            ]

        backend._rest.list_messages.side_effect = fake_list

        n = backend.resync_history()

        # unread + db-cache = 2 target unici
        assert n == 2
        called_jids = {c[0][0] for c in backend._rest.list_messages.call_args_list}
        assert called_jids == {"unread@lid", "db@lid"}

    def test_no_targets_does_not_fetch(self):
        """Nessuna chat in DB e nessuna unread => nessun GET (avvio veloce)."""
        backend = _make_backend()
        backend.cache = {}
        backend._active_chats = {"read@lid": (0, 0)}
        backend._rest._request.return_value = []
        n = backend.resync_history()
        assert n == 0
        backend._rest.list_messages.assert_not_called()

    def test_errors_are_nonfatal(self):
        """Un errore in una chat non deve bloccare o sollevare."""
        backend = _make_backend()
        backend.cache = {"a@lid": [{"id": "a"}]}
        backend._active_chats = {"b@lid": (1, 0)}
        backend._rest._request.return_value = []  # nessuna unread da /chats

        def boom(cid, limit=1):
            raise RuntimeError("wa down")

        backend._rest.list_messages.side_effect = boom
        # non deve propagare eccezioni
        n = backend.resync_history()
        assert n == 1

    def test_disconnected_returns_zero(self):
        backend = _make_backend()
        backend._connected = False
        backend.cache = {"a@lid": [{"id": "a"}]}
        n = backend.resync_history()
        assert n == 0
        backend._rest.list_messages.assert_not_called()


# ─── purge_whatsapp_cache.py ─────────────────────────────────────────────────


@pytest.fixture
def purge_mod():
    import purge_whatsapp_cache as mod

    return mod


def _init_db_file(db_file: Path) -> None:
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_file)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " protocol TEXT NOT NULL DEFAULT 'signal',"
            " contact_number TEXT NOT NULL, text TEXT,"
            " is_mine INTEGER NOT NULL DEFAULT 0,"
            " sender TEXT, timestamp INTEGER NOT NULL,"
            " read INTEGER DEFAULT 0, status TEXT DEFAULT 'read')"
        )
        # due messaggi: uno whatsapp, uno signal
        conn.execute(
            "INSERT INTO messages (protocol, contact_number, text, timestamp) "
            "VALUES ('whatsapp', 'wa@lid', 'wa-msg', 1)"
        )
        conn.execute(
            "INSERT INTO messages (protocol, contact_number, text, timestamp) "
            "VALUES ('signal', '+39', 'signal-msg', 2)"
        )
        conn.commit()
    finally:
        conn.close()


def test_purge_removes_only_whatsapp(purge_mod, monkeypatch, tmp_path):
    db_file = tmp_path / "messages.db"
    _init_db_file(db_file)
    monkeypatch.setattr(purge_mod, "_whatsapp_online", lambda: True)
    removed = purge_mod.purge(db_file)
    assert removed == 1
    conn = sqlite3.connect(db_file)
    try:
        wa = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE protocol='whatsapp'"
        ).fetchone()[0]
        sig = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE protocol='signal'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert wa == 0
    assert sig == 1  # Signal intatto


def test_purge_creates_backup_before_delete(purge_mod, monkeypatch, tmp_path):
    db_file = tmp_path / "messages.db"
    _init_db_file(db_file)
    monkeypatch.setattr(purge_mod, "_whatsapp_online", lambda: True)
    purge_mod.purge(db_file)
    backups = list(db_file.parent.glob("messages.db.bak-*"))
    assert len(backups) == 1


def test_purge_is_failsafe_when_whatsapp_offline(purge_mod, monkeypatch, tmp_path):
    db_file = tmp_path / "messages.db"
    _init_db_file(db_file)
    monkeypatch.setattr(purge_mod, "_whatsapp_online", lambda: False)
    with pytest.raises(SystemExit) as ei:
        purge_mod.purge(db_file)
    assert ei.value.code == 1
    # nessun backup, nessuna cancellazione
    assert not list(db_file.parent.glob("messages.db.bak-*"))
    conn = sqlite3.connect(db_file)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE protocol='whatsapp'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1


# ─── _wait_session_ready (Fix C) ───────────────────────────────────────────────


class TestWaitSessionReady:
    def test_returns_true_when_working(self):
        """Appena lo stato è WORKING, l'attesa termina subito (True)."""
        backend = _make_backend()
        backend._rest.get_session_status.return_value = {
            "status": "WORKING",
            "engine": {"state": "CONNECTED"},
        }
        assert backend._wait_session_ready(timeout=5) is True

    def test_retries_until_working(self):
        """Se inizialmente è connecting, riterara finché non diventa lavorativo."""
        backend = _make_backend()
        states = iter(
            [{"status": "CONNECTING"}, {"status": "PENDING"}, {"status": "WORKING"}]
        )
        backend._rest.get_session_status.side_effect = lambda: next(states)
        backend._wait_session_ready(timeout=5)
        assert backend._rest.get_session_status.call_count == 3

    def test_times_out_returns_false(self):
        """Se resta non-pronto oltre il timeout, ritorna False (best-effort)."""
        backend = _make_backend()
        backend._rest.get_session_status.return_value = {"status": "CONNECTING"}
        assert backend._wait_session_ready(timeout=0.2) is False

    def test_none_status_keeps_polling_to_timeout(self):
        """Sessione che non risponde (None) porta al timeout (False)."""
        backend = _make_backend()
        backend._rest.get_session_status.return_value = None
        assert backend._wait_session_ready(timeout=0.2) is False
