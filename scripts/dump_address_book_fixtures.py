#!/usr/bin/env python3
"""Dev-only fixture dumper for the address book (Ctrl+S rubrica).

Reads the REAL address-book responses of the three backends (WAHA REST,
Telethon ``GetContactsRequest``, signal-cli ``listContacts``) and writes
privacy-safe, deterministically-anonymized JSON fixtures under
``tests/fixtures/``.  The fixtures preserve the *shape* of the real data
(duplicate WAHA rows, ``@lid`` entries, entries without a name, Telegram
contacts without a phone) so integration tests can exercise realistic
dedup/merge counts without ever committing personal data.

NEVER commit the raw responses, and never run this against real data unless
you are deliberately dumping new fixtures for the test suite.  The
anonymization is a pure function (``anonymize_payload``) with unit tests in
``tests/test_address_book.py``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"

# ─── Deterministic anonymization ────────────────────────────────────────────

#: Keys whose string value is a phone number (E.164 / raw digits).
_PHONE_KEYS = {
    "phone",
    "number",
    "sourceNumber",
    "destinationNumber",
    "destination",
    "source",
}

#: Keys whose string value is a JID (``phone@c.us`` / ``lid@lid`` / ...).
_JID_KEYS = {
    "id",
    "_serialized",
    "jid",
    "remoteJid",
    "chatId",
    "from",
    "to",
}

#: Keys whose string value is a human display name.
_NAME_KEYS = {
    "name",
    "first_name",
    "last_name",
    "pushname",
    "pushName",
    "notifyName",
    "givenName",
    "title",
}

#: Keys whose string value is a Telegram username.
_USERNAME_KEYS = {"username"}

#: Keys whose string value is a Signal UUID/ACI.
_UUID_KEYS = {"uuid", "aci"}

#: Keys whose string value is free-form message text.
_MSG_TEXT_KEYS = {"text", "body", "caption", "message"}

#: Matches the digit prefix of a JID like ``393331234567@c.us``.
_JID_RE = re.compile(r"^(\d+)(@.*)$")


def _fake_number(token: int) -> str:
    """Return the anonymized phone for *token* (``39 0000NNNNNN``)."""
    return f"39 0000{token:06d}"


def _fake_jid_digits(token: int) -> str:
    """Return the anonymized digit prefix for a JID (no spaces)."""
    return f"39{token:010d}"


def _phone_token(digits: str, state: dict) -> int:
    """Return (and lazily allocate) the stable token for a digit string."""
    if digits not in state["phone_map"]:
        state["phone_map"][digits] = state["phone_seq"]
        state["phone_seq"] += 1
    return state["phone_map"][digits]


def _anon_string(value: str, key: str | None, state: dict) -> str:
    if not value:
        return value

    if key in _USERNAME_KEYS:
        if value not in state["username_map"]:
            state["username_map"][value] = f"user{state['username_seq']}"
            state["username_seq"] += 1
        return state["username_map"][value]

    if key in _NAME_KEYS:
        if value not in state["name_map"]:
            state["name_map"][value] = f"Contatto {state['name_seq']}"
            state["name_seq"] += 1
        return state["name_map"][value]

    if key in _UUID_KEYS:
        if value not in state["uuid_map"]:
            state["uuid_map"][value] = (
                f"00000000-0000-0000-0000-{state['uuid_seq']:012d}"
            )
            state["uuid_seq"] += 1
        return state["uuid_map"][value]

    if key in _MSG_TEXT_KEYS:
        if value not in state["msg_map"]:
            state["msg_map"][value] = f"Messaggio {state['msg_seq']}"
            state["msg_seq"] += 1
        return state["msg_map"][value]

    if key in _PHONE_KEYS:
        digits = "".join(ch for ch in value if ch.isdigit())
        if not digits:
            return value
        return _fake_number(_phone_token(digits, state))

    if key in _JID_KEYS:
        match = _JID_RE.match(value)
        if not match:
            return value
        return _fake_jid_digits(_phone_token(match.group(1), state)) + match.group(2)

    return value


def _anon(value, key, state):
    if isinstance(value, dict):
        return {k: _anon(v, k, state) for k, v in value.items()}
    if isinstance(value, list):
        return [_anon(item, None, state) for item in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if key == "access_hash":
            return 0
        if key == "id":
            # Telegram user/chat ids → small deterministic integers.
            if value not in state["int_map"]:
                state["int_map"][value] = state["int_seq"]
                state["int_seq"] += 1
            return state["int_map"][value]
        return value
    if isinstance(value, str):
        return _anon_string(value, key, state)
    return value


def anonymize_payload(data, seed: int = 0):
    """Return a deterministic, privacy-safe copy of *data*.

    The shape and duplicates are preserved.  Within a single call the mapping
    is stable per original value, so two rows sharing the same phone number
    keep sharing the same anonymized number (dedup/merge still work):

    - phone numbers → ``39 0000NNNNNN`` (fictitious international prefix);
    - JID digit prefixes → ``39NNNNNNNNNN`` (same token as the phone);
    - names → ``Contatto N``; usernames → ``userN``; uuids/aci → zeroed;
    - message text → ``Messaggio N``;
    - Telegram ``access_hash`` → ``0``; Telegram ``id`` → sequential ints.

    ``seed`` shifts the numeric tokens (useful to avoid collisions between
    separately-anonymized fixtures if they are ever merged).
    """
    state = {
        "phone_map": {},
        "phone_seq": seed,
        "name_map": {},
        "name_seq": seed,
        "username_map": {},
        "username_seq": seed,
        "uuid_map": {},
        "uuid_seq": seed,
        "msg_map": {},
        "msg_seq": seed,
        "int_map": {},
        "int_seq": seed,
    }
    return _anon(data, None, state)


# ─── Fixture writing ────────────────────────────────────────────────────────


def _write_fixture(out_dir: Path, name: str, payload) -> Path:
    """Write *payload* as a pretty-printed JSON fixture; return its path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def dump_fixtures(
    wa_contacts_all,
    wa_chats,
    tg_users,
    signal_contacts,
    *,
    seed: int = 0,
    out_dir: Path | None = None,
) -> list[Path]:
    """Anonymize and write the four address-book fixtures.

    ``tg_users`` is expected to already be the *users* list extracted from the
    Telethon ``GetContactsRequest`` result (only ``id``/``first_name``/
    ``last_name``/``username``/``phone``/``bot``/``access_hash`` are kept).
    """
    target = out_dir or FIXTURES_DIR
    fixtures = [
        ("wa_contacts_all.json", wa_contacts_all),
        ("wa_chats.json", wa_chats),
        ("tg_contacts.json", tg_users),
        ("signal_contacts.json", signal_contacts),
    ]
    return [
        _write_fixture(target, name, anonymize_payload(payload, seed))
        for name, payload in fixtures
    ]


# ─── Real-response readers (dev-only; only invoked from ``main``) ───────────


def _tg_user_to_dict(user) -> dict:
    """Extract the privacy-relevant fields of a Telethon ``User``."""
    return {
        "id": getattr(user, "id", 0),
        "first_name": getattr(user, "first_name", "") or "",
        "last_name": getattr(user, "last_name", "") or "",
        "username": getattr(user, "username", "") or "",
        "phone": getattr(user, "phone", "") or "",
        "bot": bool(getattr(user, "bot", False)),
        "access_hash": getattr(user, "access_hash", 0) or 0,
    }


def _fetch_signal_contacts() -> list[dict]:
    """Read ``listContacts`` from the Signal backend (daemon or subprocess)."""
    from protocols.signal import SignalBackend

    backend = SignalBackend()
    backend._connect_sync()
    return [c for c in backend._rpc.list_contacts()] if backend._use_daemon else []


def _fetch_whatsapp() -> tuple[list, list]:
    """Read ``/api/contacts/all`` and ``/api/{session}/chats`` from WAHA."""
    from protocols.whatsapp import WhatsAppBackend

    backend = WhatsAppBackend()
    if backend._rest is None:
        raise RuntimeError("WhatsApp REST client is not configured")
    contacts_all = backend._rest.list_all_contacts() or []
    chats = (
        backend._rest._request("GET", f"/api/{backend.session_name}/chats", timeout=10)
        or []
    )
    return contacts_all, chats if isinstance(chats, list) else []


def _fetch_telegram_users() -> list[dict]:
    """Run ``GetContactsRequest(hash=0)`` and return the anonymizable users."""
    import asyncio

    from telethon.tl.functions.contacts import GetContactsRequest

    from protocols.telegram import TelegramBackend

    backend = TelegramBackend()

    async def _rpc():
        result = await backend._client(GetContactsRequest(hash=0))
        return [_tg_user_to_dict(u) for u in getattr(result, "users", []) or []]

    backend._connect_sync()
    if backend._loop is None or backend._client is None:
        raise RuntimeError("Telegram backend did not connect")
    future = asyncio.run_coroutine_threadsafe(_rpc(), backend._loop)
    return future.result(timeout=20)


def main(argv=None) -> int:
    """Fetch real responses and dump anonymized fixtures (dev-only)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=0, help="anonymization seed")
    parser.add_argument(
        "--out",
        type=Path,
        default=FIXTURES_DIR,
        help="fixture output directory (default: tests/fixtures)",
    )
    args = parser.parse_args(argv)

    wa_contacts_all, wa_chats = _fetch_whatsapp()
    tg_users = _fetch_telegram_users()
    signal_contacts = _fetch_signal_contacts()

    for path in dump_fixtures(
        wa_contacts_all,
        wa_chats,
        tg_users,
        signal_contacts,
        seed=args.seed,
        out_dir=args.out,
    ):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
