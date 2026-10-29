"""Tests for the WAHA session guard (issue #311): deterministic, no sleeps."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from protocols import config
from protocols.whatsapp_rest import WhatsAppRESTClient
from server import wa_session_guard as g
from server.bus import EventBus
from server.core import ChatCore
from server.link import LinkService

WORKING = {"status": "WORKING", "me": {"id": "1@c.us"}}
FAILED = {"status": "FAILED"}
QR = {"status": "SCAN_QR_CODE"}


class FakeBackend:
    protocol = "whatsapp"

    def __init__(self) -> None:
        self.infos: list = []
        self.default: dict | None = WORKING
        self.restarts = 0
        self.restart_result: object = True
        self.info_calls = 0

    def session_info(self):
        self.info_calls += 1
        return self.infos.pop(0) if self.infos else self.default

    def restart_session(self) -> bool:
        self.restarts += 1
        if isinstance(self.restart_result, Exception):
            raise self.restart_result
        return bool(self.restart_result)


class Env:
    def __init__(self, core: ChatCore, backend: FakeBackend, sub, clock: list[float]):
        self.core, self.backend, self.sub, self.clock = core, backend, sub, clock

    def tick(self, info, advance: float = 0.0) -> float:
        self.clock[0] += advance
        self.backend.default = info
        return self.core.wa_session_guard_tick()

    def statuses(self) -> list[tuple[str, bool]]:
        return [
            (e.payload["text"], e.payload["persistent"])
            for e in self.sub.drain()
            if e.type == "backend_status"
        ]


@pytest.fixture
def env(monkeypatch) -> Env:
    for key in (
        "WA_SESSION_GUARD",
        "WA_SESSION_POLL_S",
        "WA_SESSION_RESTART_TIMEOUT_S",
        "WA_SESSION_MAX_RESTARTS_PER_HOUR",
        "WA_SESSION_BACKOFF_S",
        "WA_SESSION_STABLE_RESET_S",
        "WA_SESSION_FAILED_GRACE_S",
        "WA_SESSION_UNREACHABLE_AFTER",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config, "_load_config", dict)
    bus = EventBus()
    with (
        patch("server.core.whatsapp_enabled", return_value=True),
        patch("server.core.telegram_enabled", return_value=False),
    ):
        core = ChatCore(bus)
    backend = FakeBackend()
    core.whatsapp_backend = backend  # type: ignore[assignment]
    core._spawn = MagicMock()  # type: ignore[method-assign]
    clock = [1000.0]
    core._wa_guard_clock = lambda: clock[0]
    return Env(core, backend, bus.subscribe(), clock)


def _fail_and_restart(env: Env) -> None:
    env.tick(FAILED)
    env.tick(FAILED, 10)


def test_failed_restarts_once_then_recovers(env):
    core = env.core
    env.tick(WORKING)  # UNKNOWN -> HEALTHY, no reconnect
    core._spawn.assert_not_called()
    env.tick(FAILED, 1)
    assert core._wa_guard_state == g.SUSPECT
    assert env.backend.restarts == 0
    env.tick(FAILED, 10)
    assert env.backend.restarts == 1
    assert core._wa_guard_state == g.RECOVERING
    assert core._wa_guard_attempts == 1
    assert env.statuses() == [("🔄 WAHA: sessione FAILED, riavvio (1/3)…", True)]
    env.tick({"status": "STARTING"}, 2)
    assert env.backend.restarts == 1
    env.tick(WORKING, 3)
    assert core._wa_guard_state == g.HEALTHY
    core._spawn.assert_called_once_with(core.connect_whatsapp, "core-connect-whatsapp")
    assert env.statuses() == [("✅ WAHA: sessione ripristinata", False)]
    assert core._wa_guard_attempts == 1
    env.tick(WORKING, 100)
    assert core._wa_guard_attempts == 1
    env.tick(WORKING, 600)
    assert core._wa_guard_attempts == 0


def test_qr_after_restart_needs_pairing_no_more_restarts(env):
    core = env.core
    _fail_and_restart(env)
    env.statuses()
    for _ in range(4):
        env.tick(QR, 60)
    assert core._wa_guard_state == g.NEEDS_PAIRING
    assert env.backend.restarts == 1
    assert env.statuses() == [
        ("📱 WhatsApp: ricollegamento necessario (QR) — Ctrl+L", True)
    ]


def test_backoff_cap_and_gave_up(env, monkeypatch):
    core = env.core
    monkeypatch.setenv("WA_SESSION_BACKOFF_S", "100")
    monkeypatch.setenv("WA_SESSION_MAX_RESTARTS_PER_HOUR", "2")
    _fail_and_restart(env)
    assert env.backend.restarts == 1
    # restart "failed" (FAILED again after RECOVERY_POLL_S) -> backoff 100s
    env.tick(FAILED, 6)
    assert core._wa_guard_state == g.SUSPECT
    env.tick(FAILED, 50)
    assert env.backend.restarts == 1  # still inside the backoff
    env.tick(FAILED, 60)
    assert env.backend.restarts == 2
    env.statuses()
    env.tick(FAILED, 6)  # restart failed again
    env.tick(FAILED, 101)  # backoff elapsed, but cap (2/h) reached
    assert env.backend.restarts == 2
    assert core._wa_guard_state == g.GAVE_UP
    assert env.statuses()[-1] == (
        "❌ WAHA: sessione FAILED, riavvio automatico sospeso (limite raggiunto)",
        True,
    )
    env.tick(FAILED, 500)
    assert env.backend.restarts == 2
    assert env.statuses() == []
    env.tick(WORKING, 1)
    assert core._wa_guard_state == g.HEALTHY


def test_gave_up_cleared_by_manual(env, monkeypatch):
    core = env.core
    monkeypatch.setenv("WA_SESSION_MAX_RESTARTS_PER_HOUR", "0")
    env.tick(FAILED)
    env.tick(FAILED, 10)
    assert core._wa_guard_state == g.GAVE_UP
    assert env.backend.restarts == 0
    core.note_wa_session_manual()
    assert core._wa_guard_state == g.UNKNOWN
    assert core._wa_guard_failed_since is None
    assert core._wa_guard_next_attempt_at == env.clock[0] + g.MANUAL_COOLDOWN_S


def test_restart_window_prunes_old_restarts(env, monkeypatch):
    monkeypatch.setenv("WA_SESSION_MAX_RESTARTS_PER_HOUR", "1")
    monkeypatch.setenv("WA_SESSION_BACKOFF_S", "1")
    _fail_and_restart(env)
    env.tick(WORKING, 1)
    env.tick(FAILED, g.RESTART_WINDOW_S)
    env.tick(FAILED, 10)
    assert env.backend.restarts == 2


@pytest.mark.parametrize("failure", [RuntimeError("boom"), False])
def test_restart_failure_counts_and_backs_off(env, failure):
    core = env.core
    env.backend.restart_result = failure
    _fail_and_restart(env)
    assert env.backend.restarts == 1
    assert core._wa_guard_attempts == 1
    assert core._wa_guard_state == g.SUSPECT
    assert core._wa_guard_next_attempt_at == env.clock[0] + 5
    env.tick(FAILED, 1)
    assert env.backend.restarts == 1
    env.tick(FAILED, 5)
    assert env.backend.restarts == 2
    assert core._wa_guard_next_attempt_at == env.clock[0] + 30


def test_restart_timeout_while_starting(env):
    core = env.core
    _fail_and_restart(env)
    env.tick({"status": "STARTING"}, 60)
    assert core._wa_guard_state == g.RECOVERING
    env.tick({"status": "STARTING"}, 61)
    assert core._wa_guard_state == g.SUSPECT


def test_unreachable_alerts_once_then_recovers(env):
    core = env.core
    env.tick(WORKING)
    env.statuses()
    for _ in range(8):
        env.backend.infos.append(None)
        env.clock[0] += 10
        core.wa_session_guard_tick()
    assert core._wa_guard_state == g.UNREACHABLE
    assert env.backend.restarts == 0
    assert env.statuses() == [("⚠️ WAHA: non raggiungibile", True)]
    env.tick(WORKING, 10)
    assert core._wa_guard_state == g.HEALTHY
    core._spawn.assert_called_once()


@pytest.mark.parametrize(
    ("info", "state"),
    [
        (QR, g.NEEDS_PAIRING),
        ({"status": "STARTING"}, g.UNKNOWN),
        ({"status": "STOPPED"}, g.STOPPED),
        ({"status": "WEIRD"}, g.UNKNOWN),
    ],
)
def test_non_restartable_states(env, info, state):
    for _ in range(3):
        env.tick(info, 100)
    assert env.backend.restarts == 0
    assert env.core._wa_guard_state == state
    texts = env.statuses()
    if state == g.STOPPED:
        assert texts == [("⏹️ WAHA: sessione ferma", True)]


def test_link_flow_active_blocks_restart(env):
    env.core.api.link.is_active = MagicMock(return_value=True)  # type: ignore[method-assign]
    env.tick(FAILED)
    env.tick(FAILED, 100)
    assert env.backend.restarts == 0
    assert env.core._wa_guard_state == g.UNKNOWN
    assert env.statuses() == []


def test_working_without_me_is_transient(env):
    env.tick({"status": "WORKING", "me": None})
    assert env.core._wa_guard_state == g.UNKNOWN


def test_single_flight(env):
    with env.core._wa_guard_lock:
        delay = env.core.wa_session_guard_tick()
    assert delay == 60.0
    assert env.backend.info_calls == 0
    assert env.backend.restarts == 0


class TestLifecycle:
    def test_thread_starts_and_stops(self, env):
        core = env.core
        calls = []
        core.wa_session_guard_tick = lambda: calls.append(1) or 0.01  # type: ignore[method-assign]
        core.start_wa_session_guard()
        thread = core._wa_guard_thread
        assert thread is not None
        assert thread.daemon
        assert thread.name == "wa-session-guard"
        core.start_wa_session_guard()  # idempotent
        assert core._wa_guard_thread is thread
        deadline = time.monotonic() + 1
        while not calls and time.monotonic() < deadline:
            time.sleep(0.005)
        assert calls
        core.stop_wa_session_guard()
        thread.join(1.0)
        assert not thread.is_alive()

    def test_loop_survives_tick_errors(self, env):
        core = env.core
        calls = []

        def boom() -> float:
            calls.append(1)
            core.stop_wa_session_guard()
            raise RuntimeError("x")

        core.wa_session_guard_tick = boom  # type: ignore[method-assign]
        core._wa_guard_stop.clear()
        t = threading.Thread(target=core._wa_session_guard_loop, daemon=True)
        t.start()
        t.join(1.0)
        assert not t.is_alive()
        assert calls == [1]

    def test_disabled_by_env(self, env, monkeypatch):
        monkeypatch.setenv("WA_SESSION_GUARD", "false")
        env.core.start_wa_session_guard()
        assert env.core._wa_guard_thread is None

    def test_no_backend(self, env):
        env.core.whatsapp_backend = None
        env.core.start_wa_session_guard()
        assert env.core._wa_guard_thread is None


class TestConfig:
    def test_defaults(self, env):
        assert config.get_wa_session_guard() is True
        assert config.get_wa_session_poll_s() == 60
        assert config.get_wa_session_restart_timeout_s() == 120
        assert config.get_wa_session_max_restarts_per_hour() == 3
        assert config.get_wa_session_backoff_s() == [5, 30, 120, 300]
        assert config.get_wa_session_stable_reset_s() == 600
        assert config.get_wa_session_failed_grace_s() == 10
        assert config.get_wa_session_unreachable_after() == 5

    @pytest.mark.parametrize(
        ("getter", "var", "bad", "fallback"),
        [
            ("get_wa_session_poll_s", "WA_SESSION_POLL_S", "0", 60),
            (
                "get_wa_session_restart_timeout_s",
                "WA_SESSION_RESTART_TIMEOUT_S",
                "-1",
                120,
            ),
            (
                "get_wa_session_max_restarts_per_hour",
                "WA_SESSION_MAX_RESTARTS_PER_HOUR",
                "-2",
                3,
            ),
            ("get_wa_session_stable_reset_s", "WA_SESSION_STABLE_RESET_S", "0", 600),
            ("get_wa_session_failed_grace_s", "WA_SESSION_FAILED_GRACE_S", "-1", 10),
            (
                "get_wa_session_unreachable_after",
                "WA_SESSION_UNREACHABLE_AFTER",
                "0",
                5,
            ),
            ("get_wa_session_poll_s", "WA_SESSION_POLL_S", "abc", 60),
        ],
    )
    def test_invalid_falls_back(self, env, monkeypatch, getter, var, bad, fallback):
        monkeypatch.setenv(var, bad)
        assert getattr(config, getter)() == fallback

    def test_zero_valid_where_allowed(self, env, monkeypatch):
        monkeypatch.setenv("WA_SESSION_MAX_RESTARTS_PER_HOUR", "0")
        monkeypatch.setenv("WA_SESSION_FAILED_GRACE_S", "0")
        assert config.get_wa_session_max_restarts_per_hour() == 0
        assert config.get_wa_session_failed_grace_s() == 0

    @pytest.mark.parametrize("raw", ["a,b", "5,-1", "5,0", "5,,6"])
    def test_backoff_invalid(self, env, monkeypatch, raw):
        monkeypatch.setenv("WA_SESSION_BACKOFF_S", raw)
        assert config.get_wa_session_backoff_s() == [5, 30, 120, 300]

    def test_backoff_valid(self, env, monkeypatch):
        monkeypatch.setenv("WA_SESSION_BACKOFF_S", "1, 2")
        assert config.get_wa_session_backoff_s() == [1, 2]

    @pytest.mark.parametrize(
        ("raw", "default", "expected"),
        [
            (" OFF ", True, False),
            ("0", True, False),
            ("No", True, False),
            ("false", True, False),
            ("On", False, True),
            ("1", False, True),
            ("maybe", True, True),
            ("maybe", False, False),
        ],
    )
    def test_get_bool(self, env, monkeypatch, raw, default, expected):
        monkeypatch.setenv("X_FLAG", raw)
        assert config._get_bool("x_flag", "X_FLAG", default) is expected


class TestCoreIntegration:
    def test_start_and_stop_drive_the_guard(self, env):
        core = env.core
        core.start_wa_session_guard = MagicMock()  # type: ignore[method-assign]
        core.stop_wa_session_guard = MagicMock()  # type: ignore[method-assign]
        with patch("server.core.signal_enabled", return_value=False):
            core.start()
        core.start_wa_session_guard.assert_called_once_with()
        core.stop()
        core.stop_wa_session_guard.assert_called_once_with()

    def test_reconnect_notes_manual(self, env):
        core = env.core
        core.note_wa_session_manual = MagicMock()  # type: ignore[method-assign]
        core.reconnect({"whatsapp"})
        core.note_wa_session_manual.assert_called_once_with()
        core._spawn.assert_called_once_with(
            core.connect_whatsapp, "core-connect-whatsapp"
        )

    def test_reconnect_other_protocol_does_not_note(self, env):
        env.core.note_wa_session_manual = MagicMock()  # type: ignore[method-assign]
        env.core.reconnect({"signal"})
        env.core.note_wa_session_manual.assert_not_called()


class TestRestSurface:
    def test_rest_restart_session_posts(self):
        client = WhatsAppRESTClient("http://api.test")
        with patch.object(client, "_request", return_value={"ok": 1}) as req:
            assert client.restart_session() == {"ok": 1}
        req.assert_called_once_with(
            "POST", f"/api/sessions/{client.session_name}/restart"
        )

    def test_backend_restart_session(self):
        from protocols.whatsapp import WhatsAppBackend

        backend = WhatsAppBackend(api_url="http://api.test", media_dir="")
        backend._rest = MagicMock()
        backend._rest.restart_session.return_value = None
        backend._rest.last_status = 201
        assert backend.restart_session() is True
        backend._rest.last_status = 500
        assert backend.restart_session() is False
        backend._rest.get_session_status.return_value = {"status": "FAILED"}
        assert backend.session_info() == {"status": "FAILED"}
        backend._rest.get_session_status.return_value = None
        assert backend.session_info() is None

    def test_link_is_active(self):
        svc = LinkService(lambda: MagicMock())
        assert svc.is_active("whatsapp") is False
        svc._sessions["whatsapp"] = MagicMock()
        assert svc.is_active("whatsapp") is True
