"""Multi-protocol backend package.

Exposes the abstract ``ChatBackend`` interface, the concrete ``SignalBackend``
and ``WhatsAppBackend`` implementations, and the ``BackendManager`` facade
used by the Textual UI. The ``db``, ``download``, ``rpc`` and ``webhook``
modules are internal infrastructure and are not part of the public API.
"""

from __future__ import annotations

from .base import ChatBackend
from .manager import BackendManager
from .signal import SignalBackend
from .telegram import TelegramBackend
from .whatsapp import WhatsAppBackend

__all__ = [
    "BackendManager",
    "ChatBackend",
    "SignalBackend",
    "TelegramBackend",
    "WhatsAppBackend",
]
