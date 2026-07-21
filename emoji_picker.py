"""
Emoji Picker for Signal TUI Client.

Provides:
- ``EmojiPickerScreen`` — a full-screen modal with categorised emoji grid,
  search bar, and keyboard navigation.
- ``EmojiCompletionWidget`` — a popup that suggests emoji when the user types
  ``:code:``-style aliases in the message input.
- Helper functions to convert ``:alias:`` patterns in text to actual emoji.
"""

from __future__ import annotations

import logging
import re
from typing import ClassVar

import emoji
from rich.cells import cell_len
from rich.text import Text as RichText
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, Grid
from textual.screen import ModalScreen
from textual.widgets import Input, Static, Button, Label

from emoji_data import PREDEFINED_CATEGORIES

logger = logging.getLogger(__name__)

# ─── Emoji database helpers ──────────────────────────────────────────────────

# Regex to match :emoji_name: patterns (including aliases)
EMOJI_ALIAS_RE = re.compile(r":([a-zA-Z0-9_+\-&().]+?):")

# Build a lookup: alias (without colons) -> emoji character
_ALIAS_TO_EMOJI: dict[str, str] = {}
_EMOJI_TO_ALIAS: dict[str, str] = {}

for char, data in emoji.EMOJI_DATA.items():
    en_name: str = data.get("en", "")
    alias = en_name.strip(":")
    if alias:
        _ALIAS_TO_EMOJI[alias] = char
        _EMOJI_TO_ALIAS[char] = alias
    for alt in data.get("alias", []):
        alt_clean = alt.strip(":")
        if alt_clean:
            _ALIAS_TO_EMOJI[alt_clean] = char


def replace_emoji_aliases(text: str) -> str:
    """Replace ``:emoji_name:`` patterns in *text* with actual emoji characters."""
    def _replacer(m: re.Match) -> str:
        name = m.group(1).lower()
        return _ALIAS_TO_EMOJI.get(name, m.group(0))
    return EMOJI_ALIAS_RE.sub(_replacer, text)


def search_emoji(query: str, max_results: int = 30) -> list[tuple[str, str]]:
    """Search emoji by name (case-insensitive)."""
    q = query.lower().replace("_", " ").replace("-", " ")
    results: list[tuple[str, str]] = []
    for char, alias in _EMOJI_TO_ALIAS.items():
        name = alias.replace("_", " ").replace("-", " ")
        if q in name:
            results.append((char, alias))
            if len(results) >= max_results:
                break
    return results


def get_emoji_suggestions(prefix: str, max_results: int = 10) -> list[tuple[str, str]]:
    """Get emoji suggestions for an incomplete ``:prefix``."""
    p = prefix.lower().replace("_", " ").replace("-", " ")
    results: list[tuple[str, str]] = []
    for char, alias in _EMOJI_TO_ALIAS.items():
        name = alias.replace("_", " ").replace("-", " ")
        if name.startswith(p):
            results.append((char, alias))
            if len(results) >= max_results:
                break
    return results


def _get_categories() -> list[tuple[str, str, list[str]]]:
    """Return predefined emoji categories (fast, no iteration)."""
    return PREDEFINED_CATEGORIES


# ─── Emoji width normalisation ────────────────────────────────────────────────

def _normalize_emoji_width(char: str) -> str:
    """Ensure an emoji character occupies exactly 2 terminal columns.

    Some emoji are single-width (1 column) while others are double-width
    (2 columns).  When rendered in a grid, this causes misalignment.
    This function:
    1. Removes Variation Selector-16 (\\ufe0f) which can confuse width calc
    2. Pads single-width emoji with a trailing space so that
       every emoji takes up exactly 2 columns.
    """
    # Remove Variation Selector-16 which can cause width calculation issues
    char = char.replace("\ufe0f", "")
    width = cell_len(char)
    if width < 2:
        return char + " "
    return char


# ─── Emoji Picker Screen ─────────────────────────────────────────────────────


class EmojiCell(Static):
    """A single emoji cell in the grid — clickable and focusable."""

    def __init__(self, emoji_char: str) -> None:
        display = _normalize_emoji_width(emoji_char)
        super().__init__(display, classes="emoji-cell")
        self.emoji_char = emoji_char
        self.can_focus = True

    def on_click(self) -> None:
        """Click → dismiss the picker with this emoji."""
        # Walk up to the screen and dismiss
        screen = self.app.screen
        if isinstance(screen, EmojiPickerScreen):
            screen.dismiss(self.emoji_char)


class EmojiPickerScreen(ModalScreen[str]):
    """Full-screen modal emoji picker with categories, search, and grid view.

    When the user selects an emoji, the screen dismisses and returns the
    selected emoji character via ``self.dismiss(emoji_char)``.
    """

    DEFAULT_CSS = """
    EmojiPickerScreen {
        align: center middle;
        background: $surface 80%;
    }

    #emoji-picker-container {
        width: 66;
        height: 70%;
        min-height: 20;
        border: thick $accent;
        background: $surface;
        padding: 0 1;
    }

    #emoji-picker-title {
        text-style: bold;
        text-align: center;
        padding: 0 0 1 0;
        color: $text;
    }

    #emoji-search {
        dock: top;
        margin: 0 0 1 0;
    }

    #emoji-category-tabs {
        dock: top;
        height: auto;
        max-height: 6;
        overflow-x: auto;
        overflow-y: auto;
        margin: 0 0 1 0;
    }

    .emoji-cat-btn {
        min-width: 5;
        height: 3;
        text-align: center;
        padding: 0 1;
        border: none;
        background: transparent;
        color: $text-muted;
    }

    .emoji-cat-btn:hover {
        background: $accent 20%;
        color: $text;
    }

    .emoji-cat-btn-active {
        background: $accent 30%;
        color: $text;
        text-style: bold;
    }

    #emoji-grid {
        height: 1fr;
        overflow-y: auto;
        overflow-x: hidden;
        padding: 0;
        scrollbar-gutter: stable;
    }

    #emoji-grid-container {
        layout: grid;
        grid-size: 8;
        grid-gutter: 0;
        width: 100%;
        height: auto;
    }

    .emoji-cell {
        height: 1;
        width: 100%;
        overflow: hidden;
        color: $text;
    }

    .emoji-cell:hover {
        background: $accent 30%;
    }

    .emoji-cell:focus {
        background: $accent 50%;
    }

    #emoji-picker-footer {
        dock: bottom;
        height: 1;
        text-align: center;
        color: $text-muted;
        padding: 0 0 0 0;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss(None)", "Close"),
        Binding("ctrl+n", "next_category", "Next Cat"),
        Binding("ctrl+p", "prev_category", "Prev Cat"),
        Binding("ctrl+f", "focus_search", "Search"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._categories = _get_categories()
        self._current_cat_index = 0
        self._search_query = ""
        self._filtered_emojis: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="emoji-picker-container"):
            yield Static("😊 Emoji Picker", id="emoji-picker-title")
            yield Input(
                placeholder="Search emoji... (type to filter)",
                id="emoji-search",
            )
            # Category tabs
            with Vertical(id="emoji-category-tabs"):
                with Horizontal():
                    for i, (label, icon, _) in enumerate(self._categories):
                        btn = Button(
                            _normalize_emoji_width(icon),
                            id=f"emoji-cat-{i}",
                            classes="emoji-cat-btn",
                            tooltip=label,
                        )
                        yield btn
            # Emoji grid
            with Vertical(id="emoji-grid"):
                yield Vertical(id="emoji-grid-container")
            yield Static(
                "↑↓←→ navigate · Enter select · Esc close · Ctrl+F search",
                id="emoji-picker-footer",
            )

    def on_mount(self) -> None:
        """Set initial state and render the first category."""
        self._activate_category(0)

    # ── Category navigation ──────────────────────────────────────────────

    def _activate_category(self, index: int) -> None:
        """Switch to the given category index and render its emoji."""
        if index < 0 or index >= len(self._categories):
            return
        self._current_cat_index = index
        self._search_query = ""

        # Update tab button styles
        for i in range(len(self._categories)):
            btn = self.query_one(f"#emoji-cat-{i}", Button)
            if i == index:
                btn.classes = "emoji-cat-btn emoji-cat-btn-active"
            else:
                btn.classes = "emoji-cat-btn"

        # Render the emoji grid for this category
        self._render_grid(self._categories[index][2])

    def _render_grid(self, emojis: list[str]) -> None:
        """Fill the grid container with emoji cells."""
        grid = self.query_one("#emoji-grid-container", Vertical)
        grid.remove_children()
        for char in emojis:
            cell = EmojiCell(char)
            grid.mount(cell)

    # ── Search ───────────────────────────────────────────────────────────

    def on_input_changed(self, event: Input.Changed) -> None:
        """Handle search input changes."""
        if event.input.id != "emoji-search":
            return
        query = event.value.strip()
        if not query:
            # Reset to current category
            self._activate_category(self._current_cat_index)
            return

        # Search across all categories
        results: list[str] = []
        for _, _, emojis in self._categories:
            for char in emojis:
                if char not in results:
                    results.append(char)
        # Filter by query (simple substring match on emoji name)
        filtered: list[str] = []
        q = query.lower()
        for char in results:
            alias = _EMOJI_TO_ALIAS.get(char, "")
            name = alias.replace("_", " ").replace("-", " ").lower()
            if q in name:
                filtered.append(char)
                if len(filtered) >= 60:
                    break

        self._render_grid(filtered)

    # ── Emoji selection ──────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle category tab clicks."""
        if event.button.id and event.button.id.startswith("emoji-cat-"):
            idx = int(event.button.id.split("-")[-1])
            self._activate_category(idx)

    # ── Keyboard navigation ──────────────────────────────────────────────

    def action_next_category(self) -> None:
        """Switch to the next category."""
        idx = (self._current_cat_index + 1) % len(self._categories)
        self._activate_category(idx)

    def action_prev_category(self) -> None:
        """Switch to the previous category."""
        idx = (self._current_cat_index - 1) % len(self._categories)
        self._activate_category(idx)

    def action_focus_search(self) -> None:
        """Focus the search input."""
        search_input = self.query_one("#emoji-search", Input)
        search_input.focus()

    def key_enter(self) -> None:
        """Enter key: select the focused emoji cell."""
        focused = self.focused
        if isinstance(focused, EmojiCell):
            self.dismiss(focused.emoji_char)

    def key_left(self) -> None:
        """Navigate left in the grid."""
        focused = self.focused
        if isinstance(focused, EmojiCell):
            grid = self.query_one("#emoji-grid-container", Vertical)
            children = list(grid.children)
            try:
                idx = children.index(focused)
                if idx > 0:
                    children[idx - 1].focus()
            except ValueError:
                pass

    def key_right(self) -> None:
        """Navigate right in the grid."""
        focused = self.focused
        if isinstance(focused, EmojiCell):
            grid = self.query_one("#emoji-grid-container", Vertical)
            children = list(grid.children)
            try:
                idx = children.index(focused)
                if idx < len(children) - 1:
                    children[idx + 1].focus()
            except ValueError:
                pass

    def key_up(self) -> None:
        """Navigate up in the grid (8 columns)."""
        focused = self.focused
        if isinstance(focused, EmojiCell):
            grid = self.query_one("#emoji-grid-container", Vertical)
            children = list(grid.children)
            try:
                idx = children.index(focused)
                target = idx - 8
                if target >= 0:
                    children[target].focus()
            except ValueError:
                pass

    def key_down(self) -> None:
        """Navigate down in the grid (8 columns)."""
        focused = self.focused
        if isinstance(focused, EmojiCell):
            grid = self.query_one("#emoji-grid-container", Vertical)
            children = list(grid.children)
            try:
                idx = children.index(focused)
                target = idx + 8
                if target < len(children):
                    children[target].focus()
            except ValueError:
                pass


# ─── Suggestion item (clickable) ─────────────────────────────────────────────


class _SuggestionWidget(Static):
    """A single emoji suggestion in the completion popup — clickable."""

    emoji_char: str = ""
    completion_widget: EmojiCompletionWidget | None = None

    def on_click(self) -> None:
        """Click → insert this emoji into the message input."""
        if self.completion_widget:
            self.completion_widget._select_and_insert(self.emoji_char)


# ─── Emoji Completion Widget ─────────────────────────────────────────────────


class EmojiCompletionWidget(Vertical):
    """A popup widget that shows emoji suggestions as the user types ``:alias:``.

    This widget is placed above the input row and is hidden by default.
    It shows a list of matching emoji; the user can navigate with Tab/Shift+Tab
    and select with Enter.
    """

    DEFAULT_CSS = """
    EmojiCompletionWidget {
        height: auto;
        max-height: 10;
        overflow-y: auto;
        overflow-x: hidden;
        background: $surface-lighten-1;
        border: solid $accent;
        margin: 0 1;
        padding: 0 1;
        display: none;
    }

    EmojiCompletionWidget.-visible {
        display: block;
    }

    .emoji-suggestion {
        padding: 0 1;
        color: $text;
    }

    .emoji-suggestion:hover {
        background: $accent 30%;
    }

    .emoji-suggestion-selected {
        background: $accent 50%;
        color: $text;
        text-style: bold;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._suggestions: list[tuple[str, str]] = []
        self._selected_index = 0

    @property
    def selected_emoji(self) -> str | None:
        """Return the currently selected emoji character, or None."""
        if self._suggestions and 0 <= self._selected_index < len(self._suggestions):
            return self._suggestions[self._selected_index][0]
        return None

    def _rebuild(self) -> None:
        """Rebuild the suggestion children."""
        self.remove_children()
        for i, (char, alias) in enumerate(self._suggestions):
            marker = _normalize_emoji_width("▸") if i == self._selected_index else " "
            emoji_display = _normalize_emoji_width(char)
            classes = "emoji-suggestion"
            if i == self._selected_index:
                classes += " emoji-suggestion-selected"
            w = _SuggestionWidget(f"{marker} {emoji_display}  :{alias}:", classes=classes)
            w.emoji_char = char
            w.completion_widget = self
            self.mount(w)

    def show_suggestions(self, prefix: str) -> None:
        """Query emoji suggestions matching *prefix* and show the widget."""
        suggestions = get_emoji_suggestions(prefix, max_results=10)
        if not suggestions:
            self.hide_suggestions()
            return
        self._suggestions = suggestions
        self._selected_index = 0
        self._rebuild()
        self.add_class("-visible")

    def hide_suggestions(self) -> None:
        """Hide the completion widget and clear suggestions."""
        self._suggestions = []
        self._selected_index = 0
        self.remove_children()
        self.remove_class("-visible")

    def select_next(self) -> None:
        """Move selection down."""
        if self._suggestions:
            self._selected_index = (self._selected_index + 1) % len(self._suggestions)
            self._rebuild()

    def select_prev(self) -> None:
        """Move selection up."""
        if self._suggestions:
            self._selected_index = (self._selected_index - 1) % len(self._suggestions)
            self._rebuild()

    def _select_and_insert(self, emoji_char: str) -> None:
        """Insert an emoji into the message input and hide suggestions.
        
        Called when the user clicks a suggestion or presses Enter.
        """
        # Walk up to the app and insert the emoji into the message input
        from textual.app import App
        app = self.app
        try:
            msg_input = app.query_one("#message-input", Input)
            value = msg_input.value
            last_colon = value.rfind(":")
            if last_colon >= 0:
                new_value = value[:last_colon] + emoji_char + " "
                msg_input.value = new_value
                msg_input.cursor_position = len(new_value)
        except Exception:
            pass
        self.hide_suggestions()
        # Refocus the input
        try:
            msg_input = app.query_one("#message-input", Input)
            msg_input.focus()
        except Exception:
            pass
