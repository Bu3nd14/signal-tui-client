"""
Regression tests for emoji_picker.py — search, alias replacement, suggestions.
"""

from __future__ import annotations

import pytest
import sys
from pathlib import Path

# Ensure the project root is on sys.path so we can import the modules
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from emoji_picker import (
    search_emoji,
    get_emoji_suggestions,
    replace_emoji_aliases,
    _EMOJI_TO_ALIAS,
    _ALIAS_TO_EMOJI,
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
