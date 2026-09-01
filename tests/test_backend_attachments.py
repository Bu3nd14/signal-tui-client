"""
Regression tests for backend.py — attachment helpers and classification.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from protocols.rpc import get_attachment_path


class TestGetAttachmentPath:
    """📎 Risoluzione path attachment."""

    def test_attachment_found(self, tmp_path):
        """Attachment esistente → restituisce Path."""
        att_dir = tmp_path / "attachments"
        att_dir.mkdir(parents=True)
        att_file = att_dir / "att-123"
        att_file.write_text("fake image data")

        with patch("protocols.rpc.SIGNAL_CLI_ATTACHMENTS_DIR", att_dir):
            result = get_attachment_path("att-123")
        assert result == att_file

    def test_attachment_not_found(self, tmp_path):
        """Attachment inesistente → None."""
        att_dir = tmp_path / "attachments"
        att_dir.mkdir(parents=True)

        with patch("protocols.rpc.SIGNAL_CLI_ATTACHMENTS_DIR", att_dir):
            result = get_attachment_path("att-999")
        assert result is None

    def test_attachment_empty_id(self, tmp_path):
        """ID vuoto → None."""
        att_dir = tmp_path / "attachments"
        att_dir.mkdir(parents=True)

        with patch("protocols.rpc.SIGNAL_CLI_ATTACHMENTS_DIR", att_dir):
            result = get_attachment_path("")
        assert result is None

    def test_attachment_none_id(self, tmp_path):
        """ID None → None."""
        att_dir = tmp_path / "attachments"
        att_dir.mkdir(parents=True)

        with patch("protocols.rpc.SIGNAL_CLI_ATTACHMENTS_DIR", att_dir):
            result = get_attachment_path(None)  # type: ignore
        assert result is None

    def test_attachment_is_directory(self, tmp_path):
        """ID corrisponde a una directory → None."""
        att_dir = tmp_path / "attachments"
        att_dir.mkdir(parents=True)
        (att_dir / "att-dir").mkdir()

        with patch("protocols.rpc.SIGNAL_CLI_ATTACHMENTS_DIR", att_dir):
            result = get_attachment_path("att-dir")
        assert result is None
