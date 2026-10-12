"""Regression tests for the WhatsApp ``@g.us`` known-groups resolver.

Bug: ``POST /api/send`` (``web/api.py``, ~1411) resolves the recipient with the
zero-network ``backend.find_contact``.  Groups that only appear *beyond* WAHA's
default first ``/chats`` page (e.g. ``120363409791162714@g.us``) were neither in
``self.contacts`` (only the first-page chats) nor in the transient
``_address_book`` cache (wiped by the LID resolver at
``whatsapp.py:1694/1712/2814`` and never rebuilt from ``find_contact``), so the
send 404ed even though the group was searchable.

Fix (``protocols/whatsapp.py``): a durable, never-wiped ``_known_groups``
registry populated by ``list_address_book_sync`` from the paginated ``/chats``
list and consulted by ``find_contact`` as a last zero-network fallback.  Lookup
order invariant: ``self.contacts`` → ``_identify_contact`` →
``find_address_book_contact`` → ``_known_groups``.

These tests pin the fix together with its prerequisite (paginated
``WhatsAppRESTClient.list_contacts(paginate=True)``).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI
from fastapi.testclient import TestClient

from models import PROTOCOL_WHATSAPP, ChatContact, contact_storage_keys
from protocols.whatsapp import WhatsAppBackend, WhatsAppRESTClient
from web.api import create_api_router

GROUP_JID = "120363409791162714@g.us"


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _wa_backend(*, book: list[ChatContact] | None = None) -> WhatsAppBackend:
    """Real ``WhatsAppBackend`` with a mocked REST client and no disk I/O."""
    backend = WhatsAppBackend(api_url="http://api.test", media_dir="")
    backend._rest = MagicMock()
    backend._address_book = None if book is None else list(book)
    backend._address_book_ts = time.monotonic()
    #: ``{}`` = "LID cache already loaded": no disk read, deterministic.
    backend._lid_map = {}
    backend.start_lid_resolver = MagicMock()
    return backend


def _group(jid: str = GROUP_JID, name: str = "Gruppo") -> ChatContact:
    return ChatContact(
        id=jid,
        display_name=name,
        protocol=PROTOCOL_WHATSAPP,
        extras={
            "jid": jid,
            "source": "wa_chats",
            "is_chat_active": True,
        },
    )


def _chat_payload(jid: str, name: str = "Chat") -> dict:
    return {"id": jid, "name": name, "pushname": None}


def _chat_contact(jid: str, name: str = "Chat") -> ChatContact:
    return ChatContact(
        id=jid,
        display_name=name,
        protocol=PROTOCOL_WHATSAPP,
        extras={"jid": jid, "source": "wa_chats", "is_chat_active": True},
    )


class _RecordingManager:
    """Minimal manager recording the send surface used by ``/api/send``."""

    def __init__(self, backend):
        self.backend = backend
        self.send_calls: list[tuple] = []
        self.attachments_calls: list[dict] = []
        self.list_contacts_calls = 0

    def get(self, protocol):
        if self.backend is not None and self.backend.protocol == protocol:
            return self.backend
        return None

    def list_contacts(self):
        self.list_contacts_calls += 1
        return list(self.backend.contacts) if self.backend else []

    def get_attachment_path(self, protocol, attachment_id):
        return None

    def send_message_sync(self, protocol, contact_id, text, **kwargs):
        self.send_calls.append((protocol, contact_id, text, kwargs))
        return "sent-id"

    def send_attachments_sync(self, protocol, contact_id, file_paths, **kwargs):
        self.attachments_calls.append(
            {"protocol": protocol, "contact_id": contact_id, "paths": file_paths}
        )
        return [f"id-{index}" for index in range(len(file_paths))]


def _app(manager) -> FastAPI:
    app = FastAPI()
    app.state.manager = manager
    app.include_router(create_api_router())
    return app


def _fake_urlopen(
    page_for: Callable[[int], list[dict]],
    seen: list[str] | None = None,
    status: int = 200,
):
    """Build a ``urllib.request.urlopen`` fake that pages by ``offset``."""

    def fake(req, timeout=30):
        if seen is not None:
            seen.append(req.full_url)
        offset = int(parse_qs(urlparse(req.full_url).query).get("offset", ["0"])[0])
        body = json.dumps(page_for(offset)).encode("utf-8")
        resp = MagicMock()
        resp.status = status
        resp.read.return_value = body
        ctx = MagicMock()
        ctx.__enter__.return_value = resp
        return ctx

    return fake


# ─── Prerequisite: paginated list_contacts ───────────────────────────────────


class TestPaginatedListContacts:
    def test_paginate_false_is_single_unpaginated_request(self):
        client = WhatsAppRESTClient("http://api.test")
        seen: list[str] = []
        page = [{"id": "1@c.us", "name": "A"}]

        with patch("urllib.request.urlopen", _fake_urlopen(lambda _o: page, seen)):
            out = client.list_contacts()

        assert out == [
            {
                "id": "1@c.us",
                "name": "A",
                "isGroup": False,
                "last_ts": 0,
                "unread": 0,
            }
        ]
        assert len(seen) == 1
        assert "offset" not in seen[0]

    def test_paginate_walks_pages_and_includes_beyond_page(self):
        client = WhatsAppRESTClient("http://api.test")
        seen: list[str] = []

        def page_for(offset: int) -> list[dict]:
            return {
                0: [_chat_payload("1@c.us", "A"), _chat_payload("2@c.us", "B")],
                2: [_chat_payload(GROUP_JID, "Gruppo")],
            }.get(offset, [])

        with patch("urllib.request.urlopen", _fake_urlopen(page_for, seen)):
            out = client.list_contacts(paginate=True, limit=2)

        ids = [c["id"] for c in out]
        assert ids == ["1@c.us", "2@c.us", GROUP_JID]
        assert any("limit=2&offset=2" in url for url in seen)

    def test_paginate_stops_when_offset_is_ignored(self):
        """WAHA ignoring ``offset`` returns the same page forever: must stop."""
        client = WhatsAppRESTClient("http://api.test")
        seen: list[str] = []
        same = [_chat_payload("1@c.us"), _chat_payload("2@c.us")]

        with patch("urllib.request.urlopen", _fake_urlopen(lambda _o: same, seen)):
            out = client.list_contacts(paginate=True, limit=2)

        assert len(out) == 2
        assert len(seen) == 2  # offset 0 then offset 2, then dedup break

    def test_paginate_empty_page_stops(self):
        client = WhatsAppRESTClient("http://api.test")
        with patch("urllib.request.urlopen", _fake_urlopen(lambda _o: [])):
            assert client.list_contacts(paginate=True, limit=2) == []

    def test_paginate_transport_error_on_first_page_returns_none(self):
        client = WhatsAppRESTClient("http://api.test")
        with patch.object(client, "_request", return_value=None):
            assert client.list_contacts(paginate=True) is None

    def test_paginate_transport_error_after_first_page_returns_collected(self):
        client = WhatsAppRESTClient("http://api.test")
        first = [_chat_payload("1@c.us"), _chat_payload("2@c.us")]
        with patch.object(client, "_request", side_effect=[first, None]):
            out = client.list_contacts(paginate=True, limit=2)

        assert [c["id"] for c in out] == ["1@c.us", "2@c.us"]


# ─── Population of _known_groups ─────────────────────────────────────────────


class TestKnownGroupsPopulation:
    def test_known_groups_populated_by_paginated_chats(self):
        backend = _wa_backend()
        first_page = [_chat_payload(f"3933300{i:04d}@c.us") for i in range(20)]
        backend.contacts = [_chat_contact(c["id"]) for c in first_page]
        backend._rest.list_all_contacts.return_value = []
        # ``list_contacts(paginate=True)`` returns the whole session: first page
        # plus the group that WAHA would hide behind offset=20.
        backend._rest.list_contacts.return_value = first_page + [
            _chat_payload(GROUP_JID, "Gruppo Beyond")
        ]

        result = backend.list_address_book_sync()

        assert GROUP_JID in backend._known_groups
        assert backend._known_groups[GROUP_JID].id == GROUP_JID
        # The group is also exposed by the book (searchable) …
        assert GROUP_JID in [c.id for c in result]
        # … while ``self.contacts`` keeps only the first page (no pollution).
        assert len(backend.contacts) == 20
        assert GROUP_JID not in [c.id for c in backend.contacts]

    def test_known_groups_captures_first_page_group_too(self):
        """Population reads ``chats`` = ``self.contacts`` ∪ paginated extras."""
        backend = _wa_backend()
        backend.contacts = [_group("111111111111111111@g.us", "Prima pagina")]
        backend._rest.list_all_contacts.return_value = []
        backend._rest.list_contacts.return_value = [
            _chat_payload("111111111111111111@g.us", "Prima pagina")
        ]

        backend.list_address_book_sync()

        assert set(backend._known_groups) == {"111111111111111111@g.us"}

    def test_known_groups_only_contains_g_us(self):
        backend = _wa_backend()
        backend._rest.list_all_contacts.return_value = []
        backend._rest.list_contacts.return_value = [
            _chat_payload("393331234567@c.us"),
            _chat_payload("220988985864200@lid"),
            _chat_payload("status@broadcast"),
            _chat_payload(GROUP_JID, "Gruppo"),
        ]

        backend.list_address_book_sync()

        assert set(backend._known_groups) == {GROUP_JID}

    def test_known_groups_not_populated_on_book_transport_error(self):
        """Residual limitation: a book transport error returns early.

        ``raw is None`` (timeout/HTTP error on ``/api/contacts``) short-circuits
        *before* the paginated ``/chats`` fetch, so ``_known_groups`` stays
        empty for that refresh.  Documented for the residual-risk section.
        """
        backend = _wa_backend()
        backend.contacts = []
        backend._rest.list_all_contacts.return_value = None
        backend._rest.list_contacts.return_value = [_chat_payload(GROUP_JID)]

        assert backend.list_address_book_sync() == []
        assert backend._known_groups == {}
        backend._rest.list_contacts.assert_not_called()

    def test_known_groups_rebuilt_on_connect(self):
        """``_load_contacts`` → ``list_address_book_sync`` populates the registry.

        NOTE N5: ``_load_contacts`` calls ``list_address_book_sync(force=False)``;
        with a warm ``_address_book`` inside TTL the early return would skip the
        population, so the test starts from a cold cache (``_address_book=None``).
        """
        backend = _wa_backend()
        first_page = [_chat_payload("1@c.us", "A"), _chat_payload("2@c.us", "B")]
        paginated = first_page + [_chat_payload(GROUP_JID, "Gruppo")]

        def fake_list_contacts(*args, paginate=False, limit=500):
            return paginated if paginate else first_page

        backend._rest.list_contacts.side_effect = fake_list_contacts
        backend._rest.list_all_contacts.return_value = []

        backend._load_contacts()

        assert backend._known_groups[GROUP_JID].id == GROUP_JID
        assert [c.id for c in backend.contacts] == ["1@c.us", "2@c.us"]

    def test_known_groups_not_polluting_contacts(self):
        """Resolving via the registry must not auto-register the contact."""
        backend = _wa_backend()
        backend._known_groups[GROUP_JID] = _group()

        found = backend.find_contact(GROUP_JID)

        assert found is backend._known_groups[GROUP_JID]
        assert backend.contacts == []
        assert backend._contacts_by_jid == {}

    def test_known_groups_monotonic_when_group_leaves_chats(self):
        """N1 residue: a group no longer in ``/chats`` stays resolvable.

        Accumulation is monotonic (idempotent, never pruned); this is the
        accepted residual of the fix, pinned here to avoid an accidental
        regression into non-resolvability.
        """
        backend = _wa_backend()
        backend.contacts = []
        backend._rest.list_all_contacts.return_value = []
        backend._rest.list_contacts.return_value = [_chat_payload(GROUP_JID)]

        backend.list_address_book_sync(force=True)
        assert GROUP_JID in backend._known_groups

        # Next refresh: the group disappeared from /chats.
        backend._rest.list_contacts.return_value = []
        backend.list_address_book_sync(force=True)

        assert GROUP_JID in backend._known_groups
        assert backend.find_contact(GROUP_JID) is not None


# ─── find_contact lookup semantics ───────────────────────────────────────────


class TestFindContactKnownGroups:
    def test_find_contact_resolves_group_when_book_is_none(self):
        backend = _wa_backend(book=None)
        group = _group()
        backend._known_groups[GROUP_JID] = group

        found = backend.find_contact(GROUP_JID)

        assert found is group
        assert found.id == GROUP_JID

    def test_known_groups_survives_address_book_invalidation(self):
        backend = _wa_backend()
        backend.contacts = []
        backend._rest.list_all_contacts.return_value = []
        backend._rest.list_contacts.return_value = [_chat_payload(GROUP_JID)]
        backend.list_address_book_sync(force=True)

        # Simulate _lid_resolver_run / _apply_contact_lid_resolution wiping the
        # transient address-book cache.
        backend._address_book = None
        backend._address_book_ts = 0.0

        found = backend.find_contact(GROUP_JID)
        assert found is not None
        assert found.id == GROUP_JID

    def test_find_contact_unknown_group_returns_none(self):
        backend = _wa_backend(book=None)
        assert backend.find_contact("999999999999999999@g.us") is None

    def test_find_contact_unknown_group_zero_network(self, monkeypatch):
        backend = _wa_backend(book=None)
        backend.list_address_book_sync = MagicMock()
        backend._rest.list_all_contacts = MagicMock()
        backend._rest.list_contacts = MagicMock()
        backend._lid_resolve_remote = MagicMock()
        to_thread = MagicMock()
        monkeypatch.setattr("web.api.asyncio.to_thread", to_thread)

        assert backend.find_contact("999999999999999999@g.us") is None

        backend.list_address_book_sync.assert_not_called()
        backend._rest.list_all_contacts.assert_not_called()
        backend._rest.list_contacts.assert_not_called()
        backend._lid_resolve_remote.assert_not_called()
        to_thread.assert_not_called()

    def test_find_contact_order_contacts_then_book_then_known_groups(self):
        active = _chat_contact(GROUP_JID, "Active")
        book = _group(GROUP_JID, "Book")
        backend = _wa_backend(book=[book])
        backend.contacts = [active]
        backend._contacts_by_jid = {active.id: active}
        backend._known_groups[GROUP_JID] = _group(GROUP_JID, "Known")

        # 1. active chat wins.
        assert backend.find_contact(GROUP_JID) is active

        # 2. address book wins over the known-groups fallback.
        backend.contacts = []
        backend._contacts_by_jid = {}
        assert backend.find_contact(GROUP_JID) is book

        # 3. known-groups is the last resort.
        backend._address_book = None
        assert backend.find_contact(GROUP_JID) is backend._known_groups[GROUP_JID]

    def test_group_storage_keys_is_never_expanded(self):
        assert contact_storage_keys(_group()) == [GROUP_JID]


# ─── End-to-end POST /api/send ───────────────────────────────────────────────


class TestWebSendKnownGroupsE2E:
    def test_send_group_from_known_groups_200_ghost(self):
        backend = _wa_backend(book=None)
        group = _group(GROUP_JID, "Gruppo WA")
        backend._known_groups[GROUP_JID] = group
        manager = _RecordingManager(backend)

        with patch("web.api.push_event") as pushed, TestClient(_app(manager)) as client:
            response = client.post(
                "/api/send",
                json={
                    "protocol": "whatsapp",
                    "contact_id": GROUP_JID,
                    "text": "Ciao gruppo",
                },
            )

        assert response.status_code == 200
        assert len(manager.send_calls) == 1
        assert manager.attachments_calls == []
        assert manager.send_calls[0][1] == GROUP_JID
        assert manager.send_calls[0][2] == "Ciao gruppo"
        # The book-only group is registered as a ghost before the send.
        assert backend.contacts == [group]
        assert group.extras["ghost"] is True
        assert pushed.call_args.args[0]["payload"]["contact_id"] == GROUP_JID

    def test_send_unknown_group_404_no_send(self):
        backend = _wa_backend(book=None)
        manager = _RecordingManager(backend)

        with patch("web.api.push_event"), TestClient(_app(manager)) as client:
            response = client.post(
                "/api/send",
                json={
                    "protocol": "whatsapp",
                    "contact_id": "999999999999999999@g.us",
                    "text": "Ciao",
                },
            )

        assert response.status_code == 404
        assert manager.send_calls == []
        assert backend.contacts == []
