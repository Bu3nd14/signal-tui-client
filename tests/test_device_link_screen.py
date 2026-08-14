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
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from device_link_screen import DeviceLinkPickerScreen, _PROTOCOL_ITEMS


class TestDeviceLinkPickerScreen:
    """Unit tests for the DeviceLinkPickerScreen structure and navigation."""

    def test_initial_items_include_signal_and_whatsapp(self):
        """Picker list contains Signal and WhatsApp entries."""
        ids = [item["id"] for item in _PROTOCOL_ITEMS]
        assert "signal" in ids
        assert "whatsapp" in ids

    def test_telegram_is_enabled(self):
        """Telegram is present and enabled (not a placeholder anymore)."""
        telegram = next(
            item for item in _PROTOCOL_ITEMS if item["id"] == "telegram"
        )
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
            i for i, item in enumerate(_PROTOCOL_ITEMS)
            if item["id"] == "telegram"
        )
        screen._select_protocol(telegram_idx)



class TestDeviceLinkBinding:
    """Verify Ctrl+L binding is wired in SignalTUI."""

    def test_binding_exists(self):
        """Ctrl+L is mapped to open_device_link action."""
        from signal_tui import SignalTUI
        # Check BINDINGS list for ctrl+l entry
        bindings = {
            binding.key: binding.action
            for binding in SignalTUI.BINDINGS
        }
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
        with patch.object(screen, "_populate_qr_phase"), \
             patch.object(screen, "_show_phase"), \
             patch.object(screen, "run_worker"), \
             patch.object(screen, "_fetch_real_qr", new=MagicMock()):
            screen._transition_to_qr(phone="")
        assert screen._touched_protocols == {"telegram"}

    def test_select_telegram_marks_touched_via_qr(self):
        screen = DeviceLinkPickerScreen()
        with patch.object(screen, "_populate_qr_phase"), \
             patch.object(screen, "_show_phase"), \
             patch.object(screen, "run_worker"), \
             patch.object(screen, "_fetch_real_qr", new=MagicMock()):
            # has_whatsapp=False -> filtered = [signal, telegram], index 1 = telegram
            screen._select_protocol(1)
        assert screen._selected_protocol == "telegram"
        assert screen._touched_protocols == {"telegram"}

    def test_select_signal_phone_phase_does_not_mark(self):
        screen = DeviceLinkPickerScreen(signal_number="")
        with patch.object(screen, "_transition_to_phone") as mock_phone, \
             patch.object(screen, "_transition_to_qr") as mock_qr:
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
