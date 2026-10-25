"""Tests for ``ChatCore``: connect workers publish events on the bus.

Port dei test dei worker di ``tui/backend_connect.py`` (guard anti-duplicato,
boot WhatsApp, reconnect mirato, resync storico): invece di ``_status`` /
``_mark_*`` si asseriscono gli eventi pubblicati; più i test del dispatch lato
TUI e del gating di ``ChatCore.start()``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from models import PROTOCOL_SIGNAL, PROTOCOL_WHATSAPP
from protocols.whatsapp import WhatsAppBackend
from server.bus import GAP, Event, EventBus
from server.core import ChatCore


def _make_core(*, wa: bool = False, tg: bool = False) -> tuple[ChatCore, EventBus]:
    bus = EventBus()
    with (
        patch("server.core.whatsapp_enabled", return_value=wa),
        patch("server.core.telegram_enabled", return_value=tg),
    ):
        core = ChatCore(bus)
    return core, bus


def _events(sub) -> list[tuple[str, dict]]:
    return [(e.type, e.payload) for e in sub.drain()]


def _status(proto: str, text: str, persistent: bool = False) -> tuple[str, dict]:
    return (
        "backend_status",
        {"protocol": proto, "text": text, "persistent": persistent},
    )


def _wa_backend(**kw) -> WhatsAppBackend:
    backend = WhatsAppBackend(api_url="http://api.test", media_dir="")
    backend._rest = MagicMock()
    backend._connected = True
    return backend


class TestCoreConstruction:
    def test_registers_only_enabled_backends(self):
        core, bus = _make_core()
        assert core.bus is bus
        assert core.whatsapp_backend is None
        assert core.telegram_backend is None
        assert core.manager.get("signal") is core.signal_backend

    def test_registers_whatsapp_and_telegram_when_enabled(self):
        core, _ = _make_core(wa=True, tg=True)
        assert core.whatsapp_backend is not None
        assert core.telegram_backend is not None
        assert core.manager.get("whatsapp") is core.whatsapp_backend
        assert core.manager.get("telegram") is core.telegram_backend


class TestCoreStartGating:
    def _core(self, **kw):
        core, bus = _make_core(**kw)
        core._spawn = MagicMock()
        return core, bus

    def test_signal_enabled_spawns_worker(self):
        core, _ = self._core()
        with patch("server.core.signal_enabled", return_value=True):
            core.start()
        core._spawn.assert_called_once_with(core.connect_signal, "core-connect-signal")

    def test_signal_disabled_reports_done(self):
        core, bus = self._core()
        sub = bus.subscribe()
        with patch("server.core.signal_enabled", return_value=False):
            core.start()
        core._spawn.assert_not_called()
        assert _events(sub) == [
            ("backend_done", {"protocol": core.signal_backend.protocol, "ok": False})
        ]

    def test_whatsapp_uses_boot_worker(self):
        core, _ = self._core(wa=True)
        with patch("server.core.signal_enabled", return_value=False):
            core.start()
        core._spawn.assert_called_once_with(
            core.connect_whatsapp_boot, "core-connect-whatsapp-boot"
        )

    def test_telegram_started_only_when_not_needing_pairing(self):
        core, _ = self._core(tg=True)
        core.telegram_backend = MagicMock(needs_pairing=False)
        with patch("server.core.signal_enabled", return_value=False):
            core.start()
        core._spawn.assert_called_once_with(
            core.connect_telegram, "core-connect-telegram"
        )

    def test_telegram_skipped_when_pairing_needed(self):
        core, _ = self._core(tg=True)
        core.telegram_backend = MagicMock(needs_pairing=True)
        with patch("server.core.signal_enabled", return_value=False):
            core.start()
        core._spawn.assert_not_called()


class TestSignalConnectEvents:
    def _core(self, *, use_daemon: bool):
        core, bus = _make_core()
        core.signal_backend = MagicMock()
        core.signal_backend.protocol = PROTOCOL_SIGNAL
        core.signal_backend._use_daemon = use_daemon
        core.signal_backend.contacts = [object(), object()]
        return core, bus

    def test_success_with_daemon_event_order(self):
        core, bus = self._core(use_daemon=True)
        sub = bus.subscribe()
        core.connect_signal()
        assert _events(sub) == [
            ("backend_connecting", {"protocol": "signal"}),
            _status("signal", "⏳ Signal: avvio daemon...", True),
            ("backend_ready", {"protocol": "signal", "contacts": 2}),
            _status("signal", "💡 Select a contact to view chat"),
            _status("signal", "✅ Signal: daemon attivo"),
        ]

    def test_success_without_daemon_is_persistent_warning(self):
        core, bus = self._core(use_daemon=False)
        sub = bus.subscribe()
        core.connect_signal()
        assert _events(sub)[-1] == _status(
            "signal", "⚠️ Signal: daemon non disponibile (subprocess)", True
        )

    def test_failure_reports_error_then_done(self):
        core, bus = self._core(use_daemon=True)
        core.signal_backend._connect_sync.side_effect = RuntimeError("boom")
        sub = bus.subscribe()
        core.connect_signal()
        assert _events(sub) == [
            ("backend_connecting", {"protocol": "signal"}),
            _status("signal", "⏳ Signal: avvio daemon...", True),
            _status("signal", "❌ Signal: errore — boom", True),
            ("backend_done", {"protocol": "signal", "ok": False}),
        ]


class TestTelegramConnectGuard:
    """🛡️ connect_telegram non deve avviare due worker in parallelo.

    Due Ctrl+L→Esc ravvicinati avviavano due worker concorrenti che gareggiavano
    sullo stesso stato del backend (client/loop), rompendo la ricezione live.
    """

    def _core(self):
        core, bus = _make_core()
        core.telegram_backend = MagicMock()
        core.telegram_backend.protocol = "telegram"
        core.telegram_backend.contacts = []
        return core, bus

    def test_skips_when_already_connecting(self):
        core, bus = self._core()
        sub = bus.subscribe()
        core._tg_connecting = True
        core.connect_telegram()
        core.telegram_backend._connect_sync.assert_not_called()
        assert _events(sub) == []

    def test_runs_and_resets_guard(self):
        core, bus = self._core()
        sub = bus.subscribe()
        core.connect_telegram()
        core.telegram_backend._connect_sync.assert_called_once()
        assert core._tg_connecting is False
        assert _events(sub) == [
            ("backend_connecting", {"protocol": "telegram"}),
            _status("telegram", "⏳ Telegram: connecting...", True),
            ("backend_ready", {"protocol": "telegram", "contacts": 0}),
        ]

    def test_history_sync_status_when_contacts(self):
        core, bus = self._core()
        core.telegram_backend.contacts = [object()]
        sub = bus.subscribe()
        core.connect_telegram()
        core.telegram_backend.fetch_recent_history.assert_called_once_with(limit=20)
        assert _status("telegram", "⏳ Telegram: sync cronologia...", True) in _events(
            sub
        )

    def test_guard_reset_on_failure(self):
        core, bus = self._core()
        sub = bus.subscribe()
        core.telegram_backend._connect_sync.side_effect = RuntimeError("boom")
        core.connect_telegram()
        assert core._tg_connecting is False
        assert _events(sub)[-2:] == [
            _status("telegram", "❌ Telegram: boom", True),
            ("backend_done", {"protocol": "telegram", "ok": False}),
        ]


class TestWhatsappConnect:
    def _core(self, backend):
        core, bus = _make_core()
        core.whatsapp_backend = backend
        return core, bus

    def test_skips_when_already_connecting(self):
        backend = MagicMock()
        core, bus = self._core(backend)
        sub = bus.subscribe()
        core._wa_connecting = True
        core.connect_whatsapp()
        backend.connect_sync.assert_not_called()
        assert _events(sub) == []

    def test_contacts_ready_then_resync_resets_guard(self):
        backend = MagicMock()
        backend.protocol = PROTOCOL_WHATSAPP
        backend.needs_pairing = False
        backend.contacts = [object()] * 3
        backend.resync_history.return_value = 2
        core, bus = self._core(backend)
        sub = bus.subscribe()
        with patch("server.core.ensure_webhook_server"):
            core.connect_whatsapp()
        evs = _events(sub)
        assert evs[0] == ("backend_connecting", {"protocol": "whatsapp"})
        assert evs[1][0] == "backend_status"
        assert evs[1][1]["text"].startswith("✅ WAHA: 3 contatti (webhook :")
        assert evs[1][1]["persistent"] is False
        assert evs[2] == ("backend_ready", {"protocol": "whatsapp", "contacts": 3})
        assert evs[3] == _status(
            "whatsapp", "✅ WAHA: cronologia sincronizzata per 2 chat"
        )
        assert core._wa_connecting is False

    def test_no_contacts_spawns_poller_and_keeps_guard(self):
        backend = MagicMock()
        backend.protocol = PROTOCOL_WHATSAPP
        backend.needs_pairing = True
        backend.contacts = []
        core, bus = self._core(backend)
        core._spawn = MagicMock()
        sub = bus.subscribe()
        with patch("server.core.ensure_webhook_server"):
            core.connect_whatsapp()
        core._spawn.assert_called_once_with(
            core._poll_wa_contacts, "core-poll-wa-contacts"
        )
        assert _events(sub) == [
            ("backend_connecting", {"protocol": "whatsapp"}),
            _status("whatsapp", "⏳ WAHA: attesa pairing...", True),
        ]
        assert core._wa_connecting is True

    def test_failure_reports_done_and_resets_guard(self):
        backend = MagicMock()
        backend.protocol = PROTOCOL_WHATSAPP
        backend.needs_pairing = False
        backend.connect_sync.side_effect = RuntimeError("down")
        core, bus = self._core(backend)
        sub = bus.subscribe()
        core.connect_whatsapp()
        assert _events(sub)[-2:] == [
            _status("whatsapp", "❌ WAHA: non disponibile — down", True),
            ("backend_done", {"protocol": "whatsapp", "ok": False}),
        ]
        assert core._wa_connecting is False

    def test_poll_contacts_success(self):
        backend = MagicMock()
        backend.protocol = PROTOCOL_WHATSAPP
        backend._connected = False
        backend.contacts = [object()]
        core, bus = self._core(backend)
        core._wa_connecting = True
        sub = bus.subscribe()
        with patch("server.core.time.sleep"):
            core._poll_wa_contacts()
        assert _events(sub) == [
            _status("whatsapp", "🔄 WAHA: sync contatti...", True),
            _status("whatsapp", "📥 WAHA: 1 contatti caricati"),
            ("backend_ready", {"protocol": "whatsapp", "contacts": 1}),
            _status("whatsapp", "⏳ WAHA: sync cronologia...", True),
            _status("whatsapp", ""),
        ]
        assert core._wa_connecting is False

    def test_poll_contacts_timeout(self):
        backend = MagicMock()
        backend.protocol = PROTOCOL_WHATSAPP
        backend.contacts = []
        core, bus = self._core(backend)
        core._wa_connecting = True
        sub = bus.subscribe()
        with (
            patch("server.core.time.sleep"),
            patch("server.core.time.monotonic", side_effect=[0.0, 1.0, 1e9]),
        ):
            core._poll_wa_contacts()
        assert _events(sub) == [
            _status("whatsapp", "🔄 WAHA: sync contatti...", True),
            _status("whatsapp", "⚠️ WAHA: timeout contatti", True),
            ("backend_done", {"protocol": "whatsapp", "ok": False}),
        ]
        assert core._wa_connecting is False


class _FakeWhatsAppBackend:
    """Stand-in with real ``is_working``/``needs_pairing`` properties."""

    protocol = PROTOCOL_WHATSAPP

    def __init__(self, *, needs_pairing: bool = False, is_working=True):
        self.needs_pairing = needs_pairing
        self._is_working = is_working

    @property
    def is_working(self) -> bool:
        value = self._is_working
        return bool(value() if callable(value) else value)


class TestWhatsappBootConnect:
    """🚀 ``connect_whatsapp_boot``: connette appena WAHA è WORKING.

    L'app può partire mentre WAHA sta ancora sincronizzando: il vecchio gate
    ``is_working`` saltava la connessione senza retry (UI vuota fino al riavvio).
    """

    def _make(self, *, needs_pairing=False, is_working=True):
        core, bus = _make_core()
        core.connect_whatsapp = MagicMock()
        core.whatsapp_backend = _FakeWhatsAppBackend(
            needs_pairing=needs_pairing, is_working=is_working
        )
        return core, bus.subscribe()

    def test_connects_immediately_when_working(self):
        core, _ = self._make(is_working=True)
        core.connect_whatsapp_boot()
        core.connect_whatsapp.assert_called_once_with()

    def test_skips_pairing_sessions(self):
        core, sub = self._make(needs_pairing=True, is_working=False)
        core.connect_whatsapp_boot()
        core.connect_whatsapp.assert_not_called()
        assert _events(sub) == [("backend_done", {"protocol": "whatsapp", "ok": False})]

    def test_waits_until_working_then_connects(self):
        core, _ = self._make(is_working=False)
        calls = {"n": 0}

        def _is_working():
            calls["n"] += 1
            return calls["n"] >= 3

        core.whatsapp_backend._is_working = _is_working
        with patch("server.core.time.sleep"):
            core.connect_whatsapp_boot()
        core.connect_whatsapp.assert_called_once_with()
        assert calls["n"] == 3

    def test_timeout_marks_backend_done(self):
        core, sub = self._make(is_working=False)
        with patch("server.core.time.monotonic", side_effect=[0.0, 1e9]):
            core.connect_whatsapp_boot()
        core.connect_whatsapp.assert_not_called()
        assert _events(sub) == [("backend_done", {"protocol": "whatsapp", "ok": False})]

    def test_no_backend_is_noop(self):
        core, _ = _make_core()
        core.connect_whatsapp_boot()  # non deve sollevare


class TestReconnect:
    """🎯 reconnect riconnette solo i backend toccati."""

    def _core(self):
        core, _ = _make_core()
        core._spawn = MagicMock()
        core.signal_backend = MagicMock()
        core.whatsapp_backend = MagicMock()
        core.telegram_backend = MagicMock()
        return core

    def test_empty_set_reconnects_nothing(self):
        core = self._core()
        core.reconnect(set())
        core._spawn.assert_not_called()

    def test_telegram_only(self):
        core = self._core()
        core.reconnect({"telegram"})
        core._spawn.assert_called_once_with(
            core.connect_telegram, "core-connect-telegram"
        )

    def test_signal_only(self):
        core = self._core()
        core.reconnect({"signal"})
        core._spawn.assert_called_once_with(core.connect_signal, "core-connect-signal")

    def test_whatsapp_only(self):
        core = self._core()
        core.reconnect({"whatsapp"})
        core._spawn.assert_called_once_with(
            core.connect_whatsapp, "core-connect-whatsapp"
        )

    def test_multiple(self):
        core = self._core()
        core.reconnect({"signal", "telegram"})
        assert core._spawn.call_count == 2
        core._spawn.assert_any_call(core.connect_signal, "core-connect-signal")
        core._spawn.assert_any_call(core.connect_telegram, "core-connect-telegram")

    def test_missing_backend_skipped(self):
        core = self._core()
        core.telegram_backend = None
        core.reconnect({"telegram"})
        core._spawn.assert_not_called()

    def test_spawn_runs_daemon_named_thread(self):
        seen = {}

        def target():
            import threading

            t = threading.current_thread()
            seen["name"], seen["daemon"] = t.name, t.daemon

        ChatCore._spawn(target, "core-test").join(2)
        assert seen == {"name": "core-test", "daemon": True}


class TestResyncWaHistory:
    def _core(self, backend):
        core, bus = _make_core()
        core.whatsapp_backend = backend
        return core, bus

    def test_calls_resync_when_connected(self):
        backend = _wa_backend()
        backend._rest._request.return_value = []
        backend.cache = {"111@lid": [{"id": "a"}]}
        backend._rest.list_messages.return_value = []
        core, bus = self._core(backend)
        sub = bus.subscribe()
        assert core._resync_wa_history() == 1
        assert _events(sub) == [
            _status("whatsapp", "✅ WAHA: cronologia sincronizzata per 1 chat")
        ]

    def test_does_not_resync_when_disconnected(self):
        backend = _wa_backend()
        backend._connected = False
        core, _ = self._core(backend)
        assert core._resync_wa_history() == 0
        backend._rest.list_messages.assert_not_called()

    def test_no_backend_returns_zero(self):
        core, _ = self._core(None)
        assert core._resync_wa_history() == 0

    def test_backend_error_is_swallowed(self):
        backend = _wa_backend()

        def boom():
            raise RuntimeError("boom")

        backend.resync_history = boom
        core, _ = self._core(backend)
        assert core._resync_wa_history() == 0


class TestTuiDispatch:
    """Evento del core -> metodo UI giusto (persistent -> timeout 0)."""

    @pytest.fixture
    def app(self):
        from signal_tui import SignalTUI

        app = SignalTUI()
        app._status = MagicMock()
        app._mark_backend_connecting = MagicMock()
        app._mark_backend_done = MagicMock()
        app._on_backend_ready = MagicMock()
        return app

    @staticmethod
    def _ev(type_: str, payload: dict) -> Event:
        return Event(1, type_, payload, 0.0)

    def test_connecting(self, app):
        app._dispatch_core_event(self._ev("backend_connecting", {"protocol": "x"}))
        app._mark_backend_connecting.assert_called_once_with("x")

    def test_status_persistent_uses_timeout_zero(self, app):
        payload = {"protocol": "x", "text": "hi", "persistent": True}
        app._dispatch_core_event(self._ev("backend_status", payload))
        app._status.assert_called_once_with("hi", 0)

    def test_status_default_timeout(self, app):
        payload = {"protocol": "x", "text": "hi", "persistent": False}
        app._dispatch_core_event(self._ev("backend_status", payload))
        app._status.assert_called_once_with("hi")

    def test_ready_passes_the_protocol_not_the_backend(self, app):
        app.manager = MagicMock()
        app._dispatch_core_event(
            self._ev("backend_ready", {"protocol": "x", "contacts": 1})
        )
        app.manager.get.assert_not_called()
        app._on_backend_ready.assert_called_once_with("x")

    def test_done(self, app):
        app._dispatch_core_event(
            self._ev("backend_done", {"protocol": "x", "ok": False})
        )
        app._mark_backend_done.assert_called_once_with("x")

    def test_unknown_and_gap_ignored(self, app):
        app._dispatch_core_event(self._ev("mystery", {}))
        app._dispatch_core_event(self._ev(GAP, {"reason": "overflow"}))
        app._status.assert_not_called()
        app._mark_backend_connecting.assert_not_called()
        app._mark_backend_done.assert_not_called()
        app._on_backend_ready.assert_not_called()

    def test_reconnect_goes_through_the_link_reconnect_operation(self, app):
        from client_harness import StubClient

        app.client = StubClient({"ok": True})
        app._reconnect_touched_backends({"signal"})
        assert app.client.calls == [("link.reconnect", {"protocols": ["signal"]})]

    def test_backend_aliases_follow_core(self, app):
        marker = MagicMock()
        app.whatsapp_backend = marker
        assert app.core.whatsapp_backend is marker
        assert app.signal_backend is app.core.signal_backend
        assert app.manager is app.core.manager

    def test_event_worker_forwards_to_ui_thread(self, app):
        app._polling_active = True
        app._core_sub = app.bus.subscribe()
        seen = []

        def call(fn, ev):
            seen.append(ev.type)
            app._polling_active = False

        app.call_from_thread = call
        app.bus.publish("backend_done", {"protocol": "x", "ok": False})
        app._poll_worker()
        assert seen == ["backend_done"]
        assert app._core_sub.closed
