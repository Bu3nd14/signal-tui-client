"""
Regression tests for contact_picker.py — search_contacts helper.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the project root is on sys.path so we can import the modules
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend import Contact
from contact_picker import search_contacts


def _make_contacts() -> list[Contact]:
    """Build a small sample contact list for tests."""
    return [
        Contact(number="+391234567890", name="Alice Rossi"),
        Contact(number="+391234567891", name="Bob Bianchi"),
        Contact(number="+391234567892", name="Carla Verdi"),
        Contact(number="+391234567893", name=""),  # name falls back to number
    ]


class TestSearchContacts:
    """🔍 Ricerca contatti per nome o numero."""

    def test_search_by_name(self):
        """Cerca 'alice' → deve trovare Alice Rossi."""
        results = search_contacts(_make_contacts(), "alice")
        assert len(results) == 1
        assert results[0].name == "Alice Rossi"

    def test_search_case_insensitive(self):
        """Cerca 'ALICE' (maiuscolo) → stesso risultato di 'alice'."""
        lower = search_contacts(_make_contacts(), "alice")
        upper = search_contacts(_make_contacts(), "ALICE")
        assert [c.number for c in lower] == [c.number for c in upper]


    def test_search_by_number(self):
        """Cerca parte del numero → deve trovare il contatto."""
        results = search_contacts(_make_contacts(), "567890")
        assert len(results) == 1
        assert results[0].number == "+391234567890"

    def test_search_partial_name(self):
        """Cerca 'ross' → deve trovare Alice Rossi (substring)."""
        results = search_contacts(_make_contacts(), "ross")
        assert len(results) == 1
        assert results[0].name == "Alice Rossi"

    def test_search_no_results(self):
        """Cerca stringa inesistente → lista vuota."""
        results = search_contacts(_make_contacts(), "zzzznonexistentxxxx")
        assert results == []

    def test_search_empty_query(self):
        """Query vuota → restituisce tutti i contatti."""
        contacts = _make_contacts()
        results = search_contacts(contacts, "")
        assert results == contacts

    def test_search_whitespace_query(self):
        """Query di soli spazi → trattata come vuota."""
        contacts = _make_contacts()
        results = search_contacts(contacts, "   ")
        assert results == contacts

    def test_search_max_results(self):
        """Verifica il limite massimo di risultati."""
        contacts = _make_contacts()
        results = search_contacts(contacts, "", max_results=2)
        assert len(results) == 2

    def test_search_contact_without_name(self):
        """Contatto senza nome → match sul numero."""
        results = search_contacts(_make_contacts(), "567893")
        assert len(results) == 1
        assert results[0].number == "+391234567893"
