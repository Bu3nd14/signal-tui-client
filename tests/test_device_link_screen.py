"""
Tests for ``device_link_screen.py`` — UI structure and navigation.

Covers:
- Initial protocol list rendering (Signal, WhatsApp, Telegram placeholder)
- Phone input phase display (with pre-filled number from config)
- QR code generation (fake data for UI testing)
- Navigation: Esc to dismiss, Enter to select, Back button
- Telegram disabled item cannot be selected
"""

from __future__ import annotations

import sys
from asyncio import run
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from textual.containers import Vertical
from textual.widgets import Input, ListView

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from device_link_screen import _PROTOCOL_ITEMS, DeviceLinkPickerScreen


class TestDeviceLinkPickerScreen:
    """Unit tests for the DeviceLinkPickerScreen structure and navigation."""

    def test_initial_items_include_signal_and_whatsapp(self):
        """Picker list contains Signal and WhatsApp entries."""
        ids = [item["id"] for item in _PROTOCOL_ITEMS]
        assert "signal" in ids
        assert "whatsapp" in ids

    def test_telegram_is_enabled(self):
        """Telegram is present and enabled (not a placeholder anymore)."""
        telegram = next(item for item in _PROTOCOL_ITEMS if item["id"] == "telegram")
        assert telegram["disabled"] is False
        assert "📨" in telegram["label"]

    def test_screen_init_defaults(self):
        """Screen initialises with default values and phase = picker."""
        screen = DeviceLinkPickerScreen()
        assert screen._phase == "picker"
        assert screen._selected_protocol == ""
        assert screen._signal_number == ""
        assert screen._has_whatsapp is False
        assert screen._force_phone_input is False

    def test_screen_init_with_signal_number(self):
        """Pre-filled phone number from config is stored."""
        screen = DeviceLinkPickerScreen(signal_number="+393331234567")
        assert screen._signal_number == "+393331234567"

    def test_screen_init_with_whatsapp_enabled(self):
        """has_whatsapp flag is propagated."""
        screen = DeviceLinkPickerScreen(has_whatsapp=True)
        assert screen._has_whatsapp is True

    def test_force_phone_input_flag(self):
        """force_phone_input test hook is stored."""
        screen = DeviceLinkPickerScreen(force_phone_input=True)
        assert screen._force_phone_input is True

    def test_should_show_phone_input_when_forced(self):
        """With force_phone_input=True, phone phase is always shown."""
        screen = DeviceLinkPickerScreen(force_phone_input=True)
        assert screen._should_show_phone_input("signal") is True
        assert screen._should_show_phone_input("whatsapp") is True
        assert screen._should_show_phone_input("anyprotocol") is True

    def test_should_show_phone_signal_without_number(self):
        """Signal without pre-filled number: show phone input."""
        screen = DeviceLinkPickerScreen(signal_number="")
        assert screen._should_show_phone_input("signal") is True

    def test_should_skip_phone_signal_with_number(self):
        """Signal with pre-filled number: skip phone input."""
        screen = DeviceLinkPickerScreen(signal_number="+39123456")
        assert screen._should_show_phone_input("signal") is False

    def test_should_skip_phone_whatsapp(self):
        """WhatsApp never needs phone input."""
        screen = DeviceLinkPickerScreen()
        assert screen._should_show_phone_input("whatsapp") is False

    def test_should_skip_phone_telegram(self):
        """Telegram never needs phone input."""
        screen = DeviceLinkPickerScreen()
        assert screen._should_show_phone_input("telegram") is False

    def test_get_qr_data_signal(self):
        """Signal QR data is a fake sgnl:// URL."""
        screen = DeviceLinkPickerScreen()
        screen._selected_protocol = "signal"
        screen._device_name = "TestDevice"
        data = screen._get_qr_data("+39123456")
        assert data.startswith("sgnl://linkdevice?uuid=test-")
        assert "+39123456" in data
        assert "TestDevice" in data

    def test_get_qr_data_whatsapp(self):
        """WhatsApp QR data is the fake pairing string."""
        screen = DeviceLinkPickerScreen()
        screen._selected_protocol = "whatsapp"
        data = screen._get_qr_data("")
        assert data == "WA:fake-pairing-code-12345-for-ui-testing"

    def test_select_protocol_out_of_range_is_noop(self):
        """Selecting an invalid index does nothing."""
        screen = DeviceLinkPickerScreen()
        screen._select_protocol(-1)
        assert screen._selected_protocol == ""
        screen._select_protocol(999)
        assert screen._selected_protocol == ""

    def test_select_disabled_item_is_noop(self):
        """Selecting the disabled Telegram item does nothing."""
        screen = DeviceLinkPickerScreen()
        # Telegram is index 2 in the full list
        telegram_idx = next(
            i for i, item in enumerate(_PROTOCOL_ITEMS) if item["id"] == "telegram"
        )
        screen._select_protocol(telegram_idx)


class TestDeviceLinkBinding:
    """Verify Ctrl+L binding is wired in SignalTUI."""

    def test_binding_exists(self):
        """Ctrl+L is mapped to open_device_link action."""
        from signal_tui import SignalTUI

        # Check BINDINGS list for ctrl+l entry
        bindings = {binding.key: binding.action for binding in SignalTUI.BINDINGS}
        assert "ctrl+l" in bindings
        assert bindings["ctrl+l"] == "open_device_link"

    def test_action_method_exists(self):
        """action_open_device_link exists on SignalTUI."""
        from signal_tui import SignalTUI

        assert hasattr(SignalTUI, "action_open_device_link")
        assert callable(SignalTUI.action_open_device_link)

    def test_open_device_link_method_exists(self):
        """_open_device_link exists and calls push_screen."""
        from signal_tui import SignalTUI

        assert hasattr(SignalTUI, "_open_device_link")
        assert callable(SignalTUI._open_device_link)


class TestDeviceLinkTouchedTracking:
    """📌 Lo screen traccia i protocolli il cui flusso QR è stato avviato."""

    def test_init_touched_empty(self):
        screen = DeviceLinkPickerScreen()
        assert screen._touched_protocols == set()

    def test_transition_to_qr_marks_touched(self):
        screen = DeviceLinkPickerScreen()
        screen._selected_protocol = "telegram"
        with (
            patch.object(screen, "_populate_qr_phase"),
            patch.object(screen, "_show_phase"),
            patch.object(screen, "run_worker"),
            patch.object(screen, "_fetch_real_qr", new=MagicMock()),
        ):
            screen._transition_to_qr(phone="")
        assert screen._touched_protocols == {"telegram"}

    def test_select_telegram_marks_touched_via_qr(self):
        screen = DeviceLinkPickerScreen()
        with (
            patch.object(screen, "_populate_qr_phase"),
            patch.object(screen, "_show_phase"),
            patch.object(screen, "run_worker"),
            patch.object(screen, "_fetch_real_qr", new=MagicMock()),
        ):
            # has_whatsapp=False -> filtered = [signal, telegram], index 1 = telegram
            screen._select_protocol(1)
        assert screen._selected_protocol == "telegram"
        assert screen._touched_protocols == {"telegram"}

    def test_select_signal_phone_phase_does_not_mark(self):
        screen = DeviceLinkPickerScreen(signal_number="")
        with (
            patch.object(screen, "_transition_to_phone") as mock_phone,
            patch.object(screen, "_transition_to_qr") as mock_qr,
        ):
            screen._select_protocol(0)  # signal -> phone phase (no number)
        mock_phone.assert_called_once()
        mock_qr.assert_not_called()
        assert screen._touched_protocols == set()


class TestDeviceLinkReconnectWiring:
    """🔌 _open_device_link passa i protocolli toccati a _reconnect_touched_backends."""

    def test_open_device_link_reconnects_only_touched(self):
        from signal_tui import SignalTUI

        app = SignalTUI()
        fake_screen = MagicMock()
        fake_screen._touched_protocols = {"telegram"}
        app.push_screen = MagicMock()
        app._reconnect_touched_backends = MagicMock()
        with patch("tui.pickers.DeviceLinkPickerScreen", return_value=fake_screen):
            app._open_device_link()
        callback = app.push_screen.call_args[0][1]
        callback(None)
        app._reconnect_touched_backends.assert_called_once_with({"telegram"})


class TestDeviceLinkScreenFlows:
    @pytest.mark.integration
    async def test_mount_populates_picker_and_phase_visibility(self, app_for_test):
        screen = DeviceLinkPickerScreen(has_whatsapp=True, has_telegram=True)
        async with app_for_test.run_test() as pilot:
            await app_for_test.push_screen(screen)
            await pilot.pause()
            picker = screen.query_one("#link-picker-container", Vertical)
            phone = screen.query_one("#link-phone-container", Vertical)
            qr = screen.query_one("#link-qr-container", Vertical)
            protocols = screen.query_one("#link-protocol-list", ListView)
            assert picker.display is True
            assert phone.display is False
            assert qr.display is False
            assert len(protocols.children) == 3
            screen._show_phase("phone")
            assert phone.display is True
            assert picker.display is False

    @pytest.mark.integration
    async def test_phone_transition_and_handlers(self, app_for_test):
        screen = DeviceLinkPickerScreen(signal_number="", force_phone_input=True)
        async with app_for_test.run_test() as pilot:
            await app_for_test.push_screen(screen)
            await pilot.pause()
            screen._selected_protocol = "signal"
            screen._transition_to_phone()
            assert screen._phase == "phone"
            phone = screen.query_one("#link-phone-input", Input)
            device = screen.query_one("#link-device-input", Input)
            phone.value = "+39123"
            device.value = "Test device"
            screen._transition_to_qr = MagicMock()
            screen._on_phone_confirm()
            screen._transition_to_qr.assert_called_once_with("+39123")
            screen._on_go_back()
            assert screen._phase == "picker"

    def test_select_protocol_and_event_handlers(self):
        screen = DeviceLinkPickerScreen(
            signal_number="", has_whatsapp=True, has_telegram=True
        )
        screen._transition_to_phone = MagicMock()
        screen._transition_to_qr = MagicMock()
        screen._select_protocol(1)
        screen._transition_to_qr.assert_called_once_with(phone="")
        screen._selected_protocol = "signal"
        screen._phase = "picker"
        list_view = MagicMock(index=0)
        screen.query_one = MagicMock(return_value=list_view)
        screen._select_protocol = MagicMock()
        screen.on_list_view_selected(MagicMock())
        screen._select_protocol.assert_called_once_with(0)
        screen._on_phone_confirm = MagicMock()
        screen._on_go_back = MagicMock()
        screen.dismiss = MagicMock()
        for button_id in ("link-phone-start", "link-phone-back", "link-qr-cancel"):
            screen.on_button_pressed(MagicMock(button=MagicMock(id=button_id)))
        screen._on_phone_confirm.assert_called_once()
        screen._on_go_back.assert_called_once()
        screen.dismiss.assert_called_once_with(None)

    def test_async_qr_dispatch_and_completion_checks(self):
        screen = DeviceLinkPickerScreen()
        screen._get_signal_link_url = AsyncMock(return_value="signal-url")
        screen._selected_protocol = "signal"
        assert run(screen._get_qr_data_async("")) == "signal-url"
        screen._get_whatsapp_qr = AsyncMock(return_value="ascii")
        screen._selected_protocol = "whatsapp"
        assert run(screen._get_qr_data_async("")) == "ASCII:ascii"
        screen._get_telegram_qr_link = AsyncMock(return_value="telegram-url")
        screen._selected_protocol = "telegram"
        assert run(screen._get_qr_data_async("")) == "telegram-url"

        screen._linking_proc = MagicMock(poll=MagicMock(return_value=None))
        assert run(screen._check_signal_done()) is False
        screen._linking_proc.poll.return_value = 0
        assert run(screen._check_signal_done()) is True

    def test_fetch_qr_and_telegram_2fa(self):
        screen = DeviceLinkPickerScreen()
        code = MagicMock()
        status = MagicMock()
        screen.query_one = MagicMock(side_effect=[code, status])
        screen.run_worker = MagicMock(side_effect=lambda coro, **kwargs: coro.close())
        screen._get_qr_data_async = AsyncMock(return_value="ASCII:QR")
        run(screen._fetch_real_qr(""))
        code.update.assert_called_once_with("QR")

        tb = MagicMock()
        screen.query_one = MagicMock(return_value=MagicMock(value="secret"))
        with patch.object(
            DeviceLinkPickerScreen,
            "app",
            new_callable=PropertyMock,
            return_value=SimpleNamespace(telegram_backend=tb),
        ):
            screen._on_2fa_submit()
        assert screen.run_worker.call_count == 2

    def test_whatsapp_and_telegram_qr_helpers(self):
        screen = DeviceLinkPickerScreen()
        wa = SimpleNamespace(_rest=MagicMock())
        wa._rest.get_session_status.return_value = {"status": "working"}
        with patch.object(
            DeviceLinkPickerScreen,
            "app",
            new_callable=PropertyMock,
            return_value=SimpleNamespace(whatsapp_backend=wa),
        ):
            assert run(screen._get_whatsapp_qr()).startswith("INFO:")

        tb = MagicMock()
        tb.get_pairing_qr.return_value = "tg-url"
        with patch.object(
            DeviceLinkPickerScreen,
            "app",
            new_callable=PropertyMock,
            return_value=SimpleNamespace(telegram_backend=tb),
        ):
            assert run(screen._get_telegram_qr_link()) == "tg-url"

    def test_qr_populator_polling_and_fetch_paths(self):
        screen = DeviceLinkPickerScreen()
        screen._selected_protocol = "signal"
        container = MagicMock()
        code, status = MagicMock(), MagicMock()
        screen.query_one = MagicMock(side_effect=[container, code, status])
        with patch("device_link_screen.Center", return_value=MagicMock()):
            screen._populate_qr_phase("QR", "+391")
        assert container.mount.call_count >= 5

        screen._phase = "qr"
        screen._check_signal_done = AsyncMock(return_value=True)
        screen.query_one = MagicMock(side_effect=[status, code])
        screen.dismiss = MagicMock()
        with patch("asyncio.sleep", AsyncMock()):
            run(screen._poll_completion(""))
        screen.dismiss.assert_called_once_with(None)

        screen.query_one = MagicMock(side_effect=[code, status])
        screen.run_worker = MagicMock(side_effect=lambda coro, **kwargs: coro.close())
        screen._get_qr_data_async = AsyncMock(return_value="signal-url")
        with patch("device_link_screen.qr_to_ascii", return_value="ASCII"):
            run(screen._fetch_real_qr(""))
        code.update.assert_called_with("ASCII")

        screen.query_one = MagicMock(side_effect=[code, status])
        screen._get_qr_data_async = AsyncMock(side_effect=RuntimeError("bad qr"))
        run(screen._fetch_real_qr(""))
        assert "❌ bad qr" in code.update.call_args.args[0]

    def test_whatsapp_refresh_telegram_state_and_qr_variants(self):
        screen = DeviceLinkPickerScreen()
        status, code = MagicMock(), MagicMock()
        screen._qr_start_time = 0
        wa = SimpleNamespace(_rest=MagicMock())
        wa._rest.get_session_status.return_value = {"status": "scan_qr"}
        screen._get_whatsapp_qr_fresh = AsyncMock(return_value="INFO:fresh")
        with (
            patch.object(
                DeviceLinkPickerScreen,
                "app",
                new_callable=PropertyMock,
                return_value=SimpleNamespace(whatsapp_backend=wa),
            ),
            patch("device_link_screen.time.time", return_value=100),
        ):
            screen.query_one = MagicMock(return_value=code)
            assert run(screen._check_whatsapp_done()) is False
        code.update.assert_called_once()

        tb = MagicMock(_connected=False, _needs_2fa=True)
        container = MagicMock()
        screen._qr_start_time = 100
        with (
            patch.object(
                DeviceLinkPickerScreen,
                "app",
                new_callable=PropertyMock,
                return_value=SimpleNamespace(telegram_backend=tb),
            ),
            patch("device_link_screen.time.time", return_value=100),
        ):
            screen.query_one = MagicMock(side_effect=[status, container])
            assert run(screen._check_telegram_done()) is False
        status.update.assert_called_once()

        wa._rest.get_session_status.return_value = {"status": "pending"}
        wa._rest.get_pairing_qr.return_value = "current-qr"
        with patch.object(
            DeviceLinkPickerScreen,
            "app",
            new_callable=PropertyMock,
            return_value=SimpleNamespace(whatsapp_backend=wa),
        ):
            assert run(screen._get_whatsapp_qr()) == "current-qr"
        wa._rest.get_session_status.return_value = {"status": "failed"}
        wa._rest.get_fresh_pairing_qr.return_value = "fresh-qr"
        with patch.object(
            DeviceLinkPickerScreen,
            "app",
            new_callable=PropertyMock,
            return_value=SimpleNamespace(whatsapp_backend=wa),
        ):
            assert run(screen._get_whatsapp_qr()) == "fresh-qr"

    def test_complete_2fa_and_dismiss_cleanup(self):
        screen = DeviceLinkPickerScreen()
        tb = MagicMock()
        tb.complete_2fa.return_value = False
        status, inp = MagicMock(), MagicMock()
        screen.query_one = MagicMock(side_effect=[status, inp])
        with patch("asyncio.sleep", AsyncMock()):
            run(screen._complete_2fa_worker(tb, "wrong"))
        inp.focus.assert_called_once()

        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 123
        screen._linking_proc = proc
        with patch("textual.screen.Screen.dismiss") as dismiss:
            screen.dismiss(None)
        proc.terminate.assert_called_once()
        dismiss.assert_called_once()
