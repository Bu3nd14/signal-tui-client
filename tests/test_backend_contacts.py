"""
Regression tests for backend.py — Contact data model and parsing.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend import Contact


class TestContactModel:
    """📇 Modello dati Contact."""

    def test_contact_with_name(self):
        """Contact con nome → display_name = nome."""
        c = Contact(number="+391234567890", name="Mario")
        assert c.display_name == "Mario"

    def test_contact_without_name(self):
        """Contact senza nome → display_name = numero."""
        c = Contact(number="+391234567890")
        assert c.display_name == "+391234567890"

    def test_contact_with_aci(self):
        """Contact con ACI."""
        c = Contact(number="+391234567890", name="Mario", aci="uuid-123")
        assert c.aci == "uuid-123"

    def test_contact_empty_name_fallback(self):
        """Contact con name='' → display_name = numero."""
        c = Contact(number="+391234567890", name="")
        assert c.display_name == "+391234567890"
