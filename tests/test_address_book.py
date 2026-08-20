"""
Regression tests for the address book contract (Ctrl+S rubrica — milestone 1).

Covers:
- The base ``ChatBackend.list_address_book_sync`` default (marked contacts,
  never raises), its async wrapper and ``register_contact`` hook.
- The ``ChatContact.phone`` read-only property.
- The new config getters (env → config.json → default).
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from dump_address_book_fixtures import anonymize_payload

from backends import config
from backends.base import ChatBackend
from backends.manager import BackendManager
from backends.signal import SignalBackend
from backends.telegram import TelegramBackend
from backends.whatsapp import WhatsAppBackend, _dedup_book_contacts
from backends.whatsapp_rest import WhatsAppRESTClient
from models import (
    PROTOCOL_SIGNAL,
    PROTOCOL_TELEGRAM,
    PROTOCOL_WHATSAPP,
    ChatContact,
)


class _MinimalBackend(ChatBackend):
    """Concrete ChatBackend implementing every abstract method trivially."""

    protocol = "test"

    def __init__(self, contacts: list[ChatContact] | None = None):
        self.contacts: list[ChatContact] = list(contacts or [])

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def list_contacts(self) -> list[ChatContact]:
        return []

    async def send_message(self, *args, **kwargs) -> str:
        return ""

    async def mark_read(self, contact_id: str) -> None:
        pass

    async def receive(self):
        if False:
            yield


def _contact(contact_id: str = "+391234567890", name: str = "Mario") -> ChatContact:
    return ChatContact(id=contact_id, display_name=name, protocol="test")


# ─── ChatContact.phone ───────────────────────────────────────────────────────


class TestContactPhone:
    """📞 Property read-only ``phone`` su ChatContact."""

    def test_phone_from_extras(self):
        contact = ChatContact(
            id="+391234567890",
            display_name="Mario",
            protocol="test",
            extras={"phone": "391234567890"},
        )
        assert contact.phone == "391234567890"

    def test_phone_default_empty(self):
        assert _contact().phone == ""

    def test_phone_none_treated_as_empty(self):
        contact = _contact()
        contact.extras["phone"] = None
        assert contact.phone == ""


# ─── ChatBackend.list_address_book_sync default ──────────────────────────────


class TestListAddressBookSyncDefault:
    """📇 Default della rubrica: self.contacts marcati, mai eccezioni."""

    def test_returns_contacts_marked(self):
        c1 = _contact("+391234567890", "Mario")
        c2 = _contact("+391111111111", "Luigi")
        backend = _MinimalBackend([c1, c2])

        result = backend.list_address_book_sync()

        assert len(result) == 2
        assert [c.id for c in result] == [c1.id, c2.id]
        for contact in result:
            assert contact.extras["address_book"] is True

    def test_does_not_mutate_original_contacts(self):
        c1 = _contact("+391234567890", "Mario")
        backend = _MinimalBackend([c1])

        backend.list_address_book_sync()

        assert "address_book" not in backend.contacts[0].extras

    def test_empty_contacts_returns_empty(self):
        backend = _MinimalBackend()
        assert backend.list_address_book_sync() == []

    def test_force_accepted(self):
        c1 = _contact()
        backend = _MinimalBackend([c1])

        result = backend.list_address_book_sync(force=True)

        assert len(result) == 1
        assert result[0].extras["address_book"] is True

    def test_preserves_existing_extras(self):
        c1 = _contact()
        c1.extras["phone"] = "391234567890"
        backend = _MinimalBackend([c1])

        result = backend.list_address_book_sync()

        assert result[0].extras["phone"] == "391234567890"
        assert result[0].extras["address_book"] is True

    def test_async_wrapper_delegates(self):
        backend = _MinimalBackend([_contact("+391234567890", "Mario")])

        result = asyncio.run(backend.list_address_book())

        assert len(result) == 1
        assert result[0].extras["address_book"] is True


# ─── ChatBackend.register_contact hook ───────────────────────────────────────


class TestRegisterContact:
    """➕ Hook di registrazione contatti (open-or-create)."""

    def test_appends_new_contact(self):
        backend = _MinimalBackend()
        contact = _contact()

        backend.register_contact(contact)

        assert backend.contacts == [contact]

    def test_does_not_duplicate(self):
        contact = _contact()
        backend = _MinimalBackend([contact])

        backend.register_contact(contact)

        assert len(backend.contacts) == 1


# ─── Config getters ──────────────────────────────────────────────────────────


class TestAddressBookConfig:
    """⚙️ Getter config rubrica: env → config.json → default."""

    def test_address_book_ttl_default(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.delenv("ADDRESS_BOOK_TTL_S", raising=False)
        assert config.get_address_book_ttl_s() == 300

    def test_address_book_ttl_env(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.setenv("ADDRESS_BOOK_TTL_S", "60")
        assert config.get_address_book_ttl_s() == 60

    def test_address_book_ttl_env_invalid(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.setenv("ADDRESS_BOOK_TTL_S", "abc")
        assert config.get_address_book_ttl_s() == 300

    def test_address_book_ttl_config(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.delenv("ADDRESS_BOOK_TTL_S", raising=False)
        (tmp_path / "config.json").write_text(json.dumps({"address_book_ttl_s": 120}))
        assert config.get_address_book_ttl_s() == 120

    def test_wa_lid_cache_ttl_default(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.delenv("WA_LID_CACHE_TTL_DAYS", raising=False)
        assert config.get_wa_lid_cache_ttl_days() == 30

    def test_wa_lid_cache_ttl_env(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.setenv("WA_LID_CACHE_TTL_DAYS", "7")
        assert config.get_wa_lid_cache_ttl_days() == 7

    def test_wa_lid_cache_ttl_env_invalid(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.setenv("WA_LID_CACHE_TTL_DAYS", "xyz")
        assert config.get_wa_lid_cache_ttl_days() == 30

    def test_wa_lid_cache_ttl_config(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.delenv("WA_LID_CACHE_TTL_DAYS", raising=False)
        (tmp_path / "config.json").write_text(json.dumps({"wa_lid_cache_ttl_days": 15}))
        assert config.get_wa_lid_cache_ttl_days() == 15

    def test_picker_max_results_default(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.delenv("PICKER_MAX_RESULTS", raising=False)
        assert config.get_picker_max_results() == 50

    def test_picker_max_results_env(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.setenv("PICKER_MAX_RESULTS", "100")
        assert config.get_picker_max_results() == 100

    def test_picker_max_results_env_invalid(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.setenv("PICKER_MAX_RESULTS", "many")
        assert config.get_picker_max_results() == 50

    def test_picker_max_results_config(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.delenv("PICKER_MAX_RESULTS", raising=False)
        (tmp_path / "config.json").write_text(json.dumps({"picker_max_results": 25}))
        assert config.get_picker_max_results() == 25

    def test_picker_preferred_backend_default(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.delenv("PICKER_PREFERRED_BACKEND", raising=False)
        assert config.get_picker_preferred_backend() == ""

    def test_picker_preferred_backend_env(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.setenv("PICKER_PREFERRED_BACKEND", "signal")
        assert config.get_picker_preferred_backend() == "signal"

    def test_picker_preferred_backend_config(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
        monkeypatch.delenv("PICKER_PREFERRED_BACKEND", raising=False)
        (tmp_path / "config.json").write_text(
            json.dumps({"picker_preferred_backend": "whatsapp"})
        )
        assert config.get_picker_preferred_backend() == "whatsapp"


# ─── WhatsApp REST + rubrica (milestone 2) ────────────────────────────────────


def _json_response(payload):
    """Return a mock urlopen context manager that yields a JSON body."""
    data = json.dumps(payload).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = data
    mock_resp.status = 200
    mock_urlopen = MagicMock()
    mock_urlopen.return_value.__enter__.return_value = mock_resp
    return mock_urlopen


def _boom(*args, **kwargs):
    import urllib.error

    raise urllib.error.URLError("refused")


def _wa_backend() -> WhatsAppBackend:
    backend = WhatsAppBackend(api_url="http://api.test", media_dir="")
    backend._rest = MagicMock()
    return backend


def _chat(jid: str, name: str | None = None, ts: int = 0) -> ChatContact:
    return ChatContact(
        id=jid,
        display_name=name or jid,
        protocol=PROTOCOL_WHATSAPP,
        extras={"jid": jid, "last_message_ts": ts},
    )


class TestWADedup:
    """🔀 Dedup 2x della rubrica WhatsApp (``_dedup_book_contacts``)."""

    def test_duplicates_collapse_to_one(self):
        raw = [
            {"id": "393331234567@c.us", "name": "Mario", "pushname": None},
            {"id": "393331234567@c.us", "name": "Mario", "pushname": None},
        ]
        assert _dedup_book_contacts(raw) == [
            {"phone": "393331234567", "name": "Mario", "pushname": None}
        ]

    def test_name_beats_pushname(self):
        raw = [
            {"id": "393331234567@c.us", "name": None, "pushname": "MarioP"},
            {"id": "393331234567@c.us", "name": "Mario Rossi", "pushname": None},
        ]
        assert _dedup_book_contacts(raw) == [
            {"phone": "393331234567", "name": "Mario Rossi", "pushname": None}
        ]

    def test_c_us_tiebreak(self):
        raw = [
            {"id": "393331234567@s.whatsapp.net", "name": "OldName", "pushname": None},
            {"id": "393331234567@c.us", "name": "NewName", "pushname": None},
        ]
        assert _dedup_book_contacts(raw) == [
            {"phone": "393331234567", "name": "NewName", "pushname": None}
        ]

    def test_discards_groups_broadcast_and_no_digits(self):
        raw = [
            {"id": "123456789@g.us", "name": "Group", "pushname": None},
            {"id": "status@broadcast", "name": "Status", "pushname": None},
            {"id": "abc@newsletter", "name": "News", "pushname": None},
            {"id": "no-digits-here", "name": "NoDigits", "pushname": None},
            {"id": "393331234567@c.us", "name": "Mario", "pushname": None},
        ]
        assert _dedup_book_contacts(raw) == [
            {"phone": "393331234567", "name": "Mario", "pushname": None}
        ]

    def test_serialized_dict_id(self):
        raw = [
            {
                "id": {"_serialized": "393331234567@c.us"},
                "name": "Mario",
                "pushname": None,
            }
        ]
        assert _dedup_book_contacts(raw) == [
            {"phone": "393331234567", "name": "Mario", "pushname": None}
        ]


class TestWARestAddressBook:
    """🔌 Nuovi metodi REST del client WhatsApp."""

    def test_list_all_contacts_accepts_str_and_dict_id(self):
        client = WhatsAppRESTClient("http://api.test")
        payload = [
            {"id": "393331234567@c.us", "name": "Mario", "pushname": None},
            {"id": {"_serialized": "393331111111@c.us"}, "name": "Luigi"},
        ]
        with patch("urllib.request.urlopen", _json_response(payload)):
            assert client.list_all_contacts() == payload

    def test_list_all_contacts_unwraps_nested_data(self):
        client = WhatsAppRESTClient("http://api.test")
        with patch(
            "urllib.request.urlopen", _json_response({"data": [{"id": "1@c.us"}]})
        ):
            assert client.list_all_contacts() == [{"id": "1@c.us"}]

    def test_list_all_contacts_error_returns_none(self):
        client = WhatsAppRESTClient("http://api.test")
        with patch("urllib.request.urlopen", _boom):
            assert client.list_all_contacts() is None

    def test_resolve_contact_percent_encodes_path(self):
        client = WhatsAppRESTClient("http://api.test")
        seen = []

        def fake_urlopen(req, timeout=30):
            seen.append(req.full_url)
            resp = MagicMock()
            resp.read.return_value = json.dumps({"id": "393331234567@c.us"}).encode()
            resp.status = 200
            ctx = MagicMock()
            ctx.__enter__.return_value = resp
            return ctx

        with patch("urllib.request.urlopen", fake_urlopen):
            result = client.resolve_contact("220988985864200@lid")

        assert result == {"id": "393331234567@c.us"}
        assert "%40" in seen[0]
        assert "@" not in seen[0]

    def test_resolve_contact_error_returns_none(self):
        client = WhatsAppRESTClient("http://api.test")
        with patch("urllib.request.urlopen", _boom):
            assert client.resolve_contact("220988985864200@lid") is None

    def test_check_number_exists_exists(self):
        client = WhatsAppRESTClient("http://api.test")
        with patch("urllib.request.urlopen", _json_response({"exists": True})):
            assert client.check_number_exists("393331234567") is True

    def test_check_number_exists_number_exists(self):
        client = WhatsAppRESTClient("http://api.test")
        with patch("urllib.request.urlopen", _json_response({"numberExists": False})):
            assert client.check_number_exists("393331234567") is False

    def test_check_number_exists_missing_key_returns_none(self):
        client = WhatsAppRESTClient("http://api.test")
        with patch("urllib.request.urlopen", _json_response({"foo": 1})):
            assert client.check_number_exists("393331234567") is None

    def test_check_number_exists_error_returns_none(self):
        client = WhatsAppRESTClient("http://api.test")
        with patch("urllib.request.urlopen", _boom):
            assert client.check_number_exists("393331234567") is None


class TestWALidCache:
    """💾 Cache persistente ``@lid`` → numero."""

    def test_roundtrip_on_tmp_path(self, monkeypatch, tmp_path):
        import backend as backend_mod

        monkeypatch.setattr(backend_mod, "CACHE_DIR", tmp_path)
        now = int(time.time())
        b1 = _wa_backend()
        b1._lid_cache_load()
        with b1._lid_lock:
            b1._lid_map["220988985864200@lid"] = {
                "phone": "393331234567",
                "name": "Mario",
                "resolved_at": now,
            }
        b1._lid_cache_save()

        b2 = _wa_backend()
        assert b2._lid_lookup("220988985864200@lid") == "393331234567"

    def test_positive_ttl(self, monkeypatch, tmp_path):
        import backend as backend_mod

        monkeypatch.setattr(backend_mod, "CACHE_DIR", tmp_path)
        now = int(time.time())
        backend = _wa_backend()
        backend._lid_cache_load()
        with backend._lid_lock:
            backend._lid_map["fresh@lid"] = {
                "phone": "391234567890",
                "resolved_at": now,
            }
            backend._lid_map["stale@lid"] = {
                "phone": "391234567890",
                "resolved_at": now - 31 * 86400,
            }
        assert backend._lid_lookup("fresh@lid") == "391234567890"
        assert backend._lid_lookup("stale@lid") is None

    def test_negative_ttl_24h(self, monkeypatch, tmp_path):
        import backend as backend_mod

        monkeypatch.setattr(backend_mod, "CACHE_DIR", tmp_path)
        now = int(time.time())
        backend = _wa_backend()
        backend._lid_cache_load()
        with backend._lid_lock:
            backend._lid_map["neg_fresh@lid"] = {"phone": None, "resolved_at": now}
            backend._lid_map["neg_stale@lid"] = {
                "phone": None,
                "resolved_at": now - 25 * 3600,
            }
        assert backend._lid_cached("neg_fresh@lid") is True
        assert backend._lid_cached("neg_stale@lid") is False

    def test_corrupt_file_starts_empty(self, monkeypatch, tmp_path):
        import backend as backend_mod

        monkeypatch.setattr(backend_mod, "CACHE_DIR", tmp_path)
        (tmp_path / "wa_lid_map.json").write_text("{not valid json")
        backend = _wa_backend()
        backend._lid_cache_load()
        assert backend._lid_map == {}
        assert backend._lid_lookup("x@lid") is None

    def test_lookup_is_memory_only_no_network(self):
        backend = _wa_backend()
        backend._lid_cache_load()
        with backend._lid_lock:
            backend._lid_map["1@lid"] = {
                "phone": "391234567890",
                "resolved_at": int(time.time()),
            }
        assert backend._lid_lookup("1@lid") == "391234567890"
        backend._rest.resolve_contact.assert_not_called()


class TestWAMerge:
    """🧩 Merge rubrica ∪ chat attive in ``list_address_book_sync``."""

    def test_c_us_in_book_merges(self):
        backend = _wa_backend()
        backend.start_lid_resolver = MagicMock()
        backend._rest.list_all_contacts.return_value = [
            {"id": "393331234567@c.us", "name": "Mario Rossi", "pushname": None}
        ]
        backend.contacts = [_chat("393331234567@c.us", ts=12345)]

        result = backend.list_address_book_sync()

        assert len(result) == 1
        contact = result[0]
        assert contact.id == "393331234567@c.us"  # id = chat (continuity)
        assert contact.extras["phone"] == "393331234567"
        assert contact.extras["is_chat_active"] is True
        assert contact.extras["address_book"] is True
        assert contact.extras["source"] == "wa_book"
        assert contact.last_message_ts == 12345
        assert contact.display_name == "Mario Rossi"

    def test_c_us_not_in_book_extra(self):
        backend = _wa_backend()
        backend.start_lid_resolver = MagicMock()
        backend._rest.list_all_contacts.return_value = []
        backend.contacts = [_chat("393331111111@c.us", "Luigi", ts=1)]

        result = backend.list_address_book_sync()

        assert len(result) == 1
        contact = result[0]
        assert contact.id == "393331111111@c.us"
        assert contact.extras["phone"] == "393331111111"
        assert contact.extras["is_chat_active"] is True
        assert contact.extras["source"] == "wa_chats"

    def test_lid_resolved_from_cache_merges(self):
        backend = _wa_backend()
        backend.start_lid_resolver = MagicMock()
        backend._lid_cache_load()
        with backend._lid_lock:
            backend._lid_map["220988985864200@lid"] = {
                "phone": "393331234567",
                "resolved_at": int(time.time()),
            }
        backend._rest.list_all_contacts.return_value = [
            {"id": "393331234567@c.us", "name": "Mario Rossi", "pushname": None}
        ]
        backend.contacts = [_chat("220988985864200@lid", "Mario", ts=999)]

        result = backend.list_address_book_sync()

        assert len(result) == 1
        contact = result[0]
        assert contact.id == "220988985864200@lid"
        assert contact.extras["phone"] == "393331234567"
        assert contact.extras["lid"] == "220988985864200@lid"
        assert contact.extras["is_chat_active"] is True
        assert contact.display_name == "Mario Rossi"

    def test_lid_unresolved_standalone_no_network(self, monkeypatch, tmp_path):
        import backend as backend_mod

        # Isola la cache @lid reale (wa_lid_map.json): senza questa patch il
        # test leggerebbe il file della macchina su cui gira (su alcune
        # macchine "220988985864200@lid" è risolto → ramo diverso → KeyError).
        monkeypatch.setattr(backend_mod, "CACHE_DIR", tmp_path)
        backend = _wa_backend()
        backend.start_lid_resolver = MagicMock()
        backend._rest.list_all_contacts.return_value = []
        backend.contacts = [_chat("220988985864200@lid", ts=5)]

        result = backend.list_address_book_sync()

        assert len(result) == 1
        contact = result[0]
        assert contact.id == "220988985864200@lid"
        assert contact.extras["lid_unresolved"] is True
        assert contact.extras["is_chat_active"] is True
        # Zero rete per i lid al load: solo cache, mai resolve_contact.
        backend._rest.resolve_contact.assert_not_called()

    def test_groups_included_marked(self):
        backend = _wa_backend()
        backend.start_lid_resolver = MagicMock()
        backend._rest.list_all_contacts.return_value = [
            {"id": "393331234567@c.us", "name": "Mario", "pushname": None}
        ]
        backend.contacts = [
            _chat("123456789@g.us", "Gruppo", ts=7),
            _chat("393331234567@c.us", ts=1),
        ]

        result = backend.list_address_book_sync()

        ids = [c.id for c in result]
        assert "123456789@g.us" in ids
        group = next(c for c in result if c.id == "123456789@g.us")
        assert group.extras["is_chat_active"] is True
        assert group.extras["source"] == "wa_chats"
        assert group.extras["address_book"] is True

    def test_display_name_falls_back_to_chat_when_book_empty(self):
        backend = _wa_backend()
        backend.start_lid_resolver = MagicMock()
        backend._rest.list_all_contacts.return_value = [
            {"id": "393331234567@c.us", "name": None, "pushname": None}
        ]
        backend.contacts = [_chat("393331234567@c.us", "Luigi", ts=1)]

        result = backend.list_address_book_sync()

        assert len(result) == 1
        assert result[0].display_name == "Luigi"


class TestWAAddressBookCache:
    """⏱️ TTL della rubrica + errori (mai eccezioni)."""

    def test_ttl_cache_and_force(self):
        backend = _wa_backend()
        backend.start_lid_resolver = MagicMock()
        backend._rest.list_all_contacts.return_value = []
        backend.contacts = []

        assert backend.list_address_book_sync() == []
        assert backend.list_address_book_sync() == []
        assert backend._rest.list_all_contacts.call_count == 1  # cached
        assert backend.list_address_book_sync(force=True) == []
        assert backend._rest.list_all_contacts.call_count == 2

    def test_error_returns_stale_cache(self):
        backend = _wa_backend()
        backend.start_lid_resolver = MagicMock()
        backend._rest.list_all_contacts.return_value = [
            {"id": "393331234567@c.us", "name": "Mario", "pushname": None}
        ]
        backend.contacts = []

        first = backend.list_address_book_sync()
        assert len(first) == 1
        backend._rest.list_all_contacts.side_effect = Exception("boom")
        assert backend.list_address_book_sync(force=True) == first

    def test_error_returns_empty(self):
        backend = _wa_backend()
        backend.start_lid_resolver = MagicMock()
        backend._rest.list_all_contacts.side_effect = Exception("boom")
        backend.contacts = []

        assert backend.list_address_book_sync() == []


class TestWALidResolver:
    """🧵 Resolver @lid in background (idempotente, batch ≤30, save finale)."""

    def test_start_idempotent(self):
        backend = _wa_backend()
        with patch("threading.Thread") as mock_thread:
            backend.start_lid_resolver()
            backend.start_lid_resolver()
            assert mock_thread.call_count == 1
        assert backend._lid_resolver_started is True

    def test_batch_max_30_and_save_once(self, monkeypatch, tmp_path):
        import backend as backend_mod

        monkeypatch.setattr(backend_mod, "CACHE_DIR", tmp_path)
        backend = _wa_backend()
        backend.contacts = [_chat(f"{i}@lid", ts=i) for i in range(35)]
        backend._rest.resolve_contact.return_value = {
            "id": "391234567890@c.us",
            "name": "X",
        }
        backend._address_book = ["stale"]

        with (
            patch("time.sleep"),
            patch.object(backend, "_lid_cache_save") as mock_save,
        ):
            backend._lid_resolver_run()

        assert backend._rest.resolve_contact.call_count == 30
        mock_save.assert_called_once()
        assert backend._address_book is None


# ─── Telegram rubrica (milestone 3) ───────────────────────────────────────────


def _tg_user(**overrides) -> SimpleNamespace:
    """Mock Telethon ``User`` (SimpleNamespace) with address-book fields."""
    fields = {
        "id": 42,
        "first_name": "Ada",
        "last_name": "Lovelace",
        "username": "ada",
        "phone": "+391234567890",
        "access_hash": 123456789,
        "bot": False,
        "deleted": False,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _tg_dialog_contact(
    eid, name, ts=0, read_max=None, group=False, channel=False
) -> ChatContact:
    """Build a dialog ``ChatContact`` shaped like ``_load_contacts`` output."""
    extras: dict = {"last_message_ts": ts}
    if read_max:
        extras["read_outbox_max_id"] = read_max
    if group:
        extras["is_group"] = True
    elif channel:
        extras["is_channel"] = True
    else:
        extras["is_group"] = False
    return ChatContact(
        id=str(eid),
        display_name=name,
        protocol=PROTOCOL_TELEGRAM,
        extras=extras,
    )


def _tg_backend_with_book(monkeypatch, users, dialogs=None) -> TelegramBackend:
    """TelegramBackend connected to a fake client returning *users* from RPC."""
    backend = TelegramBackend()
    backend._api_id = 123
    backend._api_hash = "hash"
    backend._connected = True
    backend._loop = MagicMock()
    backend._client = AsyncMock(return_value=SimpleNamespace(users=users))
    backend.contacts = list(dialogs or [])
    backend._contacts_by_id = {}
    for c in backend.contacts:
        try:
            backend._contacts_by_id[int(c.id)] = c
        except (ValueError, TypeError):
            pass
    monkeypatch.setattr(
        "backends.telegram.asyncio.run_coroutine_threadsafe",
        lambda coro, loop: SimpleNamespace(result=lambda timeout: asyncio.run(coro)),
    )
    return backend


class TestTelegramAddressBook:
    """📨 Rubrica Telegram: build + merge + lookup esteso."""

    def test_build_skips_bots_deleted_and_sets_extras(self, monkeypatch):
        users = [
            _tg_user(
                id=1,
                first_name="Ada",
                last_name="Lovelace",
                username="ada",
                phone="+391234567890",
                access_hash=111,
            ),
            _tg_user(
                id=2,
                first_name="Mamma",
                last_name="Vod",
                username="",
                phone="",
                access_hash=222,
            ),  # senza numero
            _tg_user(id=3, first_name="Bot", bot=True),
            _tg_user(id=4, first_name="Deleted Account", deleted=True),
        ]
        backend = _tg_backend_with_book(monkeypatch, users)

        result = backend.list_address_book_sync()

        assert [c.id for c in result] == ["1", "2"]
        ada = result[0]
        assert ada.extras["phone"] == "391234567890"
        assert ada.extras["username"] == "ada"
        assert ada.extras["access_hash"] == "111"
        assert ada.extras["is_group"] is False
        assert ada.extras["address_book"] is True
        assert ada.extras["source"] == "tg_book"
        assert ada.display_name == "Ada Lovelace"
        mamma = result[1]
        assert mamma.extras["phone"] == ""
        assert mamma.display_name == "Mamma Vod"

    def test_display_name_falls_back_to_phone_then_id(self, monkeypatch):
        users = [
            _tg_user(
                id=5,
                first_name="",
                last_name="",
                username="",
                phone="+393331234567",
                access_hash=5,
            ),
            _tg_user(
                id=6, first_name="", last_name="", username="", phone="", access_hash=6
            ),
        ]
        backend = _tg_backend_with_book(monkeypatch, users)

        result = backend.list_address_book_sync()
        by_id = {c.id: c for c in result}

        assert by_id["5"].display_name == "+393331234567"
        assert by_id["6"].display_name == "6"

    def test_merge_with_dialogs_and_lookup_extension(self, monkeypatch):
        users = [
            _tg_user(
                id=10,
                first_name="Ada",
                last_name="",
                username="",
                phone="+391234567890",
                access_hash=111,
            ),
            _tg_user(
                id=20,
                first_name="Book",
                last_name="Only",
                username="",
                phone="",
                access_hash=222,
            ),  # nessun dialogo
        ]
        dialogs = [
            _tg_dialog_contact(10, "Ada", ts=12345, read_max=99),
            _tg_dialog_contact(-100999, "Channel", ts=1, channel=True),
            _tg_dialog_contact(-42, "Group", ts=2, group=True),
            _tg_dialog_contact(30, "NonBookUser", ts=3),  # utente non-rubrica
        ]
        backend = _tg_backend_with_book(monkeypatch, users, dialogs)

        result = backend.list_address_book_sync()
        by_id = {c.id: c for c in result}

        # book-user con dialogo: merge ts + read_outbox_max_id
        assert by_id["10"].extras["is_chat_active"] is True
        assert by_id["10"].last_message_ts == 12345
        assert by_id["10"].extras["read_outbox_max_id"] == 99
        assert by_id["10"].extras["access_hash"] == "111"
        # book-user senza dialogo: nessun marker di chat attiva
        assert "is_chat_active" not in by_id["20"].extras
        assert "read_outbox_max_id" not in by_id["20"].extras
        assert by_id["20"].extras["access_hash"] == "222"
        # gruppi/canali/utenti-non-rubrica: solo da dialogs
        assert by_id["-100999"].extras["is_channel"] is True
        assert by_id["-100999"].extras["source"] == "tg_dialogs"
        assert by_id["-42"].extras["is_group"] is True
        assert by_id["30"].display_name == "NonBookUser"
        assert by_id["30"].extras["source"] == "tg_dialogs"
        assert by_id["30"].extras["address_book"] is True
        # lookup esteso con i book-user; self.contacts invariato
        assert backend._contacts_by_id[20] is by_id["20"]
        assert backend.contacts == dialogs

    def test_error_returns_stale_or_empty(self, monkeypatch):
        backend = _tg_backend_with_book(monkeypatch, [])
        backend._client = AsyncMock(side_effect=RuntimeError("RPC error"))

        assert backend.list_address_book_sync() == []
        assert backend._address_book is None

    def test_not_connected_returns_empty(self):
        backend = TelegramBackend()
        backend._connected = False
        assert backend.list_address_book_sync() == []


class TestTelegramResolveInputEntity:
    """📤 Send fallback verso contatti senza dialogo (``_resolve_input_entity``)."""

    def test_get_input_entity_fast_path(self):
        backend = TelegramBackend()
        backend._client = SimpleNamespace(
            get_input_entity=AsyncMock(return_value="entity")
        )

        result = asyncio.run(backend._resolve_input_entity(42))

        assert result == "entity"
        backend._client.get_input_entity.assert_awaited_once_with(42)

    def test_fallback_builds_input_peer_user_with_access_hash(self):
        from telethon.tl.types import InputPeerUser

        backend = TelegramBackend()
        backend._client = SimpleNamespace(
            get_input_entity=AsyncMock(side_effect=ValueError("no entity"))
        )
        backend._contacts_by_id = {
            42: ChatContact(
                id="42",
                display_name="Ada",
                protocol=PROTOCOL_TELEGRAM,
                extras={"access_hash": "123456789"},
            )
        }

        result = asyncio.run(backend._resolve_input_entity(42))

        assert isinstance(result, InputPeerUser)
        assert result.user_id == 42
        assert result.access_hash == 123456789

    def test_fallback_without_hash_raises_runtime_error(self):
        backend = TelegramBackend()
        backend._client = SimpleNamespace(
            get_input_entity=AsyncMock(side_effect=ValueError("no entity"))
        )
        backend._contacts_by_id = {
            42: ChatContact(id="42", display_name="Ada", protocol=PROTOCOL_TELEGRAM)
        }

        with pytest.raises(RuntimeError, match="access_hash mancante per 42"):
            asyncio.run(backend._resolve_input_entity(42))


# ─── Signal rubrica (milestone 3) ─────────────────────────────────────────────


class TestSignalAddressBook:
    """📱 Rubrica Signal: markers phone/address_book/is_chat_active + copia."""

    def test_markers_and_no_mutation_of_originals(self):
        backend = SignalBackend()
        active = ChatContact(
            id="+391234567890",
            display_name="Mario",
            protocol=PROTOCOL_SIGNAL,
            extras={"aci": "aci-1", "number": "+391234567890"},
        )
        active.last_message_ts = 5000
        inactive = ChatContact(
            id="+391111111111",
            display_name="Luigi",
            protocol=PROTOCOL_SIGNAL,
            extras={"aci": "aci-2", "number": "+391111111111"},
        )
        backend.contacts = [active, inactive]

        result = backend.list_address_book_sync()

        mario = next(c for c in result if c.id == "+391234567890")
        luigi = next(c for c in result if c.id == "+391111111111")
        assert mario.extras["phone"] == "391234567890"
        assert mario.extras["address_book"] is True
        assert mario.extras["is_chat_active"] is True
        assert luigi.extras["phone"] == "391111111111"
        assert luigi.extras["is_chat_active"] is False
        # originali non mutati
        assert "phone" not in active.extras
        assert "address_book" not in active.extras
        assert "is_chat_active" not in active.extras
        assert mario is not active

    def test_cache_until_force(self):
        backend = SignalBackend()
        contact = ChatContact(
            id="+391234567890",
            display_name="Mario",
            protocol=PROTOCOL_SIGNAL,
        )
        backend.contacts = [contact]

        result = backend.list_address_book_sync()
        assert len(result) == 1

        backend.contacts = []  # cache serve finché non forzo
        assert backend.list_address_book_sync() == result
        assert backend.list_address_book_sync(force=True) == []


# ─── Manager aggregazione (milestone 3) ───────────────────────────────────────


class TestManagerAddressBook:
    """🗂️ Aggregazione multi-backend, isolamento errori, scoping protocols."""

    def test_aggregates_across_backends(self):
        manager = BackendManager()
        sig = _MinimalBackend(
            [
                ChatContact(
                    id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
                )
            ]
        )
        sig.protocol = PROTOCOL_SIGNAL
        wa = _MinimalBackend(
            [
                ChatContact(
                    id="391234567890@c.us",
                    display_name="MarioWA",
                    protocol=PROTOCOL_WHATSAPP,
                )
            ]
        )
        wa.protocol = PROTOCOL_WHATSAPP
        manager.register(sig)
        manager.register(wa)

        result = manager.list_address_book_sync()

        assert len(result) == 2
        assert all(c.extras["address_book"] is True for c in result)
        assert manager.address_book_errors == {}

    def test_error_isolation_keeps_other_backends(self):
        manager = BackendManager()
        ok = _MinimalBackend(
            [ChatContact(id="1", display_name="Ok", protocol=PROTOCOL_SIGNAL)]
        )
        ok.protocol = PROTOCOL_SIGNAL
        bad = _MinimalBackend()
        bad.protocol = PROTOCOL_TELEGRAM
        bad.list_address_book_sync = MagicMock(side_effect=RuntimeError("boom"))
        manager.register(ok)
        manager.register(bad)

        result = manager.list_address_book_sync()

        assert len(result) == 1
        assert result[0].display_name == "Ok"
        assert manager.address_book_errors == {PROTOCOL_TELEGRAM: "boom"}

    def test_protocols_scoping(self):
        manager = BackendManager()
        sig = _MinimalBackend(
            [
                ChatContact(
                    id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
                )
            ]
        )
        sig.protocol = PROTOCOL_SIGNAL
        wa = _MinimalBackend(
            [
                ChatContact(
                    id="391234567890@c.us",
                    display_name="MarioWA",
                    protocol=PROTOCOL_WHATSAPP,
                )
            ]
        )
        wa.protocol = PROTOCOL_WHATSAPP
        manager.register(sig)
        manager.register(wa)

        result = manager.list_address_book_sync(protocols={PROTOCOL_WHATSAPP})

        assert len(result) == 1
        assert result[0].protocol == PROTOCOL_WHATSAPP

    def test_errors_reset_between_calls(self):
        manager = BackendManager()
        bad = _MinimalBackend()
        bad.protocol = PROTOCOL_TELEGRAM
        bad.list_address_book_sync = MagicMock(side_effect=RuntimeError("boom"))
        manager.register(bad)

        manager.list_address_book_sync()
        assert manager.address_book_errors == {PROTOCOL_TELEGRAM: "boom"}

        bad.list_address_book_sync = MagicMock(return_value=[])
        manager.list_address_book_sync()
        assert manager.address_book_errors == {}


# ─── Fixture anonimizzate (milestone 5) ───────────────────────────────────────


class TestFixtureAnonymizer:
    """🕵️ Anonimizzazione deterministica delle fixture (``anonymize_payload``)."""

    def test_deterministic_same_input(self):
        data = [{"number": "+391234567890", "name": "Mario"}]
        assert anonymize_payload(data) == anonymize_payload(data)

    def test_seed_shifts_tokens_but_stays_deterministic(self):
        data = [{"number": "+391234567890", "name": "Mario"}]
        assert anonymize_payload(data, seed=0) != anonymize_payload(data, seed=10)
        assert anonymize_payload(data, seed=10) == anonymize_payload(data, seed=10)

    def test_phone_number_format(self):
        out = anonymize_payload([{"number": "+391234567890"}])
        assert out[0]["number"].startswith("39 0000")

    def test_duplicate_numbers_stay_equal(self):
        data = [
            {"id": "393331234567@c.us", "name": "Mario"},
            {"id": "393331234567@c.us", "name": "Mario"},
        ]
        out = anonymize_payload(data)
        assert out[0]["id"] == out[1]["id"]

    def test_names_usernames_uuid(self):
        data = [{"first_name": "Ada", "username": "ada", "uuid": "uuid-1"}]
        out = anonymize_payload(data)
        assert out[0]["first_name"].startswith("Contatto ")
        assert out[0]["username"] == "user0"
        assert out[0]["uuid"].startswith("00000000-0000-0000-0000-")

    def test_access_hash_zeroed_and_ids_sequential(self):
        data = {
            "users": [
                {"id": 42, "access_hash": 123},
                {"id": 99, "access_hash": 456},
            ]
        }
        out = anonymize_payload(data)
        assert out["users"][0]["access_hash"] == 0
        assert out["users"][1]["access_hash"] == 0
        assert out["users"][0]["id"] != out["users"][1]["id"]

    def test_shape_preserved_and_input_not_mutated(self):
        data = {"users": [{"id": 42, "name": "X", "nested": [1, 2, {"k": "v"}]}]}
        original = json.dumps(data)
        out = anonymize_payload(data)
        assert json.dumps(data) == original  # input intatto
        assert list(out["users"][0].keys()) == list(data["users"][0].keys())
        assert out["users"][0]["nested"] == [1, 2, {"k": "v"}]

    def test_lid_jid_preserves_suffix(self):
        data = [{"id": {"_serialized": "220988985864200@lid"}}]
        out = anonymize_payload(data)
        assert out[0]["id"]["_serialized"].endswith("@lid")

    def test_empty_values_preserved(self):
        data = [{"name": None, "pushname": "", "phone": ""}]
        out = anonymize_payload(data)
        assert out[0]["name"] is None
        assert out[0]["pushname"] == ""
        assert out[0]["phone"] == ""


class TestFixtureIntegration:
    """🧩 Fixture anonime nei mock WAHA/Telethon: conteggi dedup/merge."""

    def test_wa_fixture_dedup_counts(self):
        raw = [
            {"id": "393331234567@c.us", "name": "Mario Rossi", "pushname": None},
            {"id": "393331234567@c.us", "name": None, "pushname": "MarioP"},
            {"id": "393331234568@c.us", "name": None, "pushname": None},
        ]
        anon = anonymize_payload(raw)

        # Duplicati preservati (stesso numero → stesso id anonimizzato).
        assert anon[0]["id"] == anon[1]["id"]
        assert anon[1]["id"] != anon[2]["id"]

        deduped = _dedup_book_contacts(anon)
        assert len(deduped) == 2
        # Il vincitore è quello con nome (su solo-pushname).
        mario = next(d for d in deduped if d["phone"] == "390000000000")
        assert mario["name"].startswith("Contatto ")

    def test_wa_fixture_merge_with_chat(self, monkeypatch, tmp_path):
        import backend as backend_mod

        monkeypatch.setattr(backend_mod, "CACHE_DIR", tmp_path)
        raw = [
            {"id": "393331234567@c.us", "name": "Mario Rossi", "pushname": None},
            {"id": "393331234567@c.us", "name": None, "pushname": "MarioP"},
        ]
        anon_book = anonymize_payload(raw)
        # Il numero anonimizzato della coppia duplicata (id senza dominio).
        anon_phone = anon_book[0]["id"].split("@")[0]

        backend = _wa_backend()
        backend.start_lid_resolver = MagicMock()
        backend._rest.list_all_contacts.return_value = anon_book
        backend.contacts = [_chat(f"{anon_phone}@c.us", "Mario", ts=123)]

        result = backend.list_address_book_sync()

        assert len(result) == 1
        contact = result[0]
        assert contact.id == f"{anon_phone}@c.us"  # merge: id = chat attiva
        assert contact.extras["is_chat_active"] is True
        assert contact.extras["phone"] == anon_phone
        assert contact.last_message_ts == 123

    def test_tg_fixture_counts_skip_bots(self, monkeypatch):
        raw_users = [
            {
                "id": 42,
                "first_name": "Ada",
                "last_name": "Lovelace",
                "username": "ada",
                "phone": "+391234567890",
                "bot": False,
                "access_hash": 111,
            },
            {
                "id": 43,
                "first_name": "Mamma",
                "last_name": "Vod",
                "username": "",
                "phone": "",
                "bot": False,
                "access_hash": 222,
            },
            {"id": 44, "first_name": "Bot", "bot": True, "access_hash": 0},
        ]
        anon_users = anonymize_payload(raw_users)
        users = [SimpleNamespace(**u) for u in anon_users]

        backend = _tg_backend_with_book(monkeypatch, users)

        result = backend.list_address_book_sync()

        # Bot scartato; id anonimizzati sequenziali (0, 1).
        assert [c.id for c in result] == ["0", "1"]
        mamma = next(c for c in result if c.id == "1")
        assert mamma.extras["phone"] == ""  # contatto senza numero preservato
        # access_hash anonimizzato a 0 → backend lo mappa a "" (nessun hash).
        assert mamma.extras["access_hash"] == ""
