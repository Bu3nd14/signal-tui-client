"""``ChatCore.shutdown``: one orderly, idempotent exit for every frontend."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from server.bus import EventBus
from server.core import ChatCore


@pytest.fixture
def core():
    with (
        patch("server.core.BackendManager"),
        patch("server.core.SignalBackend", return_value=MagicMock(protocol="signal")),
        patch("server.core.whatsapp_enabled", return_value=False),
        patch("server.core.telegram_enabled", return_value=False),
    ):
        yield ChatCore(EventBus())


def _record(core: ChatCore, calls: list[str]) -> MagicMock:
    telegram = MagicMock()
    telegram.disconnect_sync.side_effect = lambda: calls.append("telegram")
    core.telegram_backend = telegram
    core.stop = lambda: calls.append("stop")  # type: ignore[method-assign]
    return telegram


def test_order_stop_adapters_telegram_prune(core):
    calls: list[str] = []
    _record(core, calls)
    with patch("protocols.db._prune_cache", side_effect=lambda: calls.append("prune")):
        core.shutdown(lambda: calls.append("adapters"))
    assert calls == ["stop", "adapters", "telegram", "prune"]


def test_runs_once(core):
    calls: list[str] = []
    telegram = _record(core, calls)
    with patch("protocols.db._prune_cache") as prune:
        core.shutdown()
        core.shutdown(lambda: calls.append("late-adapters"))
    assert calls == ["stop", "telegram"]
    telegram.disconnect_sync.assert_called_once()
    prune.assert_called_once_with()


def test_each_step_is_best_effort(core):
    calls: list[str] = []
    telegram = _record(core, calls)
    core.stop = MagicMock(side_effect=RuntimeError("stop"))  # type: ignore[method-assign]
    telegram.disconnect_sync.side_effect = RuntimeError("tg")

    def adapters() -> None:
        calls.append("adapters")
        raise RuntimeError("web")

    with patch("protocols.db._prune_cache") as prune:
        core.shutdown(adapters)
    assert calls == ["adapters"]
    prune.assert_called_once_with()


def test_without_telegram_still_prunes(core):
    core.telegram_backend = None
    core.stop = MagicMock()  # type: ignore[method-assign]
    with patch("protocols.db._prune_cache") as prune:
        core.shutdown()
    core.stop.assert_called_once()
    prune.assert_called_once_with()


def test_sigint_shuts_the_core_down_once(core):
    """The SIGINT path shuts down; ``on_exit_app`` then finds nothing to do."""
    from tui.app import SignalTUI

    app = SignalTUI.__new__(SignalTUI)
    app.core = core
    app.client = MagicMock()
    app._hires_executor = MagicMock()
    app._web_enabled = False
    app.exit = MagicMock()  # type: ignore[method-assign]
    core.stop = MagicMock()  # type: ignore[method-assign]
    with patch("protocols.db._prune_cache") as prune:
        app.shutdown_from_signal()
        app.on_exit_app()
    core.stop.assert_called_once()
    prune.assert_called_once_with()
    app.exit.assert_called_once()
    assert app._polling_active is False


def test_sigint_stops_the_web_adapter(core):
    from tui.app import SignalTUI

    app = SignalTUI.__new__(SignalTUI)
    app.core = core
    app._web_enabled = True
    app._web_server = object()
    app.exit = MagicMock()  # type: ignore[method-assign]
    core.stop = MagicMock()  # type: ignore[method-assign]
    with (
        patch("protocols.db._prune_cache"),
        patch("web.server.stop_web_server") as stop_web,
    ):
        app.shutdown_from_signal()
    stop_web.assert_called_once_with(app._web_server)
