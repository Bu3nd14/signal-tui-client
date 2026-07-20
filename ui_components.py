"""
Widget personalizzati per Signal TUI Client.
Contiene i componenti UI riutilizzabili basati su Textual.
"""

from textual.containers import Vertical
from textual.widgets import Label, ListView, Input


class ContactListWidget(Vertical):
    """Colonna sinistra: lista contatti."""

    def compose(self):
        yield Label("📇 Contatti", classes="section-title")
        yield ListView(id="contact-list")

    def on_mount(self):
        self.styles.width = 30


class ChatAreaWidget(Vertical):
    """Colonna destra: area messaggi + input."""

    def compose(self):
        yield Label("💬 Chat", classes="section-title")
        yield Vertical(id="chat-log")
        yield Input(placeholder="Scrivi un messaggio...", id="message-input")
