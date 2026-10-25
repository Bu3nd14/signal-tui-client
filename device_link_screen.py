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

import asyncio
import base64
import logging
import time
from typing import Any, ClassVar

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

from models import DEFAULT_SIGNAL_DEVICE_NAME
from qr_utils import qr_png_to_ascii, qr_to_ascii
from server.client import ApiError

logger = logging.getLogger(__name__)

# ─── Protocol items (extensible) ───────────────────────────────────────────────

_PROTOCOL_ITEMS: list[dict[str, str]] = [
    {"id": "signal", "label": "📶 Signal", "disabled": False},
    {"id": "whatsapp", "label": "💬 WhatsApp", "disabled": False},
    {"id": "telegram", "label": "📨 Telegram", "disabled": False},
]


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
        Binding("ctrl+q", "quit_app", "Quit", priority=True),
    ]

    # ── Constructor ────────────────────────────────────────────────────────

    def __init__(
        self,
        signal_number: str = "",
        has_whatsapp: bool = False,
        has_telegram: bool = False,
        force_phone_input: bool = False,
        client: Any = None,
    ) -> None:
        super().__init__()
        #: The server client the flow talks to (default: the app's ``client``).
        self._link_client = client
        self._signal_number = signal_number
        self._has_whatsapp = has_whatsapp
        self._has_telegram = has_telegram
        self._force_phone_input = force_phone_input
        self._phase: str = "picker"
        self._selected_protocol: str = ""
        self._device_name: str = DEFAULT_SIGNAL_DEVICE_NAME
        self._signal_finalizing = False
        self._dismiss_after_finalize = False
        # Protocolli il cui flusso QR è stato effettivamente avviato in questa
        # schermata: al dismiss servono a riconnettere solo i backend toccati.
        self._touched_protocols: set[str] = set()
        #: Protocol whose link the server is running for this screen; the
        #: server is told to cancel it on dismiss.
        self._active_link: str | None = None

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
            unavailable_reason = self._protocol_unavailable_reason(item["id"])
            label_text = item["label"]
            if unavailable_reason:
                label_text = f"{label_text}  ({unavailable_reason})"
            disabled = item["disabled"] or bool(unavailable_reason)
            label = Label(label_text)
            if disabled:
                label.add_class("disabled-item")
            li = ListItem(label, disabled=disabled)
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
        if self._selected_protocol != "signal":
            self._touched_protocols.add(self._selected_protocol)
        self._populate_qr_phase("⏳ Generating QR code...", phone)
        self._show_phase("qr")
        self._active_link = self._selected_protocol
        self.run_worker(self._fetch_real_qr(phone), exclusive=True)

    # ── Polling for completion ──────────────────────────────────────────────

    async def _poll_completion(self, phone: str) -> None:
        """After QR is shown, poll for linking completion.

        Signal: monitors the ``signal-cli link`` subprocess exit code.
        WhatsApp: polls WAHA session status; refreshes QR on expiry.
        Both timeout after 5 minutes.
        """
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
                if proto == "signal":
                    self._signal_finalizing = True
                    try:
                        await self._finalize_signal_link(phone)
                    except Exception as exc:
                        self._signal_finalizing = False
                        if self._dismiss_after_finalize:
                            self.dismiss(None)
                            return
                        logger.exception("Failed to configure linked Signal account")
                        try:
                            status = self.query_one("#link-qr-status", Static)
                            status.update(
                                "❌ Device linked, but account configuration failed: "
                                f"{exc}"
                            )
                        except Exception:
                            logger.debug(
                                "Failed to update Signal configuration error",
                                exc_info=True,
                            )
                        return
                    except BaseException:
                        self._signal_finalizing = False
                        raise
                    self._signal_finalizing = False
                    self._touched_protocols.add("signal")
                    if self._dismiss_after_finalize:
                        self.dismiss(None)
                        return
                try:
                    status = self.query_one("#link-qr-status", Static)
                    status.update("✅ Device linked successfully!")
                    code = self.query_one("#link-qr-code", Static)
                    code.update("\n\n✅ Linked!\n")
                    code.refresh()
                except Exception as _e:
                    logger.debug("Failed to update linked status", exc_info=True)
                await asyncio.sleep(2)
                self.dismiss(None)
                return

            await asyncio.sleep(2)

        # Timeout (only if still on QR phase)
        if self._phase == "qr":
            try:
                status = self.query_one("#link-qr-status", Static)
                status.update("❌ Timed out waiting for scan")
            except Exception as _e:
                logger.debug("Failed to update timeout status", exc_info=True)

    # ── Server calls (the linking itself runs in the core) ─────────────────

    def _client(self) -> Any:
        """The client of the server running the link."""
        return self._link_client if self._link_client is not None else self.app.client

    async def _call(self, op: str, data: dict[str, Any]) -> Any:
        """Run operation *op* off the UI thread (it may block for seconds)."""
        return await asyncio.to_thread(self._client().call, op, data)

    @staticmethod
    def _render_qr(qr: dict[str, str]) -> str:
        """Text shown in the QR widget for a server QR payload."""
        kind, data = qr["kind"], qr["data"]
        if kind == "url":
            return qr_to_ascii(data)
        if kind == "png":
            return qr_png_to_ascii(base64.b64decode(data))
        if kind == "info":
            return f"\n\n{data}\n"
        if kind == "error":
            return f"\n\n❌ {data}\n"
        return data  # "text": already rendered

    def _show_qr(self, qr: dict[str, str]) -> None:
        """Replace the QR on screen."""
        code_widget = self.query_one("#link-qr-code", Static)
        code_widget.update(self._render_qr(qr))
        code_widget.refresh()

    async def _poll(self, protocol: str) -> dict[str, Any]:
        """``link.poll``; a failed poll counts as "still waiting" (the
        deadline in :meth:`_poll_completion` ends the wait)."""
        try:
            return await self._call("link.poll", {"protocol": protocol})
        except ApiError as exc:
            logger.warning("link.poll(%s) failed: %s", protocol, exc)
            return {"state": "waiting", "needs_2fa": False, "qr": None}

    async def _check_signal_done(self) -> bool:
        """Check if the signal-cli link subprocess finished successfully."""
        return (await self._poll("signal"))["state"] == "finalize"

    async def _finalize_signal_link(self, phone: str) -> None:
        """Persist the linked account and update the active Signal backend."""
        result = await self._call(
            "link.finalize", {"protocol": "signal", "phone": phone}
        )
        self._signal_number = result["account"]

    async def _check_whatsapp_done(self) -> bool:
        """Check WAHA session status; the server renews the QR when expired."""
        result = await self._poll("whatsapp")
        if result["qr"] is not None:
            try:
                self._show_qr(result["qr"])
            except Exception:
                logger.exception("Failed to refresh WhatsApp QR")
        return result["state"] == "done"

    async def _check_telegram_done(self) -> bool:
        """Check if Telegram QR login completed; the server renews the QR and
        reports when the account asks for its 2FA password."""
        result = await self._poll("telegram")
        if result["state"] == "done":
            return True

        if result["needs_2fa"]:
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

        if result["qr"] is not None:
            try:
                self._show_qr(result["qr"])
            except Exception:
                logger.exception("Failed to refresh Telegram QR")
        return False

    # ── Real QR fetching (async workers) ────────────────────────────────────

    async def _fetch_real_qr(self, phone: str) -> None:
        """Background worker: ask the server to start the link and show its QR."""
        try:
            qr = await self._start_link(phone)

            if qr["kind"] == "info":
                # Already-connected type message
                code_widget = self.query_one("#link-qr-code", Static)
                code_widget.update(f"\n\n{qr['data'].strip()}\n")
                code_widget.refresh()
                status = self.query_one("#link-qr-status", Static)
                status.update("")
                return

            code_widget = self.query_one("#link-qr-code", Static)
            code_widget.update(self._render_qr(qr))
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

    async def _start_link(self, phone: str) -> dict[str, str]:
        """Start the selected protocol's link on the server; returns its QR.

        Signal: a link URL (``signal-cli link`` runs in the core).  WhatsApp:
        a PNG/text QR, or an "already linked" info.  Telegram: a login URL.
        """
        result = await self._call(
            "link.start",
            {
                "protocol": self._selected_protocol,
                "phone": phone,
                "device_name": self._device_name,
            },
        )
        return result["qr"]

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
        if self._signal_finalizing:
            self._dismiss_after_finalize = True
            return
        # Signal polling worker to stop
        self._phase = "done"
        # Tell the server to drop the link (it kills a signal-cli subprocess)
        active, self._active_link = self._active_link, None
        if active is not None:
            try:
                self._client().call("link.cancel", {"protocol": active})
            except Exception as _e:
                logger.debug("Failed to cancel the device link", exc_info=True)
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

    def action_quit_app(self) -> None:
        """Let the global quit shortcut work while this modal has focus."""
        self.app.action_quit()

    # ── Internal helpers ───────────────────────────────────────────────────

    def _protocol_unavailable_reason(self, protocol: str) -> str:
        """Explain why an optional protocol cannot start its linking flow."""
        if protocol == "whatsapp" and not self._has_whatsapp:
            return "start or configure WAHA"
        if protocol == "telegram" and not self._has_telegram:
            return "set TELEGRAM_API_ID and TELEGRAM_API_HASH"
        return ""

    def _select_protocol(self, index: int) -> None:
        """Handle protocol selection from the picker list."""
        if index < 0 or index >= len(_PROTOCOL_ITEMS):
            return
        item = _PROTOCOL_ITEMS[index]
        if item["disabled"] or self._protocol_unavailable_reason(item["id"]):
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
        password = self.query_one("#link-2fa-input", Input).value.strip()
        if not password:
            return
        try:
            status = self.query_one("#link-qr-status", Static)
            status.update("⏳ Verifying 2FA password...")
        except Exception as _e:
            logger.debug("Failed to update 2FA verifying status", exc_info=True)
        # Run in thread to not block UI
        self.run_worker(self._complete_2fa_worker(password), exclusive=False)

    async def _complete_2fa_worker(self, password: str) -> None:
        """Worker: submit the 2FA password to the server and update UI."""
        try:
            success = (await self._call("link.submit_2fa", {"password": password}))[
                "ok"
            ]
        except ApiError as exc:
            try:
                self.query_one("#link-qr-status", Static).update(f"❌ {exc.message}")
            except Exception as _e:
                logger.debug("Failed to update 2FA error status", exc_info=True)
            return
        if success:
            try:
                self.query_one("#link-qr-status", Static).update(
                    "✅ Device linked successfully!"
                )
                self.query_one("#link-qr-code", Static).update("\n\n✅ Linked!\n")
            except Exception as _e:
                logger.debug("Failed to update linked status", exc_info=True)
            await asyncio.sleep(2)
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
