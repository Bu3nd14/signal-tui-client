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
    app._web_adapter = None
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
    app._web_adapter = MagicMock()
    app.exit = MagicMock()  # type: ignore[method-assign]
    core.stop = MagicMock()  # type: ignore[method-assign]
    with patch("protocols.db._prune_cache"):
        app.shutdown_from_signal()
    app._web_adapter.stop.assert_called_once_with(app._web_server)


def test_shutdown_stops_the_transcription_service_after_the_adapters(core):
    calls: list[str] = []
    _record(core, calls)
    service = MagicMock()
    service.stop.side_effect = lambda: calls.append("transcription")
    core.api.transcription.service = service
    with patch("protocols.db._prune_cache", side_effect=lambda: calls.append("prune")):
        core.shutdown(lambda: calls.append("adapters"))
    assert calls == ["stop", "adapters", "transcription", "telegram", "prune"]
    assert core.api.transcription.service is None


def test_start_initializes_the_transcription_service(core):
    core._start_pump = MagicMock()  # type: ignore[method-assign]
    core.start_address_book_refresh = MagicMock()  # type: ignore[method-assign]
    core._spawn = MagicMock()  # type: ignore[method-assign]
    core._done = MagicMock()  # type: ignore[method-assign]
    core.api.transcription.init = MagicMock()  # type: ignore[method-assign]
    with patch("server.core.signal_enabled", return_value=False):
        core.start()
    core.api.transcription.init.assert_called_once_with()


def test_a_failing_transcription_init_does_not_stop_the_core(core):
    core._start_pump = MagicMock()  # type: ignore[method-assign]
    core.start_address_book_refresh = MagicMock()  # type: ignore[method-assign]
    core._spawn = MagicMock()  # type: ignore[method-assign]
    core._done = MagicMock()  # type: ignore[method-assign]
    core.api.transcription.init = MagicMock(side_effect=RuntimeError("no key"))  # type: ignore[method-assign]
    with patch("server.core.signal_enabled", return_value=False):
        core.start()
    core._done.assert_called()


def test_shutdown_closes_the_api_resources_after_the_adapters(core):
    """The Gmail IMAP pool is closed once the adapters can no longer call it."""
    calls: list[str] = []
    _record(core, calls)
    core.api.close_resources = lambda: calls.append("resources")  # type: ignore[method-assign]
    with patch("protocols.db._prune_cache", side_effect=lambda: calls.append("prune")):
        core.shutdown(lambda: calls.append("adapters"))
    assert calls == ["stop", "adapters", "resources", "telegram", "prune"]


def test_close_resources_closes_the_imap_pool_and_wipes_blobs(core):
    from server import gmail

    blob = gmail.write_blob(b"secret attachment")
    with (
        patch.object(gmail, "close_imap_pool") as close_pool,
        patch("protocols.db._prune_cache"),
    ):
        core.shutdown()
        core.api.close_resources()  # idempotent
    close_pool.assert_called()
    assert not blob.exists()


def test_a_failing_resource_close_does_not_skip_the_rest(core):
    from server import gmail

    calls: list[str] = []
    telegram = _record(core, calls)
    with (
        patch.object(gmail, "close_imap_pool", side_effect=RuntimeError("pool")),
        patch("protocols.db._prune_cache", side_effect=lambda: calls.append("prune")),
    ):
        core.shutdown()
    telegram.disconnect_sync.assert_called_once()
    assert calls == ["stop", "telegram", "prune"]
