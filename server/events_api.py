"""Wire schema of the core events (the ``payload`` of every bus event).

The core publishes JSON-serializable payloads only ("wire form"), so any
transport (the Phase 2 Unix-socket adapter, ``/ws/v2``) can forward them
unchanged.  The schema is declared once here, as one pydantic model per event
type, and exported with :func:`json_schema` (like ``ChatAPI.json_schema``).

Event types (envelope ``{seq, type, payload}``, see :mod:`server.bus`):

``backend_connecting``, ``backend_status``, ``backend_ready``, ``backend_done``
    Connect progress, see :mod:`server.core`.
``message``, ``message_edit``, ``reaction_update``, ``receipt``, ``typing``,
``contact_update``, ``sent_mirror``, ``lid_warmup_done``
    Domain events produced by ``ChatCore.process``.
``web_push``
    Legacy ``/ws`` frame ``{"event": {...}}`` (replaced by typed events later).
``gap``
    Synthetic: the subscriber lost events and must resync.

A contact travels as :class:`ContactWire`; :func:`contact_to_wire` /
:func:`contact_from_wire` convert from/to ``models.ChatContact``.  A contact
rebuilt from the wire is a *copy*: it never shares identity with the object a
backend holds.

This module must not import Textual, FastAPI, ``tui`` or ``web``.
"""

from __future__ import annotations

import base64
import dataclasses
import enum
import logging
import sqlite3
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import PurePath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from models import ChatContact

logger = logging.getLogger(__name__)


def jsonable(value: Any) -> Any:
    """Coerce *value* to plain JSON data (dict/list/str/int/float/bool/None).

    Sets become sorted lists, tuples lists, ``Path`` and unknown objects
    strings, ``bytes`` base64 text, ``sqlite3.Row`` and dataclasses dicts;
    mapping keys become strings.  Never raises.
    """
    if isinstance(value, enum.Enum):
        return jsonable(value.value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, ChatContact):
        return contact_to_wire(value)
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, sqlite3.Row):
        return {k: jsonable(value[k]) for k in value.keys()}  # noqa: SIM118
    if isinstance(value, (set, frozenset)):
        items = [jsonable(v) for v in value]
        try:
            return sorted(items)
        except TypeError:
            return sorted(items, key=repr)
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return jsonable(dataclasses.asdict(value))
    logger.debug("jsonable: stringifying %s", type(value).__name__)
    return str(value)


class _Wire(BaseModel):
    model_config = ConfigDict(extra="allow")


class ContactWire(_Wire):
    """A contact snapshot: identity + the JSON-only part of ``extras``."""

    id: str
    display_name: str
    protocol: str
    cache_key: str
    last_message_ts: int = 0
    extras: dict[str, Any] = Field(default_factory=dict)


def contact_to_wire(contact: ChatContact) -> dict[str, Any]:
    """Snapshot of *contact* as a JSON-able dict (see :class:`ContactWire`)."""
    return {
        "id": contact.id,
        "display_name": contact.display_name,
        "protocol": contact.protocol,
        "cache_key": contact.cache_key,
        "last_message_ts": contact.last_message_ts,
        "extras": jsonable(contact.extras),
    }


def contact_from_wire(data: Mapping[str, Any] | ContactWire) -> ChatContact:
    """Build a (new, unshared) ``ChatContact`` from its wire form."""
    if isinstance(data, ContactWire):
        data = data.model_dump(mode="json")
    extras = dict(data.get("extras") or {})
    ts = int(data.get("last_message_ts") or 0)
    if ts and "last_message_ts" not in extras:
        extras["last_message_ts"] = ts
    return ChatContact(
        id=str(data["id"]),
        display_name=str(data["display_name"]),
        protocol=str(data["protocol"]),
        extras=extras,
    )


# -- backend progress ------------------------------------------------------


class BackendConnecting(_Wire):
    protocol: str


class BackendStatus(_Wire):
    protocol: str
    text: str
    persistent: bool = False


class BackendReady(_Wire):
    protocol: str
    contacts: int


class BackendDone(_Wire):
    protocol: str
    ok: bool = False


# -- domain events ---------------------------------------------------------


class MessageEvent(_Wire):
    """A message ingested by the core (always emitted; ``added``/``changed``
    say whether anything new was stored)."""

    protocol: str
    contact_id: str
    contact: ContactWire
    was_placeholder: bool = False
    message: dict[str, Any]
    timestamp: Any = 0
    added: bool = False
    changed: bool = False


class MessageEditEvent(_Wire):
    protocol: str
    contact_id: str
    contact: ContactWire
    info: dict[str, Any]
    text: str


class ReactionUpdateEvent(_Wire):
    protocol: str
    contact_id: str
    info: dict[str, Any]


class ReceiptEvent(_Wire):
    protocol: str
    contact_id: str
    updated: list[dict[str, Any]]


class TypingEvent(_Wire):
    protocol: str
    contact_id: str
    action: str  # "STARTED" | "STOPPED"


class ContactUpdateEvent(_Wire):
    """A contact appeared/was renamed; ``merged_into`` (WhatsApp) says that
    ``contact_id`` was folded into another contact (``contact`` is then the
    survivor)."""

    protocol: str
    contact_id: str
    contact: ContactWire | None = None
    display_name: str | None = None
    phone: str | None = None
    merged_into: str | None = None


class SentMirrorEvent(_Wire):
    protocol: str
    contact_id: str
    contact: ContactWire
    id: str | None = None
    timestamp: Any = None
    batch_id: str | None = None


class LidWarmupDoneEvent(_Wire):
    protocol: str
    contact_id: str = ""


class WebPushEvent(_Wire):
    event: dict[str, Any]


#: Event type -> payload model.
EVENT_MODELS: dict[str, type[BaseModel]] = {
    "backend_connecting": BackendConnecting,
    "backend_status": BackendStatus,
    "backend_ready": BackendReady,
    "backend_done": BackendDone,
    "message": MessageEvent,
    "message_edit": MessageEditEvent,
    "reaction_update": ReactionUpdateEvent,
    "receipt": ReceiptEvent,
    "typing": TypingEvent,
    "contact_update": ContactUpdateEvent,
    "sent_mirror": SentMirrorEvent,
    "lid_warmup_done": LidWarmupDoneEvent,
    "web_push": WebPushEvent,
}


def to_wire(event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Wire form of a raw core payload (``ChatContact`` objects allowed).

    Every value is coerced with :func:`jsonable` (a ``ChatContact``, anywhere,
    becomes a :class:`ContactWire` dict), then the payload is validated against the event's
    model.  An unknown event type is only coerced.
    """
    data: dict[str, Any] = {key: jsonable(value) for key, value in payload.items()}
    model = EVENT_MODELS.get(event_type)
    if model is None:
        return data
    try:
        return model.model_validate(data).model_dump(mode="json", exclude_unset=True)
    except ValidationError:
        # A schema mismatch must not drop the event: publish the coerced data.
        logger.warning("Event %r does not match its wire schema", event_type)
        return data


def json_schema() -> dict[str, dict[str, Any]]:
    """``{event_type: JSON Schema of its payload}`` for every event."""
    return {name: model.model_json_schema() for name, model in EVENT_MODELS.items()}
