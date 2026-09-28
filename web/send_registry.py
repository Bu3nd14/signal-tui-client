"""In-memory idempotency registry per gli invii di testo della web UI (v1).

Solo logica pura e thread-safe, nessun I/O.  La chiave è scoped al contatto:
``(protocol, contact_number, client_msg_id)``.  Vedi
``docs/DESIGN_WEB_SEND_RETRY_DISCONNECTION.md`` §8.

Politica di eviction (R-A): nessuna eviction per capacità.  A capienza piena si
pruna solo ciò che è già scaduto per TTL; una voce ``sent`` giovane non viene
mai espulsa (al più la struttura si espande oltre il cap soft).  Il contratto di
``claim_or_lookup`` resta quindi a tre esiti: ``sent`` / ``inflight`` /
``claimed``.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any, Literal

TTL_SENT_S = 600.0
TTL_INFLIGHT_S = 120.0
MAX_ENTRIES = 100_000
PRUNE_INTERVAL_S = 60.0

Outcome = Literal["sent", "inflight", "claimed"]
Key = tuple[str, str, str]


def build_fingerprint(
    *,
    protocol: str,
    contact_id: str,
    text: str,
    quote_timestamp: int | None,
    quote_author: str | None,
    quote_message: str | None,
    reply_to_message_id: str | None,
    quote_content_type: str | None,
    quote_attachment_id: str | None,
) -> str:
    """Hash stabile del payload normalizzato del server (N6)."""
    payload = {
        "protocol": protocol,
        "contact_id": contact_id,
        "text": text,
        "quote_timestamp": quote_timestamp,
        "quote_author": quote_author,
        "quote_message": quote_message,
        "reply_to_message_id": reply_to_message_id,
        "quote_content_type": quote_content_type,
        "quote_attachment_id": quote_attachment_id,
    }
    blob = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class SendRegistry:
    """Registry in-memory di claim ``inflight``/``sent`` con token di ownership."""

    def __init__(
        self,
        *,
        ttl_sent: float = TTL_SENT_S,
        ttl_inflight: float = TTL_INFLIGHT_S,
        max_entries: int = MAX_ENTRIES,
        prune_interval: float = PRUNE_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_sent = ttl_sent
        self._ttl_inflight = ttl_inflight
        self._max_entries = max_entries
        self._prune_interval = prune_interval
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[Key, dict[str, Any]] = {}
        self._last_prune = clock()

    def claim_or_lookup(self, key: Key, fingerprint: str) -> tuple[Outcome, Any]:
        """Atomica. Ritorna ``("sent", result)``, ``("inflight", None)`` o
        ``("claimed", token)``.  Nessuna mutazione sul ramo ``inflight``."""
        with self._lock:
            now = self._clock()
            entry = self._entries.get(key)
            if entry is not None:
                if entry["status"] == "sent":
                    # Un hit di ``sent`` non estende la dedup oltre il TTL: una
                    # voce scaduta va trattata come assente anche a chiave calda.
                    if now - entry["at"] <= self._ttl_sent:
                        return ("sent", dict(entry))
                elif now - entry["at"] <= self._ttl_inflight:
                    return ("inflight", None)
            if self._needs_prune(now):
                self._prune(now)
            token = uuid.uuid4().hex
            self._entries[key] = {
                "status": "inflight",
                "token": token,
                "message_id": None,
                "timestamp": None,
                "fingerprint": fingerprint,
                "at": now,
            }
            return ("claimed", token)

    def mark_sent(
        self,
        key: Key,
        token: str,
        message_id: str | None = None,
        timestamp: int | None = None,
    ) -> None:
        """``inflight -> sent`` solo se il token di ownership coincide."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry["status"] != "inflight":
                return
            if entry["token"] != token:
                return
            entry.update(
                status="sent",
                token=None,
                message_id=message_id,
                timestamp=timestamp,
                at=self._clock(),
            )

    def release(self, key: Key, token: str) -> None:
        """Rimuove la voce ``inflight`` solo se il token di ownership coincide."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry["status"] != "inflight":
                return
            if entry["token"] != token:
                return
            del self._entries[key]

    def prune(self) -> None:
        """Rimuove le voci scadute.  Utile ai test e al pruning periodico."""
        with self._lock:
            self._prune(self._clock())

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()
            self._last_prune = self._clock()

    def snapshot(self, key: Key) -> dict[str, Any] | None:
        with self._lock:
            entry = self._entries.get(key)
            return dict(entry) if entry is not None else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def _needs_prune(self, now: float) -> bool:
        return (
            len(self._entries) >= self._max_entries
            or now - self._last_prune >= self._prune_interval
        )

    def _prune(self, now: float) -> None:
        for key, entry in list(self._entries.items()):
            ttl = self._ttl_sent if entry["status"] == "sent" else self._ttl_inflight
            if now - entry["at"] > ttl:
                del self._entries[key]
        self._last_prune = now
