"""
Multi-protocol backend package.

Exposes the abstract ``ChatBackend`` interface, the concrete ``SignalBackend``
and ``WhatsAppBackend`` implementations, and the ``BackendManager`` facade
used by the Textual UI.
"""

from __future__ import annotations

from .base import ChatBackend
from .manager import BackendManager
from .signal import SignalBackend
from .whatsapp import WhatsAppBackend

__all__ = ["ChatBackend", "BackendManager", "SignalBackend", "WhatsAppBackend"]
