"""
Device Link Screen for Signal TUI Client.

Provides:
- ``DeviceLinkPickerScreen`` — a ModalScreen with three phases:
  1. **Picker**  — choose protocol (Signal / WhatsApp / future Telegram)
  2. **Phone**   — phone number + device name input (shown for all protocols
     during UI testing via ``force_phone_input``; after backend integration
     this is only shown for Signal when the number is unknown)
  3. **QR**      — QR code display + status message + cancel button
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

from qr_utils import qr_to_ascii

logger = logging.getLogger(__name__)

# ─── Protocol items (extensible) ───────────────────────────────────────────────

_PROTOCOL_ITEMS: list[dict[str, str]] = [
    {"id": "signal", "label": "📶 Signal", "disabled": False},
    {"id": "whatsapp", "label": "💬 WhatsApp", "disabled": False},
    {"id": "telegram", "label": "📨 Telegram", "disabled": False},
]

# Default device name used for Signal linking when the user doesn't customise it.
_DEFAULT_SIGNAL_DEVICE_NAME = "Signal-TUI-Client"


# ─── Screen ────────────────────────────────────────────────────────────────────


class DeviceLinkPickerScreen(ModalScreen[None]):
    """Modal screen to link a new device (Signal / WhatsApp).

    Phases
    ------
    ``"picker"``
        ListView with available protocols.  ↑/↓ to navigate, Enter to select.

    ``"phone"`` (shown when ``force_phone_input=True`` or number is unknown)
        Input for phone number and device name.  Enter / Start button advances.

    ``"qr"``
        QR code rendered as ASCII, status message, Cancel button.
    """

    DEFAULT_CSS = """
    DeviceLinkPickerScreen {
        align: center middle;
        background: $surface 80%;
    }

    #link-picker-container {
        width: 50;
        height: auto;
        min-height: 12;
        max-height: 60%;
        border: thick $accent;
        background: $surface;
        padding: 1 1;
    }

    #link-picker-title {
        text-style: bold;
        text-align: center;
        padding: 0 0 1 0;
        color: $text;
    }

    #link-protocol-list {
        height: auto;
        overflow-y: auto;
        padding: 0;
        margin-bottom: 1;
    }

    #link-protocol-list ListItem {
        padding: 0 1;
    }

    #link-protocol-list ListItem:hover {
        background: $accent 20%;
    }

    #link-protocol-list ListItem:focus {
        background: $accent 40%;
    }

    #link-protocol-list .disabled-item {
        color: $text-muted;
        text-style: italic;
    }

    #link-picker-footer {
        dock: bottom;
        height: 1;
        text-align: center;
        color: $text-muted;
        padding: 0 1;
    }

    /* ── Phone phase ────────────────────────────────── */

    #link-phone-container {
        width: 50;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }

    #link-phone-title {
        text-style: bold;
        text-align: center;
        padding: 0 0 1 0;
        color: $text;
    }

    #link-phone-input {
        width: 100%;
        margin-bottom: 1;
    }

    #link-device-input {
        width: 100%;
        margin-bottom: 1;
    }

    #link-phone-buttons {
        width: 100%;
        height: auto;
        align: center middle;
    }

    #link-phone-buttons Button {
        margin: 0 1;
    }

    /* ── QR phase ───────────────────────────────────── */

    #link-qr-container {
        width: auto;
        max-width: 85%;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 0 1;
    }

    #link-qr-title {
        text-style: bold;
        text-align: center;
        padding: 0;
        color: $text;
    }

    #link-qr-info {
        text-align: center;
        color: $text-muted;
        margin-bottom: 0;
    }

    #link-qr-code {
        width: auto;
        height: auto;
        min-height: 12;
        text-align: left;
        color: $text;
        padding: 0;
        overflow-x: auto;
    }

    #link-qr-status {
        text-align: center;
        color: $warning;
        margin-top: 0;
    }

    #link-qr-buttons {
        width: 100%;
        height: auto;
        align: center middle;
        margin-top: 0;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss(None)", "Close", priority=True),
        Binding("enter", "select_item", "Select", priority=True),
    ]

    # ── Constructor ────────────────────────────────────────────────────────

    def __init__(
        self,
        signal_number: str = "",
        has_whatsapp: bool = False,
        has_telegram: bool = False,
        force_phone_input: bool = False,
    ) -> None:
        super().__init__()
        self._signal_number = signal_number
        self._has_whatsapp = has_whatsapp
        self._has_telegram = has_telegram
        self._force_phone_input = force_phone_input
        self._phase: str = "picker"
        self._selected_protocol: str = ""
        self._device_name: str = _DEFAULT_SIGNAL_DEVICE_NAME
        # Protocolli il cui flusso QR è stato effettivamente avviato in questa
        # schermata: al dismiss servono a riconnettere solo i backend toccati.
        self._touched_protocols: set[str] = set()

    # ── Compose ────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        """Yield all three phase containers; only one is visible at a time."""
        # Phase 1: Picker
        yield self._build_picker_container()
        # Phase 2: Phone (hidden)
        yield self._build_phone_container()
        # Phase 3: QR (hidden)
        yield self._build_qr_container()

    def on_mount(self) -> None:
        """After all containers are mounted, populate and show the picker."""
        self._populate_picker_phase()
        self._show_phase("picker")

    # ── Container builders (empty shells) ──────────────────────────────────

    @staticmethod
    def _build_picker_container() -> Vertical:
        return Vertical(id="link-picker-container")

    @staticmethod
    def _build_phone_container() -> Vertical:
        container = Vertical(id="link-phone-container")
        container.display = False
        return container

    @staticmethod
    def _build_qr_container() -> Vertical:
        container = Vertical(id="link-qr-container")
        container.display = False
        return container

    # ── Phase visibility ───────────────────────────────────────────────────

    def _show_phase(self, phase: str) -> None:
        """Show only the given phase container, hide the others."""
        self._phase = phase
        for pid in (
            "link-picker-container",
            "link-phone-container",
            "link-qr-container",
        ):
            try:
                c = self.query_one(f"#{pid}", Vertical)
                c.display = pid == f"link-{phase}-container"
            except Exception as _e:
                logger.debug("Failed to toggle phase container", exc_info=True)

    # ── Phase populators (fill already-mounted containers) ─────────────────

    def _populate_picker_phase(self) -> None:
        """Fill the picker container with protocol list items (once)."""
        if hasattr(self, "_picker_populated") and self._picker_populated:
            return
        self._picker_populated = True
        container = self.query_one("#link-picker-container", Vertical)
        container.mount(Static("🔗 Link New Device", id="link-picker-title"))

        lv = ListView(id="link-protocol-list")
        container.mount(lv)
        for item in _PROTOCOL_ITEMS:
            if item["id"] == "whatsapp" and not self._has_whatsapp:
                continue
            if item["id"] == "telegram" and not self._has_telegram:
                continue
            label = Label(item["label"])
            if item["disabled"]:
                label.add_class("disabled-item")
            li = ListItem(label, disabled=item["disabled"])
            lv.append(li)

        container.mount(
            Static(
                "↑/↓ navigate · Enter select · Esc close",
                id="link-picker-footer",
            )
        )

    def _populate_phone_phase(self) -> None:
        """Fill the phone container (once)."""
        if hasattr(self, "_phone_populated") and self._phone_populated:
            # Update just the input values
            try:
                pi = self.query_one("#link-phone-input", Input)
                pi.value = self._signal_number
                di = self.query_one("#link-device-input", Input)
                di.value = self._device_name
            except Exception as _e:
                logger.debug("Failed to update phone inputs", exc_info=True)
            return
        self._phone_populated = True
        container = self.query_one("#link-phone-container", Vertical)
        container.mount(Static("📱 Device Info", id="link-phone-title"))

        container.mount(
            Input(
                placeholder="+39 123 456 7890",
                value=self._signal_number,
                id="link-phone-input",
            )
        )
        container.mount(
            Input(
                placeholder="Device name",
                value=self._device_name,
                id="link-device-input",
            )
        )

        btn_row = Center(id="link-phone-buttons")
        container.mount(btn_row)
        btn_row.mount(
            Button("Start Linking ▶", id="link-phone-start", variant="success")
        )
        btn_row.mount(Button("Back", id="link-phone-back"))

        container.mount(
            Static(
                "Esc back · Enter to confirm",
                id="link-picker-footer",
            )
        )

    def _populate_qr_phase(self, qr_ascii: str, phone: str) -> None:
        """Fill the QR container (clears and rebuilds each time)."""
        container = self.query_one("#link-qr-container", Vertical)
        container.remove_children()
        title = f"🔗 Link {self._selected_protocol.title()}"
        container.mount(Static(title, id="link-qr-title"))
        if phone:
            info = f"📱 {phone}  ·  🖥 {self._device_name}"
            container.mount(Static(info, id="link-qr-info"))
        container.mount(Static(qr_ascii, id="link-qr-code"))
        container.mount(
            Static(
                "⏳ Waiting for scan from phone...",
                id="link-qr-status",
            )
        )

        btn_row = Center(id="link-qr-buttons")
        container.mount(btn_row)
        btn_row.mount(Button("Cancel", id="link-qr-cancel", variant="error"))

        container.mount(Static("Esc to cancel", id="link-picker-footer"))

    # ── Phase transitions ──────────────────────────────────────────────────

    def _transition_to_phone(self) -> None:
        """Move from picker to phone phase."""
        self._populate_phone_phase()
        self._show_phase("phone")
        self.query_one("#link-phone-input", Input).focus()

    def _transition_to_qr(self, phone: str) -> None:
        """Move from phone to QR phase, fetching a real QR code."""
        self._touched_protocols.add(self._selected_protocol)
        self._populate_qr_phase("⏳ Generating QR code...", phone)
        self._show_phase("qr")
        self._linking_proc: subprocess.Popen | None = None
        self._qr_start_time: float = time.time()
        self.run_worker(self._fetch_real_qr(phone), exclusive=True)

    # ── Polling for completion ──────────────────────────────────────────────

    async def _poll_completion(self, phone: str) -> None:
        """After QR is shown, poll for linking completion.

        Signal: monitors the ``signal-cli link`` subprocess exit code.
        WhatsApp: polls WAHA session status; refreshes QR on expiry.
        Both timeout after 5 minutes.
        """
        import asyncio as _asyncio

        deadline = time.time() + 300  # 5 min timeout
        proto = self._selected_protocol

        while time.time() < deadline and self._phase == "qr":
            if proto == "signal":
                done = await self._check_signal_done()
            elif proto == "whatsapp":
                done = await self._check_whatsapp_done()
            elif proto == "telegram":
                done = await self._check_telegram_done()
            else:
                return

            if self._phase != "qr":
                return  # dismissed during check

            if done:
                try:
                    status = self.query_one("#link-qr-status", Static)
                    status.update("✅ Device linked successfully!")
                    code = self.query_one("#link-qr-code", Static)
                    code.update("\n\n✅ Linked!\n")
                    code.refresh()
                except Exception as _e:
                    logger.debug("Failed to update linked status", exc_info=True)
                await _asyncio.sleep(2)
                self.dismiss(None)
                return

            await _asyncio.sleep(2)

        # Timeout (only if still on QR phase)
        if self._phase == "qr":
            try:
                status = self.query_one("#link-qr-status", Static)
                status.update("❌ Timed out waiting for scan")
            except Exception as _e:
                logger.debug("Failed to update timeout status", exc_info=True)

    async def _check_signal_done(self) -> bool:
        """Check if the signal-cli link subprocess finished successfully."""
        proc = getattr(self, "_linking_proc", None)
        if proc is None:
            return False
        rc = proc.poll()
        if rc is None:
            return False  # still running
        return rc == 0  # 0 = success

    async def _check_whatsapp_done(self) -> bool:
        """Check WAHA session status; refresh QR if expired."""
        import asyncio as _asyncio

        app = self.app
        wa = getattr(app, "whatsapp_backend", None)
        if wa is None or wa._rest is None:
            return False

        def _run() -> tuple[bool, bool]:
            status = wa._rest.get_session_status() or {}
            s = str(status.get("status") or "").lower()
            if s == "working":
                return True, False  # done, no refresh needed
            # Check QR age for refresh
            age = time.time() - self._qr_start_time
            if age >= 60 and s in ("scan_qr", "scan_qr_code", "unpaired", "pending"):
                return False, True  # not done, need refresh
            return False, False

        done, need_refresh = await _asyncio.to_thread(_run)

        if need_refresh:
            logger.info("WhatsApp QR expired, refreshing...")
            try:
                new_qr = await self._get_whatsapp_qr_fresh()
                if new_qr.startswith("INFO:"):
                    qr_ascii = "\n\n" + new_qr[5:]
                else:
                    qr_ascii = new_qr  # already ASCII-rendered
                code_widget = self.query_one("#link-qr-code", Static)
                code_widget.update(qr_ascii)
                code_widget.refresh()
                self._qr_start_time = time.time()
            except Exception:
                logger.exception("Failed to refresh WhatsApp QR")

        return done

    async def _check_telegram_done(self) -> bool:
        """Check if Telegram QR login completed; refresh QR if expired."""
        app = self.app
        tb = getattr(app, "telegram_backend", None)
        if tb is None:
            return False

        # _connected is set to True by the background wait task
        if tb._connected:
            return True

        # Check if 2FA password is needed
        if getattr(tb, "_needs_2fa", False) and not getattr(self, "_2fa_shown", False):
            self._2fa_shown = True
            try:
                status = self.query_one("#link-qr-status", Static)
                status.update("🔐 2FA required — enter password below")
            except Exception as _e:
                logger.debug("Failed to update 2FA status", exc_info=True)
            try:
                container = self.query_one("#link-qr-container", Vertical)
                self._2fa_input = Input(
                    placeholder="Telegram 2FA password",
                    password=True,
                    id="link-2fa-input",
                )
                container.mount(self._2fa_input)
                self._2fa_input.focus()
            except Exception as _e:
                logger.debug("Failed to mount 2FA input", exc_info=True)

        # Check QR age for refresh (Telegram QR tokens expire ~60s)
        age = time.time() - self._qr_start_time
        if age >= 60:
            logger.info("Telegram QR expired, refreshing...")
            try:
                new_url = await self._get_telegram_qr_link()
                self._2fa_shown = False
                if new_url.startswith("INFO:"):
                    code_widget = self.query_one("#link-qr-code", Static)
                    code_widget.update(f"\n\n{new_url[5:]}\n")
                    code_widget.refresh()
                elif new_url.startswith("ERROR:"):
                    code_widget = self.query_one("#link-qr-code", Static)
                    code_widget.update(f"\n\n❌ {new_url[7:]}\n")
                    code_widget.refresh()
                else:
                    qr_ascii = qr_to_ascii(new_url)
                    code_widget = self.query_one("#link-qr-code", Static)
                    code_widget.update(qr_ascii)
                    code_widget.refresh()
                self._qr_start_time = time.time()
            except Exception:
                logger.exception("Failed to refresh Telegram QR")

        return False

    async def _get_whatsapp_qr_fresh(self) -> str:
        """Get a fresh WhatsApp QR (resets session, unlike _get_whatsapp_qr)."""
        import asyncio as _asyncio

        app = self.app
        wa = getattr(app, "whatsapp_backend", None)
        if wa is None or wa._rest is None:
            return "ERROR: WhatsApp backend not available"

        def _run() -> str:
            qr = wa._rest.get_fresh_pairing_qr(reset=False)
            if isinstance(qr, bytes):
                from qr_utils import qr_png_to_ascii

                return qr_png_to_ascii(qr)
            elif isinstance(qr, str):
                return qr
            return "ERROR: No fresh QR available"

        return await _asyncio.to_thread(_run)

    # ── Real QR fetching (async workers) ────────────────────────────────────

    async def _fetch_real_qr(self, phone: str) -> None:
        """Background worker: fetch the real QR from the backend."""
        try:
            qr_data = await self._get_qr_data_async(phone)

            # WhatsApp returns pre-rendered ASCII (prefixed with "ASCII:")
            if qr_data.startswith("ASCII:"):
                qr_ascii = qr_data[6:]
                if qr_ascii.startswith("INFO:"):
                    # Already-connected info message
                    info_text = qr_ascii[5:].strip()
                    qr_ascii = f"\n\n{info_text}\n"
                    code_widget = self.query_one("#link-qr-code", Static)
                    code_widget.update(qr_ascii)
                    code_widget.refresh()
                    status = self.query_one("#link-qr-status", Static)
                    status.update("")
                    return
                code_widget = self.query_one("#link-qr-code", Static)
                code_widget.update(qr_ascii)
                code_widget.refresh()
                status = self.query_one("#link-qr-status", Static)
                status.update("⏳ Waiting for scan from phone...")
                self.run_worker(self._poll_completion(phone), exclusive=False)
                return

            if qr_data.startswith("INFO:"):
                # Already-connected type message
                info_text = qr_data[5:].strip()
                code_widget = self.query_one("#link-qr-code", Static)
                code_widget.update(f"\n\n{info_text}\n")
                code_widget.refresh()
                status = self.query_one("#link-qr-status", Static)
                status.update("")
                return

            # Signal: generate QR from link URL
            qr_ascii = qr_to_ascii(qr_data)
            code_widget = self.query_one("#link-qr-code", Static)
            code_widget.update(qr_ascii)
            code_widget.refresh()
            status = self.query_one("#link-qr-status", Static)
            status.update("⏳ Waiting for scan from phone...")

            # Start polling for completion (non-blocking)
            self.run_worker(self._poll_completion(phone), exclusive=False)
        except Exception as exc:
            logger.exception("Failed to fetch QR data")
            try:
                code_widget = self.query_one("#link-qr-code", Static)
                code_widget.update(f"\n\n❌ {exc}\n")
                code_widget.refresh()
                status = self.query_one("#link-qr-status", Static)
                status.update("")
            except Exception:
                logger.exception("Failed to update QR widget")

    async def _get_qr_data_async(self, phone: str) -> str:
        """Return the real QR data for the selected protocol.

        For Signal: returns a link URL that still needs ``qr_to_ascii()``.
        For WhatsApp: returns ``"ASCII:"`` + the pre-rendered QR text.
        """
        proto = self._selected_protocol
        if proto == "signal":
            return await self._get_signal_link_url()
        elif proto == "whatsapp":
            ascii_qr = await self._get_whatsapp_qr()
            return f"ASCII:{ascii_qr}"
        elif proto == "telegram":
            return await self._get_telegram_qr_link()
        return f"fake-{proto}-link"

    async def _get_signal_link_url(self) -> str:
        """Run ``signal-cli link`` in a thread, extract the sgnl:// URL."""
        import asyncio as _asyncio

        from backend import find_signal_cli

        def _run() -> str:
            args = [
                str(find_signal_cli()),
                "link",
                "-n",
                self._device_name,
            ]
            logger.info("Starting signal-cli link: %s", args)
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            self._linking_proc = proc

            link_found = None
            for line in iter(proc.stdout.readline, ""):
                line = line.rstrip()
                logger.debug("signal-cli: %s", line)
                match = re.search(r"((?:sgnl|signal)://link[^\s]*)", line)
                if match:
                    link_found = match.group(1)
                    break
                if "error" in line.lower() or "cannot" in line.lower():
                    logger.warning("signal-cli error line: %s", line)

            if link_found:
                logger.info("Signal link URL found: %s...", link_found[:40])
                return link_found
            raise RuntimeError(
                "Could not find Signal link URL in signal-cli output. "
                "Is signal-cli configured correctly?"
            )

        return await _asyncio.to_thread(_run)

    async def _get_whatsapp_qr(self) -> str:
        """Get a WhatsApp pairing QR.

        If the session is already WORKING → info message.
        If the session is FAILED / stopped → restarts and gets a fresh QR.
        Otherwise tries the current QR first, then falls back to a fresh one.
        """
        import asyncio as _asyncio

        app = self.app
        wa = getattr(app, "whatsapp_backend", None)
        if wa is None or wa._rest is None:
            raise RuntimeError("WhatsApp backend not available")

        def _run() -> str:
            status = wa._rest.get_session_status() or {}
            s = str(status.get("status") or "").lower()

            if s == "working":
                return "ALREADY_CONNECTED"

            # If the session is in a dead/failed state, restart it
            restart_states = {"failed", "stopped", "stop", ""}
            if s in restart_states:
                logger.info("WhatsApp session is %s, restarting for fresh QR...", s)
                qr = wa._rest.get_fresh_pairing_qr(reset=True)
                if isinstance(qr, bytes):
                    from qr_utils import qr_png_to_ascii

                    return qr_png_to_ascii(qr)
                elif isinstance(qr, str):
                    return qr
                raise RuntimeError(f"Failed to restart session (status was: {s})")

            # Try current QR, then fresh
            qr = wa._rest.get_pairing_qr()
            if isinstance(qr, bytes):
                from qr_utils import qr_png_to_ascii

                return qr_png_to_ascii(qr)
            elif isinstance(qr, str):
                return qr

            # Last resort: fresh QR without reset
            qr = wa._rest.get_fresh_pairing_qr(reset=False)
            if isinstance(qr, bytes):
                from qr_utils import qr_png_to_ascii

                return qr_png_to_ascii(qr)
            elif isinstance(qr, str):
                return qr

            raise RuntimeError(
                f"No QR available (session status: {s or 'unknown'}). "
                "The session may already be linked."
            )

        result = await _asyncio.to_thread(_run)
        if result == "ALREADY_CONNECTED":
            return "INFO: WhatsApp is already linked and working ✅\nNo QR needed."
        return result

    async def _get_telegram_qr_link(self) -> str:
        """Get a Telegram pairing QR link via the backend."""
        import asyncio as _asyncio

        app = self.app
        tb = getattr(app, "telegram_backend", None)
        if tb is None:
            raise RuntimeError("Telegram backend not available")

        # get_pairing_qr is sync (manages its own event loop internally)
        # Run in thread to avoid blocking the UI
        return await _asyncio.to_thread(tb.get_pairing_qr) or "ERROR: No QR data"

    # ── Hooks ──────────────────────────────────────────────────────────────

    def _should_show_phone_input(self, protocol: str) -> bool:
        """Decide whether to show the phone input phase.

        Signal: only when phone number is not already configured.
        WhatsApp / Telegram: never (phone not needed for pairing).
        """
        if self._force_phone_input:
            return True
        if protocol == "signal":
            return not self._signal_number
        return False

    def _get_qr_data(self, phone: str) -> str:
        """Legacy fake-data hook (kept for tests, not used in normal flow)."""
        proto = self._selected_protocol
        if proto == "signal":
            return f"sgnl://linkdevice?uuid=test-{phone}-{self._device_name}"
        elif proto == "whatsapp":
            return "WA:fake-pairing-code-12345-for-ui-testing"
        return f"fake-{proto}-link"

    # ── Cleanup on dismiss ─────────────────────────────────────────────────

    def dismiss(self, result: None = None) -> None:
        """Kill any running subprocess and stop workers before dismissing."""
        # Signal polling worker to stop
        self._phase = "done"
        # Kill subprocess if still running
        proc = getattr(self, "_linking_proc", None)
        if proc is not None and proc.poll() is None:
            logger.info("Killing signal-cli link subprocess (PID %s)", proc.pid)
            try:
                proc.terminate()
            except Exception as _e:
                logger.debug("Failed to terminate link subprocess", exc_info=True)
        self._linking_proc = None
        super().dismiss(result)

    # ── Event handlers ─────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses across all phases."""
        btn_id = event.button.id

        if btn_id == "link-phone-start":
            self._on_phone_confirm()
        elif btn_id == "link-phone-back":
            self._on_go_back()
        elif btn_id == "link-qr-cancel":
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter in input fields."""
        if event.input.id in ("link-phone-input", "link-device-input"):
            self._on_phone_confirm()
        elif event.input.id == "link-2fa-input":
            self._on_2fa_submit()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle click / Enter on a protocol in the ListView."""
        if self._phase != "picker":
            return
        lv = self.query_one("#link-protocol-list", ListView)
        if lv.index is not None:
            self._select_protocol(lv.index)

    def action_select_item(self) -> None:
        """Enter: select protocol, confirm phone, or submit 2FA."""
        if self._phase == "picker":
            lv = self.query_one("#link-protocol-list", ListView)
            idx = lv.index
            if idx is not None:
                self._select_protocol(idx)
        elif self._phase == "phone":
            self._on_phone_confirm()
        elif self._phase == "qr":
            # If 2FA input is visible, submit the password
            try:
                inp = self.query_one("#link-2fa-input", Input)
                if inp.value.strip():
                    self._on_2fa_submit()
            except Exception as _e:
                logger.debug("Failed to read 2FA input", exc_info=True)

    # ── Internal helpers ───────────────────────────────────────────────────

    def _select_protocol(self, index: int) -> None:
        """Handle protocol selection from the picker list."""
        filtered = [
            item
            for item in _PROTOCOL_ITEMS
            if not (item["id"] == "whatsapp" and not self._has_whatsapp)
        ]
        if index < 0 or index >= len(filtered):
            return
        item = filtered[index]
        if item["disabled"]:
            return

        self._selected_protocol = item["id"]
        logger.info("Device link: selected protocol=%s", self._selected_protocol)

        if self._should_show_phone_input(self._selected_protocol):
            self._transition_to_phone()
        else:
            self._transition_to_qr(phone="")

    def _on_phone_confirm(self) -> None:
        """User confirmed phone number → move to QR phase."""
        phone = self.query_one("#link-phone-input", Input).value.strip()
        device = self.query_one("#link-device-input", Input).value.strip()
        if device:
            self._device_name = device
        self._transition_to_qr(phone)

    def _on_2fa_submit(self) -> None:
        """User submitted 2FA password — complete login."""
        app = self.app
        tb = getattr(app, "telegram_backend", None)
        if tb is None:
            return
        password = self.query_one("#link-2fa-input", Input).value.strip()
        if not password:
            return
        try:
            status = self.query_one("#link-qr-status", Static)
            status.update("⏳ Verifying 2FA password...")
        except Exception as _e:
            logger.debug("Failed to update 2FA verifying status", exc_info=True)
        # Run in thread to not block UI
        self.run_worker(self._complete_2fa_worker(tb, password), exclusive=False)

    async def _complete_2fa_worker(self, tb, password: str) -> None:
        """Worker: call complete_2fa and update UI."""
        import asyncio as _asyncio

        def _run():
            return tb.complete_2fa(password)

        success = await _asyncio.to_thread(_run)
        if success:
            try:
                self.query_one("#link-qr-status", Static).update(
                    "✅ Device linked successfully!"
                )
                self.query_one("#link-qr-code", Static).update("\n\n✅ Linked!\n")
            except Exception as _e:
                logger.debug("Failed to update linked status", exc_info=True)
            await _asyncio.sleep(2)
            self.dismiss(None)
        else:
            try:
                self.query_one("#link-qr-status", Static).update(
                    "❌ Wrong password — try again"
                )
                inp = self.query_one("#link-2fa-input", Input)
                inp.value = ""
                inp.focus()
            except Exception as _e:
                logger.debug("Failed to update wrong-password status", exc_info=True)

    def _on_go_back(self) -> None:
        """Go back from phone phase to picker phase."""
        self._show_phase("picker")
        self.query_one("#link-protocol-list", ListView).focus()
