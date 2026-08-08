# Device Link TUI — TODO

## Done ✅
- [x] Branch `feature/device-link-tui`
- [x] `qr_utils.py` — shared QR helpers (`qr_to_ascii`, `qr_png_to_ascii`, `_decode_png_luminance`)
- [x] `device_link_screen.py` — `DeviceLinkPickerScreen` with 3-phase UI (picker / phone / QR)
- [x] `Ctrl+L` binding in `signal_tui.py`
- [x] Signal QR: real `signal-cli link` subprocess → live QR code
- [x] WhatsApp QR: `get_pairing_qr()` via WAHA REST (already-connected detection)
- [x] Half-block QR rendering for compact display
- [x] Error display as text (not fake QR)
- [x] Refactored `link_account.py` / `link_whatsapp.py` to import from `qr_utils`
- [x] Tests: `test_device_link_screen.py` (11 tests), `test_qr_ascii.py` (4 tests, updated imports)
- [x] Full test suite: 389 passed

## Remaining

### Phase 1 — Remove test hook
- [ ] Remove `force_phone_input=True` in `signal_tui.py` → `_open_device_link()`
- [ ] Implement real `_should_show_phone_input()` logic:
  - Signal: show phone input ONLY if `signal_number` is empty
  - WhatsApp: never show phone input (always skip to QR)
- [ ] Skip phone phase entirely for WhatsApp (direct picker → QR)

### Phase 2 — Signal linking lifecycle
- [ ] Kill `signal-cli link` subprocess on Cancel / Esc from QR phase
- [ ] Monitor subprocess exit code → auto-dismiss on success
- [ ] Show "✅ Device linked!" on successful completion
- [ ] Timeout handling (e.g. 5 minutes)

### Phase 3 — WhatsApp linking lifecycle
- [ ] Add `get_fresh_pairing_qr()` to `WhatsAppBackend` (currently only on REST client)
- [ ] QR auto-refresh on expiry (WhatsApp QRs expire ~60s)
- [ ] Poll session status → auto-dismiss on `WORKING`
- [ ] Timeout handling

### Phase 4 — Polish
- [ ] Telegram placeholder → real integration when backend ready
- [ ] Integration tests with async Textual harness (`pytest-asyncio`)
- [ ] README update: document `Ctrl+L` key binding
- [ ] Better loading spinner / animation during QR fetch
