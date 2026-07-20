"""
Custom widgets for Signal TUI Client.
Contains reusable UI components based on Textual.
"""

from textual.containers import Vertical
from textual.widgets import Label, ListView, Input


class ContactListWidget(Vertical):
    """Left column: contact list."""

    def compose(self):
        yield Label("📇 Contacts", classes="section-title")
        yield ListView(id="contact-list")

    def on_mount(self):
        self.styles.width = 30


class ChatAreaWidget(Vertical):
    """Right column: messages area + input."""

    def compose(self):
        yield Label("💬 Chat", classes="section-title")
        yield Vertical(id="chat-log")
        yield Input(placeholder="Type a message...", id="message-input")
