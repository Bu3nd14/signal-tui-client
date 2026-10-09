"""Unit + integration tests for the dynamic address book (design v3).

Covers the TUI side of the feature (§9.2/§9.4 of
``docs/DESIGN_DYNAMIC_ADDRESS_BOOK.md``) and the refresh scheduler (§6.4):

- ``_handle_message_event`` re-anchors the TUI contact object (BLOCCANTE 1).
- ``_handle_contact_update_event`` applies/appends without touching the cache.
- R1: Signal uuid↔number dedup via ``_signal_stable_key``.
- ``_looks_unresolved`` for WhatsApp/Signal/Telegram placeholders.
- Lazy trigger: scoped, on unresolved only, never gated on ``contact is None``.
- Scheduler: lazy cooldown, periodic cadence, ``stop()`` without join.

The backend refresh tests live in ``tests/test_address_book.py``.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from models import (
    PROTOCOL_SIGNAL,
    PROTOCOL_TELEGRAM,
    PROTOCOL_WHATSAPP,
    ChatContact,
    ChatEvent,
    contact_cache_key,
)
from protocols.whatsapp import WhatsAppBackend
from tui.address_book_refresh import DynamicAddressBookMixin
from tui.events import EventHandlingMixin, _looks_unresolved, _signal_stable_key
from tui.pickers import PickerMixin

# ─── Harness ─────────────────────────────────────────────────────────────────


class _App(EventHandlingMixin):
    """Minimal harness exposing only the attributes touched by the handlers."""

    def __init__(self, backend, contacts=None, *, web_enabled=False):
        self.manager = SimpleNamespace(get=lambda _protocol: backend)
        self.contacts = list(contacts or [])
        self.selected_contact = None
        self._contact_list_dirty = False
        self._dirty_contact_keys: set[str] = set()
        self._contact_widgets: dict[str, object] = {}
        self._cache: dict[str, list[dict]] = {}
        self._typing_contacts: dict[str, float] = {}
        self._typing_mumbling: dict[str, float] = {}
        self._seen_message_ids: set[tuple] = set()
        self._seen_timestamps: set[tuple] = set()
        self._TYPING_MUMBLING_DURATION = 5.0
        self._web_enabled = web_enabled
        self.scheduled: list[str] = []

    def call_from_thread(self, *_args, **_kwargs):  # pragma: no cover - no-op
        return None

    def schedule_address_book_refresh(self, protocol: str) -> None:
        self.scheduled.append(protocol)


def _message_event(
    protocol: str,
    contact_id: str,
    *,
    contact: ChatContact | None = None,
    timestamp: int = 1000,
) -> ChatEvent:
    payload = {
        "id": f"m-{contact_id}",
        "text": "ciao",
        "is_mine": False,
        "sender": contact_id,
        "timestamp": timestamp,
        "msg_type": "text",
    }
    if contact is not None:
        payload["contact"] = contact
    return ChatEvent(
        type="message", protocol=protocol, contact_id=contact_id, payload=payload
    )


def _contact_update_event(contact: ChatContact, phone: str | None = None) -> ChatEvent:
    payload = {
        "display_name": contact.display_name,
        "phone": phone if phone is not None else contact.extras.get("phone"),
        "contact": contact,
    }
    return ChatEvent(
        type="contact_update",
        protocol=contact.protocol,
        contact_id=contact.id,
        payload=payload,
    )


def _wa_backend() -> WhatsAppBackend:
    backend = WhatsAppBackend(api_url="")
    backend.ingest_message = MagicMock(return_value=True)
    return backend


# ─── §7.1 BLOCCANTE 1 — re-anchor the TUI object ─────────────────────────────


class TestMessageEventReanchor:
    def test_message_event_updates_tui_object_not_payload_copy(self):
        tui_contact = ChatContact(
            id="1@c.us",
            display_name="Mario",
            protocol=PROTOCOL_WHATSAPP,
            extras={"last_message_ts": 10},
        )
        payload_contact = ChatContact(
            id="1@c.us", display_name="Mario", protocol=PROTOCOL_WHATSAPP
        )
        app = _App(_wa_backend(), contacts=[tui_contact])

        event = _message_event(
            PROTOCOL_WHATSAPP, "1@c.us", contact=payload_contact, timestamp=5000
        )
        assert app._handle_message_event(event) is True

        assert app.contacts[0] is tui_contact
        assert tui_contact.last_message_ts == 5000
        # The payload copy must not have been mutated.
        assert payload_contact.last_message_ts == 0

    def test_message_event_appends_unknown_contact_once(self):
        backend = _wa_backend()
        backend._identify_contact = MagicMock(return_value=None)
        backend.register_contact = MagicMock(return_value=True)
        app = _App(backend, contacts=[])

        event = _message_event(PROTOCOL_WHATSAPP, "2@c.us")
        assert app._handle_message_event(event) is True

        assert len(app.contacts) == 1
        assert app.contacts[0].cache_key == contact_cache_key(
            PROTOCOL_WHATSAPP, "2@c.us"
        )


# ─── §7.2 apply/append ───────────────────────────────────────────────────────


class TestContactUpdateEvent:
    def test_appends_new_contact_without_touching_cache(self):
        app = _App(_wa_backend(), contacts=[])
        new = ChatContact(id="9@c.us", display_name="Nina", protocol=PROTOCOL_WHATSAPP)

        assert app._handle_contact_update_event(_contact_update_event(new)) is True

        assert app.contacts == [new]
        assert app._cache == {}
        assert app._contact_list_dirty is True
        assert new.cache_key in app._dirty_contact_keys

    def test_updates_existing_display_name_in_place(self):
        existing = ChatContact(
            id="1@c.us",
            display_name="Mario",
            protocol=PROTOCOL_WHATSAPP,
            extras={},
        )
        app = _App(_wa_backend(), contacts=[existing])
        incoming = ChatContact(
            id="1@c.us", display_name="Mario Rossi", protocol=PROTOCOL_WHATSAPP
        )

        assert app._handle_contact_update_event(_contact_update_event(incoming)) is True

        assert len(app.contacts) == 1
        assert app.contacts[0] is existing
        assert existing.display_name == "Mario Rossi"
        assert app._cache == {}

    def test_fills_missing_phone_without_overwriting_existing(self):
        existing = ChatContact(
            id="1@c.us",
            display_name="Mario",
            protocol=PROTOCOL_WHATSAPP,
            extras={"phone": "111"},
        )
        app = _App(_wa_backend(), contacts=[existing])
        incoming = ChatContact(
            id="1@c.us", display_name="Mario", protocol=PROTOCOL_WHATSAPP
        )

        app._handle_contact_update_event(_contact_update_event(incoming, phone="222"))

        assert existing.extras["phone"] == "111"


# ─── R1 — Signal uuid↔number dedup ───────────────────────────────────────────


class TestSignalStableKey:
    def test_aci_wins_over_number(self):
        contact = ChatContact(
            id="uuid-1",
            display_name="Mario",
            protocol=PROTOCOL_SIGNAL,
            extras={"aci": "aci-1", "number": "+39123"},
        )
        assert _signal_stable_key(contact) == "aci:aci-1"

    def test_number_normalized_when_no_aci(self):
        contact = ChatContact(
            id="uuid-1",
            display_name="Mario",
            protocol=PROTOCOL_SIGNAL,
            extras={"number": "+39 123 456 7890"},
        )
        assert _signal_stable_key(contact) == "phone:391234567890"

    def test_falls_back_to_id(self):
        contact = ChatContact(
            id="+39 333 123 4567", display_name="Mario", protocol=PROTOCOL_SIGNAL
        )
        assert _signal_stable_key(contact) == "phone:393331234567"


class TestSignalDedupOnContactUpdate:
    def test_uuid_to_number_removes_stale_row(self):
        stale = ChatContact(
            id="uuid-123",
            display_name="+391234567890",
            protocol=PROTOCOL_SIGNAL,
            extras={"aci": "aci-1", "number": ""},
        )
        fresh = ChatContact(
            id="+391234567890",
            display_name="Mario",
            protocol=PROTOCOL_SIGNAL,
            extras={"aci": "aci-1", "number": "+391234567890"},
        )
        app = _App(_wa_backend(), contacts=[stale])
        app._contact_widgets["signal:uuid-123"] = object()

        assert app._handle_contact_update_event(_contact_update_event(fresh)) is True

        assert len(app.contacts) == 1
        assert app.contacts[0] is fresh
        # The stale widget + key are marked dirty so the row is re-rendered.
        assert "signal:uuid-123" not in app._contact_widgets
        assert "signal:uuid-123" in app._dirty_contact_keys

    def test_number_match_without_aci_dedups(self):
        stale = ChatContact(
            id="uuid-abc",
            display_name="+391234567890",
            protocol=PROTOCOL_SIGNAL,
            extras={"number": "+391234567890"},
        )
        fresh = ChatContact(
            id="+391234567890",
            display_name="Mario",
            protocol=PROTOCOL_SIGNAL,
            extras={"number": "+391234567890"},
        )
        app = _App(_wa_backend(), contacts=[stale])

        app._handle_contact_update_event(_contact_update_event(fresh))

        assert len(app.contacts) == 1
        assert app.contacts[0] is fresh

    def test_different_person_is_not_deduped(self):
        other = ChatContact(
            id="+391111111111",
            display_name="Luigi",
            protocol=PROTOCOL_SIGNAL,
            extras={"aci": "aci-2", "number": "+391111111111"},
        )
        fresh = ChatContact(
            id="+391234567890",
            display_name="Mario",
            protocol=PROTOCOL_SIGNAL,
            extras={"aci": "aci-1", "number": "+391234567890"},
        )
        app = _App(_wa_backend(), contacts=[other])

        app._handle_contact_update_event(_contact_update_event(fresh))

        assert len(app.contacts) == 2
        assert other in app.contacts


# ─── §6.2 _looks_unresolved ──────────────────────────────────────────────────


class TestLooksUnresolved:
    @pytest.mark.parametrize(
        "display_name,contact_id,phone,expected",
        [
            ("", "1@c.us", None, True),
            ("1@c.us", "1@c.us", None, True),
            ("220988985864200", "220988985864200@lid", None, True),
            ("393331234567", "393331234567@c.us", None, True),
            ("Mario", "393331234567@c.us", None, False),
            ("Mario", "1@c.us", "393331234567", False),
        ],
    )
    def test_whatsapp(self, display_name, contact_id, phone, expected):
        extras = {"phone": phone} if phone else {}
        contact = ChatContact(
            id=contact_id,
            display_name=display_name,
            protocol=PROTOCOL_WHATSAPP,
            extras=extras,
        )
        assert _looks_unresolved(contact) is expected

    @pytest.mark.parametrize(
        "protocol,display_name,contact_id,expected",
        [
            (PROTOCOL_SIGNAL, "", "+391234567890", True),
            (PROTOCOL_SIGNAL, "+391234567890", "+391234567890", True),
            (PROTOCOL_SIGNAL, "12345", "12345", True),
            (PROTOCOL_SIGNAL, "Mario", "+391234567890", False),
            (PROTOCOL_TELEGRAM, "", "42", True),
            (PROTOCOL_TELEGRAM, "42", "42", True),
            (PROTOCOL_TELEGRAM, "Ada", "42", False),
        ],
    )
    def test_signal_and_telegram(self, protocol, display_name, contact_id, expected):
        contact = ChatContact(
            id=contact_id, display_name=display_name, protocol=protocol
        )
        assert _looks_unresolved(contact) is expected


# ─── §6.2 Lazy trigger ───────────────────────────────────────────────────────


class TestLazyTrigger:
    def test_unresolved_placeholder_schedules_scoped_refresh(self):
        backend = _wa_backend()
        backend._identify_contact = MagicMock(return_value=None)
        backend.register_contact = MagicMock(return_value=True)
        app = _App(backend, contacts=[])

        app._handle_message_event(_message_event(PROTOCOL_WHATSAPP, "1@c.us"))

        assert app.scheduled == [PROTOCOL_WHATSAPP]

    def test_resolved_contact_does_not_schedule(self):
        resolved = ChatContact(
            id="1@c.us",
            display_name="Mario",
            protocol=PROTOCOL_WHATSAPP,
            extras={"phone": "1"},
        )
        app = _App(_wa_backend(), contacts=[resolved])

        app._handle_message_event(
            _message_event(
                PROTOCOL_WHATSAPP, "1@c.us", contact=resolved, timestamp=2000
            )
        )

        assert app.scheduled == []

    def test_trigger_not_gated_on_contact_none(self):
        """An explicitly provided (non-None) unresolved contact still triggers."""
        placeholder = ChatContact(
            id="1@c.us", display_name="1@c.us", protocol=PROTOCOL_WHATSAPP
        )
        app = _App(_wa_backend(), contacts=[placeholder])

        app._handle_message_event(
            _message_event(
                PROTOCOL_WHATSAPP, "1@c.us", contact=placeholder, timestamp=3000
            )
        )

        assert app.scheduled == [PROTOCOL_WHATSAPP]

    def test_trigger_is_scoped_to_message_protocol(self):
        backend = SimpleNamespace(
            protocol=PROTOCOL_TELEGRAM, ingest_message=MagicMock(return_value=True)
        )
        placeholder = ChatContact(
            id="42", display_name="42", protocol=PROTOCOL_TELEGRAM
        )
        app = _App(backend, contacts=[placeholder])

        app._handle_message_event(
            _message_event(PROTOCOL_TELEGRAM, "42", contact=placeholder, timestamp=4000)
        )

        assert app.scheduled == [PROTOCOL_TELEGRAM]


# ─── §6.4 Scheduler ──────────────────────────────────────────────────────────


class _RecordingManager:
    def __init__(self, block: threading.Event | None = None):
        self.calls: list[tuple[set[str] | None, bool]] = []
        self.called = threading.Event()
        self._block = block

    def refresh_contacts_sync(self, protocols=None, force=True):
        if self._block is not None:
            self._block.wait(5)
        self.calls.append((protocols, force))
        self.called.set()
        return {}


class _Scheduler(DynamicAddressBookMixin):
    """DynamicAddressBookMixin with the state attributes normally in ``__init__``."""

    def __init__(self, manager):
        self.manager = manager
        self._address_book_refresh_stop = False
        self._address_book_refresh_wake = threading.Event()
        self._address_book_refresh_lock = threading.Lock()
        self._address_book_last_periodic = 0.0
        self._address_book_last_lazy = 0.0
        self._address_book_pending_protocols: set[str] = set()
        self._address_book_refresh_thread: threading.Thread | None = None


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture
def patch_refresh_config(monkeypatch):
    import tui.address_book_refresh as abr

    def _apply(interval: float, cooldown: float):
        monkeypatch.setattr(abr, "get_dynamic_refresh_interval_s", lambda: interval)
        monkeypatch.setattr(abr, "get_dynamic_refresh_cooldown_s", lambda: cooldown)

    return _apply


class TestScheduler:
    def test_start_is_idempotent(self, patch_refresh_config):
        patch_refresh_config(3600, 3600)
        sched = _Scheduler(_RecordingManager())
        sched.start_address_book_refresh()
        first = sched._address_book_refresh_thread
        try:
            sched.start_address_book_refresh()
            assert sched._address_book_refresh_thread is first
        finally:
            sched.stop_address_book_refresh()
            first.join(timeout=2)

    def test_lazy_cooldown_throttles_second_rapid_wake(self, patch_refresh_config):
        patch_refresh_config(interval=3600, cooldown=3600)
        mgr = _RecordingManager()
        sched = _Scheduler(mgr)
        sched.start_address_book_refresh()
        try:
            sched.schedule_address_book_refresh(PROTOCOL_SIGNAL)
            assert mgr.called.wait(2.0)
            assert _wait_until(lambda: len(mgr.calls) == 1)

            # Second wake within the cooldown → throttled, no second refresh.
            sched.schedule_address_book_refresh(PROTOCOL_WHATSAPP)
            time.sleep(0.25)
            assert len(mgr.calls) == 1
            assert mgr.calls[0][0] == {PROTOCOL_SIGNAL}
        finally:
            sched.stop_address_book_refresh()
            sched._address_book_refresh_thread.join(timeout=2)

    def test_periodic_cadence_independent_of_lazy_cooldown(self, patch_refresh_config):
        patch_refresh_config(interval=0.1, cooldown=3600)
        mgr = _RecordingManager()
        sched = _Scheduler(mgr)
        sched.start_address_book_refresh()
        try:
            # No lazy schedule is ever issued: only the periodic trigger runs.
            assert _wait_until(
                lambda: sum(1 for protocols, _ in mgr.calls if protocols is None) >= 2,
                timeout=2.0,
            )
            assert all(protocols is None for protocols, _ in mgr.calls)
            assert all(force is True for _, force in mgr.calls)
        finally:
            sched.stop_address_book_refresh()
            sched._address_book_refresh_thread.join(timeout=2)

    def test_stop_unblocks_without_join(self, patch_refresh_config):
        patch_refresh_config(interval=3600, cooldown=3600)
        entered = threading.Event()
        release = threading.Event()

        class _BlockingManager:
            def refresh_contacts_sync(self, protocols=None, force=True):
                entered.set()
                release.wait(5)
                return {}

        sched = _Scheduler(_BlockingManager())
        sched.start_address_book_refresh()
        try:
            sched.schedule_address_book_refresh(PROTOCOL_SIGNAL)
            assert entered.wait(2.0)

            # ``stop`` must return promptly even while the worker is blocked
            # inside a refresh (no join on the daemon thread).
            start = time.monotonic()
            sched.stop_address_book_refresh()
            assert time.monotonic() - start < 0.5

            release.set()
            sched._address_book_refresh_thread.join(timeout=2)
            assert not sched._address_book_refresh_thread.is_alive()
        finally:
            release.set()


# ─── §9.4 Integration ────────────────────────────────────────────────────────


@pytest.mark.integration
class TestDynamicAddressBookIntegration:
    def test_picker_worker_forces_address_book(self):
        class _PickerHarness(PickerMixin):
            def __init__(self, manager, token=1):
                self.whatsapp_backend = None
                self.manager = manager
                self._address_book_token = token
                self.statuses: list[str] = []

            def _status(self, message: str) -> None:
                self.statuses.append(message)

            def call_from_thread(self, fn):
                fn()

        contact = ChatContact(
            id="1@c.us", display_name="Mario", protocol=PROTOCOL_WHATSAPP
        )
        manager = MagicMock()
        manager.list_address_book_sync.return_value = [contact]
        manager.address_book_errors = {}
        screen = MagicMock()
        screen.is_mounted = True

        _PickerHarness(manager)._address_book_worker(1, {PROTOCOL_WHATSAPP}, screen)

        manager.list_address_book_sync.assert_called_once_with(
            protocols={PROTOCOL_WHATSAPP}, force=True
        )
        screen.set_contacts.assert_called_once_with([contact])

    def test_unknown_sender_then_contact_update_renames_row(self):
        backend = _wa_backend()
        backend._identify_contact = MagicMock(return_value=None)
        backend.register_contact = MagicMock(return_value=True)
        app = _App(backend, contacts=[])

        app._handle_message_event(_message_event(PROTOCOL_WHATSAPP, "1@c.us"))
        assert len(app.contacts) == 1
        placeholder = app.contacts[0]
        assert placeholder.display_name == "1@c.us"

        resolved = ChatContact(
            id="1@c.us", display_name="Mario", protocol=PROTOCOL_WHATSAPP
        )
        app._handle_contact_update_event(_contact_update_event(resolved))

        assert len(app.contacts) == 1
        assert app.contacts[0] is placeholder
        assert placeholder.display_name == "Mario"
