"""ChatCore.process (domain step per raw backend event) and the event pump."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from models import (
    PROTOCOL_SIGNAL,
    PROTOCOL_WHATSAPP,
    ChatContact,
    ChatEvent,
)
from server.bus import EventBus
from server.core import ChatCore
from server.events_api import contact_to_wire

CID = "+391234567890"


def _core(backend=None, *, backends=None) -> ChatCore:
    core = ChatCore(EventBus())
    backends = backends if backends is not None else ([backend] if backend else [])
    core.manager = SimpleNamespace(
        get=lambda _protocol: backend,
        all=lambda: list(backends),
    )
    return core


def _backend(**attrs):
    base = {"protocol": PROTOCOL_SIGNAL, "contacts": []}
    base.update(attrs)
    return SimpleNamespace(**base)


def _msg_event(contact_id=CID, protocol=PROTOCOL_SIGNAL, **payload) -> ChatEvent:
    data = {"id": "m1", "text": "ciao", "is_mine": False, "timestamp": 1000}
    data.update(payload)
    return ChatEvent(
        type="message", protocol=protocol, contact_id=contact_id, payload=data
    )


# ─── message ─────────────────────────────────────────────────────────────────


class TestProcessMessage:
    def test_no_backend_yields_nothing(self):
        core = _core(None)
        assert core.process(_msg_event()) == []

    def test_payload_contact_ingested_and_emitted(self):
        contact = ChatContact(id=CID, display_name="Ada", protocol=PROTOCOL_SIGNAL)
        backend = _backend(
            contacts=[contact], ingest_message=MagicMock(return_value=True)
        )
        event = _msg_event(contact=contact)

        [(etype, payload)] = _core(backend).process(event)

        assert etype == "message"
        backend.ingest_message.assert_called_once_with(CID, event.payload, 1000)
        assert payload == {
            "protocol": PROTOCOL_SIGNAL,
            "contact_id": CID,
            "contact": contact_to_wire(contact),
            "was_placeholder": False,
            # the raw payload carries the live contact; the wire has a snapshot
            "message": {**event.payload, "contact": contact_to_wire(contact)},
            "timestamp": 1000,
            "added": True,
            "changed": False,
        }

    @pytest.mark.parametrize(
        ("result", "added", "changed"),
        [(True, True, False), ("changed", False, True), (False, False, False)],
    )
    def test_ingest_result_flags_always_emitted(self, result, added, changed):
        contact = ChatContact(id=CID, display_name="Ada", protocol=PROTOCOL_SIGNAL)
        backend = _backend(
            contacts=[contact], ingest_message=MagicMock(return_value=result)
        )

        [(etype, payload)] = _core(backend).process(_msg_event(contact=contact))

        assert etype == "message"
        assert (payload["added"], payload["changed"]) == (added, changed)

    def test_backend_without_ingest_counts_as_added(self):
        contact = ChatContact(id=CID, display_name="Ada", protocol=PROTOCOL_SIGNAL)
        backend = _backend(contacts=[contact])
        [(_, payload)] = _core(backend).process(_msg_event(contact=contact))
        assert payload["added"] is True

    def test_identify_contact_used_when_payload_has_none(self):
        known = ChatContact(id=CID, display_name="Ada", protocol=PROTOCOL_SIGNAL)
        backend = _backend(
            contacts=[known],
            _identify_contact=MagicMock(return_value=known),
            ingest_message=MagicMock(return_value=True),
        )

        [(_, payload)] = _core(backend).process(_msg_event())

        backend._identify_contact.assert_called_once_with(CID)
        assert payload["contact"] == contact_to_wire(known)
        assert payload["was_placeholder"] is False

    def test_placeholder_registered_with_backend(self):
        backend = _backend(
            _identify_contact=MagicMock(return_value=None),
            register_contact=MagicMock(return_value=True),
            ingest_message=MagicMock(return_value=True),
        )

        [(_, payload)] = _core(backend).process(_msg_event())

        assert payload["was_placeholder"] is True
        assert payload["contact"]["cache_key"] == f"{PROTOCOL_SIGNAL}:{CID}"
        assert payload["contact"]["display_name"] == CID
        [placeholder] = backend.register_contact.call_args.args
        assert contact_to_wire(placeholder) == payload["contact"]
        backend.ingest_message.assert_called_once()

    def test_unknown_contact_appended_when_backend_has_no_register(self):
        backend = _backend(_identify_contact=MagicMock(return_value=None))
        [(_, payload)] = _core(backend).process(_msg_event())
        assert [contact_to_wire(c) for c in backend.contacts] == [payload["contact"]]

    def test_contact_known_to_backend_is_not_reregistered(self):
        known = ChatContact(id=CID, display_name="Ada", protocol=PROTOCOL_SIGNAL)
        backend = _backend(
            contacts=[known],
            register_contact=MagicMock(),
            ingest_message=MagicMock(return_value=True),
        )
        _core(backend).process(_msg_event(contact=known))
        backend.register_contact.assert_not_called()

    def test_ingest_uses_resolved_contact_id(self):
        resolved = ChatContact(
            id="393@c.us", display_name="Ada", protocol=PROTOCOL_WHATSAPP
        )
        backend = _backend(
            protocol=PROTOCOL_WHATSAPP,
            contacts=[resolved],
            _identify_contact=MagicMock(return_value=resolved),
            ingest_message=MagicMock(return_value=True),
        )
        event = _msg_event("393@lid", PROTOCOL_WHATSAPP)

        [(_, payload)] = _core(backend).process(event)

        assert backend.ingest_message.call_args.args[0] == "393@c.us"
        assert payload["contact_id"] == "393@c.us"

    def test_process_does_not_publish(self):
        contact = ChatContact(id=CID, display_name="Ada", protocol=PROTOCOL_SIGNAL)
        backend = _backend(contacts=[contact])
        core = _core(backend)
        sub = core.bus.subscribe()
        core.process(_msg_event(contact=contact))
        assert sub.drain() == []


# ─── message_edit ────────────────────────────────────────────────────────────

_EDIT_INFO = {
    "message_id": "77",
    "timestamp": 1000,
    "old_text": "Prima",
    "is_mine": False,
}


def _edit_event(**payload) -> ChatEvent:
    data = {"edit_message_id": 77, "text": "Dopo", "is_mine": False}
    data.update(payload)
    return ChatEvent(
        type="message_edit", protocol=PROTOCOL_SIGNAL, contact_id=CID, payload=data
    )


class TestProcessEdit:
    def test_emits_info_and_text(self):
        backend = _backend(apply_edit=MagicMock(return_value=_EDIT_INFO))

        [(etype, payload)] = _core(backend).process(_edit_event(edit_timestamp=5))

        assert etype == "message_edit"
        backend.apply_edit.assert_called_once_with(
            CID, "77", "Dopo", is_mine=False, edit_timestamp=5
        )
        assert payload["info"] == _EDIT_INFO
        assert payload["text"] == "Dopo"
        assert payload["protocol"] == PROTOCOL_SIGNAL
        assert payload["contact_id"] == CID
        assert payload["contact"]["id"] == CID  # placeholder resolved

    def test_noop_edit_emits_nothing(self):
        backend = _backend(apply_edit=MagicMock(return_value=None))
        assert _core(backend).process(_edit_event()) == []

    @pytest.mark.parametrize(
        "payload", [{"edit_message_id": None}, {"text": ""}], ids=["no-id", "no-text"]
    )
    def test_missing_id_or_text_is_ignored(self, payload):
        backend = _backend(apply_edit=MagicMock(return_value=_EDIT_INFO))
        assert _core(backend).process(_edit_event(**payload)) == []
        backend.apply_edit.assert_not_called()

    def test_no_backend_or_no_apply_edit(self):
        assert _core(None).process(_edit_event()) == []
        assert _core(_backend()).process(_edit_event()) == []


# ─── reaction_update ─────────────────────────────────────────────────────────


class TestProcessReaction:
    def _event(self):
        return ChatEvent(
            type="reaction_update",
            protocol="telegram",
            contact_id="42",
            payload={"mode": "snapshot"},
        )

    def test_emits_info(self):
        info = {"message_id": 1, "timestamp": 2, "reactions": []}
        backend = _backend(apply_reaction=MagicMock(return_value=info))
        event = self._event()

        [(etype, payload)] = _core(backend).process(event)

        backend.apply_reaction.assert_called_once_with("42", event.payload)
        assert etype == "reaction_update"
        assert payload == {"protocol": "telegram", "contact_id": "42", "info": info}

    def test_nothing_without_info_backend_or_method(self):
        assert (
            _core(_backend(apply_reaction=MagicMock(return_value=None))).process(
                self._event()
            )
            == []
        )
        assert _core(None).process(self._event()) == []
        assert _core(_backend()).process(self._event()) == []


# ─── receipt ─────────────────────────────────────────────────────────────────


class TestProcessReceipt:
    def test_signal_builds_envelope(self):
        updated = [{"id": "1", "timestamp": 1000, "status": "read"}]
        backend = _backend(process_receipt=MagicMock(return_value=updated))
        event = ChatEvent(
            type="receipt",
            protocol=PROTOCOL_SIGNAL,
            contact_id=CID,
            payload={"receipt": {"isRead": True, "timestamps": [1000]}},
        )

        [(etype, payload)] = _core(backend).process(event)

        backend.process_receipt.assert_called_once_with(
            {
                "sourceNumber": CID,
                "source": CID,
                "receiptMessage": {"isRead": True, "timestamps": [1000]},
            }
        )
        assert etype == "receipt"
        assert payload == {
            "protocol": PROTOCOL_SIGNAL,
            "contact_id": CID,
            "updated": updated,
        }

    def test_generic_uses_message_ids_payload(self):
        updated = [{"id": "77", "timestamp": 1, "status": "read"}]
        backend = _backend(process_receipt=MagicMock(return_value=updated))
        event = ChatEvent(
            type="receipt",
            protocol="telegram",
            contact_id="42",
            payload={"message_ids": ["77"], "is_read": True},
        )

        [(_, payload)] = _core(backend).process(event)

        backend.process_receipt.assert_called_once_with(
            {"message_ids": ["77"], "is_read": True, "contact_id": "42"}
        )
        assert payload["updated"] == updated

    def test_generic_defaults(self):
        backend = _backend(process_receipt=MagicMock(return_value=[]))
        event = ChatEvent(type="receipt", protocol="telegram", contact_id="42")
        assert _core(backend).process(event) == []
        backend.process_receipt.assert_called_once_with(
            {"message_ids": [], "is_read": False, "contact_id": "42"}
        )

    def test_nothing_without_backend_or_method(self):
        event = ChatEvent(type="receipt", protocol="telegram", contact_id="42")
        assert _core(None).process(event) == []
        assert _core(_backend()).process(event) == []


# ─── typing ──────────────────────────────────────────────────────────────────


class TestProcessTyping:
    @pytest.mark.parametrize("action", ["STARTED", "STOPPED"])
    def test_valid_actions(self, action):
        event = ChatEvent(
            type="typing",
            protocol=PROTOCOL_SIGNAL,
            contact_id=CID,
            payload={"action": action},
        )
        # No backend needed for typing.
        assert _core(None).process(event) == [
            (
                "typing",
                {"protocol": PROTOCOL_SIGNAL, "contact_id": CID, "action": action},
            )
        ]

    @pytest.mark.parametrize("payload", [{}, {"action": "started"}, {"action": "X"}])
    def test_invalid_action_ignored(self, payload):
        event = ChatEvent(
            type="typing",
            protocol=PROTOCOL_SIGNAL,
            contact_id=CID,
            payload=payload,
        )
        assert _core(None).process(event) == []


# ─── pass-through events ─────────────────────────────────────────────────────


class TestProcessPassthrough:
    def test_contact_update_passthrough(self):
        contact = ChatContact(id="1@lid", display_name="G", protocol=PROTOCOL_WHATSAPP)
        event = ChatEvent(
            type="contact_update",
            protocol=PROTOCOL_WHATSAPP,
            contact_id="1@lid",
            payload={"display_name": "G", "phone": "39", "contact": contact},
        )
        assert _core(None).process(event) == [
            (
                "contact_update",
                {
                    "protocol": PROTOCOL_WHATSAPP,
                    "contact_id": "1@lid",
                    "display_name": "G",
                    "phone": "39",
                    "contact": contact_to_wire(contact),
                },
            )
        ]

    def test_contact_merge_passthrough_keeps_merged_into(self):
        event = ChatEvent(
            type="contact_update",
            protocol=PROTOCOL_WHATSAPP,
            contact_id="1@lid",
            payload={"merged_into": "39@c.us"},
        )
        [(etype, payload)] = _core(None).process(event)
        assert etype == "contact_update"
        assert payload["merged_into"] == "39@c.us"
        assert payload["contact_id"] == "1@lid"

    def test_lid_warmup_done_passthrough(self):
        event = ChatEvent(
            type="lid_warmup_done", protocol=PROTOCOL_WHATSAPP, contact_id=""
        )
        assert _core(None).process(event) == [
            ("lid_warmup_done", {"protocol": PROTOCOL_WHATSAPP, "contact_id": ""})
        ]

    def test_sent_mirror_registers_unknown_contact(self):
        backend = _backend(_identify_contact=MagicMock(return_value=None))
        event = ChatEvent(
            type="sent-mirror",
            protocol=PROTOCOL_SIGNAL,
            contact_id=CID,
            payload={"id": "1", "timestamp": 5, "batch_id": "b"},
        )

        [(etype, payload)] = _core(backend).process(event)

        assert etype == "sent_mirror"
        assert payload["batch_id"] == "b"
        assert payload["timestamp"] == 5
        assert payload["contact"]["cache_key"] == f"{PROTOCOL_SIGNAL}:{CID}"
        assert [contact_to_wire(c) for c in backend.contacts] == [payload["contact"]]

    def test_sent_mirror_prefers_backend_object_without_lookup(self):
        known = ChatContact(id=CID, display_name="Ada", protocol=PROTOCOL_SIGNAL)
        backend = _backend(contacts=[known], _identify_contact=MagicMock())
        event = ChatEvent(type="sent-mirror", protocol=PROTOCOL_SIGNAL, contact_id=CID)

        [(_, payload)] = _core(backend).process(event)

        assert payload["contact"] == contact_to_wire(known)
        backend._identify_contact.assert_not_called()

    def test_sent_mirror_without_backend(self):
        event = ChatEvent(type="sent-mirror", protocol=PROTOCOL_SIGNAL, contact_id=CID)
        assert _core(None).process(event) == []

    def test_unknown_type_ignored(self):
        event = ChatEvent(type="mystery", protocol=PROTOCOL_SIGNAL, contact_id=CID)
        assert _core(_backend()).process(event) == []


# ─── pump ────────────────────────────────────────────────────────────────────


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class TestPump:
    def _typing(self, action="STARTED") -> ChatEvent:
        return ChatEvent(
            type="typing",
            protocol=PROTOCOL_SIGNAL,
            contact_id=CID,
            payload={"action": action},
        )

    def test_pump_publishes_processed_events_and_stops_promptly(self):
        batches = [[self._typing("STARTED"), self._typing("STOPPED")]]
        backend = _backend(poll_once=lambda: batches.pop(0) if batches else [])
        core = _core(backend)
        sub = core.bus.subscribe()

        core._start_pump()
        try:
            first = sub.get(timeout=3)
            second = sub.get(timeout=3)
            assert first is not None and second is not None
            assert [first.type, second.type] == ["typing", "typing"]
            assert [first.payload["action"], second.payload["action"]] == [
                "STARTED",
                "STOPPED",
            ]
            thread = core._pump_thread
            assert thread is not None and thread.daemon and thread.is_alive()
        finally:
            started = time.monotonic()
            core.stop()
        assert thread is not None
        assert not thread.is_alive()
        assert time.monotonic() - started < 1.0

    def test_start_launches_pump_and_stop_exits(self, monkeypatch):
        monkeypatch.setattr("server.core.signal_enabled", lambda: False)
        core = _core(_backend(poll_once=list))
        core._spawn = MagicMock()  # no connect workers in this test
        core.start()
        thread = core._pump_thread
        assert thread is not None and thread.is_alive()
        core.start()  # idempotent: no second pump
        assert core._pump_thread is thread
        core.stop()
        assert not thread.is_alive()

    def test_bad_events_and_backends_do_not_kill_the_loop(self):
        calls = {"n": 0}

        def poll_once():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            if calls["n"] == 2:
                return [
                    ChatEvent(type="receipt", protocol=PROTOCOL_SIGNAL, contact_id=CID),
                    self._typing(),
                ]
            return []

        def bad_receipt(_envelope):
            raise ValueError("bad event")

        no_poll = SimpleNamespace(protocol="x")  # AttributeError on poll_once
        backend = _backend(poll_once=poll_once, process_receipt=bad_receipt)
        core = _core(backend, backends=[no_poll, backend])
        sub = core.bus.subscribe()

        core._start_pump()
        try:
            # round 1 raises, round 2 has one failing and one good event
            event = sub.get(timeout=5)
        finally:
            core.stop()
        assert event is not None and event.type == "typing"

    def test_stop_before_start_and_restart(self):
        core = _core(_backend(poll_once=list))
        core.stop()  # no thread: no-op
        core._start_pump()
        first = core._pump_thread
        core.stop()
        assert first is not None and not first.is_alive()
        core._start_pump()
        second = core._pump_thread
        assert second is not None and second is not first and second.is_alive()
        core.stop()

    def test_pump_runs_on_single_named_daemon_thread(self):
        names = []
        backend = _backend(
            poll_once=lambda: names.append(threading.current_thread().name) or []
        )
        core = _core(backend)
        core._start_pump()
        try:
            assert _wait_for(lambda: bool(names))
        finally:
            core.stop()
        assert set(names) == {"core-pump"}
