"""
Emoji Picker for Signal TUI Client.

Provides:
- ``EmojiPickerScreen`` — a full-screen modal with categorised emoji grid,
  search bar, and keyboard navigation.
- ``EmojiCompletion`` — a popup that suggests emoji when the user types
  ``:code:``-style aliases in the message input.
- Helper functions to convert ``:alias:`` patterns in text to actual emoji.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import ClassVar

import emoji
from rich.text import Text as RichText
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Static, Button, Label

logger = logging.getLogger(__name__)

# ─── Emoji database helpers ──────────────────────────────────────────────────

# Regex to match :emoji_name: patterns (including aliases)
EMOJI_ALIAS_RE = re.compile(r":([a-zA-Z0-9_+\-&().]+?):")

# Build a lookup: alias (without colons) -> emoji character
_ALIAS_TO_EMOJI: dict[str, str] = {}
_EMOJI_TO_ALIAS: dict[str, str] = {}

for char, data in emoji.EMOJI_DATA.items():
    en_name: str = data.get("en", "")
    # Strip colons from the canonical name
    alias = en_name.strip(":")
    if alias:
        _ALIAS_TO_EMOJI[alias] = char
        _EMOJI_TO_ALIAS[char] = alias
    # Also register any additional aliases
    for alt in data.get("alias", []):
        alt_clean = alt.strip(":")
        if alt_clean:
            _ALIAS_TO_EMOJI[alt_clean] = char


def replace_emoji_aliases(text: str) -> str:
    """Replace ``:emoji_name:`` patterns in *text* with actual emoji characters.

    Example: ``"I :heart: you"`` → ``"I ❤️ you"``
    """
    def _replacer(m: re.Match) -> str:
        name = m.group(1).lower()
        return _ALIAS_TO_EMOJI.get(name, m.group(0))
    return EMOJI_ALIAS_RE.sub(_replacer, text)


def search_emoji(query: str, max_results: int = 30) -> list[tuple[str, str]]:
    """Search emoji by name.

    Returns a list of ``(emoji_char, alias)`` tuples whose alias contains
    *query* (case-insensitive).
    """
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
    """Get emoji suggestions for an incomplete ``:prefix``.

    Returns emoji whose alias starts with *prefix* (case-insensitive).
    """
    p = prefix.lower().replace("_", " ").replace("-", " ")
    results: list[tuple[str, str]] = []
    for char, alias in _EMOJI_TO_ALIAS.items():
        name = alias.replace("_", " ").replace("-", " ")
        if name.startswith(p):
            results.append((char, alias))
            if len(results) >= max_results:
                break
    return results


# ─── Emoji categories ────────────────────────────────────────────────────────

# Manually curated categories with representative emoji.
# Each category has a label, an icon, and a list of emoji characters.
@dataclass
class EmojiCategory:
    """A category of emoji with a label and icon."""
    label: str
    icon: str
    emojis: list[str] = field(default_factory=list)


# Build categories from the emoji database using keyword matching
def _build_categories() -> list[EmojiCategory]:
    """Build emoji categories by matching keywords in emoji names."""
    categories: list[EmojiCategory] = [
        EmojiCategory("Smileys & People", "😀"),
        EmojiCategory("Gestures & Body", "👋"),
        EmojiCategory("Animals & Nature", "🐻"),
        EmojiCategory("Food & Drink", "🍔"),
        EmojiCategory("Travel & Places", "🚗"),
        EmojiCategory("Activities", "⚽"),
        EmojiCategory("Objects", "💡"),
        EmojiCategory("Symbols", "❤️"),
        EmojiCategory("Flags", "🏁"),
    ]

    # Keyword maps for each category
    category_keywords: dict[str, list[str]] = {
        "Smileys & People": [
            "face", "smile", "laugh", "happy", "sad", "cry", "angry", "wink",
            "kiss", "mouth", "nose", "ear", "eye", "hair", "tongue", "person",
            "man", "woman", "boy", "girl", "baby", "people", "family", "couple",
            "skin", "tone", "fairy", "angel", "devil", "skull", "ghost", "alien",
            "robot", "emoji", "grin", "joy", "blush", "heart_eyes",
        ],
        "Gestures & Body": [
            "hand", "finger", "thumb", "wave", "clap", "fist", "arm", "leg",
            "foot", "point", "ok", "muscle", "pray", "writing", "nail",
            "dance", "bow", "raise", "cross", "sign", "selfie", "middle",
        ],
        "Animals & Nature": [
            "cat", "dog", "rabbit", "fox", "bear", "panda", "koala", "lion",
            "tiger", "cow", "pig", "frog", "monkey", "chicken", "bird", "eagle",
            "duck", "owl", "bat", "wolf", "horse", "unicorn", "whale", "dolphin",
            "fish", "snake", "turtle", "dragon", "bee", "butterfly", "snail",
            "bug", "ant", "spider", "flower", "rose", "tree", "leaf", "mushroom",
            "plant", "cactus", "palm", "seed",
        ],
        "Food & Drink": [
            "apple", "banana", "grape", "melon", "watermelon", "orange",
            "lemon", "pineapple", "cherry", "strawberry", "blueberry",
            "peach", "pear", "avocado", "eggplant", "tomato", "carrot",
            "corn", "pizza", "burger", "fries", "hotdog", "sandwich",
            "taco", "sushi", "rice", "noodle", "spaghetti", "bread",
            "cake", "cookie", "chocolate", "candy", "donut", "ice_cream",
            "coffee", "tea", "beer", "wine", "cocktail", "milk", "juice",
            "fork", "knife", "spoon", "plate", "bowl", "egg", "cheese",
        ],
        "Travel & Places": [
            "car", "bus", "train", "plane", "airplane", "rocket", "bicycle",
            "motorcycle", "boat", "ship", "anchor", "house", "building",
            "city", "mountain", "volcano", "beach", "desert", "island",
            "sun", "moon", "star", "cloud", "rain", "snow", "lightning",
            "rainbow", "earth", "globe", "map", "compass", "clock",
        ],
        "Activities": [
            "ball", "soccer", "football", "basketball", "baseball", "tennis",
            "golf", "bowling", "game", "sport", "medal", "trophy", "winner",
            "music", "guitar", "drum", "trumpet", "violin", "microphone",
            "headphone", "radio", "video", "movie", "clapper", "ticket",
            "art", "palette", "paint", "draw", "book", "pencil", "pen",
            "camera", "phone", "computer", "keyboard", "mouse",
        ],
        "Objects": [
            "light", "bulb", "flashlight", "book", "newspaper", "magazine",
            "letter", "envelope", "package", "gift", "money", "coin", "dollar",
            "credit", "card", "key", "lock", "bell", "clock", "watch",
            "glass", "cup", "bottle", "bag", "shoe", "clothing", "shirt",
            "hat", "ring", "crown", "tool", "hammer", "wrench", "screwdriver",
            "gear", "chain", "magnet", "lamp", "door", "window", "chair",
            "table", "bed", "toilet", "shower", "bath", "soap", "toothbrush",
        ],
        "Symbols": [
            "heart", "love", "kiss", "sparkle", "fire", "cool", "100",
            "warning", "prohibited", "check", "cross", "question", "exclamation",
            "arrow", "star", "circle", "square", "triangle", "peace", "yin",
            "yang", "wheelchair", "restroom", "no_smoking", "radioactive",
            "biohazard", "recycle", "infinity", "heavy_dollar",
        ],
        "Flags": [
            "flag", "england", "scotland", "wales", "rainbow", "pirate",
            "checkered", "triangle", "crossed",
        ],
    }

    # Assign emoji to categories based on keyword matching
    assigned: set[str] = set()
    for cat in categories:
        keywords = category_keywords.get(cat.label, [])
        for char, alias in _EMOJI_TO_ALIAS.items():
            if char in assigned:
                continue
            name = alias.lower().replace("_", " ").replace("-", " ")
            for kw in keywords:
                if kw in name:
                    cat.emojis.append(char)
                    assigned.add(char)
                    break

    # Add any unassigned emoji to "Smileys & People" as fallback
    unassigned = [c for c in _EMOJI_TO_ALIAS if c not in assigned]
    if unassigned:
        categories[0].emojis.extend(unassigned[:200])  # limit to 200

    return categories


EMOJI_CATEGORIES = _build_categories()


# ─── Emoji Picker Screen ─────────────────────────────────────────────────────

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
        width: 60;
        height: 70%;
        min-height: 20;
        border: thick $accent;
        background: $surface;
        padding: 1;
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
        height: 3;
        overflow-x: auto;
        overflow-y: hidden;
        margin: 0 0 1 0;
    }

    .emoji-cat-btn {
        min-width: 4;
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
        padding: 0 0 1 0;
    }

    #emoji-grid-container {
        layout: grid;
        grid-size: 8;
        grid-gutter: 0;
        width: 100%;
        height: auto;
    }

    .emoji-cell {
        width: 100%;
        height: 2;
        text-align: center;
        padding: 0;
        border: none;
        background: transparent;
        color: $text;
        min-width: 4;
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
        self._categories = EMOJI_CATEGORIES
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
            with Horizontal(id="emoji-category-tabs"):
                for i, cat in enumerate(self._categories):
                    btn = Button(
                        f"{cat.icon}",
                        id=f"emoji-cat-{i}",
                        classes="emoji-cat-btn",
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
        search_input = self.query_one("#emoji-search", Input)
        search_input.focus()

    # ── Category navigation ──────────────────────────────────────────────

    def _activate_category(self, index: int) -> None:
        """Switch to the given category index and render its emoji."""
        if index < 0 or index >= len(self._categories):
            return
        self._current_cat_index = index
        self._search_query = ""

        # Update tab button styles
        for i, cat in enumerate(self._categories):
            btn = self.query_one(f"#emoji-cat-{i}", Button)
            if i == index:
                btn.classes = "emoji-cat-btn emoji-cat-btn-active"
            else:
                btn.classes = "emoji-cat-btn"

        # Render the emoji grid for this category
        self._render_grid(self._categories[index].emojis)

    def _render_grid(self, emojis: list[str]) -> None:
        """Fill the grid container with emoji buttons."""
        grid = self.query_one("#emoji-grid-container", Vertical)
        grid.remove_children()
        for em_char in emojis:
            btn = Button(em_char, classes="emoji-cell")
            grid.mount(btn)
        # Focus the first emoji if any
        if emojis:
            first = grid.children[0]
            first.focus()

    # ── Search ───────────────────────────────────────────────────────────

    def _perform_search(self, query: str) -> None:
        """Filter emoji by search query and render results."""
        self._search_query = query
        if not query.strip():
            # Revert to current category
            self._activate_category(self._current_cat_index)
            return

        results = search_emoji(query, max_results=80)
        self._filtered_emojis = [char for char, _ in results]
        self._render_grid(self._filtered_emojis)

    def on_input_changed(self, event: Input.Changed) -> None:
        """Handle search input changes."""
        if event.input.id == "emoji-search":
            self._perform_search(event.value)

    # ── Button / navigation handlers ─────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle emoji selection and category switching."""
        btn_id = event.button.id or ""

        # Category tab buttons
        if btn_id.startswith("emoji-cat-"):
            idx = int(btn_id.split("-")[-1])
            self._activate_category(idx)
            return

        # Emoji cell buttons (no specific id)
        if "emoji-cell" in event.button.classes:
            emoji_char = event.button.label
            if emoji_char:
                self.dismiss(emoji_char)
            return

    def action_next_category(self) -> None:
        """Switch to the next category."""
        next_idx = (self._current_cat_index + 1) % len(self._categories)
        self._activate_category(next_idx)

    def action_prev_category(self) -> None:
        """Switch to the previous category."""
        prev_idx = (self._current_cat_index - 1) % len(self._categories)
        self._activate_category(prev_idx)

    def action_focus_search(self) -> None:
        """Focus the search input."""
        search_input = self.query_one("#emoji-search", Input)
        search_input.focus()

    def key_escape(self) -> None:
        """Close the picker without selecting."""
        self.dismiss(None)

    def key_q(self) -> None:
        """Close the picker without selecting (alternative)."""
        self.dismiss(None)


# ─── Emoji Completion Popup ──────────────────────────────────────────────────

class EmojiCompletionWidget(Static):
    """A small popup that shows emoji suggestions when the user types
    ``:partial_name`` in the message input.

    This widget is mounted inside the chat area and shows/hides based on
    whether the user is typing an emoji alias.
    """

    DEFAULT_CSS = """
    EmojiCompletionWidget {
        dock: bottom;
        height: auto;
        max-height: 10;
        width: 40;
        layer: overlay;
        background: $surface;
        border: solid $accent;
        padding: 0 1;
        overflow-y: auto;
        display: none;
        margin: 0 1;
    }

    EmojiCompletionWidget.visible {
        display: block;
    }

    .completion-item {
        padding: 0 1;
        color: $text;
    }

    .completion-item:hover {
        background: $accent 20%;
    }

    .completion-item-highlight {
        background: $accent 40%;
        color: $text;
        text-style: bold;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._suggestions: list[tuple[str, str]] = []
        self._selected_index = 0
        self._prefix = ""


    def show_suggestions(self, prefix: str) -> bool:
        """Show suggestions for the given prefix.

        Returns True if there are suggestions to show.
        """
        self._prefix = prefix
        self._suggestions = get_emoji_suggestions(prefix, max_results=8)
        self._selected_index = 0

        if not self._suggestions:
            self.hide_suggestions()
            return False

        self._render()
        self.add_class("visible")
        return True

    def hide_suggestions(self) -> None:
        """Hide the completion popup."""
        self.remove_class("visible")
        self._suggestions = []
        self._prefix = ""

    def select_next(self) -> None:
        """Move selection down."""
        if not self._suggestions:
            return
        self._selected_index = (self._selected_index + 1) % len(self._suggestions)
        self._render()

    def select_prev(self) -> None:
        """Move selection up."""
        if not self._suggestions:
            return
        self._selected_index = (self._selected_index - 1) % len(self._suggestions)
        self._render()

    @property
    def selected_emoji(self) -> str | None:
        """Return the currently selected emoji character, or None."""
        if self._suggestions and 0 <= self._selected_index < len(self._suggestions):
            return self._suggestions[self._selected_index][0]
        return None

    @property
    def selected_alias(self) -> str | None:
        """Return the currently selected emoji alias (without colons)."""
        if self._suggestions and 0 <= self._selected_index < len(self._suggestions):
            return self._suggestions[self._selected_index][1]
        return None

    def _render(self) -> None:
        """Rebuild the suggestion list display."""
        # We use a simple approach: update the text content
        lines: list[str] = []
        for i, (char, alias) in enumerate(self._suggestions):
            marker = "▸" if i == self._selected_index else " "
            lines.append(f"{marker} {char}  :{alias}:")
        self.update("\n".join(lines))
