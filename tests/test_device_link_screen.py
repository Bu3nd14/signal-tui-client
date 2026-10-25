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

import base64
import itertools
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
from server.client import ApiError


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

    def test_unavailable_protocols_explain_required_setup(self):
        screen = DeviceLinkPickerScreen()

        assert screen._protocol_unavailable_reason("whatsapp") == (
            "start or configure WAHA"
        )
        assert screen._protocol_unavailable_reason("telegram") == (
            "set TELEGRAM_API_ID and TELEGRAM_API_HASH"
        )
        assert screen._protocol_unavailable_reason("signal") == ""

    def test_select_unavailable_protocol_is_noop(self):
        screen = DeviceLinkPickerScreen()
        screen._transition_to_phone = MagicMock()
        screen._transition_to_qr = MagicMock()

        screen._select_protocol(1)
        screen._select_protocol(2)

        assert screen._selected_protocol == ""
        screen._transition_to_phone.assert_not_called()
        screen._transition_to_qr.assert_not_called()

    def test_ctrl_q_delegates_to_app_quit(self):
        screen = DeviceLinkPickerScreen()
        app = MagicMock()
        with patch.object(
            DeviceLinkPickerScreen,
            "app",
            new_callable=PropertyMock,
            return_value=app,
        ):
            screen.action_quit_app()

        app.action_quit.assert_called_once_with()


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
        screen = DeviceLinkPickerScreen(has_telegram=True)
        with (
            patch.object(screen, "_populate_qr_phase"),
            patch.object(screen, "_show_phase"),
            patch.object(screen, "run_worker"),
            patch.object(screen, "_fetch_real_qr", new=MagicMock()),
        ):
            screen._select_protocol(2)
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


class TestSignalLinkPersistenceEdgeCases:
    """Edge cases of the Signal link persistence patch (PR #214)."""

    def test_poll_completion_finalize_error_updates_status(self):
        """Finalize failure shows the error on the QR status widget."""
        screen = DeviceLinkPickerScreen()
        screen._phase = "qr"
        screen._selected_protocol = "signal"
        screen._check_signal_done = AsyncMock(return_value=True)
        screen._finalize_signal_link = AsyncMock(side_effect=RuntimeError("boom"))
        status = MagicMock()
        screen.query_one = MagicMock(return_value=status)
        screen.dismiss = MagicMock()
        with patch("asyncio.sleep", AsyncMock()):
            run(screen._poll_completion(""))
        status.update.assert_called_once()
        assert "account configuration failed" in status.update.call_args.args[0]
        screen.dismiss.assert_not_called()

    def test_poll_completion_finalize_error_query_fails(self):
        """Finalize failure with missing status widget is logged, not raised."""
        screen = DeviceLinkPickerScreen()
        screen._phase = "qr"
        screen._selected_protocol = "signal"
        screen._check_signal_done = AsyncMock(return_value=True)
        screen._finalize_signal_link = AsyncMock(side_effect=RuntimeError("boom"))
        screen.query_one = MagicMock(side_effect=Exception("no widget"))
        screen.dismiss = MagicMock()
        with patch("asyncio.sleep", AsyncMock()):
            run(screen._poll_completion(""))
        screen.dismiss.assert_not_called()

    def test_poll_completion_finalize_error_dismisses_if_requested(self):
        """A dismiss requested during a failed finalize is honoured."""
        screen = DeviceLinkPickerScreen()
        screen._phase = "qr"
        screen._selected_protocol = "signal"
        screen._check_signal_done = AsyncMock(return_value=True)
        screen._finalize_signal_link = AsyncMock(side_effect=RuntimeError("boom"))
        screen._dismiss_after_finalize = True
        screen.dismiss = MagicMock()
        with patch("asyncio.sleep", AsyncMock()):
            run(screen._poll_completion(""))
        screen.dismiss.assert_called_once_with(None)

    def test_poll_completion_dismisses_after_finalize(self):
        """A dismiss requested during a successful finalize is honoured."""
        screen = DeviceLinkPickerScreen()
        screen._phase = "qr"
        screen._selected_protocol = "signal"
        screen._check_signal_done = AsyncMock(return_value=True)
        screen._finalize_signal_link = AsyncMock()
        screen._dismiss_after_finalize = True
        screen.dismiss = MagicMock()
        with patch("asyncio.sleep", AsyncMock()):
            run(screen._poll_completion(""))
        screen._finalize_signal_link.assert_awaited_once_with("")
        screen.dismiss.assert_called_once_with(None)

    def test_poll_completion_status_update_failure_logs(self):
        """Missing status widget after a successful finalize is logged."""
        screen = DeviceLinkPickerScreen()
        screen._phase = "qr"
        screen._selected_protocol = "signal"
        screen._check_signal_done = AsyncMock(return_value=True)
        screen._finalize_signal_link = AsyncMock()
        screen.query_one = MagicMock(side_effect=Exception("no widget"))
        screen.dismiss = MagicMock()
        with patch("asyncio.sleep", AsyncMock()):
            run(screen._poll_completion(""))
        screen.dismiss.assert_called_once_with(None)

    def test_transition_to_qr_signal_does_not_mark_touched(self):
        """Signal QR flow is not added to touched protocols (handled on finalize)."""
        screen = DeviceLinkPickerScreen()
        screen._selected_protocol = "signal"
        with (
            patch.object(screen, "_populate_qr_phase"),
            patch.object(screen, "_show_phase"),
            patch.object(screen, "run_worker"),
            patch.object(screen, "_fetch_real_qr", new=MagicMock()),
        ):
            screen._transition_to_qr(phone="")
        assert screen._touched_protocols == set()

    def test_poll_completion_whatsapp_done_skips_signal_finalize(self):
        """A done WhatsApp link skips the Signal finalize block."""
        screen = DeviceLinkPickerScreen()
        screen._phase = "qr"
        screen._selected_protocol = "whatsapp"
        screen._check_whatsapp_done = AsyncMock(return_value=True)
        status, code = MagicMock(), MagicMock()
        screen.query_one = MagicMock(side_effect=[status, code])
        screen.dismiss = MagicMock()
        with patch("asyncio.sleep", AsyncMock()):
            run(screen._poll_completion(""))
        screen._finalize_signal_link = AsyncMock()
        assert (
            not hasattr(screen, "_signal_finalizing") or not screen._signal_finalizing
        )
        screen.dismiss.assert_called_once_with(None)

    def test_poll_completion_finalize_base_exception_propagates(self):
        """A BaseException from finalize is re-raised after resetting state."""
        screen = DeviceLinkPickerScreen()
        screen._phase = "qr"
        screen._selected_protocol = "signal"
        screen._check_signal_done = AsyncMock(return_value=True)
        screen._finalize_signal_link = AsyncMock(side_effect=KeyboardInterrupt())
        with (
            patch("asyncio.sleep", AsyncMock()),
            pytest.raises(KeyboardInterrupt),
        ):
            run(screen._poll_completion(""))
        assert screen._signal_finalizing is False


class TestDeviceLinkScreenFlows:
    @pytest.mark.integration
    async def test_ctrl_q_quits_with_link_modal_open(self, app_for_test):
        screen = DeviceLinkPickerScreen()
        async with app_for_test.run_test() as pilot:
            await app_for_test.push_screen(screen)
            await pilot.pause()

            await pilot.press("ctrl+q")
            await pilot.pause()

            assert app_for_test._exit is True

    @pytest.mark.integration
    @pytest.mark.parametrize(
        ("has_whatsapp", "has_telegram"),
        [(False, False), (True, False), (False, True), (True, True)],
    )
    async def test_picker_always_shows_all_protocols(
        self, app_for_test, has_whatsapp, has_telegram
    ):
        screen = DeviceLinkPickerScreen(
            has_whatsapp=has_whatsapp,
            has_telegram=has_telegram,
        )
        async with app_for_test.run_test() as pilot:
            await app_for_test.push_screen(screen)
            await pilot.pause()
            protocols = screen.query_one("#link-protocol-list", ListView)

            assert len(protocols.children) == 3
            assert [item.disabled for item in protocols.children] == [
                False,
                not has_whatsapp,
                not has_telegram,
            ]

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

    # The linking itself (signal-cli, WAHA, Telegram) runs in the core: see
    # tests/test_link_service.py.  Below, the screen against a scripted server.

    def test_start_link_sends_protocol_phone_and_device(self):
        link = LinkStub(**{"link.start": {"qr": {"kind": "url", "data": "u"}}})
        screen = DeviceLinkPickerScreen(client=link)
        screen._selected_protocol = "signal"
        screen._device_name = "My Laptop"
        assert run(screen._start_link("+39123")) == {"kind": "url", "data": "u"}
        assert link.calls == [
            (
                "link.start",
                {"protocol": "signal", "phone": "+39123", "device_name": "My Laptop"},
            )
        ]

    def test_client_defaults_to_the_apps(self):
        screen = DeviceLinkPickerScreen()
        app = SimpleNamespace(client="the-client")
        with patch.object(
            DeviceLinkPickerScreen, "app", new_callable=PropertyMock, return_value=app
        ):
            assert screen._client() == "the-client"

    def test_render_qr_kinds(self):
        render = DeviceLinkPickerScreen._render_qr
        with patch("device_link_screen.qr_to_ascii", return_value="ASCII") as qr:
            assert render({"kind": "url", "data": "sgnl://x"}) == "ASCII"
        qr.assert_called_once_with("sgnl://x")
        with patch("device_link_screen.qr_png_to_ascii", return_value="PNG") as png:
            encoded = base64.b64encode(b"\x89PNG").decode()
            assert render({"kind": "png", "data": encoded}) == "PNG"
        png.assert_called_once_with(b"\x89PNG")
        assert render({"kind": "text", "data": "raw"}) == "raw"
        assert render({"kind": "info", "data": "hi"}) == "\n\nhi\n"
        assert render({"kind": "error", "data": "bad"}) == "\n\n❌ bad\n"

    @pytest.mark.parametrize(
        ("qr", "shown"),
        [
            ({"kind": "url", "data": "sgnl://x"}, "ASCII"),
            ({"kind": "text", "data": "QR"}, "QR"),
        ],
    )
    def test_fetch_qr_shows_it_and_starts_polling(self, qr, shown):
        screen = DeviceLinkPickerScreen(client=LinkStub(**{"link.start": {"qr": qr}}))
        screen._selected_protocol = "signal"
        code, status = MagicMock(), MagicMock()
        screen.query_one = MagicMock(side_effect=[code, status])
        screen.run_worker = MagicMock(side_effect=lambda coro, **kwargs: coro.close())
        with patch("device_link_screen.qr_to_ascii", return_value="ASCII"):
            run(screen._fetch_real_qr(""))
        code.update.assert_called_once_with(shown)
        status.update.assert_called_once_with("⏳ Waiting for scan from phone...")
        screen.run_worker.assert_called_once()

    def test_fetch_qr_info_shows_message_and_does_not_poll(self):
        info = {
            "kind": "info",
            "data": " WhatsApp is already linked ✅\nNo QR needed.\n",
        }
        screen = DeviceLinkPickerScreen(client=LinkStub(**{"link.start": {"qr": info}}))
        screen._selected_protocol = "whatsapp"
        code, status = MagicMock(), MagicMock()
        screen.query_one = MagicMock(side_effect=[code, status])
        screen.run_worker = MagicMock()
        run(screen._fetch_real_qr(""))
        code.update.assert_called_once_with(
            "\n\nWhatsApp is already linked ✅\nNo QR needed.\n"
        )
        status.update.assert_called_once_with("")
        screen.run_worker.assert_not_called()

    def test_fetch_qr_failure_shows_the_server_message(self):
        link = LinkStub(**{"link.start": ApiError("backend_error", "bad qr")})
        screen = DeviceLinkPickerScreen(client=link)
        screen._selected_protocol = "signal"
        code, status = MagicMock(), MagicMock()
        screen.query_one = MagicMock(side_effect=[code, status])
        screen.run_worker = MagicMock()
        run(screen._fetch_real_qr(""))
        assert code.update.call_args.args[0] == "\n\n❌ bad qr\n"
        status.update.assert_called_once_with("")
        screen.run_worker.assert_not_called()

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
        screen._finalize_signal_link = AsyncMock()
        screen.query_one = MagicMock(side_effect=[status, code])
        screen.dismiss = MagicMock()
        with patch("asyncio.sleep", AsyncMock()):
            run(screen._poll_completion(""))
        screen._finalize_signal_link.assert_awaited_once_with("")
        assert screen._touched_protocols == {"signal"}
        screen.dismiss.assert_called_once_with(None)

    # ── completion checks ──────────────────────────────────────────────────

    @pytest.mark.parametrize(
        ("state", "done"), [("waiting", False), ("finalize", True), ("done", False)]
    )
    def test_check_signal_done(self, state, done):
        link = LinkStub(
            **{"link.poll": {"state": state, "needs_2fa": False, "qr": None}}
        )
        screen = DeviceLinkPickerScreen(client=link)
        assert run(screen._check_signal_done()) is done
        assert link.calls == [("link.poll", {"protocol": "signal"})]

    def test_failed_poll_counts_as_waiting(self):
        link = LinkStub(**{"link.poll": ApiError("conflict", "No link in progress")})
        screen = DeviceLinkPickerScreen(client=link)
        assert run(screen._check_signal_done()) is False
        assert run(screen._check_whatsapp_done()) is False
        assert run(screen._check_telegram_done()) is False

    def test_check_whatsapp_done_and_waiting(self):
        link = LinkStub(
            **{
                "link.poll": [
                    {"state": "waiting", "needs_2fa": False, "qr": None},
                    {"state": "done", "needs_2fa": False, "qr": None},
                ]
            }
        )
        screen = DeviceLinkPickerScreen(client=link)
        screen.query_one = MagicMock(side_effect=Exception("nothing to touch"))
        assert run(screen._check_whatsapp_done()) is False
        assert run(screen._check_whatsapp_done()) is True
        assert [c[1] for c in link.calls] == [{"protocol": "whatsapp"}] * 2

    def test_check_whatsapp_done_shows_a_renewed_qr(self):
        qr = {"kind": "text", "data": "fresh"}
        link = LinkStub(
            **{"link.poll": {"state": "waiting", "needs_2fa": False, "qr": qr}}
        )
        screen = DeviceLinkPickerScreen(client=link)
        code = MagicMock()
        screen.query_one = MagicMock(return_value=code)
        assert run(screen._check_whatsapp_done()) is False
        code.update.assert_called_once_with("fresh")
        code.refresh.assert_called_once()

    def test_check_telegram_done(self):
        link = LinkStub(
            **{"link.poll": {"state": "done", "needs_2fa": False, "qr": None}}
        )
        assert run(DeviceLinkPickerScreen(client=link)._check_telegram_done()) is True

    def test_check_telegram_asks_for_2fa(self):
        link = LinkStub(
            **{"link.poll": {"state": "waiting", "needs_2fa": True, "qr": None}}
        )
        screen = DeviceLinkPickerScreen(client=link)
        status, container = MagicMock(), MagicMock()
        screen.query_one = MagicMock(side_effect=[status, container])
        with patch("device_link_screen.Input") as make_input:
            assert run(screen._check_telegram_done()) is False
        status.update.assert_called_once_with("🔐 2FA required — enter password below")
        make_input.assert_called_once_with(
            placeholder="Telegram 2FA password", password=True, id="link-2fa-input"
        )
        container.mount.assert_called_once_with(make_input.return_value)
        make_input.return_value.focus.assert_called_once()

    @pytest.mark.parametrize(
        ("qr", "shown"),
        [
            (
                {"kind": "info", "data": " Already logged in"},
                "\n\n Already logged in\n",
            ),
            ({"kind": "error", "data": "flood wait"}, "\n\n❌ flood wait\n"),
            ({"kind": "url", "data": "tg://login?token=2"}, "ASCII"),
        ],
    )
    def test_check_telegram_shows_a_renewed_qr(self, qr, shown):
        link = LinkStub(
            **{"link.poll": {"state": "waiting", "needs_2fa": False, "qr": qr}}
        )
        screen = DeviceLinkPickerScreen(client=link)
        code = MagicMock()
        screen.query_one = MagicMock(return_value=code)
        with patch("device_link_screen.qr_to_ascii", return_value="ASCII"):
            assert run(screen._check_telegram_done()) is False
        code.update.assert_called_once_with(shown)

    def test_finalize_asks_the_server_and_remembers_the_account(self):
        link = LinkStub(**{"link.finalize": {"account": "+391111111111"}})
        screen = DeviceLinkPickerScreen(client=link)
        run(screen._finalize_signal_link("+39 111"))
        assert screen._signal_number == "+391111111111"
        assert link.calls == [
            ("link.finalize", {"protocol": "signal", "phone": "+39 111"})
        ]

    def test_finalize_failure_propagates_with_the_server_message(self):
        link = LinkStub(
            **{
                "link.finalize": ApiError(
                    "backend_error",
                    "switching Signal accounts in an active profile is not supported",
                )
            }
        )
        screen = DeviceLinkPickerScreen(client=link)
        with pytest.raises(ApiError, match="switching Signal accounts"):
            run(screen._finalize_signal_link(""))
        assert screen._signal_number == ""

    # ── 2FA ────────────────────────────────────────────────────────────────

    def test_2fa_submit_runs_a_worker_and_ignores_empty_password(self):
        screen = DeviceLinkPickerScreen()
        screen.run_worker = MagicMock(side_effect=lambda coro, **kwargs: coro.close())
        status = MagicMock()
        screen.query_one = MagicMock(
            side_effect=[
                MagicMock(value="  "),
            ]
        )
        screen._on_2fa_submit()
        screen.run_worker.assert_not_called()

        screen.query_one = MagicMock(side_effect=[MagicMock(value="secret"), status])
        screen._on_2fa_submit()
        status.update.assert_called_once_with("⏳ Verifying 2FA password...")
        screen.run_worker.assert_called_once()

    def test_complete_2fa_wrong_password(self):
        link = LinkStub(**{"link.submit_2fa": {"ok": False}})
        screen = DeviceLinkPickerScreen(client=link)
        status, inp = MagicMock(), MagicMock()
        screen.query_one = MagicMock(side_effect=[status, inp])
        with patch("asyncio.sleep", AsyncMock()):
            run(screen._complete_2fa_worker("wrong"))
        status.update.assert_called_once_with("❌ Wrong password — try again")
        assert inp.value == ""
        inp.focus.assert_called_once()
        assert link.calls == [("link.submit_2fa", {"password": "wrong"})]

    def test_complete_2fa_success_dismisses_after_a_pause(self):
        screen = DeviceLinkPickerScreen(
            client=LinkStub(**{"link.submit_2fa": {"ok": True}})
        )
        status, code = MagicMock(), MagicMock()
        screen.query_one = MagicMock(side_effect=[status, code])
        screen.dismiss = MagicMock()
        sleep = AsyncMock()
        with patch("asyncio.sleep", sleep):
            run(screen._complete_2fa_worker("right"))
        status.update.assert_called_once_with("✅ Device linked successfully!")
        code.update.assert_called_once_with("\n\n✅ Linked!\n")
        sleep.assert_awaited_once_with(2)
        screen.dismiss.assert_called_once_with(None)

    def test_complete_2fa_server_error_is_shown(self):
        link = LinkStub(
            **{
                "link.submit_2fa": ApiError(
                    "backend_down", "Telegram backend not available"
                )
            }
        )
        screen = DeviceLinkPickerScreen(client=link)
        status = MagicMock()
        screen.query_one = MagicMock(return_value=status)
        screen.dismiss = MagicMock()
        run(screen._complete_2fa_worker("x"))
        status.update.assert_called_once_with("❌ Telegram backend not available")
        screen.dismiss.assert_not_called()

    # ── dismiss ────────────────────────────────────────────────────────────

    def test_dismiss_cancels_the_started_link_once(self):
        link = LinkStub(**{"link.cancel": {"ok": True}})
        screen = DeviceLinkPickerScreen(client=link)
        screen._active_link = "signal"
        with patch("textual.screen.Screen.dismiss") as dismiss:
            screen.dismiss(None)
            screen.dismiss(None)
        assert link.calls == [("link.cancel", {"protocol": "signal"})]
        assert dismiss.call_count == 2
        assert screen._phase == "done"

    def test_dismiss_without_a_started_link_does_not_call_the_server(self):
        link = LinkStub()
        screen = DeviceLinkPickerScreen(client=link)
        with patch("textual.screen.Screen.dismiss") as dismiss:
            screen.dismiss(None)
        assert link.calls == []
        dismiss.assert_called_once()

    def test_dismiss_survives_a_failing_cancel(self):
        link = LinkStub(**{"link.cancel": ApiError("internal", "kill failed")})
        screen = DeviceLinkPickerScreen(client=link)
        screen._active_link = "signal"
        with patch("textual.screen.Screen.dismiss") as dismiss:
            screen.dismiss(None)
        assert len(link.calls) == 1
        dismiss.assert_called_once()

    def test_dismiss_during_finalize_defers(self):
        """Dismiss while finalizing defers cleanup instead of cancelling."""
        link = LinkStub()
        screen = DeviceLinkPickerScreen(client=link)
        screen._active_link = "signal"
        screen._signal_finalizing = True
        with patch("textual.screen.Screen.dismiss") as dismiss:
            screen.dismiss(None)
        assert screen._dismiss_after_finalize is True
        assert link.calls == []
        dismiss.assert_not_called()

    def test_transition_to_qr_remembers_the_active_link(self):
        screen = DeviceLinkPickerScreen()
        screen._selected_protocol = "signal"
        with (
            patch.object(screen, "_populate_qr_phase"),
            patch.object(screen, "_show_phase"),
            patch.object(screen, "run_worker"),
            patch.object(screen, "_fetch_real_qr", new=MagicMock()),
        ):
            screen._transition_to_qr(phone="")
        assert screen._active_link == "signal"

    # ── whole flows against the real operations ────────────────────────────

    def _real_flow_screen(self, manager):
        from client_harness import client_for

        screen = DeviceLinkPickerScreen(client=client_for(manager))
        screen._phase = "qr"
        screen.dismiss = MagicMock()
        code, status = MagicMock(), MagicMock()
        screen.query_one = MagicMock(
            side_effect=lambda sel, *_: {
                "#link-qr-code": code,
                "#link-qr-status": status,
            }[sel]
        )
        return screen, code, status

    def test_whatsapp_flow_over_real_operations(self):
        rest = MagicMock()
        rest.get_session_status.side_effect = [
            {"status": "scan_qr"},  # start
            {"status": "scan_qr"},  # poll 1
            {"status": "working", "me": {"id": "1@c.us"}},  # poll 2
        ]
        rest.get_pairing_qr.return_value = b"\x89PNG"
        manager = MagicMock()
        manager.get.side_effect = lambda p: (
            SimpleNamespace(_rest=rest) if p == "whatsapp" else None
        )
        screen, code, status = self._real_flow_screen(manager)
        screen._selected_protocol = "whatsapp"
        screen.run_worker = MagicMock(side_effect=lambda coro, **k: coro.close())
        with patch("device_link_screen.qr_png_to_ascii", return_value="WA-QR"):
            run(screen._fetch_real_qr(""))
        code.update.assert_called_once_with("WA-QR")
        status.update.assert_called_once_with("⏳ Waiting for scan from phone...")

        with patch("asyncio.sleep", AsyncMock()) as sleep:
            run(screen._poll_completion(""))
        assert sleep.await_count == 2  # one pause between polls, one before dismiss
        status.update.assert_called_with("✅ Device linked successfully!")
        screen.dismiss.assert_called_once_with(None)

    def test_telegram_2fa_flow_over_real_operations(self):
        tb = MagicMock(_connected=False, _needs_2fa=True)
        tb.get_pairing_qr.return_value = "tg://login?token=1"
        tb.complete_2fa.return_value = True
        manager = MagicMock()
        manager.get.side_effect = lambda p: tb if p == "telegram" else None
        screen, code, status = self._real_flow_screen(manager)
        screen._selected_protocol = "telegram"
        screen.run_worker = MagicMock(side_effect=lambda coro, **k: coro.close())
        with patch("device_link_screen.qr_to_ascii", return_value="TG-QR"):
            run(screen._fetch_real_qr(""))
        code.update.assert_called_once_with("TG-QR")

        # first poll: the account asks for a password
        container = MagicMock()
        screen.query_one = MagicMock(
            side_effect=lambda sel, *_: {
                "#link-qr-status": status,
                "#link-qr-container": container,
            }[sel]
        )
        with patch("device_link_screen.Input") as make_input:
            assert run(screen._check_telegram_done()) is False
        container.mount.assert_called_once_with(make_input.return_value)
        # not asked twice
        assert run(screen._check_telegram_done()) is False
        container.mount.assert_called_once()

        screen.query_one = MagicMock(return_value=status)
        with patch("asyncio.sleep", AsyncMock()):
            run(screen._complete_2fa_worker("pw"))
        tb.complete_2fa.assert_called_once_with("pw")
        screen.dismiss.assert_called_once_with(None)

    def test_signal_flow_over_real_operations(self, tmp_path):
        from protocols import rpc as signal_rpc

        proc = MagicMock()
        proc.stdout.readline.side_effect = ["sgnl://linkdevice?uuid=abc", ""]
        proc.poll.side_effect = itertools.chain([None], itertools.repeat(0))
        backend = SimpleNamespace(user_number="")
        manager = MagicMock()
        manager.get.side_effect = lambda p: backend if p == "signal" else None
        screen, code, _status = self._real_flow_screen(manager)
        screen._selected_protocol = "signal"
        screen.run_worker = MagicMock(side_effect=lambda coro, **k: coro.close())
        with (
            patch.object(signal_rpc, "PROJECT_DIR", tmp_path),
            patch.object(signal_rpc, "find_signal_cli", return_value=Path("cli")),
            patch("server.link.subprocess.Popen", return_value=proc),
            patch(
                "server.link.list_signal_accounts",
                side_effect=[[], ["+391111111111"], ["+391111111111"]],
            ),
            patch("device_link_screen.qr_to_ascii", return_value="SIG-QR"),
            patch("asyncio.sleep", AsyncMock()),
        ):
            run(screen._fetch_real_qr("+39 111"))
            code.update.assert_called_once_with("SIG-QR")
            run(screen._poll_completion("+39 111"))
        assert backend.user_number == "+391111111111"
        assert screen._signal_number == "+391111111111"
        assert screen._touched_protocols == {"signal"}
        screen.dismiss.assert_called_once_with(None)

    def test_poll_times_out_after_five_minutes(self):
        link = LinkStub(
            **{"link.poll": {"state": "waiting", "needs_2fa": False, "qr": None}}
        )
        screen = DeviceLinkPickerScreen(client=link)
        screen._phase = "qr"
        screen._selected_protocol = "whatsapp"
        status = MagicMock()
        screen.query_one = MagicMock(return_value=status)
        screen.dismiss = MagicMock()
        clock = iter([1000.0, 1000.0, 1100.0, 1299.0, 1301.0, 1301.0])
        with (
            patch("device_link_screen.time.time", side_effect=lambda: next(clock)),
            patch("asyncio.sleep", AsyncMock()),
        ):
            run(screen._poll_completion(""))
        status.update.assert_called_once_with("❌ Timed out waiting for scan")
        assert len(link.calls) == 3
        screen.dismiss.assert_not_called()


class LinkStub:
    """Scripted ``client.call``: per op a value, an exception or a list of them."""

    def __init__(self, **ops):
        self.ops = ops
        self.calls: list[tuple[str, dict]] = []

    def call(self, name, data=None):
        self.calls.append((name, data))
        answer = self.ops[name]
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class TestDeviceLinkOpening:
    """Ctrl+L: the screen is built from ``link.status``; dismiss reconnects."""

    def _app(self, status):
        from signal_tui import SignalTUI

        app = SignalTUI()
        app.client = LinkStub(**{"link.status": {"protocols": status}})
        app.push_screen = MagicMock()
        return app

    def test_screen_is_built_from_link_status(self):
        app = self._app(
            {
                "signal": {"available": True, "account": "+39123", "linked": True},
                "whatsapp": {"available": True, "account": "", "linked": None},
                "telegram": {"available": False, "account": "", "linked": None},
            }
        )
        with patch("tui.pickers.DeviceLinkPickerScreen") as screen_cls:
            app._open_device_link()
        screen_cls.assert_called_once_with(
            signal_number="+39123",
            has_whatsapp=True,
            has_telegram=False,
            client=app.client,
        )
        app.push_screen.assert_called_once()

    def test_dismiss_reconnects_only_touched_protocols_through_the_server(self):
        app = self._app(
            {
                p: {"available": True, "account": "", "linked": None}
                for p in ("signal", "whatsapp", "telegram")
            }
        )
        app.client.ops["link.reconnect"] = {"ok": True}
        fake_screen = MagicMock()
        fake_screen._touched_protocols = {"telegram", "whatsapp"}
        with patch("tui.pickers.DeviceLinkPickerScreen", return_value=fake_screen):
            app._open_device_link()
        app.push_screen.call_args[0][1](None)
        assert app.client.calls[-1] == (
            "link.reconnect",
            {"protocols": ["telegram", "whatsapp"]},
        )

    def test_dismiss_without_touched_protocols_reconnects_nothing(self):
        app = self._app({})
        app.client.ops["link.reconnect"] = {"ok": True}
        app._reconnect_touched_backends(set())
        assert [c[0] for c in app.client.calls] == []

    def test_reconnect_failure_is_logged_not_raised(self):
        app = self._app({})
        app.client.ops["link.reconnect"] = ApiError("internal", "boom")
        app._reconnect_touched_backends({"signal"})
