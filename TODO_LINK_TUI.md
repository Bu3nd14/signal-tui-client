# Device Link TUI — TODO

## Done ✅
- [x] Branch `feature/device-link-tui`
- [x] `qr_utils.py` — shared QR helpers
- [x] `device_link_screen.py` — `DeviceLinkPickerScreen` with 3-phase UI
- [x] `Ctrl+L` binding in `signal_tui.py`
- [x] Signal QR: real `signal-cli link` subprocess → live QR code
- [x] WhatsApp QR: `get_pairing_qr()` via WAHA REST (already-connected detection)
- [x] Half-block QR rendering for compact display
- [x] Error display as text (not fake QR)
- [x] Refactored `link_account.py` / `link_whatsapp.py` to import from `qr_utils`
- [x] Tests: `test_device_link_screen.py` (15 tests), `test_qr_ascii.py` (4 tests)
- [x] Full test suite: 393 passed
- [x] Remove `force_phone_input` test hook
- [x] Real `_should_show_phone_input()` — Signal only when number unknown, WhatsApp never
- [x] Kill `signal-cli link` subprocess on Cancel / Esc / dismiss
- [x] Signal: monitor subprocess exit code → auto-dismiss on success
- [x] WhatsApp: poll session status → auto-dismiss on WORKING
- [x] WhatsApp: QR auto-refresh on expiry (every 60s)
- [x] Timeout after 5 minutes for both protocols
- [x] `on_screen_dismiss()` cleanup

## Remaining

### Polish
- [ ] Telegram placeholder → real integration when backend ready
- [ ] Integration tests with async Textual harness (`pytest-asyncio`)
- [ ] README update: document `Ctrl+L` key binding

