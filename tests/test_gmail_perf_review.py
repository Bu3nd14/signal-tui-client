"""Targeted regression tests added during the P0/P1 testing review.

These cover two gaps left by the main Gmail suites:

* ``mark_read`` must advance the global epoch even when the cached entry is
  not changed (target outside the window or already marked seen), because the
  epoch is what discards an in-flight list fetch started before the STORE.
* both caches are in-memory only: reading the secret or fetching the list must
  not create a persistent cache file.
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager

import pytest

from protocols import config
from server import gmail


@pytest.fixture(autouse=True)
def _isolate_caches(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
    config.clear_gmail_secret_cache()
    gmail.clear_inbox_cache()
    yield
    config.clear_gmail_secret_cache()
    gmail.clear_inbox_cache()


def _entry(uid: int, *, seen: bool) -> gmail._InboxCacheEntry:
    message = {
        "uid": uid,
        "flags": ("\\Seen",) if seen else (),
        "seen": seen,
    }
    return gmail._InboxCacheEntry(
        uidvalidity=1,
        messages=(message,),
        expires_at=gmail._now() + 60,
    )


def test_mark_read_advances_epoch_for_a_target_outside_the_window():
    key = ("INBOX", "fingerprint")
    entry = _entry(1, seen=False)
    with gmail._inbox_cache_lock:
        gmail._inbox_cache[key] = entry
        epoch_before = gmail._inbox_epoch

    gmail._mark_read_cached(key[0], key[1], 99)

    with gmail._inbox_cache_lock:
        assert gmail._inbox_epoch == epoch_before + 1
        assert gmail._inbox_cache[key] is entry


def test_mark_read_advances_epoch_for_an_already_seen_target():
    key = ("INBOX", "fingerprint")
    entry = _entry(1, seen=True)
    with gmail._inbox_cache_lock:
        gmail._inbox_cache[key] = entry
        epoch_before = gmail._inbox_epoch

    gmail._mark_read_cached(key[0], key[1], 1)

    with gmail._inbox_cache_lock:
        assert gmail._inbox_epoch == epoch_before + 1
        assert gmail._inbox_cache[key] is entry


def test_secret_cache_is_memory_only(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAIL_APP_PASSWORD_CMD", "/bin/secret-helper")
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "cmd-secret")

    monkeypatch.setattr(config.subprocess, "run", run)
    before = set(tmp_path.rglob("*"))

    assert config.get_gmail_app_password() == "cmd-secret"
    assert config.get_gmail_app_password() == "cmd-secret"
    config.clear_gmail_secret_cache()

    assert calls == [["/bin/secret-helper"]]
    assert set(tmp_path.rglob("*")) == before


class _ListConnection:
    def select(self, mailbox, readonly=False):
        return "OK", [b"1"]

    def response(self, name):
        return ("UIDVALIDITY", [b"1"])

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [b"1"]
        if command == "FETCH":
            raw = b"From: sender@example.com\r\nSubject: hello\r\n\r\n"
            meta = (f"1 (UID 1 FLAGS ()) BODY[HEADER.FIELDS] {{{len(raw)}}}").encode()
            return "OK", [(meta, raw), b")"]
        return "BAD", []

    def logout(self):
        pass


@contextmanager
def _fake_imap_connect(settings):
    yield _ListConnection()


def test_inbox_cache_is_memory_only(monkeypatch, tmp_path):
    settings = gmail.GmailSettings(
        address="sender@example.com",
        password="not-a-real-secret",
        imap_host="imap.example.com",
        imap_port=993,
        smtp_host="smtp.example.com",
        smtp_port=587,
        folder="INBOX",
        timeout_s=30,
    )
    monkeypatch.setattr(gmail, "load_settings", lambda: settings)
    monkeypatch.setattr(gmail, "imap_connect", _fake_imap_connect)
    before = set(tmp_path.rglob("*"))

    body = gmail.fetch_message_list(folder="INBOX")

    assert body["total"] == 1
    assert set(tmp_path.rglob("*")) == before
