"""Exit dialog of a TUI that is a client of a server (design §5).

``ExitDialogScreen`` offers **Detach** (close this client, the server keeps
running), **Quit** (stop the server, then exit) and **Cancel**.  The default
button is Quit when this TUI spawned the server and Detach when it attached to
one that was already running.  When other clients are connected the dialog says
so and Quit needs a second confirmation.  The screen only returns the choice;
``tui.exit_flow`` acts on it.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

ExitChoice = Literal["detach", "quit", "cancel"]

TITLE = "Esci da Megachat"
BODY = (
    "Detach: chiude questo client, il server resta attivo.\n"
    "Quit: arresta il server ed esce."
)
CONFIRM_TITLE = "Arrestare il server?"
CONFIRM_BODY = "Gli altri client collegati perderanno la connessione."


def other_clients_text(count: int) -> str:
    """The warning line for *count* other connected clients ('' for none)."""
    if count <= 0:
        return ""
    if count == 1:
        return "1 altro client connesso"
    return f"Altri {count} client connessi"


class ExitDialogScreen(ModalScreen[str]):
    """Detach / Quit / Cancel.  Dismisses with an :data:`ExitChoice`."""

    DEFAULT_CSS = """
    ExitDialogScreen {
        align: center middle;
        background: $surface 80%;
    }

    #exit-dialog {
        width: 60;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }

    #exit-title {
        text-style: bold;
        text-align: center;
        padding: 0 0 1 0;
        color: $text;
    }

    #exit-body {
        padding: 0 0 1 0;
        color: $text;
    }

    #exit-others {
        color: $warning;
        text-style: bold;
        padding: 0 0 1 0;
    }

    #exit-buttons {
        width: 100%;
        height: auto;
        align: center middle;
    }

    #exit-buttons Button {
        margin: 0 1;
    }

    #exit-footer {
        text-align: center;
        color: $text-muted;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Annulla", priority=True),
        Binding("d", "detach", "Detach", priority=True),
        Binding("q", "quit_server", "Quit", priority=True),
        Binding("left", "focus_previous", show=False, priority=True),
        Binding("right", "focus_next", show=False, priority=True),
    ]

    def __init__(self, *, default_quit: bool, other_clients: int = 0) -> None:
        super().__init__()
        self._default_quit = default_quit
        self._others = max(0, other_clients)
        self._confirming = False

    def compose(self) -> ComposeResult:
        with Vertical(id="exit-dialog"):
            yield Label(TITLE, id="exit-title")
            yield Static(BODY, id="exit-body")
            yield Static(other_clients_text(self._others), id="exit-others")
            with Horizontal(id="exit-buttons"):
                yield Button("Detach (d)", id="exit-detach")
                yield Button("Quit (q)", id="exit-quit", variant="error")
                yield Button("Annulla (Esc)", id="exit-cancel")
            yield Static("←/→ Tab: scegli · Invio: conferma", id="exit-footer")

    def on_mount(self) -> None:
        self.query_one("#exit-others").display = self._others > 0
        first = "#exit-quit" if self._default_quit else "#exit-detach"
        self.query_one(first, Button).focus()

    # -- actions ----------------------------------------------------------------

    def action_cancel(self) -> None:
        self.dismiss("cancel")

    def action_detach(self) -> None:
        if not self._confirming:
            self.dismiss("detach")

    def action_quit_server(self) -> None:
        if self._others > 0 and not self._confirming:
            self._ask_confirmation()
            return
        self.dismiss("quit")

    def action_focus_previous(self) -> None:
        self.focus_previous()

    def action_focus_next(self) -> None:
        self.focus_next()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "exit-detach":
            self.action_detach()
        elif event.button.id == "exit-quit":
            self.action_quit_server()
        else:
            self.action_cancel()

    def _ask_confirmation(self) -> None:
        """Second step of Quit with other clients connected: the safe button
        (Annulla) is focused, Detach is no longer offered."""
        self._confirming = True
        self.query_one("#exit-title", Label).update(CONFIRM_TITLE)
        self.query_one("#exit-body", Static).update(CONFIRM_BODY)
        self.query_one("#exit-detach").display = False
        self.query_one("#exit-quit", Button).label = "Si, arresta (q)"
        self.query_one("#exit-cancel", Button).focus()
