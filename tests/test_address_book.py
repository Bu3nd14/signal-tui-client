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
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backends import config
from backends.base import ChatBackend
from models import ChatContact


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
