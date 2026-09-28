"""Ratifica v3.3 — le 6 condizioni del red team per il fill incoming.

Ogni test è nominato ``test_rN_*`` e mappa 1:1 la condizione di ratifica.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

from models import ChatContact, ChatEvent
from protocols import db as db_mod
from protocols.signal import SignalBackend
from tui.events import EventHandlingMixin
from tui.unread_reply import UnreadReplyMixin

CONTACT = "+391234567890"
T0 = 1_700_000_000_000


def _incoming(**overrides) -> dict:
    base = {
        "id": "in-1",
        "text": "answer",
        "is_mine": False,
        "sender": "Mario",
        "timestamp": T0,
        "quote_text": None,
        "msg_type": "text",
        "attachment_info": None,
        "attachment_id": None,
        "content_type": None,
    }
    base.update(overrides)
    return base


def _enriched() -> dict:
    return {
        **_incoming(),
        "quote_text": "voice.ogg — 🎵 Audio",
        "quote_timestamp": T0 - 1_000,
        "quote_author": CONTACT,
        "quote_attachment_id": "voice.ogg",
        "quote_content_type": "audio/ogg",
    }


def _db_row(msg_id: str = "in-1") -> dict:
    conn = sqlite3.connect(db_mod.DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM messages WHERE msg_id = ?", (msg_id,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def _db_row_count() -> int:
    with sqlite3.connect(db_mod.DB_FILE) as conn:
        return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


def _app(backend, contact: ChatContact, *, web_enabled: bool = False):
    return SimpleNamespace(
        manager=SimpleNamespace(get=lambda _protocol: backend),
        contacts=[contact],
        selected_contact=None,
        _contact_list_dirty=False,
        _dirty_contact_keys=set(),
        _cache={},
        _typing_contacts={},
        _typing_mumbling={},
        _web_enabled=web_enabled,
    )


class _UnreadHost(UnreadReplyMixin):
    def __init__(self, contacts, cache):
        self.contacts = contacts
        self._cache = cache
        self._unread_counts: dict[str, int] = {}


def _event(payload: dict, contact: ChatContact) -> ChatEvent:
    return ChatEvent(
        type="message",
        protocol="signal",
        contact_id=CONTACT,
        payload={**payload, "contact": contact},
    )


# ─── R1 — fill incoming + "changed", secondo echo no-op ──────────────────────


def test_r1_incoming_partial_quote_fills_and_second_echo_is_noop():
    backend = SignalBackend()
    assert backend.ingest_message(CONTACT, _incoming(), T0) is True

    assert backend.ingest_message(CONTACT, _enriched(), T0) == "changed"
    cached = backend.cache[CONTACT][0]
    assert cached["quote_text"] == "voice.ogg — 🎵 Audio"
    assert cached["quote_content_type"] == "audio/ogg"

    # Idempotenza: secondo echo identico → False.
    assert backend.ingest_message(CONTACT, _enriched(), T0) is False


# ─── R2 — nessuna nuova riga DB, nessuna append alla cache UI ────────────────


def test_r2_incoming_changed_adds_no_db_row_and_no_ui_cache_entry():
    backend = SignalBackend()
    contact = ChatContact(id=CONTACT, display_name="Mario", protocol="signal")
    app = _app(backend, contact)

    assert EventHandlingMixin._handle_message_event(app, _event(_incoming(), contact))
    assert _db_row_count() == 1
    assert len(app._cache[contact.cache_key]) == 1

    assert EventHandlingMixin._handle_message_event(app, _event(_enriched(), contact))

    assert _db_row_count() == 1
    assert len(app._cache[contact.cache_key]) == 1
    assert _db_row()["quote_text"] == "voice.ogg — 🎵 Audio"


# ─── R3 — unread invariato (UI e DB) ─────────────────────────────────────────


def test_r3_incoming_changed_keeps_unread_unchanged_in_ui_and_db():
    backend = SignalBackend()
    contact = ChatContact(id=CONTACT, display_name="Mario", protocol="signal")
    app = _app(backend, contact)
    host = _UnreadHost([contact], app._cache)

    # Primo arrivo: riga DB + cache UI + unread = 1.
    EventHandlingMixin._handle_message_event(app, _event(_incoming(), contact))
    assert host._recompute_unread(contact.cache_key) is True
    assert host._unread_counts[contact.cache_key] == 1
    assert db_mod._count_unread() == {CONTACT: 1}

    # Echo arricchito ("changed"): unread resta 1 su entrambi i lati.
    EventHandlingMixin._handle_message_event(app, _event(_enriched(), contact))

    assert host._recompute_unread(contact.cache_key) is False
    assert host._unread_counts[contact.cache_key] == 1
    assert db_mod._count_unread() == {CONTACT: 1}


# ─── R4 — branch `existing` outgoing con attachment upgrade ──────────────────


def test_r4_existing_outgoing_upgrade_and_quote_fill_then_noop(tmp_path, monkeypatch):
    remote = tmp_path / "photo.jpg"
    remote.write_bytes(b"remote")

    backend = SignalBackend()
    monkeypatch.setattr(
        backend,
        "get_attachment_path",
        lambda aid: remote if aid == "remote-123" else None,
    )

    # Riga outgoing id-less (branch `existing`, non optimistic id-match).
    backend.cache[CONTACT] = [
        {
            "id": None,
            "text": "",
            "is_mine": True,
            "sender": "You",
            "timestamp": T0,
            "quote_text": "",
            "msg_type": "image",
            "attachment_info": "photo.jpg",
            "attachment_id": "sent-photo.png",
            "content_type": "image/png",
            "media_kind": "image",
        }
    ]

    echo = {
        "id": None,
        "text": "",
        "is_mine": True,
        "sender": "You",
        "timestamp": T0,
        "quote_text": "photo.jpg — 🖼️ Immagine",
        "quote_timestamp": T0 - 1_000,
        "quote_author": CONTACT,
        "quote_attachment_id": "remote-123",
        "quote_content_type": "image/png",
        "msg_type": "image",
        "attachment_info": "photo.jpg",
        "attachment_id": "remote-123",
        "content_type": "image/png",
        "media_kind": "image",
    }

    assert backend.ingest_message(CONTACT, echo, T0) == "changed"
    entry = backend.cache[CONTACT][0]
    assert entry["attachment_id"] == "remote-123"
    assert entry["quote_text"] == "photo.jpg — 🖼️ Immagine"
    assert entry["quote_content_type"] == "image/png"

    # Secondo echo identico → nessun upgrade, nessun fill → False.
    assert backend.ingest_message(CONTACT, echo, T0) is False


# ─── R5 — _fill_message_quote_fields non tocca status/id/timestamp ───────────


def test_r5_fill_quote_fields_preserves_status_id_timestamp():
    db_mod._init_db()
    db_mod._add_message_to_cache(
        CONTACT,
        "answer",
        True,
        "You",
        T0,
        protocol="signal",
        msg_id="42",
        status="read",
        attachment_id="att-1",
        content_type="image/png",
    )
    before = _db_row("42")

    changed = db_mod._fill_message_quote_fields(
        "signal",
        CONTACT,
        "42",
        T0,
        quote_text="voice.ogg — 🎵 Audio",
        quote_author=CONTACT,
        quote_content_type="audio/ogg",
    )

    assert changed is True
    after = _db_row("42")
    for column in ("id", "status", "timestamp", "msg_id", "text", "is_mine", "read"):
        assert after[column] == before[column], column
    assert after["quote_text"] == "voice.ogg — 🎵 Audio"
    assert after["quote_content_type"] == "audio/ogg"


# ─── R6 — un solo push_event per echo "changed" ──────────────────────────────


def test_r6_changed_echo_pushes_web_event_exactly_once():
    backend = SignalBackend()
    contact = ChatContact(id=CONTACT, display_name="Mario", protocol="signal")
    # Seme diretto: nessuna append UI, solo riga DB.
    assert backend.ingest_message(CONTACT, _incoming(), T0) is True

    app = _app(backend, contact, web_enabled=True)
    event = _event(_enriched(), contact)

    with patch("web.bridge.push_event") as push_event:
        EventHandlingMixin._handle_message_event(app, event)
        assert push_event.call_count == 1

        # Secondo echo no-op: nessun secondo push.
        EventHandlingMixin._handle_message_event(app, event)
        assert push_event.call_count == 1
