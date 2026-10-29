"""``messages.window`` / ``messages.snapshot`` and the TUI chat loading on them.

The operations hand out the core's own cache rows (the TUI's message shape),
including rows that are not persisted yet, with the WhatsApp ``@c.us``/``@lid``
read-union applied; the TUI only reconciles them with its view cache.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from client_harness import client_for

from models import PROTOCOL_SIGNAL, PROTOCOL_WHATSAPP, ChatContact, contact_cache_key
from server.api import ApiError
from signal_tui import SignalTUI


def _row(
    mid: str | None,
    text: str = "hi",
    *,
    ts: int = 1000,
    mine: bool = False,
    status: str | None = None,
    attachment_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": mid,
        "text": text,
        "is_mine": mine,
        "timestamp": ts,
        "msg_type": "image" if attachment_id else "text",
        "attachment_id": attachment_id,
        "status": status,
        "read": mine,
    }


def _wa(contact_id: str = "39123@lid", **extras: Any) -> ChatContact:
    return ChatContact(
        id=contact_id,
        display_name="Mario",
        protocol=PROTOCOL_WHATSAPP,
        extras={"phone": "39123", **extras},
    )


def _backend(
    protocol: str,
    cache: dict[str, list[dict]],
    contacts: list[ChatContact] | None = None,
    *,
    fetch: Any = None,
) -> SimpleNamespace:
    contacts = contacts or []
    backend = SimpleNamespace(
        protocol=protocol,
        cache=cache,
        contacts=contacts,
        find_contact=lambda cid: next((c for c in contacts if c.id == cid), None),
    )
    if fetch is not None:
        backend.fetch_history = fetch
    return backend


def _client(backend: Any):
    return client_for(SimpleNamespace(get=lambda _p: backend))


def _window(client, contact: ChatContact, **extra: Any) -> dict:
    return client.call(
        "messages.window",
        {"protocol": contact.protocol, "contact_id": contact.id, **extra},
    )


class TestWindowOperation:
    def test_returns_core_cache_rows_in_tui_shape(self):
        contact = ChatContact(id="+1", display_name="A", protocol=PROTOCOL_SIGNAL)
        rows = [_row("m1", mine=True, status="read"), _row("m2", ts=2000)]
        client = _client(_backend(PROTOCOL_SIGNAL, {"+1": rows}, [contact]))

        out = _window(client, contact)

        assert out == {"messages": rows, "refreshed": False}

    def test_rows_that_are_not_persisted_are_included(self):
        # An optimistic send is ingested with persist=False: it is only in the
        # core's cache (no DB involved at all here).
        contact = ChatContact(id="+1", display_name="A", protocol=PROTOCOL_SIGNAL)
        pending = _row(None, "sending…", mine=True, status="pending", ts=3000)
        client = _client(_backend(PROTOCOL_SIGNAL, {"+1": [pending]}, [contact]))

        (row,) = _window(client, contact)["messages"]

        assert row["id"] is None and row["status"] == "pending"

    def test_rows_are_copies(self):
        contact = ChatContact(id="+1", display_name="A", protocol=PROTOCOL_SIGNAL)
        cache = {"+1": [_row("m1", status="sent")]}
        client = _client(_backend(PROTOCOL_SIGNAL, cache, [contact]))

        _window(client, contact)["messages"][0]["status"] = "read"

        assert cache["+1"][0]["status"] == "sent"

    def test_whatsapp_read_union_collapses_the_ghost_twin(self):
        contact = _wa()
        cache = {
            "39123@lid": [_row("M1", mine=True, attachment_id="http://waha/a")],
            "39123@c.us": [
                _row("M1", mine=True, attachment_id="sent-a.png"),
                _row("M2", "only on the alias", ts=2000),
            ],
        }
        client = _client(_backend(PROTOCOL_WHATSAPP, cache, [contact]))

        rows = _window(client, contact)["messages"]

        assert [r["id"] for r in rows] == ["M1", "M2"]
        assert rows[0]["attachment_id"] == "http://waha/a"  # canonical key wins

    def test_ghost_id_opens_the_canonical_chat(self):
        contact = _wa()
        fetched: list[tuple] = []
        cache = {"39123@lid": [_row("M1")], "39123@c.us": [_row("M2", ts=2000)]}
        backend = _backend(
            PROTOCOL_WHATSAPP,
            cache,
            [contact],
            fetch=lambda cid, limit, **_kw: fetched.append((cid, limit)) or [],
        )
        backend.find_contact = lambda cid: contact if cid.startswith("39123") else None
        client = _client(backend)

        out = client.call(
            "messages.window",
            {
                "protocol": "whatsapp",
                "contact_id": "39123@c.us",
                "refresh": True,
                "refresh_limit": 50,
            },
        )

        assert fetched == [("39123@lid", 50)]
        assert [r["id"] for r in out["messages"]] == ["M1", "M2"]
        assert out["refreshed"] is True

    def test_refresh_sees_the_rows_the_download_adds(self):
        contact = _wa()
        cache: dict[str, list[dict]] = {"39123@lid": []}

        def fetch(cid, limit, **_kw):
            cache["39123@lid"].append(_row("M1"))
            return cache["39123@lid"]

        client = _client(_backend(PROTOCOL_WHATSAPP, cache, [contact], fetch=fetch))

        out = _window(client, contact, refresh=True)

        assert [r["id"] for r in out["messages"]] == ["M1"]
        assert out["refreshed"] is True

    def test_failed_download_is_not_reported_as_refreshed(self):
        contact = _wa()

        def boom(cid, limit, **_kw):
            raise RuntimeError("waha down")

        client = _client(
            _backend(
                PROTOCOL_WHATSAPP, {"39123@lid": [_row("M1")]}, [contact], fetch=boom
            )
        )

        out = _window(client, contact, refresh=True)

        assert out["refreshed"] is False
        assert [r["id"] for r in out["messages"]] == ["M1"]  # local cache stays

    def test_backend_without_history_download_is_not_refreshed(self):
        contact = _wa()
        client = _client(_backend(PROTOCOL_WHATSAPP, {}, [contact]))

        assert _window(client, contact, refresh=True) == {
            "messages": [],
            "refreshed": False,
        }

    def test_unknown_backend_is_empty(self):
        contact = _wa()
        client = _client(None)

        assert _window(client, contact, refresh=True) == {
            "messages": [],
            "refreshed": False,
        }

    def test_invalid_protocol_is_refused(self):
        with pytest.raises(ApiError) as err:
            _client(None).call(
                "messages.window", {"protocol": "icq", "contact_id": "x"}
            )
        assert err.value.code == "invalid_request"


class TestSnapshotOperation:
    def test_non_whatsapp_keeps_every_key(self):
        cache = {"+1": [_row("a")], "+2": [_row("b")]}
        client = _client(_backend(PROTOCOL_SIGNAL, cache))

        assert client.call("messages.snapshot", {"protocol": "signal"}) == cache

    def test_whatsapp_union_appears_once_under_the_canonical_id(self):
        contact = _wa()
        cache = {
            "39123@lid": [_row("M1", attachment_id="a.png")],
            "39123@c.us": [_row("M1", attachment_id="b.png"), _row("M2", ts=2000)],
            "999@g.us": [_row("G1")],  # no contact owns it: kept as is
        }
        client = _client(_backend(PROTOCOL_WHATSAPP, cache, [contact]))

        out = client.call("messages.snapshot", {"protocol": "whatsapp"})

        assert set(out) == {"39123@lid", "999@g.us"}
        assert [r["id"] for r in out["39123@lid"]] == ["M1", "M2"]
        assert out["39123@lid"][0]["attachment_id"] == "a.png"

    def test_rows_are_copies(self):
        cache = {"+1": [_row("a", status="sent")]}
        client = _client(_backend(PROTOCOL_SIGNAL, cache))

        client.call("messages.snapshot", {"protocol": "signal"})["+1"][0]["status"] = (
            "x"
        )

        assert cache["+1"][0]["status"] == "sent"

    def test_unknown_backend_is_empty(self):
        assert _client(None).call("messages.snapshot", {"protocol": "signal"}) == {}


# ─── TUI flows over the client ────────────────────────────────────────────────


def _app(backend: Any) -> SignalTUI:
    app = SignalTUI()
    app.core.manager = SimpleNamespace(get=lambda _p: backend)
    app._render_contact_list = MagicMock()
    app._update_unread_badges = MagicMock()
    app._status = MagicMock()
    app._sync_last_ts = MagicMock()
    app._sort_contacts = MagicMock()
    app._select_contact = MagicMock()
    app.contacts = []
    app._pending_backends = set()
    app.selected_contact = None
    return app


class TestBackendReadyThroughClient:
    def test_ghost_and_lid_history_lands_once_under_the_canonical_key(self):
        contact = _wa()
        cache = {
            "39123@lid": [_row("M1", attachment_id="a.png", mine=True)],
            "39123@c.us": [_row("M1", attachment_id="b.png", mine=True)],
        }
        app = _app(_backend(PROTOCOL_WHATSAPP, cache, [contact]))

        app._on_backend_ready(PROTOCOL_WHATSAPP)

        assert len(app._cache[contact_cache_key(PROTOCOL_WHATSAPP, "39123@lid")]) == 1
        assert contact_cache_key(PROTOCOL_WHATSAPP, "39123@c.us") not in app._cache

    def test_second_ready_adds_no_duplicate_bubbles(self):
        contact = ChatContact(id="+1", display_name="A", protocol=PROTOCOL_SIGNAL)
        cache = {"+1": [_row("m1"), _row(None, "no id", ts=2000, mine=True)]}
        app = _app(_backend(PROTOCOL_SIGNAL, cache, [contact]))

        app._on_backend_ready(PROTOCOL_SIGNAL)
        app._on_backend_ready(PROTOCOL_SIGNAL)

        assert len(app._cache[contact_cache_key(PROTOCOL_SIGNAL, "+1")]) == 2

    def test_status_is_never_downgraded_by_a_re_ready(self):
        contact = ChatContact(id="+1", display_name="A", protocol=PROTOCOL_SIGNAL)
        cache = {"+1": [_row("m1", mine=True, status="sent")]}
        app = _app(_backend(PROTOCOL_SIGNAL, cache, [contact]))
        key = contact_cache_key(PROTOCOL_SIGNAL, "+1")
        app._cache[key] = [_row("m1", mine=True, status="read")]

        app._on_backend_ready(PROTOCOL_SIGNAL)
        assert app._cache[key][0]["status"] == "read"

        cache["+1"][0]["status"] = "delivered"
        app._cache[key][0]["status"] = "sent"
        app._on_backend_ready(PROTOCOL_SIGNAL)
        assert app._cache[key][0]["status"] == "delivered"  # upgraded, not lowered
        assert len(app._cache[key]) == 1

    def test_optimistic_row_learns_its_id_instead_of_duplicating(self):
        contact = ChatContact(id="+1", display_name="A", protocol=PROTOCOL_SIGNAL)
        key = contact_cache_key(PROTOCOL_SIGNAL, "+1")
        cache = {"+1": [_row("echo-id", "ciao", mine=True, status="sent")]}
        app = _app(_backend(PROTOCOL_SIGNAL, cache, [contact]))
        app._cache[key] = [_row(None, "ciao", mine=True, status="pending")]

        app._on_backend_ready(PROTOCOL_SIGNAL)

        (entry,) = app._cache[key]
        assert entry["id"] == "echo-id" and entry["status"] == "sent"

    def test_unpersisted_core_rows_are_mirrored(self):
        contact = ChatContact(id="+1", display_name="A", protocol=PROTOCOL_SIGNAL)
        cache = {"+1": [_row(None, "queued", mine=True, status="pending", ts=5)]}
        app = _app(_backend(PROTOCOL_SIGNAL, cache, [contact]))

        app._on_backend_ready(PROTOCOL_SIGNAL)

        (entry,) = app._cache[contact_cache_key(PROTOCOL_SIGNAL, "+1")]
        assert entry["status"] == "pending" and entry["text"] == "queued"

    def test_refused_snapshot_still_completes_the_ready(self):
        app = _app(None)  # the core knows no backend
        app._pending_backends = {PROTOCOL_SIGNAL}

        app._on_backend_ready(PROTOCOL_SIGNAL)

        assert app._cache == {} and app._pending_backends == set()


class TestLoadWorkerThroughClient:
    def _setup(self, backend: Any, contact: ChatContact) -> SignalTUI:
        app = SignalTUI()
        app.core.manager = SimpleNamespace(get=lambda _p: backend)
        app.contacts = [contact]
        app.selected_contact = contact
        app._chat_reload_token = 1
        app._cache = {}
        app._loaded_all = False
        app.call_from_thread = MagicMock(side_effect=lambda fn, *a, **k: fn(*a, **k))
        return app

    def test_worker_merges_the_downloaded_union_without_touching_the_backend(self):
        contact = _wa()
        cache: dict[str, list[dict]] = {"39123@lid": [], "39123@c.us": []}

        def fetch(cid, limit, **_kw):
            cache["39123@lid"].append(_row("M1", "one", ts=1))
            cache["39123@c.us"].append(_row("M1", "one", ts=1))  # twin
            cache["39123@c.us"].append(_row("M2", "two", ts=2))
            return []

        backend = _backend(PROTOCOL_WHATSAPP, cache, [contact], fetch=fetch)
        app = self._setup(backend, contact)

        with patch.object(app, "_render_chat_window", return_value=True) as render:
            app._load_messages_worker()

        rows = app._cache[contact.cache_key]
        assert [r["id"] for r in rows] == ["M1", "M2"]
        assert render.call_count == 2  # painted from cache, re-rendered after merge
        assert all(r["read"] for r in rows if not r["is_mine"])

    def test_failed_download_stays_on_the_local_cache(self):
        contact = _wa()

        def boom(cid, limit, **_kw):
            raise RuntimeError("waha down")

        backend = _backend(
            PROTOCOL_WHATSAPP, {"39123@lid": [_row("M1")]}, [contact], fetch=boom
        )
        app = self._setup(backend, contact)

        with (
            patch.object(app, "_render_chat_window", return_value=True) as render,
            patch("time.sleep") as sleep,
        ):
            app._load_messages_worker()

        assert app._cache == {}  # nothing mirrored from a failed refresh
        assert render.call_count == 1
        sleep.assert_not_called()  # a failure is not retried

    def test_empty_download_retries_three_times_then_gives_up(self):
        contact = _wa()
        calls: list[int] = []
        backend = _backend(
            PROTOCOL_WHATSAPP,
            {},
            [contact],
            fetch=lambda cid, limit, **_kw: calls.append(1) or [],
        )
        app = self._setup(backend, contact)

        with (
            patch.object(app, "_render_chat_window", return_value=False),
            patch("time.sleep") as sleep,
        ):
            app._load_messages_worker()

        assert len(calls) == 3
        assert [c.args for c in sleep.call_args_list] == [(0.8,), (0.8,)]


def test_chat_loading_modules_do_not_read_backends():
    """The TUI chat loading goes through operations only."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "tui"
    for name in ("chat_view.py", "backend_connect.py"):
        source = (root / name).read_text()
        assert not re.search(r"\.manager\b|backend\.cache|sqlite3", source), name
