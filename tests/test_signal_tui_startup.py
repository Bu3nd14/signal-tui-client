"""``signal_tui.main`` startup: attach / spawn / --connect / --in-process."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import signal_tui
from server.attach import Attached, AttachError


@pytest.fixture
def harness(monkeypatch, tmp_path):
    h = SimpleNamespace()
    h.client = MagicMock(name="client")
    h.attach = MagicMock(return_value=Attached(h.client, True, tmp_path / "s.sock"))
    h.app = MagicMock(name="app")
    h.app_cls = MagicMock(return_value=h.app)
    h.acquire = MagicMock(return_value=True)
    h.release = MagicMock()
    monkeypatch.setattr(signal_tui, "attach_or_spawn", h.attach)
    monkeypatch.setattr(signal_tui, "SignalTUI", h.app_cls)
    monkeypatch.setattr(signal_tui, "_acquire_lock", h.acquire)
    monkeypatch.setattr(signal_tui, "_release_lock", h.release)
    monkeypatch.setattr(signal_tui, "web_enabled", lambda: False)
    monkeypatch.setattr(
        signal_tui, "detect_image_support", lambda **_: signal_tui.ImageSupport.CATIMG
    )
    monkeypatch.setattr(signal_tui.logging, "basicConfig", MagicMock())
    monkeypatch.setattr("signal.signal", MagicMock())
    return h


def test_default_attaches_or_spawns_and_never_takes_the_lock(harness):
    assert signal_tui.main([]) == 0
    kwargs = harness.attach.call_args.kwargs
    assert kwargs["connect_path"] is None
    assert kwargs["serve_args"] == ["--no-web"]
    harness.acquire.assert_not_called()
    harness.release.assert_not_called()
    app_kwargs = harness.app_cls.call_args.kwargs
    assert app_kwargs["client"] is harness.client
    assert app_kwargs["server_spawned"] is True
    assert app_kwargs["web_adapter"] is None and app_kwargs["web_enabled"] is False
    harness.app.run.assert_called_once()
    harness.client.close.assert_called_once()  # detach only: the server lives on


def test_attached_tui_records_spawned_false(harness, tmp_path):
    harness.attach.return_value = Attached(harness.client, False, tmp_path / "s")
    assert signal_tui.main([]) == 0
    assert harness.app_cls.call_args.kwargs["server_spawned"] is False


def test_connect_passes_the_path(harness):
    assert signal_tui.main(["--connect", "/tmp/x.sock"]) == 0
    assert harness.attach.call_args.kwargs["connect_path"] == "/tmp/x.sock"
    harness.acquire.assert_not_called()


def test_web_flags_are_passed_through_to_serve(harness):
    signal_tui.main(
        ["--web", "--web-port", "5000", "--web-host", "0.0.0.0", "--web-no-auth"]
        + ["--debug"]
    )
    assert harness.attach.call_args.kwargs["serve_args"] == [
        "--web-port",
        "5000",
        "--web-host",
        "0.0.0.0",
        "--web-no-auth",
        "--debug",
    ]
    # The TUI process itself never embeds the web adapter.
    assert harness.app_cls.call_args.kwargs["web_adapter"] is None


def test_config_enabled_web_is_served_by_the_spawned_server(harness, monkeypatch):
    monkeypatch.setattr(signal_tui, "web_enabled", lambda: True)
    signal_tui.main([])
    assert harness.attach.call_args.kwargs["serve_args"] == []


def test_web_flags_are_ignored_with_a_note_when_attaching(harness, tmp_path, capsys):
    harness.attach.return_value = Attached(harness.client, False, tmp_path / "s")
    signal_tui.main(["--web"])
    assert "web options are ignored" in capsys.readouterr().err


def test_attach_failure_exits_1_with_the_message(harness, capsys):
    harness.attach.side_effect = AttachError("server exited\nlog tail here")
    assert signal_tui.main([]) == 1
    err = capsys.readouterr().err
    assert "server exited" in err and "log tail here" in err
    harness.app_cls.assert_not_called()


def test_ctrl_c_while_waiting_for_the_server_exits_quietly(harness):
    harness.attach.side_effect = KeyboardInterrupt
    assert signal_tui.main([]) == 130
    harness.app_cls.assert_not_called()


def test_in_process_owns_the_core_and_the_lock(harness):
    assert signal_tui.main(["--in-process"]) == 0
    harness.attach.assert_not_called()
    harness.acquire.assert_called_once()
    harness.release.assert_called_once()
    kwargs = harness.app_cls.call_args.kwargs
    assert kwargs["client"] is None and kwargs["server_spawned"] is None


def test_in_process_refuses_when_the_lock_is_held(harness, capsys):
    harness.acquire.return_value = False
    assert signal_tui.main(["--in-process"]) == 1
    assert "already running" in capsys.readouterr().err
    harness.app_cls.assert_not_called()


def test_connect_and_in_process_are_exclusive(harness):
    with pytest.raises(SystemExit) as exc:
        signal_tui.main(["--connect", "/x", "--in-process"])
    assert exc.value.code == 2


def test_client_modes_use_their_own_log_file(harness, monkeypatch):
    handler = MagicMock()
    signal_tui.logging.basicConfig.reset_mock()
    import logging

    # A fresh process has no root handlers (pytest adds its own capture ones);
    # ``main`` only opens its log file when basicConfig would use it.
    monkeypatch.setattr(logging.getLogger(), "handlers", [])

    created = []
    orig = logging.FileHandler

    def fh(path, mode="a", **kw):
        created.append(path)
        return handler

    logging.FileHandler = fh  # type: ignore[assignment]
    try:
        signal_tui.main([])
        signal_tui.main(["--in-process"])
    finally:
        logging.FileHandler = orig  # type: ignore[assignment]
    assert created == [signal_tui.TUI_LOG_FILE, signal_tui.CORE_LOG_FILE]


def test_lock_file_untouched_by_a_real_attached_start(harness, monkeypatch, tmp_path):
    """With the real lock functions the lock file never appears."""
    import server.lock as lock_mod

    lock_file = tmp_path / "real.lock"
    monkeypatch.setattr(lock_mod, "LOCK_FILE", str(lock_file))
    monkeypatch.setattr(signal_tui, "_acquire_lock", lock_mod._acquire_lock)
    monkeypatch.setattr(signal_tui, "_release_lock", lock_mod._release_lock)
    signal_tui.main([])
    assert not lock_file.exists()
