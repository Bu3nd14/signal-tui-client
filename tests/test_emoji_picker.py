"""
Regression tests for emoji_picker.py — search, alias replacement, suggestions.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from textual.containers import Vertical
from textual.widgets import Button, Input

# Ensure the project root is on sys.path so we can import the modules
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from emoji_picker import (
    _ALIAS_TO_EMOJI,
    _EMOJI_TO_ALIAS,
    EmojiCell,
    EmojiCompletionWidget,
    EmojiPickerScreen,
    _SuggestionWidget,
    get_emoji_suggestions,
    replace_emoji_aliases,
    search_emoji,
)


class TestSearchEmoji:
    """🔍 Ricerca emoji per nome."""

    def test_search_by_name(self):
        """Cerca 'smile' → deve trovare almeno un risultato."""
        results = search_emoji("smile")
        assert len(results) > 0
        # Almeno un risultato deve contenere 'smile' nel nome
        assert any("smile" in alias.lower().replace("_", " ") for _, alias in results)

    def test_search_case_insensitive(self):
        """Cerca 'SMILE' (maiuscolo) → stesso risultato di 'smile'."""
        lower = search_emoji("smile")
        upper = search_emoji("SMILE")
        assert lower == upper

    def test_search_with_underscores(self):
        """Cerca 'face_with_tears' → deve trovare 😂."""
        results = search_emoji("face_with_tears")
        assert len(results) > 0

    def test_search_no_results(self):
        """Cerca stringa inesistente → lista vuota."""
        results = search_emoji("zzzznonexistentxxxx")
        assert results == []

    def test_search_max_results(self):
        """Verifica il limite massimo di risultati."""
        results = search_emoji("a", max_results=5)
        assert len(results) <= 5

    def test_search_empty_query(self):
        """Query vuota → risultati vuoti (nessun match sensato)."""
        results = search_emoji("")
        # Con query vuota, tutti gli alias contengono "" → tutti matchano
        # Ma il limite di default è 30
        assert len(results) <= 30


class TestGetEmojiSuggestions:
    """💡 Suggerimenti emoji per autocompletamento."""

    def test_suggestions_start_with_prefix(self):
        """Prefisso 'smi' → suggerisce emoji che iniziano con 'smile'."""
        results = get_emoji_suggestions("smi")
        assert len(results) > 0
        for _, alias in results:
            name = alias.replace("_", " ").replace("-", " ")
            assert name.startswith("smi")

    def test_suggestions_no_match(self):
        """Prefisso inesistente → lista vuota."""
        results = get_emoji_suggestions("zzzzzzz")
        assert results == []

    def test_suggestions_max_results(self):
        """Verifica il limite massimo di suggerimenti."""
        results = get_emoji_suggestions("a", max_results=3)
        assert len(results) <= 3


class TestReplaceEmojiAliases:
    """🔄 Sostituzione alias :emoji: nel testo."""

    def test_replace_simple(self):
        """:smile: → 😄"""
        result = replace_emoji_aliases(":smile: ciao")
        assert "😄" in result
        assert "ciao" in result

    def test_replace_multiple(self):
        """:smile: :wave: → 😄 👋"""
        result = replace_emoji_aliases(":smile: :wave:")
        assert "😄" in result

    def test_replace_no_alias(self):
        """Testo senza alias → invariato."""
        text = "Ciao come stai?"
        assert replace_emoji_aliases(text) == text

    def test_replace_invalid_alias(self):
        """Alias inesistente → lasciato com'è."""
        text = ":questo_non_esiste:"
        assert replace_emoji_aliases(text) == text

    def test_replace_partial(self):
        """Solo alias validi vengono sostituiti."""
        result = replace_emoji_aliases(":smile: :invalid_alias_xyz:")
        assert "😄" in result
        assert ":invalid_alias_xyz:" in result


class TestEmojiDatabase:
    """🗄️ Verifica che il database emoji sia popolato correttamente."""

    def test_alias_to_emoji_populated(self):
        """La mappa _ALIAS_TO_EMOJI deve contenere almeno 'smile'."""
        assert "smile" in _ALIAS_TO_EMOJI
        assert _ALIAS_TO_EMOJI["smile"] is not None

    def test_emoji_to_alias_populated(self):
        """La mappa _EMOJI_TO_ALIAS deve contenere almeno un emoji."""
        assert len(_EMOJI_TO_ALIAS) > 0


class TestEmojiPickerScreen:
    @pytest.mark.integration
    async def test_mount_search_categories_and_navigation(self, app_for_test):
        screen = EmojiPickerScreen()
        async with app_for_test.run_test() as pilot:
            await app_for_test.push_screen(screen)
            await pilot.pause()
            grid = screen.query_one("#emoji-grid-container", Vertical)
            assert screen.query_one("#emoji-search", Input) is not None
            assert screen.query_one("#emoji-grid", Vertical) is not None
            assert screen.query_one("#emoji-category-tabs", Vertical) is not None
            assert len(grid.children) > 0
            assert isinstance(grid.children[0], EmojiCell)

            current = screen._current_cat_index
            screen._activate_category(-1)
            assert screen._current_cat_index == current
            await pilot.press("ctrl+n")
            assert screen._current_cat_index != current
            await pilot.press("ctrl+p")
            await pilot.press("ctrl+f")
            assert screen.focused.id == "emoji-search"

    @pytest.mark.integration
    async def test_search_tabs_grid_navigation_and_enter(self, app_for_test):
        screen = EmojiPickerScreen()
        selected = {}
        async with app_for_test.run_test() as pilot:
            await app_for_test.push_screen(
                screen, lambda value: selected.setdefault("value", value)
            )
            await pilot.pause()
            search = screen.query_one("#emoji-search", Input)
            search.value = "smile"
            await pilot.pause()
            grid = screen.query_one("#emoji-grid-container", Vertical)
            assert len(grid.children) > 0
            search.value = ""
            await pilot.pause()

            screen.on_button_pressed(
                MagicMock(button=screen.query_one("#emoji-cat-1", Button))
            )
            assert screen._current_cat_index == 1
            grid = screen.query_one("#emoji-grid-container", Vertical)
            grid.children[0].focus()
            await pilot.pause()
            screen.key_right()
            screen.key_left()
            screen.key_down()
            screen.key_up()
            screen._focus_first_cell()
            await pilot.pause()
            screen.action_select_emoji()
            await pilot.pause()
            assert "value" in selected

    @pytest.mark.integration
    async def test_click_cell_dismisses_picker(self, app_for_test):
        screen = EmojiPickerScreen()
        selected = {}
        async with app_for_test.run_test() as pilot:
            await app_for_test.push_screen(
                screen, lambda value: selected.setdefault("value", value)
            )
            await pilot.pause()
            cell = screen.query_one("#emoji-grid-container", Vertical).children[0]
            await pilot.click(cell)
            await pilot.pause()
            assert selected["value"] == cell.emoji_char


class TestEmojiCompletionWidget:
    def test_selection_rebuild_and_navigation(self):
        widget = EmojiCompletionWidget()
        widget._suggestions = [("😄", "smile"), ("👋", "wave")]
        mounted = []
        widget.remove_children = MagicMock()
        widget.mount = mounted.append
        widget.add_class = MagicMock()
        widget._rebuild()
        assert widget.selected_emoji == "😄"
        assert len(mounted) == 2
        widget.select_next()
        assert widget.selected_emoji == "👋"
        widget.select_prev()
        assert widget.selected_emoji == "😄"

    def test_show_hide_and_suggestion_click(self):
        widget = EmojiCompletionWidget()
        widget._rebuild = MagicMock()
        widget._refresh_selection = MagicMock()
        widget.add_class = MagicMock()
        widget.remove_class = MagicMock()
        widget.remove_children = MagicMock()
        widget.show_suggestions("smi")
        assert widget._suggestions
        widget.show_suggestions("smi")
        widget.show_suggestions("zzzz-not-found")
        assert widget._suggestions == []

        suggestion = _SuggestionWidget("x")
        suggestion.completion_widget = widget
        widget._select_and_insert = MagicMock()
        suggestion.emoji_char = "😄"
        suggestion.on_click()
        widget._select_and_insert.assert_called_once_with("😄")

    def test_refresh_and_insert_into_input(self):
        widget = EmojiCompletionWidget()
        widget._suggestions = [("😄", "smile"), ("👋", "wave")]
        first, second = _SuggestionWidget("first"), _SuggestionWidget("second")
        first.remove_class = MagicMock()
        second.add_class = MagicMock()
        with patch.object(
            EmojiCompletionWidget,
            "children",
            new_callable=PropertyMock,
            return_value=[first, second],
        ):
            widget._selected_index = 1
            widget._refresh_selection()
        second.add_class.assert_called_once_with("emoji-suggestion-selected")

        input_widget = MagicMock()
        input_widget.text = "say :smi"
        input_widget.cursor_location = (0, len(input_widget.text))
        input_widget.replace.return_value.end_location = (0, 6)
        app = MagicMock()
        app.query_one.return_value = input_widget
        widget.hide_suggestions = MagicMock()
        with patch.object(
            EmojiCompletionWidget, "app", new_callable=PropertyMock, return_value=app
        ):
            widget._select_and_insert("😄")
        input_widget.replace.assert_called_once_with("😄 ", (0, 4), (0, 8))
        input_widget.move_cursor.assert_called_once_with((0, 6))
        input_widget.focus.assert_called_once()

    def test_picker_focus_sections_and_grid_key_fallbacks(self):
        screen = EmojiPickerScreen()
        cells = [MagicMock(spec=EmojiCell) for _ in range(9)]
        grid = MagicMock(children=cells)
        search, button = MagicMock(), MagicMock()
        screen.query_one = MagicMock(
            side_effect=lambda selector, cls=None: {
                "#emoji-grid-container": grid,
                "#emoji-search": search,
                "#emoji-cat-0": button,
            }[selector]
        )
        with patch.object(
            EmojiPickerScreen, "focused", new_callable=PropertyMock, return_value=None
        ):
            screen._focus_section(1)
            screen._focus_section(-1)
        with patch.object(
            EmojiPickerScreen,
            "focused",
            new_callable=PropertyMock,
            return_value=cells[8],
        ):
            screen.key_left()
            screen.key_right()
            screen.key_up()
            screen.key_down()
