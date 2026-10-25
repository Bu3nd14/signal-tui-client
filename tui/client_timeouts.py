"""Per-operation timeouts of the TUI's client calls, in one place.

A ``SocketClient`` call waits at most *timeout* seconds for the server's
answer (the in-process client ignores it).  A timed-out operation is NOT
cancelled server side: for a mutation the outcome is unknown and the TUI
resyncs (see ``tui.client_calls``).  Every operation the TUI calls must be
listed here: :func:`timeout_for` raises ``KeyError`` for an unknown one, so a
new call site cannot forget to choose a value.

Values are upper bounds for a healthy server, not targets:

- UI-driven lookups answer from memory: a few seconds;
- operations that may touch the network or the disk (lazy media download,
  WhatsApp LID cache, history refresh, address book) get tens of seconds;
- ``messages.send`` may stage large attachments and talk to a backend.
"""

from __future__ import annotations

from typing import Any

from server.client import ChatClient

#: Seconds, by operation name.
OP_TIMEOUTS: dict[str, float] = {
    # Lookups answered from the server's memory.
    "server.config": 5.0,
    "server.status": 5.0,
    "link.status": 5.0,
    "contacts.check_number": 10.0,
    "media.quoted_attachment": 10.0,
    "contacts.for_protocol": 15.0,
    "messages.snapshot": 30.0,
    "messages.window": 15.0,
    # May download from a remote backend (WAHA/Telegram up to ~30 s).
    "media.attachment": 60.0,
    # Small mutations / actions.
    "chats.mark_read": 15.0,
    "contacts.register": 15.0,
    "download.serve_attachment": 15.0,
    "download.serve_text": 10.0,
    "link.cancel": 10.0,
    "link.reconnect": 15.0,
    "messages.edit": 30.0,
    # Slow by nature.
    "messages.send": 90.0,
    "contacts.address_book": 90.0,
    "link.start": 60.0,
    "link.poll": 20.0,
    "link.finalize": 60.0,
    "link.submit_2fa": 30.0,
}

#: ``messages.window`` with ``refresh`` downloads the remote history first.
WINDOW_REFRESH_TIMEOUT = 60.0


def timeout_for(op: str, data: dict[str, Any] | None = None) -> float:
    """Timeout (seconds) for operation *op* with request *data*."""
    if op == "messages.window" and data and data.get("refresh"):
        return WINDOW_REFRESH_TIMEOUT
    return OP_TIMEOUTS[op]


def call_op(client: ChatClient, op: str, data: dict[str, Any] | None = None) -> Any:
    """``client.call`` with the timeout configured for *op*.  May block."""
    return client.call(op, data, timeout=timeout_for(op, data))
