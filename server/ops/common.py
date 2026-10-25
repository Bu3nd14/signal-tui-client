"""Helpers shared by operation modules (and imported back by ``web/api.py``)."""

from __future__ import annotations

#: Protocols the API knows about, in display order.
PROTOCOL_ORDER = ("signal", "whatsapp", "telegram")
PROTOCOLS = frozenset(PROTOCOL_ORDER)
