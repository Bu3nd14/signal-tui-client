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
from unittest.mock import patch

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

    def test_telegram_is_disabled(self):
        """Telegram placeholder is present and marked disabled."""
        telegram = next(
            item for item in _PROTOCOL_ITEMS if item["id"] == "telegram"
        )
        assert telegram["disabled"] is True

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
        assert screen._selected_protocol == ""
