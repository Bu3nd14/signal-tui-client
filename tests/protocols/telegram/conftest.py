"""
Telegram test fixtures — prevents SQLite contamination.
"""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _mock_sqlite_writes():
    """Prevent Telegram tests from writing to the real SQLite DB."""
    with (
        patch("protocols.db._add_message_to_cache"),
        patch("protocols.db._update_message_id"),
    ):
        yield
