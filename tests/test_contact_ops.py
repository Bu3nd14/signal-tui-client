"""Contact operations used by the TUI (P1.3) and the TUI flows built on them."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from client_harness import client_for

from models import PROTOCOL_SIGNAL, PROTOCOL_WHATSAPP, ChatContact
from server.api import ApiError
from server.bus import EventBus
from server.core import ChatCore
from server.domain import bump_recency, sync_recency
from server.events_api import contact_from_wire
from tui.app import SignalTUI


def _contact(proto=PROTOCOL_SIGNAL, cid="+1", name="Anna", **extras) -> ChatContact:
    return ChatContact(id=cid, display_name=name, protocol=proto, extras=dict(extras))


def _manager(**backends):
    return SimpleNamespace(
        get=lambda proto: backends.get(proto),
        address_book_errors={},
        list_address_book_sync=MagicMock(return_value=[]),
    )


# -- contacts.for_protocol -----------------------------------------------------


class TestForProtocol:
    def test_returns_json_wire_copies(self):
        anna = _contact(aci="uuid", number="+1", last_message_ts=5, is_group=False)
        backend = SimpleNamespace(contacts=[anna])
        client = client_for(_manager(signal=backend))

        wire = client.call("contacts.for_protocol", {"protocol": "signal"})

        json.dumps(wire)
        assert wire[0]["cache_key"] == "signal:+1"
        assert wire[0]["last_message_ts"] == 5
        copy = contact_from_wire(wire[0])
        assert copy == anna
        assert copy is not anna and copy.extras is not anna.extras

    def test_unknown_backend_is_refused(self):
        client = client_for(_manager())
        with pytest.raises(ApiError) as err:
            client.call("contacts.for_protocol", {"protocol": "signal"})
        assert err.value.code == "backend_down"


# -- contacts.register ---------------------------------------------------------


class TestRegister:
    def test_registers_a_ghost_in_the_backend_and_returns_it(self):
        backend = MagicMock()
        client = client_for(_manager(whatsapp=backend))
        mine = _contact(PROTOCOL_WHATSAPP, "39@c.us", "Mario", phone="39")

        wire = client.call(
            "contacts.register",
            {
                "id": mine.id,
                "display_name": "Mario",
                "protocol": "whatsapp",
                "extras": mine.extras,
            },
        )

        (registered,) = backend.register_contact.call_args.args
        assert registered.extras == {"phone": "39", "ghost": True}
        assert registered is not mine and "ghost" not in mine.extras
        assert contact_from_wire(wire) == registered

    def test_metadata_merged_by_the_backend_is_returned(self):
        backend = MagicMock()
        backend.register_contact.side_effect = lambda c: c.extras.update(lid="1@lid")
        client = client_for(_manager(whatsapp=backend))

        wire = client.call(
            "contacts.register",
            {"id": "39@c.us", "display_name": "M", "protocol": "whatsapp"},
        )

        assert wire["extras"]["lid"] == "1@lid"
        assert wire["extras"]["ghost"] is True

    @pytest.mark.parametrize(
        "payload,code",
        [
            ({"id": "x", "display_name": "x", "protocol": "nosuch"}, "invalid_request"),
            ({"id": "", "display_name": "x", "protocol": "signal"}, "invalid_request"),
            ({"id": "x", "display_name": "x", "protocol": "signal"}, "backend_down"),
        ],
    )
    def test_refusals(self, payload, code):
        client = client_for(_manager())
        with pytest.raises(ApiError) as err:
            client.call("contacts.register", payload)
        assert err.value.code == code


# -- contacts.address_book -----------------------------------------------------


class TestAddressBook:
    def test_scope_force_errors_and_resolver(self):
        wa = MagicMock()
        manager = _manager(whatsapp=wa)
        manager.list_address_book_sync.return_value = [
            _contact(PROTOCOL_WHATSAPP, "1@c.us", "Mario", address_book=True)
        ]
        manager.address_book_errors = {"telegram": "boom"}
        client = client_for(manager)

        out = client.call(
            "contacts.address_book", {"protocols": ["whatsapp"], "force": True}
        )

        manager.list_address_book_sync.assert_called_once_with(
            protocols={"whatsapp"}, force=True
        )
        wa.start_lid_resolver.assert_called_once_with()
        assert out["errors"] == {"telegram": "boom"}
        assert out["contacts"][0]["extras"]["address_book"] is True

    def test_no_scope_means_all_protocols(self):
        manager = _manager()
        client_for(manager).call("contacts.address_book", {})
        manager.list_address_book_sync.assert_called_once_with(
            protocols=None, force=False
        )


# -- contacts.touch / recency ---------------------------------------------------


class TestRecency:
    def test_touch_raises_but_never_lowers_the_backend_contact(self):
        anna = _contact(last_message_ts=10)
        client = client_for(_manager(signal=SimpleNamespace(contacts=[anna])))

        client.call(
            "contacts.touch",
            {"protocol": "signal", "contact_id": "+1", "timestamp": 20},
        )
        assert anna.last_message_ts == 20
        client.call(
            "contacts.touch", {"protocol": "signal", "contact_id": "+1", "timestamp": 3}
        )
        assert anna.last_message_ts == 20

    def test_touch_unknown_backend_or_contact_is_a_noop(self):
        client = client_for(_manager(signal=SimpleNamespace(contacts=[])))
        for proto in ("signal", "whatsapp"):
            assert client.call(
                "contacts.touch",
                {"protocol": proto, "contact_id": "x", "timestamp": 1},
            ) == {"ok": True}

    def test_bump_recency_ignores_non_int(self):
        anna = _contact()
        assert not bump_recency(SimpleNamespace(contacts=[anna]), anna.cache_key, "9")
        assert anna.last_message_ts == 0

    def test_sync_recency_uses_cache_across_storage_keys(self):
        wa = _contact(PROTOCOL_WHATSAPP, "39@lid", "M", phone="39")
        other = _contact(PROTOCOL_WHATSAPP, "1@c.us", "N", last_message_ts=50)
        backend = SimpleNamespace(
            contacts=[wa, other],
            cache={
                "39@lid": [{"timestamp": 7}],
                "39@c.us": [{"timestamp": 9}],
                "1@c.us": [{"timestamp": 8}],
            },
        )
        sync_recency(backend)
        assert wa.last_message_ts == 9
        assert other.last_message_ts == 50
        assert "last_message_ts" not in _contact().extras  # untouched when equal

    def test_core_syncs_recency_before_announcing_the_backend(self):
        bus = EventBus()
        with (
            patch("server.core.whatsapp_enabled", return_value=False),
            patch("server.core.telegram_enabled", return_value=False),
        ):
            core = ChatCore(bus)
        anna = _contact()
        backend = SimpleNamespace(
            protocol="signal", contacts=[anna], cache={"+1": [{"timestamp": 42}]}
        )
        sub = bus.subscribe()

        core._ready(backend)

        assert anna.last_message_ts == 42
        assert sub.drain()[-1].payload == {"protocol": "signal", "contacts": 1}


# -- TUI flows -----------------------------------------------------------------


def _app(manager) -> SignalTUI:
    with (
        patch("server.core.whatsapp_enabled", return_value=False),
        patch("server.core.telegram_enabled", return_value=False),
    ):
        app = SignalTUI()
    app.manager = manager
    app._render_contact_list = MagicMock()
    app._update_unread_badges = MagicMock()
    app._status = MagicMock()
    app._select_contact = MagicMock()
    return app


class TestTuiSeeding:
    def test_backend_ready_seeds_wire_copies_not_backend_objects(self):
        anna = _contact(is_group=True, last_message_ts=9)
        backend = SimpleNamespace(protocol="signal", contacts=[anna], cache={})
        app = _app(_manager(signal=backend))
        app._pending_backends = {"signal"}

        app._on_backend_ready(backend)

        (seeded,) = app.contacts
        assert seeded == anna and seeded is not anna
        assert seeded.extras["is_group"] is True
        seeded.last_message_ts = 99
        seeded.extras["ghost"] = True
        assert anna.last_message_ts == 9 and "ghost" not in anna.extras
        app._status.assert_called_with("✅ Signal: 1 contacts loaded")
        app._select_contact.assert_called_once()

    def test_second_ready_upgrades_recency_and_does_not_duplicate(self):
        anna = _contact(last_message_ts=1)
        backend = SimpleNamespace(protocol="signal", contacts=[anna], cache={})
        app = _app(_manager(signal=backend))
        app._on_backend_ready(backend)
        anna.last_message_ts = 7

        app._on_backend_ready(backend)

        assert len(app.contacts) == 1 and app.contacts[0].last_message_ts == 7

    def test_second_ready_refreshes_name_and_extras_keeping_identity(self):
        anna = _contact(phone="1", last_message_ts=5)
        backend = SimpleNamespace(protocol="signal", contacts=[anna], cache={})
        app = _app(_manager(signal=backend))
        app._on_backend_ready(backend)
        (seeded,) = app.contacts
        seeded.last_message_ts = 50  # newer on the TUI side
        # The backend refreshes its own contact in place (e.g. WhatsApp reconnect).
        anna.display_name = "Anna B"
        anna.extras.update(lid="9@lid", phone="2")

        app._on_backend_ready(backend)

        assert app.contacts == [seeded] and app.contacts[0] is seeded
        assert seeded.display_name == "Anna B"
        assert seeded.extras["lid"] == "9@lid" and seeded.phone == "2"
        assert seeded.last_message_ts == 50

    def test_refused_operation_still_reports_the_backend_done(self):
        backend = SimpleNamespace(protocol="signal", contacts=[], cache={})
        app = _app(_manager())  # core knows no backend
        app._pending_backends = {"signal"}

        app._on_backend_ready(backend)

        assert app.contacts == [] and app._pending_backends == set()


class TestTuiRegisterAndMarkRead:
    def test_picker_contact_becomes_registered_ghost_copy(self):
        backend = MagicMock()
        app = _app(_manager(whatsapp=backend))
        picked = _contact(PROTOCOL_WHATSAPP, "39@c.us", "Mario", phone="39")

        result = app._ensure_contact_selectable(picked)

        assert result is not None and result is not picked
        assert result.extras["ghost"] is True and app.contacts == [result]
        assert "ghost" not in picked.extras

    def test_mark_read_runs_off_the_caller_thread(self):
        seen: dict[str, object] = {}
        manager = _manager()

        async def mark_read(protocol, contact_id):
            seen["thread"] = threading.current_thread()
            seen["args"] = (protocol, contact_id)

        manager.mark_read = mark_read
        app = _app(manager)
        threads: list[threading.Thread] = []

        def run_worker(fn, thread=False, **_kw):
            t = threading.Thread(target=fn)
            threads.append(t)
            t.start()

        app.run_worker = run_worker  # type: ignore[method-assign]
        with patch.object(
            SignalTUI, "is_running", new_callable=PropertyMock, return_value=True
        ):
            app._client_call_background(
                "chats.mark_read", {"protocol": "signal", "contact_id": "+1"}
            )
        for t in threads:
            t.join(5)

        assert seen["args"] == ("signal", "+1")
        assert seen["thread"] is not threading.main_thread()

    def test_background_failure_is_logged_not_raised(self):
        app = _app(_manager())
        app._client_call_background("chats.mark_read", {"protocol": "bogus"})


class TestTuiAddressBook:
    def test_worker_applies_contacts_and_reports_partial_errors(self):
        manager = _manager()
        manager.list_address_book_sync.return_value = [
            _contact(PROTOCOL_WHATSAPP, "1@c.us", "Mario", address_book=True)
        ]
        manager.address_book_errors = {"telegram": "down"}
        app = _app(manager)
        app.call_from_thread = lambda fn, *a: fn(*a)  # type: ignore[method-assign]
        app._address_book_token = 1
        screen = MagicMock()
        screen.is_mounted = True

        app._address_book_worker(1, {"whatsapp"}, screen)

        (shown,) = screen.set_contacts.call_args.args[0]
        assert shown.cache_key == "whatsapp:1@c.us" and shown.extras["address_book"]
        app._status.assert_called_once_with("⚠️ Rubrica parziale: telegram")
