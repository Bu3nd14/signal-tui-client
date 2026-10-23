"""WEBJS ``e2e_notification`` frames are service notices, never chat bubbles.

The "security code changed" notice arrives with the affected contact's JID
as ``body`` (e.g. ``20508603076702@lid``): it used to be shown verbatim as a
text message from the group.
"""

from types import SimpleNamespace

from models import MSG_TYPE_SYSTEM
from protocols.whatsapp_events import _event_from_message

GROUP = "120363000000000000@g.us"
LID = "20508603076702@lid"


def _identity_raw(**data_extra):
    data = {"type": "e2e_notification", "subtype": "identity", "body": LID}
    data.update(data_extra)
    return {
        "id": "false_120363000000000000@g.us_3EB0ABC_20508603076702@lid",
        "from": GROUP,
        "fromMe": False,
        "timestamp": 1792578000,
        "body": LID,
        "hasMedia": False,
        "_data": data,
    }


def test_identity_change_becomes_system_notice():
    events = _event_from_message(_identity_raw())
    assert len(events) == 1
    payload = events[0].payload
    assert events[0].contact_id == GROUP
    assert payload["msg_type"] == MSG_TYPE_SYSTEM
    assert payload["sender"] == ""
    assert payload["is_mine"] is False
    assert LID not in payload["text"]
    assert payload["text"] == "Il codice di sicurezza di un contatto è cambiato."
    assert payload["timestamp"] == 1792578000 * 1000


def test_identity_change_resolves_contact_name():
    contacts = {LID: SimpleNamespace(display_name="Arefoneb")}
    payload = _event_from_message(_identity_raw(), contacts)[0].payload
    assert payload["text"] == "Il tuo codice di sicurezza con Arefoneb è cambiato."


def test_identity_change_falls_back_to_lid_phone():
    payload = _event_from_message(
        _identity_raw(), None, lambda jid: "393331234567" if jid == LID else None
    )[0].payload
    assert payload["text"] == (
        "Il tuo codice di sicurezza con +393331234567 è cambiato."
    )


def test_unresolved_lid_schedules_background_resolve():
    scheduled = []
    _event_from_message(_identity_raw(), None, lambda _jid: None, scheduled.append)
    assert scheduled == [LID]


def test_other_e2e_subtypes_are_dropped():
    raw = _identity_raw(subtype="encrypt", body="")
    raw["body"] = ""
    assert _event_from_message(raw) == []


def test_top_level_type_is_honoured():
    raw = _identity_raw()
    raw["type"] = "e2e_notification"
    raw["subtype"] = "identity"
    raw["_data"] = {}
    events = _event_from_message(raw)
    assert events[0].payload["msg_type"] == MSG_TYPE_SYSTEM


def test_plain_text_message_is_unaffected():
    raw = _identity_raw()
    raw["_data"] = {"type": "chat"}
    raw["body"] = "ciao"
    payload = _event_from_message(raw)[0].payload
    assert payload["msg_type"] == "text"
    assert payload["text"] == "ciao"
