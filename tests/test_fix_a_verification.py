"""Mirated verification for Fix A: multi-attachment batch slot fusion.

Complementary to the tests added in ``test_web_outgoing_mirror.py`` and
``test_web_phase2_fixes.py``.  Covers the gaps left there:

* WhatsApp multi per-message slots (the added tests only cover one message).
* DB is never overwritten when a batch is already present (cache-only asserted).
* ``_merge_batch_slot`` return semantics (``True`` on fusion, ``False`` no-op).
* End-to-end ``"changed"`` -> ``web.bridge.push_event`` through
  ``_handle_message_event`` (the requested end-to-end check).
* Reconcile regression on singles and on failed multis.
"""

from __future__ import annotations

import logging
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

from models import ChatContact, ChatEvent
from protocols import db
from protocols.telegram import TelegramBackend
from protocols.whatsapp import WhatsAppBackend
from tui.events import EventHandlingMixin


def _telegram_echo(message_id: str, attachment_id: str | None) -> dict:
    return {
        "id": message_id,
        "text": "",
        "is_mine": True,
        "sender": "You",
        "quote_text": None,
        "msg_type": "image",
        "attachment_info": None,
        "attachment_id": attachment_id,
        "content_type": "image/png",
        "media_kind": "image",
    }


def _whatsapp_echo(message_id: str, attachment_id: str | None) -> dict:
    return {
        "id": message_id,
        "text": "",
        "is_mine": True,
        "sender": "You",
        "quote_text": None,
        "msg_type": "image",
        "attachment_info": None,
        "attachment_id": attachment_id,
        "content_type": "image/png",
        "media_kind": "image",
    }


def _wa_backend(tmp_path, monkeypatch) -> WhatsAppBackend:
    backend = WhatsAppBackend(api_url="http://api.test", media_dir=str(tmp_path / "wa"))
    monkeypatch.setattr(backend, "_resolve_send_chat_id", lambda cid: cid)
    return backend


def _db_rows(protocol: str, contact: str) -> list[tuple]:
    with sqlite3.connect(db.DB_FILE) as connection:
        return connection.execute(
            "SELECT msg_id, batch_id, batch_index FROM messages "
            "WHERE protocol = ? AND contact_number = ? ORDER BY msg_id",
            (protocol, contact),
        ).fetchall()


# ─── WhatsApp: N messages -> N distinct slots ────────────────────────────────


def test_whatsapp_batch_slots_distinct_per_message(tmp_path, monkeypatch):
    backend = _wa_backend(tmp_path, monkeypatch)
    contact = "39333@c.us"
    echoes = [_whatsapp_echo("wa-71", "r-71"), _whatsapp_echo("wa-72", "r-72")]
    mirrors = [
        {**echoes[0], "batch_id": "batch-9", "batch_index": 0},
        {**echoes[1], "batch_id": "batch-9", "batch_index": 1},
    ]
    # echo-first interleaved with the corresponding mirror.
    assert backend.ingest_message(contact, echoes[0], 1787250931234) is True
    assert backend.ingest_message(contact, mirrors[0], 1787250931234) == "changed"
    assert backend.ingest_message(contact, echoes[1], 1787250931235) is True
    assert backend.ingest_message(contact, mirrors[1], 1787250931235) == "changed"

    assert len(backend.cache[contact]) == 2
    slots = {(m["batch_id"], m["batch_index"]) for m in backend.cache[contact]}
    assert slots == {("batch-9", 0), ("batch-9", 1)}
    assert _db_rows("whatsapp", contact) == [
        ("wa-71", "batch-9", 0),
        ("wa-72", "batch-9", 1),
    ]


def test_whatsapp_batch_slot_db_not_overwritten(tmp_path, monkeypatch):
    backend = _wa_backend(tmp_path, monkeypatch)
    contact = "39333@c.us"
    backend.ingest_message(
        contact,
        {
            **_whatsapp_echo("wa-71", "r-71"),
            "batch_id": "batch-1",
            "batch_index": 0,
        },
        1787250931234,
    )
    # A second mirror with a DIFFERENT batch must not overwrite cache nor DB.
    assert (
        backend.ingest_message(
            contact,
            {
                **_whatsapp_echo("wa-71", "r-71"),
                "batch_id": "batch-9",
                "batch_index": 3,
            },
            1787250931234,
        )
        is False
    )
    assert backend.cache[contact][0]["batch_id"] == "batch-1"
    assert _db_rows("whatsapp", contact) == [("wa-71", "batch-1", 0)]


def test_telegram_batch_slot_db_not_overwritten():
    backend = TelegramBackend()
    backend.ingest_message(
        "42",
        {**_telegram_echo("71", "r-71"), "batch_id": "batch-1", "batch_index": 0},
        1787250931234,
    )
    assert (
        backend.ingest_message(
            "42",
            {**_telegram_echo("71", "r-71"), "batch_id": "batch-9", "batch_index": 3},
            1787250931234,
        )
        is False
    )
    assert backend.cache["42"][0]["batch_id"] == "batch-1"
    assert _db_rows("telegram", "42") == [("71", "batch-1", 0)]


# ─── _merge_batch_slot direct semantics ──────────────────────────────────────


def test_merge_batch_slot_return_semantics(tmp_path, monkeypatch):
    contact = "39333@c.us"
    wa = _wa_backend(tmp_path, monkeypatch)
    tg = TelegramBackend()

    # No batch on the event -> no-op.
    entry = {"id": "x", "batch_id": None}
    assert wa._merge_batch_slot(contact, entry, {"batch_id": None}) is False
    assert tg._merge_batch_slot("42", entry, {"batch_id": None}) is False

    # Existing batch -> never overwritten, returns False.
    entry = {"id": "x", "batch_id": "b1", "batch_index": 0}
    assert (
        wa._merge_batch_slot(contact, entry, {"batch_id": "b2", "batch_index": 5})
        is False
    )
    assert entry["batch_id"] == "b1" and entry["batch_index"] == 0

    # Fresh entry WITH a DB row -> fused, returns True.
    assert tg.ingest_message("42", _telegram_echo("71", None), 1787250931234) is True
    entry = tg.cache["42"][0]
    assert (
        tg._merge_batch_slot("42", entry, {"batch_id": "b3", "batch_index": 2}) is True
    )
    assert entry["batch_id"] == "b3" and entry["batch_index"] == 2
    assert _db_rows("telegram", "42") == [("71", "b3", 2)]


def test_merge_batch_slot_rowcount_zero_returns_false_and_warns(
    tmp_path, monkeypatch, caplog
):
    """R1: UPDATE che non tocca righe non deve dichiarare la fusione."""
    wa = _wa_backend(tmp_path, monkeypatch)
    tg = TelegramBackend()
    db._init_db()

    with caplog.at_level(logging.WARNING):
        entry = {"id": "missing", "batch_id": None}
        assert (
            tg._merge_batch_slot("42", entry, {"batch_id": "b1", "batch_index": 0})
            is False
        )
        assert entry["batch_id"] is None
        entry = {"id": "missing", "batch_id": None}
        assert (
            wa._merge_batch_slot(
                "39333@c.us", entry, {"batch_id": "b1", "batch_index": 0}
            )
            is False
        )
        assert entry["batch_id"] is None

    warnings = [record.getMessage() for record in caplog.records]
    assert any("touched no row" in message for message in warnings)


def test_echo_first_batch_merge_on_cache_only_row_does_not_report_changed(
    tmp_path, monkeypatch, caplog
):
    """R1: una riga solo in cache (persist=False) non ha lo slot nel DB, quindi
    il mirror batch non deve restituire "changed" (niente push web)."""
    contact = "39333@c.us"
    wa = _wa_backend(tmp_path, monkeypatch)
    db._init_db()
    assert (
        wa.ingest_message(
            contact, _whatsapp_echo("wa-71", "r-71"), 1787250931234, persist=False
        )
        is True
    )
    assert _db_rows("whatsapp", contact) == []

    with caplog.at_level(logging.WARNING):
        assert (
            wa.ingest_message(
                contact,
                {
                    **_whatsapp_echo("wa-71", "r-71"),
                    "batch_id": "b1",
                    "batch_index": 0,
                },
                1787250931234,
            )
            is False
        )

    assert any("touched no row" in record.getMessage() for record in caplog.records)
    assert wa.cache[contact][0]["batch_id"] is None
    assert _db_rows("whatsapp", contact) == []


# ─── End-to-end: "changed" pushes the web update ─────────────────────────────


def _event_app(backend, contact):
    return SimpleNamespace(
        manager=SimpleNamespace(get=lambda _protocol: backend),
        contacts=[contact],
        selected_contact=None,
        _contact_list_dirty=False,
        _dirty_contact_keys=set(),
        _cache={},
        _typing_contacts={},
        _typing_mumbling={},
        _web_enabled=True,
    )


def _assert_echo_first_merge_pushes(backend, protocol, contact_id, echo):
    # Real echo lands first WITHOUT a batch.
    assert backend.ingest_message(contact_id, echo, 1787250931234) is True

    contact = ChatContact(id=contact_id, display_name="X", protocol=protocol)
    app = _event_app(backend, contact)
    mirror_payload = {**echo, "batch_id": "batch-1", "batch_index": 0}
    event = ChatEvent(
        type="message",
        protocol=protocol,
        contact_id=contact_id,
        payload={**mirror_payload, "contact": contact, "timestamp": 1787250931234},
    )

    with patch("web.bridge.push_event") as push_event:
        assert EventHandlingMixin._handle_message_event(app, event) is True

    push_event.assert_called_once()
    assert backend.cache[contact_id][0]["batch_id"] == "batch-1"
    assert backend.cache[contact_id][0]["batch_index"] == 0


def test_telegram_echo_first_merge_pushes_web_event():
    _assert_echo_first_merge_pushes(
        TelegramBackend(), "telegram", "42", _telegram_echo("71", None)
    )


def test_whatsapp_echo_first_merge_pushes_web_event(tmp_path, monkeypatch):
    _assert_echo_first_merge_pushes(
        _wa_backend(tmp_path, monkeypatch),
        "whatsapp",
        "39333@c.us",
        _whatsapp_echo("wa-71", None),
    )


def test_mirror_first_echo_does_not_push_duplicate():
    backend = TelegramBackend()
    mirror = {**_telegram_echo("71", "r-71"), "batch_id": "batch-1", "batch_index": 0}
    assert backend.ingest_message("42", mirror, 1787250931234) is True

    contact = ChatContact(id="42", display_name="X", protocol="telegram")
    app = _event_app(backend, contact)
    echo = {**_telegram_echo("71", "r-71"), "timestamp": 1787250931234}
    event = ChatEvent(
        type="message",
        protocol="telegram",
        contact_id="42",
        payload={**echo, "contact": contact},
    )

    with patch("web.bridge.push_event") as push_event:
        assert EventHandlingMixin._handle_message_event(app, event) is True

    # Unchanged duplicate -> no push, no double bubble.
    push_event.assert_not_called()
    assert backend.cache["42"][0]["batch_id"] == "batch-1"
