"""Wire form of the core events: schema, contact snapshots, JSON round trip."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from models import PROTOCOL_SIGNAL, PROTOCOL_WHATSAPP, ChatContact, ChatEvent
from server import events_api
from server.bus import Event, EventBus
from server.core import ChatCore
from server.events_api import (
    EVENT_MODELS,
    contact_from_wire,
    contact_to_wire,
    jsonable,
    to_wire,
)
from web.legacy_events import to_legacy

CID = "+391234567890"


def _core(backend) -> ChatCore:
    core = ChatCore(EventBus())
    core.manager = SimpleNamespace(get=lambda _p: backend, all=lambda: [backend])
    return core


def _backend(**attrs):
    base = {"protocol": PROTOCOL_SIGNAL, "contacts": []}
    base.update(attrs)
    return SimpleNamespace(**base)


def _roundtrips(payload) -> None:
    assert json.loads(json.dumps(payload)) == payload


# -- contacts --------------------------------------------------------------


def test_contact_roundtrip_keeps_identity_fields_and_extras():
    contact = ChatContact(
        id="1@lid",
        display_name="Giulia",
        protocol=PROTOCOL_WHATSAPP,
        extras={"phone": "39", "lid": "1@lid", "archived": False, "last_message_ts": 7},
    )
    wire = contact_to_wire(contact)
    _roundtrips(wire)
    assert wire["cache_key"] == "whatsapp:1@lid"
    assert wire["last_message_ts"] == 7
    copy = contact_from_wire(json.loads(json.dumps(wire)))
    assert copy == contact
    assert copy is not contact
    copy.extras["phone"] = "changed"
    assert contact.extras["phone"] == "39"


def test_contact_without_recency_roundtrips_without_adding_it():
    contact = ChatContact(id=CID, display_name="Ada", protocol=PROTOCOL_SIGNAL)
    wire = contact_to_wire(contact)
    assert wire["last_message_ts"] == 0
    assert contact_from_wire(wire) == contact


def test_contact_extras_are_restricted_to_json_values():
    contact = ChatContact(
        id="42",
        display_name="Ada",
        protocol="telegram",
        extras={"access_hash": b"\x01\x02", "path": Path("/x"), "tags": {"b", "a"}},
    )
    wire = contact_to_wire(contact)
    _roundtrips(wire)
    assert wire["extras"]["path"] == "/x"
    assert wire["extras"]["tags"] == ["a", "b"]
    assert isinstance(wire["extras"]["access_hash"], str)


def test_jsonable_coerces_non_json_values():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    row = db.execute("select 1 as a, 'x' as b").fetchone()
    db.close()
    value = {1: (1, 2), "s": {3, 1}, "p": Path("a/b"), "r": row, "n": None}
    out = jsonable(value)
    assert out == {
        "1": [1, 2],
        "s": [1, 3],
        "p": "a/b",
        "r": {"a": 1, "b": "x"},
        "n": None,
    }
    _roundtrips(out)


def test_json_schema_covers_every_event_model():
    schema = events_api.json_schema()
    assert set(schema) == set(EVENT_MODELS)
    json.dumps(schema)
    assert "contact" in schema["message"]["properties"]


# -- one wire round trip per event type ------------------------------------


def _wire_events() -> list[tuple[str, dict]]:
    """Run one representative raw event per type through ``ChatCore.process``."""
    contact = ChatContact(id=CID, display_name="Ada", protocol=PROTOCOL_SIGNAL)
    info = {
        "message_id": "77",
        "timestamp": 1000,
        "old_text": "Prima",
        "is_mine": False,
        "reactions": {"👍": ["a"]},
    }
    backend = _backend(
        contacts=[contact],
        ingest_message=MagicMock(return_value=True),
        apply_edit=MagicMock(return_value=info),
        apply_reaction=MagicMock(return_value=info),
        process_receipt=MagicMock(
            return_value=[{"id": "77", "timestamp": 1, "status": "read", "x": {1, 2}}]
        ),
    )
    core = _core(backend)
    raw = [
        ChatEvent(
            "message",
            PROTOCOL_SIGNAL,
            CID,
            {
                "id": "m1",
                "text": "hi",
                "is_mine": False,
                "timestamp": 1000,
                "contact": contact,
                "attachment_info": {"path": Path("/tmp/a.png")},
            },
        ),
        ChatEvent(
            "message_edit",
            PROTOCOL_SIGNAL,
            CID,
            {"edit_message_id": 77, "text": "Dopo"},
        ),
        ChatEvent("reaction_update", PROTOCOL_SIGNAL, CID, {"mode": "delta"}),
        ChatEvent("receipt", PROTOCOL_SIGNAL, CID, {"receipt": {}}),
        ChatEvent("typing", PROTOCOL_SIGNAL, CID, {"action": "STARTED"}),
        ChatEvent(
            "contact_update",
            PROTOCOL_SIGNAL,
            CID,
            {"display_name": "Ada", "phone": "39", "contact": contact},
        ),
        ChatEvent(
            "contact_update",
            PROTOCOL_WHATSAPP,
            "1@lid",
            {"merged_into": "39@c.us", "contact": contact},
        ),
        ChatEvent(
            "sent-mirror",
            PROTOCOL_SIGNAL,
            CID,
            {"id": "1", "timestamp": 5, "batch_id": "b", "contact": contact},
        ),
        ChatEvent("lid_warmup_done", PROTOCOL_WHATSAPP, ""),
    ]
    out: list[tuple[str, dict]] = []
    for event in raw:
        out.extend(core.process(event))
    return out


def test_every_core_event_type_is_wire_form():
    events = _wire_events()
    assert {t for t, _ in events} == {
        "message",
        "message_edit",
        "reaction_update",
        "receipt",
        "typing",
        "contact_update",
        "sent_mirror",
        "lid_warmup_done",
    }
    for etype, payload in events:
        _roundtrips(payload)
        # Valid against its model (no fallback path was taken).
        EVENT_MODELS[etype].model_validate(payload)
    by_type = {t: p for t, p in events}
    assert by_type["message"]["contact"]["cache_key"] == f"signal:{CID}"
    assert by_type["message"]["message"]["attachment_info"] == {"path": "/tmp/a.png"}
    assert by_type["message"]["message"]["contact"]["id"] == CID
    assert by_type["sent_mirror"]["contact"]["id"] == CID
    assert by_type["receipt"]["updated"][0]["x"] == [1, 2]


def test_to_wire_keeps_unmodelled_values_when_schema_mismatches():
    out = to_wire("typing", {"protocol": 5, "contact_id": {"a"}, "action": "STARTED"})
    assert out == {"protocol": 5, "contact_id": ["a"], "action": "STARTED"}


def test_to_wire_passes_unknown_event_types_through():
    assert to_wire("custom", {"p": Path("x")}) == {"p": "x"}


def test_message_payload_is_a_copy_not_the_backend_dict():
    contact = ChatContact(id=CID, display_name="Ada", protocol=PROTOCOL_SIGNAL)
    backend = _backend(contacts=[contact], ingest_message=MagicMock(return_value=True))
    raw = {"id": "m", "text": "t", "is_mine": False, "timestamp": 1}
    [(_, payload)] = _core(backend).process(
        ChatEvent("message", PROTOCOL_SIGNAL, CID, raw)
    )
    assert payload["message"] == raw
    assert payload["message"] is not raw


# -- /ws legacy frames are unchanged ---------------------------------------


def test_legacy_frames_from_wire_events_match_the_old_shapes():
    frames = {}
    for etype, payload in _wire_events():
        frame = to_legacy(Event(1, etype, payload, 0.0))
        if frame is not None:
            frames.setdefault(frame["type"], []).append(frame["payload"])
    assert frames["message"] == [
        {
            "id": "m1",
            "protocol": "signal",
            "contact_id": CID,
            "timestamp": 1000,
            "is_mine": False,
        }
    ]
    assert frames["message_edit"][0] == {
        "protocol": "signal",
        "contact_id": CID,
        "message_id": "77",
        "timestamp": 1000,
        "old_text": "Prima",
        "text": "Dopo",
        "is_mine": False,
    }
    assert frames["reaction_update"][0]["reactions"] == {"👍": ["a"]}
    assert frames["receipt"][0]["updates"] == [
        {"id": "77", "timestamp": 1, "status": "read", "text": ""}
    ]
    assert frames["typing"][0]["action"] == "STARTED"
    assert frames["contact_update"][0] == {
        "protocol": "signal",
        "contact_id": CID,
        "display_name": "Ada",
        "phone": "39",
    }
    assert frames["contact_update"][1] == {
        "protocol": "whatsapp",
        "contact_id": "1@lid",
        "merged_into": "39@c.us",
        "display_name": None,
        "phone": None,
    }
    assert frames["lid_warmup_done"] == [{"protocol": "whatsapp"}]
    assert "sent_mirror" not in frames


@pytest.mark.allow_unserializable_events
def test_guard_marker_allows_non_json_payloads():
    EventBus().publish("x", {"o": object()})
