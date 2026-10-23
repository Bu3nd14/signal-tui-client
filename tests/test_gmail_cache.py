"""Unit tests for the Gmail secret and inbox caches (no network)."""

from __future__ import annotations

import hashlib

import pytest

from protocols import config
from web import gmail

_ENV_VARS = (
    "GMAIL_ADDRESS",
    "GMAIL_APP_PASSWORD",
    "GMAIL_APP_PASSWORD_FILE",
    "GMAIL_APP_PASSWORD_CMD",
    "GMAIL_IMAP_HOST",
    "GMAIL_IMAP_PORT",
    "GMAIL_FOLDER",
    "GMAIL_TIMEOUT_S",
)


@pytest.fixture(autouse=True)
def isolate_caches(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    config.clear_gmail_secret_cache()
    gmail.clear_inbox_cache()
    yield
    config.clear_gmail_secret_cache()
    gmail.clear_inbox_cache()


def _clock(monkeypatch, start: float = 1000.0) -> dict[str, float]:
    now = {"value": start}
    monkeypatch.setattr(config, "_now", lambda: now["value"])
    monkeypatch.setattr(gmail, "_now", lambda: now["value"])
    return now


def _entry(expires_at: float, uid: int = 1) -> gmail._InboxCacheEntry:
    return gmail._InboxCacheEntry(
        uidvalidity=uid,
        messages=({"uid": uid, "flags": ()},),
        expires_at=expires_at,
    )


def test_secret_cache_key_is_opaque_sha256():
    key = config._gmail_secret_cache_key("hunter2 --with-secret")

    assert isinstance(key, str)
    assert key == hashlib.sha256(b"hunter2 --with-secret").hexdigest()
    assert len(key) == 64
    assert "hunter2" not in key
    assert key != config._gmail_secret_cache_key("hunter2 --with-secret2")


def test_secret_generation_rejects_an_in_flight_put():
    key = config._gmail_secret_cache_key("credential-helper")
    with config._gmail_secret_cache_lock:
        gen0 = config._gmail_secret_cache_gen

    config.clear_gmail_secret_cache()
    config._gmail_secret_cache_put(key, "stale-secret", gen0)

    assert config._gmail_secret_cache_get(key) is None


def test_secret_prune_removes_every_expired_entry(monkeypatch):
    clock = _clock(monkeypatch)
    now = clock["value"]
    with config._gmail_secret_cache_lock:
        config._gmail_secret_cache["expired-a"] = (now - 1, "old-a")
        config._gmail_secret_cache["expired-b"] = (now - 10, "old-b")
        config._gmail_secret_cache["live"] = (now + 100, "live-secret")

    assert config._gmail_secret_cache_get("live") == "live-secret"
    assert "expired-a" not in config._gmail_secret_cache
    assert "expired-b" not in config._gmail_secret_cache
    assert "live" in config._gmail_secret_cache


def test_inbox_epoch_rejects_an_in_flight_put():
    key = ("INBOX", "fingerprint")
    entry = _entry(gmail._now() + 60)
    with gmail._inbox_cache_lock:
        gmail._inbox_cache[key] = entry
        epoch0 = gmail._inbox_epoch

    gmail.clear_inbox_cache()
    with gmail._inbox_cache_lock:
        accepted = gmail._inbox_cache_put_locked(key, entry, epoch0)

    assert accepted is False
    assert key not in gmail._inbox_cache


def test_add_flag_is_case_insensitive_and_ordered():
    assert gmail._add_flag(["\\Answered", "\\Flagged"], "\\Seen") == [
        "\\Answered",
        "\\Flagged",
        "\\Seen",
    ]
    assert gmail._add_flag(["\\Answered", "\\seen", "\\Flagged"], "\\Seen") == [
        "\\Answered",
        "\\seen",
        "\\Flagged",
    ]
    assert gmail._add_flag([], "\\Seen") == ["\\Seen"]


def test_freeze_and_thaw_do_not_share_mutable_flags():
    message = {"uid": 1, "flags": ["\\b", "\\a", "\\a"], "seen": True}
    frozen = gmail._freeze_message(message)

    assert frozen["flags"] == ("\\a", "\\b")
    thawed = gmail._thaw_message(frozen)
    assert thawed["flags"] == ["\\a", "\\b"]
    assert isinstance(thawed["flags"], list)

    thawed["flags"].append("\\Draft")
    thawed["subject"] = "mutated"
    assert frozen["flags"] == ("\\a", "\\b")
    assert "subject" not in frozen


def test_freeze_and_thaw_deep_copy_nested_fields():
    message = {"uid": 1, "flags": [], "nested": {"value": ["original"]}}
    frozen = gmail._freeze_message(message)
    thawed = gmail._thaw_message(frozen)

    thawed["nested"]["value"].append("mutated")
    assert frozen["nested"] == {"value": ["original"]}


def test_inbox_lru_evicts_the_oldest_entry_without_touching_the_epoch():
    with gmail._inbox_cache_lock:
        epoch0 = gmail._inbox_epoch
        for index in range(gmail._INBOX_CACHE_MAX_ENTRIES + 1):
            key = (f"folder-{index}", "fingerprint")
            gmail._inbox_cache_put_locked(
                key, _entry(gmail._now() + 60, uid=index), epoch0
            )

    assert len(gmail._inbox_cache) == gmail._INBOX_CACHE_MAX_ENTRIES
    assert ("folder-0", "fingerprint") not in gmail._inbox_cache
    assert gmail._inbox_epoch == epoch0


def test_inbox_expired_entry_is_removed_on_get():
    key = ("INBOX", "fingerprint")
    with gmail._inbox_cache_lock:
        gmail._inbox_cache[key] = _entry(gmail._now() - 1)

    with gmail._inbox_cache_lock:
        assert gmail._inbox_cache_get_locked(key, gmail._now()) is None
    assert key not in gmail._inbox_cache


def test_account_fingerprint_is_a_hash_of_address_host_and_port():
    settings = gmail.GmailSettings(
        address="secret@example.com",
        password="nope",
        imap_host="imap.example.com",
        imap_port=993,
        smtp_host="smtp.example.com",
        smtp_port=587,
        folder="INBOX",
        timeout_s=30,
    )

    fingerprint = gmail._account_fingerprint(settings)

    expected = hashlib.sha256(
        b"secret@example.com\x00imap.example.com\x00993"
    ).hexdigest()
    assert fingerprint == expected
    assert "secret" not in fingerprint
    assert "example.com" not in fingerprint
