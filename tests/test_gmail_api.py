"""API tests for ``/api/gmail/*`` with mocked IMAP/SMTP (no network)."""

from __future__ import annotations

import imaplib
import logging
import re
import threading
from email.message import EmailMessage
from typing import ClassVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from protocols import config
from web import gmail
from web.gmail_api import create_gmail_router

_ENV_VARS = (
    "GMAIL_ADDRESS",
    "GMAIL_APP_PASSWORD",
    "GMAIL_APP_PASSWORD_FILE",
    "GMAIL_APP_PASSWORD_CMD",
    "GMAIL_IMAP_HOST",
    "GMAIL_IMAP_PORT",
    "GMAIL_SMTP_HOST",
    "GMAIL_SMTP_PORT",
    "GMAIL_FOLDER",
    "GMAIL_TIMEOUT_S",
)


@pytest.fixture(autouse=True)
def isolate_gmail_config(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    config.clear_gmail_secret_cache()
    gmail.clear_inbox_cache()
    yield
    config.clear_gmail_secret_cache()
    gmail.clear_inbox_cache()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(create_gmail_router())
    return TestClient(app)


_HEADER_FIELDS_RE = re.compile(r"HEADER\.FIELDS \(([^)]*)\)")


def _header_block(raw: bytes) -> tuple[bytes, bytes]:
    for separator in (b"\r\n\r\n", b"\n\n"):
        if separator in raw:
            return raw.split(separator, 1)[0], separator
    return raw, b"\r\n\r\n"


def _filter_header_fields(raw: bytes, fetch_item: str) -> bytes:
    """Emulate a real IMAP ``HEADER.FIELDS`` response."""
    match = _HEADER_FIELDS_RE.search(fetch_item)
    if match is None:
        return raw
    wanted = {name.upper().encode("ascii") for name in match.group(1).split()}
    header, _separator = _header_block(raw)
    output: list[bytes] = []
    keep = False
    for line in re.split(rb"\r\n|\n", header):
        if line[:1] in (b" ", b"\t"):
            if keep:
                output.append(line)
            continue
        name = line.split(b":", 1)[0].strip().upper()
        keep = name in wanted
        if keep:
            output.append(line)
    return b"\r\n".join(output) + b"\r\n\r\n"


def _add_escape_headers(message: EmailMessage) -> None:
    message["Received"] = "from escape.example.com by mx.example.com"
    message["DKIM-Signature"] = "v=1; d=escape.example.com"
    message["List-Id"] = "<escape.example.com>"


def _build_messages() -> dict[int, bytes]:
    messages: dict[int, bytes] = {}

    reply = EmailMessage()
    reply["From"] = "Mario Rossi <mario@example.com>"
    reply["To"] = "me@example.com"
    reply["Subject"] = "Hello"
    reply["Date"] = "Fri, 23 Oct 2026 10:00:00 +0000"
    reply["Message-ID"] = "<orig@example.com>"
    reply["References"] = "<root@example.com>"
    _add_escape_headers(reply)
    reply.set_content("original line\nsecond line")
    reply.add_attachment(
        b"%PDF-1.4\npdf",
        maintype="application",
        subtype="pdf",
        filename="../../evil?.pdf",
    )
    reply.add_attachment(
        b"note text",
        maintype="text",
        subtype="plain",
        filename="città.txt",
    )
    messages[1] = reply.as_bytes()

    html = EmailMessage()
    html["From"] = "HTML Sender <html@example.com>"
    html["To"] = "me@example.com"
    html["Subject"] = "HTML only"
    html["Date"] = "Thu, 22 Oct 2026 10:00:00 +0000"
    html["Message-ID"] = "<html@example.com>"
    _add_escape_headers(html)
    html.set_content("<p>HTML body</p>", subtype="html")
    messages[2] = html.as_bytes()

    for uid, subject in ((3, "Third"), (4, "Latest")):
        message = EmailMessage()
        message["From"] = f"Sender {uid} <sender{uid}@example.com>"
        message["To"] = "me@example.com"
        message["Subject"] = subject
        message["Date"] = f"Wed, 21 Oct 2026 1{uid}:00:00 +0000"
        message["Message-ID"] = f"<msg-{uid}@example.com>"
        _add_escape_headers(message)
        message.set_content(f"{subject.lower()} body")
        messages[uid] = message.as_bytes()

    no_date = EmailMessage()
    no_date["From"] = "No Date <nodate@example.com>"
    no_date["To"] = "me@example.com"
    no_date["Subject"] = "No date"
    no_date["Message-ID"] = "<no-date@example.com>"
    _add_escape_headers(no_date)
    no_date.set_content("no date body")
    messages[5] = no_date.as_bytes()

    return messages


class FakeIMAP:
    instances: ClassVar[list[FakeIMAP]] = []
    messages: ClassVar[dict[int, bytes]] = {}
    seen_uids: ClassVar[set[int]] = set()
    uidvalidity: bytes = b"777"
    search_all: bytes = b"1 2 3 4 5"
    search_unseen: bytes = b"2 4"
    list_data: ClassVar[list[bytes]] = [
        b'(\\HasNoChildren \\Inbox) "/" "INBOX"',
        b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"',
    ]

    def __init__(self, host, port=None, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls: list[tuple[str, tuple]] = []
        self.selected: tuple[str, bool] | None = None
        self.logged_out = False
        self.expunged = False
        self.fetched_headers: dict[int, bytes] = {}
        self.messages = type(self).messages
        FakeIMAP.instances.append(self)

    def login(self, user, password):
        self.credentials = (user, password)

    def select(self, mailbox, readonly=False):
        self.selected = (mailbox, readonly)
        return "OK", [b"5"]

    def response(self, name):
        if name == "UIDVALIDITY":
            # imaplib returns the response *type* as first element, not "OK".
            return "UIDVALIDITY", [type(self).uidvalidity]
        return "NO", [None]

    def list(self, directory='""', pattern="*"):
        self.calls.append(("LIST", (directory, pattern)))
        return "OK", list(type(self).list_data)

    def expunge(self):
        self.calls.append(("EXPUNGE", ()))
        self.expunged = True
        return "OK", [b"1"]

    def uid(self, command, *args):
        self.calls.append((command, args))
        if command == "SEARCH":
            criteria = args[-1]
            data = (
                type(self).search_unseen
                if criteria == "UNSEEN"
                else type(self).search_all
            )
            return "OK", [data]
        if command == "FETCH":
            uids = [int(value) for value in str(args[0]).split(",")]
            fetch_item = str(args[1]) if len(args) > 1 else ""
            payload = []
            for uid in uids:
                raw = self.messages.get(uid)
                if raw is None:
                    continue
                seen = uid in type(self).seen_uids or uid == 1
                flags = "\\Seen" if seen else ""
                if uid == 4:
                    flags = "\\Flagged \\Seen \\Flagged"
                if "HEADER.FIELDS" in fetch_item:
                    raw = _filter_header_fields(raw, fetch_item)
                    self.fetched_headers[uid] = raw
                    meta = (
                        f"{uid} (UID {uid} FLAGS ({flags}) "
                        f"BODY[HEADER.FIELDS] {{{len(raw)}}}"
                    ).encode()
                    payload.extend([(meta, raw), b")"])
                elif "BODY.PEEK[HEADER]" in fetch_item:
                    header, _separator = _header_block(raw)
                    raw = header + b"\r\n\r\n"
                    meta = (
                        f"{uid} (UID {uid} FLAGS ({flags}) BODY[HEADER] {{{len(raw)}}}"
                    ).encode()
                    payload.extend([(meta, raw), b")"])
                elif "BODY.PEEK[]" in fetch_item:
                    meta = (
                        f"{uid} (UID {uid} FLAGS ({flags}) BODY[] {{{len(raw)}}}"
                    ).encode()
                    payload.extend([(meta, raw), b")"])
                else:
                    # imaplib returns bytes (no tuple/literal) for FLAGS-only.
                    payload.append(f"{uid} (UID {uid} FLAGS ({flags}))".encode())
            return "OK", payload
        if command == "STORE":
            if len(args) >= 3 and args[1] == "+FLAGS" and "\\Seen" in str(args[2]):
                for value in str(args[0]).split(","):
                    type(self).seen_uids.add(int(value))
            return "OK", [b"1"]
        if command == "COPY":
            return "OK", [b"1"]
        return "BAD", []

    def logout(self):
        self.logged_out = True


class FakeSMTP:
    instances: ClassVar[list[FakeSMTP]] = []

    def __init__(self, host, port=None, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.starttls_called = False
        self.ehlo_count = 0
        self.message = None
        FakeSMTP.instances.append(self)

    def ehlo(self):
        self.ehlo_count += 1
        return 250, b"ok"

    def starttls(self):
        self.starttls_called = True
        return 220, b"ready"

    def login(self, user, password):
        self.credentials = (user, password)

    def send_message(self, message):
        self.message = message
        return {}

    def quit(self):
        self.quit_called = True

    def close(self):
        pass


@pytest.fixture
def fake_imap(monkeypatch):
    FakeIMAP.instances = []
    FakeIMAP.messages = _build_messages()
    FakeIMAP.seen_uids = set()
    FakeIMAP.list_data = [
        b'(\\HasNoChildren \\Inbox) "/" "INBOX"',
        b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"',
    ]
    monkeypatch.setattr(gmail.imaplib, "IMAP4_SSL", FakeIMAP)
    return FakeIMAP


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(gmail.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def _search_count(fake_imap) -> int:
    return sum(
        1
        for instance in fake_imap.instances
        for command, _args in instance.calls
        if command == "SEARCH"
    )


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-secret")


def test_status_is_offline_and_hides_password(client, monkeypatch, configured):
    def fail(*args, **kwargs):
        raise AssertionError("status must not open a network connection")

    monkeypatch.setattr(gmail.imaplib, "IMAP4_SSL", fail)

    response = client.get("/api/gmail/status")

    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True
    assert body["address"] == "me@example.com"
    assert body["folder"] == "INBOX"
    assert body["connected"] is None
    assert body["limits"]["max_attachments"] == 10
    assert "password" not in response.text


def test_status_not_configured(client):
    response = client.get("/api/gmail/status")

    assert response.status_code == 200
    assert response.json()["configured"] is False
    assert response.json()["address"] == ""


def test_list_is_date_descending_and_reports_window(client, fake_imap, configured):
    response = client.get("/api/gmail/messages")

    assert response.status_code == 200
    body = response.json()
    assert body["uidvalidity"] == 777
    assert body["folder"] == "INBOX"
    assert body["offset"] == 0
    assert body["limit"] == 50
    assert body["window"] == 200
    assert body["total"] == 5
    assert body["has_more"] is False
    # UID order is 1,2,3,4,5 but Date order is 1,2,4,3 and UID 5 has no Date.
    assert [message["uid"] for message in body["messages"]] == [1, 2, 4, 3, 5]
    assert body["messages"][-1]["date_ts"] is None
    first = body["messages"][0]
    assert first["subject"] == "Hello"
    assert first["has_attachments"] is True
    assert first["seen"] is True
    assert first["flags"] == ["\\Seen"]
    by_uid = {message["uid"]: message for message in body["messages"]}
    assert by_uid[4]["flags"] == ["\\Flagged", "\\Seen"]
    assert by_uid[4]["seen"] is True
    # The list fetches headers only: no body, no attachments, no snippet.
    assert first["snippet"] == ""
    assert fake_imap.instances[0].selected == ('"INBOX"', True)
    calls = fake_imap.instances[0].calls
    assert ("SEARCH", (None, "ALL")) in calls
    assert not any(command == "SEARCH" and "UNSEEN" in args for command, args in calls)
    fetch_calls = [args for command, args in calls if command == "FETCH"]
    assert fetch_calls == [
        (
            "5,4,3,2,1",
            (
                "(FLAGS BODY.PEEK[HEADER.FIELDS "
                "(FROM TO SUBJECT DATE CONTENT-TYPE CONTENT-DISPOSITION)])"
            ),
        )
    ]
    fetched_headers = fake_imap.instances[0].fetched_headers
    assert set(fetched_headers) == {1, 2, 3, 4, 5}
    for raw in fetched_headers.values():
        assert b"Received:" not in raw
        assert b"DKIM-Signature:" not in raw
        assert b"List-Id:" not in raw
        assert b"Subject:" in raw


def test_list_offset_limit_and_has_more(client, fake_imap, configured):
    first_page = client.get("/api/gmail/messages?limit=2").json()
    assert [item["uid"] for item in first_page["messages"]] == [1, 2]
    assert first_page["offset"] == 0
    assert first_page["limit"] == 2
    assert first_page["total"] == 5
    assert first_page["has_more"] is True

    second_page = client.get("/api/gmail/messages?offset=2&limit=2").json()
    assert [item["uid"] for item in second_page["messages"]] == [4, 3]
    assert second_page["offset"] == 2
    assert second_page["has_more"] is True

    last_page = client.get("/api/gmail/messages?offset=4&limit=2").json()
    assert [item["uid"] for item in last_page["messages"]] == [5]
    assert last_page["has_more"] is False

    beyond = client.get("/api/gmail/messages?offset=10&limit=2").json()
    assert beyond["messages"] == []
    assert beyond["total"] == 5
    assert beyond["has_more"] is False

    assert client.get("/api/gmail/messages?limit=0").status_code == 400
    assert client.get("/api/gmail/messages?limit=201").status_code == 400
    assert client.get("/api/gmail/messages?offset=-1").status_code == 400


def test_list_ignores_removed_unread_only_filter(client, fake_imap, configured):
    response = client.get("/api/gmail/messages?unread_only=true")

    assert response.status_code == 200
    assert [item["uid"] for item in response.json()["messages"]] == [1, 2, 4, 3, 5]
    calls = fake_imap.instances[0].calls
    assert ("SEARCH", (None, "ALL")) in calls
    assert not any(command == "SEARCH" and "UNSEEN" in args for command, args in calls)


def test_list_rejects_unsafe_folder(client, fake_imap, configured):
    response = client.get("/api/gmail/messages", params={"folder": "INBOX*"})

    assert response.status_code == 400


def test_filtered_headers_remove_unrequested_headers():
    raw = _build_messages()[1]
    filtered = _filter_header_fields(raw, gmail._LIST_FETCH_ITEM)

    assert b"Received:" not in filtered
    assert b"DKIM-Signature:" not in filtered
    assert b"List-Id:" not in filtered
    assert b"Subject:" in filtered
    assert b"Content-Type:" in filtered


@pytest.mark.parametrize("uid", [1, 2, 3, 4, 5])
def test_filtered_headers_parse_like_the_full_message(uid):
    raw = _build_messages()[uid]
    filtered = _filter_header_fields(raw, gmail._LIST_FETCH_ITEM)

    assert gmail.parse_summary(filtered) == gmail.parse_summary(raw)


def test_single_part_content_disposition_attachment_is_detected():
    raw = (
        b"From: sender@example.com\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-Disposition: attachment\r\n"
        b"\r\n"
        b"body\r\n"
    )

    summary = gmail.parse_summary(_filter_header_fields(raw, gmail._LIST_FETCH_ITEM))

    assert summary["has_attachments"] is True


def test_content_type_name_is_detected_as_attachment():
    raw = (
        b"From: sender@example.com\r\n"
        b'Content-Type: application/pdf; name="report.pdf"\r\n'
        b"\r\n"
        b"body\r\n"
    )

    summary = gmail.parse_summary(_filter_header_fields(raw, gmail._LIST_FETCH_ITEM))

    assert summary["has_attachments"] is True


def test_missing_headers_are_tolerated():
    raw = b"From: sender@example.com\r\n\r\nbody\r\n"

    summary = gmail.parse_summary(_filter_header_fields(raw, gmail._LIST_FETCH_ITEM))

    assert summary["from"] == "sender@example.com"
    assert summary["subject"] == ""
    assert summary["date_ts"] is None
    assert summary["has_attachments"] is False


def test_folded_and_duplicate_headers_match_the_full_message():
    raw = (
        b"From: sender@example.com\r\n"
        b"Subject: Hello\r\n"
        b" world\r\n"
        b"Subject: Second\r\n"
        b"Date: Fri, 23 Oct 2026 10:00:00 +0000\r\n"
        b"\r\n"
        b"body\r\n"
    )

    filtered = _filter_header_fields(raw, gmail._LIST_FETCH_ITEM)

    assert gmail.parse_summary(filtered) == gmail.parse_summary(raw)


def test_empty_filtered_literal_does_not_crash():
    raw = (
        b"Received: by mx.example.com\r\n"
        b"DKIM-Signature: v=1; d=example.com\r\n"
        b"List-Id: <list.example.com>\r\n"
        b"\r\n"
        b"body\r\n"
    )

    filtered = _filter_header_fields(raw, gmail._LIST_FETCH_ITEM)
    assert filtered == b"\r\n\r\n"

    summary = gmail.parse_summary(filtered)
    assert summary["from"] == ""
    assert summary["to"] == ""
    assert summary["subject"] == ""
    assert summary["date_ts"] is None
    assert summary["has_attachments"] is False


def test_second_list_is_a_cache_hit(client, fake_imap, configured):
    first = client.get("/api/gmail/messages")
    assert first.status_code == 200
    assert _search_count(fake_imap) == 1

    second = client.get("/api/gmail/messages")

    assert second.status_code == 200
    assert second.json() == first.json()
    assert _search_count(fake_imap) == 1
    assert len(fake_imap.instances) == 1


def test_cached_list_paginates_with_the_same_contract(client, fake_imap, configured):
    first_page = client.get("/api/gmail/messages?limit=2").json()
    second_page = client.get("/api/gmail/messages?offset=2&limit=2").json()

    assert [item["uid"] for item in first_page["messages"]] == [1, 2]
    assert [item["uid"] for item in second_page["messages"]] == [4, 3]
    assert second_page["offset"] == 2
    assert second_page["limit"] == 2
    assert second_page["total"] == 5
    assert second_page["has_more"] is True
    assert second_page["window"] == 200
    assert second_page["folder"] == "INBOX"
    assert _search_count(fake_imap) == 1


def test_mutating_a_hit_does_not_corrupt_the_cache(client, fake_imap, configured):
    first = client.get("/api/gmail/messages").json()
    first["messages"][0]["flags"].append("\\Draft")
    first["messages"][0]["subject"] = "Hacked"

    second = client.get("/api/gmail/messages").json()

    assert second["messages"][0]["flags"] == ["\\Seen"]
    assert second["messages"][0]["subject"] == "Hello"
    assert _search_count(fake_imap) == 1


def test_account_address_change_is_a_cache_miss(
    client, fake_imap, configured, monkeypatch
):
    assert client.get("/api/gmail/messages").status_code == 200
    monkeypatch.setenv("GMAIL_ADDRESS", "other@example.com")

    response = client.get("/api/gmail/messages")

    assert response.status_code == 200
    assert _search_count(fake_imap) == 2
    assert fake_imap.instances[-1].credentials[0] == "other@example.com"


def test_account_host_change_is_a_cache_miss(
    client, fake_imap, configured, monkeypatch
):
    assert client.get("/api/gmail/messages").status_code == 200
    monkeypatch.setenv("GMAIL_IMAP_HOST", "imap.example.com")

    assert client.get("/api/gmail/messages").status_code == 200

    assert _search_count(fake_imap) == 2
    assert fake_imap.instances[-1].host == "imap.example.com"


def test_account_port_change_is_a_cache_miss(
    client, fake_imap, configured, monkeypatch
):
    assert client.get("/api/gmail/messages").status_code == 200
    monkeypatch.setenv("GMAIL_IMAP_PORT", "1993")

    assert client.get("/api/gmail/messages").status_code == 200

    assert _search_count(fake_imap) == 2
    assert fake_imap.instances[-1].port == 1993


def test_delete_invalidates_every_cached_folder(client, fake_imap, configured):
    assert client.get("/api/gmail/messages").status_code == 200
    assert _search_count(fake_imap) == 1

    assert client.delete("/api/gmail/messages/3").status_code == 200
    assert _search_count(fake_imap) == 1

    assert client.get("/api/gmail/messages").status_code == 200
    assert _search_count(fake_imap) == 2


def test_bulk_delete_invalidates_every_cached_folder(client, fake_imap, configured):
    assert client.get("/api/gmail/messages").status_code == 200
    assert _search_count(fake_imap) == 1

    response = client.post("/api/gmail/messages/bulk-delete", json={"uids": [1, 3]})
    assert response.status_code == 200

    assert client.get("/api/gmail/messages").status_code == 200
    assert _search_count(fake_imap) == 2


def test_delete_in_another_folder_invalidates_the_cached_folder(
    client, fake_imap, configured
):
    assert client.get("/api/gmail/messages?folder=INBOX").status_code == 200
    assert client.get("/api/gmail/messages?folder=Archive").status_code == 200
    assert _search_count(fake_imap) == 2

    assert (
        client.delete("/api/gmail/messages/3", params={"folder": "Archive"}).status_code
        == 200
    )

    assert client.get("/api/gmail/messages?folder=INBOX").status_code == 200
    assert _search_count(fake_imap) == 3


@pytest.mark.parametrize("failed_command", ["COPY", "STORE", "EXPUNGE"])
def test_partial_delete_failure_still_invalidates(
    client, fake_imap, configured, monkeypatch, failed_command
):
    class FailingIMAP(FakeIMAP):
        def uid(self, command, *args):
            if command == failed_command:
                self.calls.append((command, args))
                return "NO", []
            return super().uid(command, *args)

        def expunge(self):
            if failed_command == "EXPUNGE":
                self.calls.append(("EXPUNGE", ()))
                return "NO", []
            return super().expunge()

    monkeypatch.setattr(gmail.imaplib, "IMAP4_SSL", FailingIMAP)

    assert client.get("/api/gmail/messages").status_code == 200
    assert _search_count(fake_imap) == 1

    assert client.delete("/api/gmail/messages/3").status_code == 502

    assert client.get("/api/gmail/messages").status_code == 200
    assert _search_count(fake_imap) == 2


def test_send_invalidates_the_cached_list(client, fake_imap, fake_smtp, configured):
    assert client.get("/api/gmail/messages").status_code == 200
    assert _search_count(fake_imap) == 1

    response = client.post(
        "/api/gmail/send",
        data={"to": "dest@example.com", "subject": "Ciao", "text": "Corpo"},
    )
    assert response.status_code == 200

    assert client.get("/api/gmail/messages").status_code == 200
    assert _search_count(fake_imap) == 2


def test_mark_read_updates_the_entry_in_place(client, fake_imap, configured):
    assert client.get("/api/gmail/messages").status_code == 200
    assert _search_count(fake_imap) == 1

    assert client.post("/api/gmail/messages/3/read").status_code == 200
    assert len(fake_imap.instances) == 2

    body = client.get("/api/gmail/messages").json()

    assert len(fake_imap.instances) == 2
    assert _search_count(fake_imap) == 1
    target = next(message for message in body["messages"] if message["uid"] == 3)
    assert target["seen"] is True
    assert target["flags"] == ["\\Seen"]


def test_mark_read_on_already_seen_message_keeps_the_entry(
    client, fake_imap, configured
):
    assert client.get("/api/gmail/messages").status_code == 200

    assert client.post("/api/gmail/messages/1/read").status_code == 200
    body = client.get("/api/gmail/messages").json()

    assert _search_count(fake_imap) == 1
    assert body["messages"][0]["uid"] == 1
    assert body["messages"][0]["seen"] is True


def test_mark_read_outside_the_window_keeps_the_entry(client, fake_imap, configured):
    assert client.get("/api/gmail/messages").status_code == 200

    assert client.post("/api/gmail/messages/99/read").status_code == 200
    assert client.get("/api/gmail/messages").status_code == 200

    assert _search_count(fake_imap) == 1


def test_mark_read_does_not_touch_another_folder_entry(client, fake_imap, configured):
    assert client.get("/api/gmail/messages?folder=INBOX").status_code == 200
    assert client.get("/api/gmail/messages?folder=Archive").status_code == 200

    assert (
        client.post(
            "/api/gmail/messages/3/read", params={"folder": "INBOX"}
        ).status_code
        == 200
    )

    archive = client.get("/api/gmail/messages?folder=Archive").json()

    assert len(fake_imap.instances) == 3
    assert _search_count(fake_imap) == 2
    target = next(message for message in archive["messages"] if message["uid"] == 3)
    assert target["seen"] is False


def test_mark_read_with_empty_cache_discards_the_in_flight_put(
    fake_imap, configured, monkeypatch
):
    class BlockingIMAP(FakeIMAP):
        search_started = threading.Event()
        release_search = threading.Event()

        def uid(self, command, *args):
            if command == "SEARCH":
                type(self).search_started.set()
                if not type(self).release_search.wait(5):
                    raise AssertionError("SEARCH was not released")
            return super().uid(command, *args)

    monkeypatch.setattr(gmail.imaplib, "IMAP4_SSL", BlockingIMAP)
    result: dict[str, dict] = {}

    def fetch():
        result["response"] = gmail.fetch_message_list()

    thread = threading.Thread(target=fetch)
    thread.start()
    assert BlockingIMAP.search_started.wait(5)

    gmail.mark_read(3)
    BlockingIMAP.release_search.set()
    thread.join(5)

    assert not thread.is_alive()
    assert "response" in result

    body = gmail.fetch_message_list()

    assert _search_count(fake_imap) == 2
    target = next(message for message in body["messages"] if message["uid"] == 3)
    assert target["seen"] is True


@pytest.mark.parametrize("value", ["1", "true"])
def test_refresh_forces_a_miss(client, fake_imap, configured, value):
    assert client.get("/api/gmail/messages").status_code == 200
    assert _search_count(fake_imap) == 1

    response = client.get("/api/gmail/messages", params={"refresh": value})

    assert response.status_code == 200
    assert _search_count(fake_imap) == 2

    assert client.get("/api/gmail/messages").status_code == 200
    assert _search_count(fake_imap) == 2


def test_refresh_zero_keeps_the_cache_hit(client, fake_imap, configured):
    assert client.get("/api/gmail/messages").status_code == 200

    assert client.get("/api/gmail/messages?refresh=0").status_code == 200

    assert _search_count(fake_imap) == 1


@pytest.mark.parametrize("value", ["2", "banana"])
def test_refresh_rejects_non_boolean_values(client, fake_imap, configured, value):
    response = client.get("/api/gmail/messages", params={"refresh": value})

    assert response.status_code == 422
    assert fake_imap.instances == []


def test_list_cache_expires_after_the_fixed_ttl(
    client, fake_imap, configured, monkeypatch
):
    clock = {"value": 1000.0}
    monkeypatch.setattr(gmail, "_now", lambda: clock["value"])

    assert client.get("/api/gmail/messages").status_code == 200
    clock["value"] += 14
    assert client.get("/api/gmail/messages").status_code == 200
    assert _search_count(fake_imap) == 1

    clock["value"] += 2
    assert client.get("/api/gmail/messages").status_code == 200
    assert _search_count(fake_imap) == 2


def test_clear_inbox_cache_forces_a_miss(client, fake_imap, configured):
    assert client.get("/api/gmail/messages").status_code == 200
    gmail.clear_inbox_cache()

    assert client.get("/api/gmail/messages").status_code == 200

    assert _search_count(fake_imap) == 2


def test_detail_prefers_plain_text_and_hides_payload(client, fake_imap, configured):
    response = client.get("/api/gmail/messages/1")

    assert response.status_code == 200
    body = response.json()
    assert body["uid"] == 1
    assert body["subject"] == "Hello"
    assert body["text"] == "original line\nsecond line"
    assert body["html"] == ""
    assert body["message_id"] == "<orig@example.com>"
    assert body["references"] == "<root@example.com>"
    assert [attachment["index"] for attachment in body["attachments"]] == [0, 1]
    assert body["attachments"][0]["filename"] == "evil_.pdf"
    assert body["attachments"][1]["filename"] == "città.txt"
    assert "data" not in body["attachments"][0]


def test_detail_html_only_is_text_and_sanitized_html(client, fake_imap, configured):
    response = client.get("/api/gmail/messages/2")

    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "HTML body"
    assert "<p>" not in body["text"]
    assert body["html"].strip() == "<p>HTML body</p>"


def test_detail_missing_uid_is_404(client, fake_imap, configured):
    response = client.get("/api/gmail/messages/99")

    assert response.status_code == 404


def test_attachment_download_is_deterministic(client, fake_imap, configured):
    response = client.get("/api/gmail/messages/1/attachments/0")

    assert response.status_code == 200
    assert response.content == b"%PDF-1.4\npdf"
    assert response.headers["content-type"] == "application/pdf"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "filename*=UTF-8''evil_.pdf" in disposition

    second = client.get("/api/gmail/messages/1/attachments/1")
    assert second.status_code == 200
    assert second.content == b"note text"
    assert "filename*=UTF-8''citt%C3%A0.txt" in second.headers["content-disposition"]


def test_attachment_out_of_range_is_404(client, fake_imap, configured):
    response = client.get("/api/gmail/messages/1/attachments/9")

    assert response.status_code == 404


def test_mark_read_sets_seen_flag(client, fake_imap, configured):
    response = client.post("/api/gmail/messages/3/read")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    connection = fake_imap.instances[0]
    assert ("STORE", ("3", "+FLAGS", "(\\Seen)")) in connection.calls
    assert connection.selected == ('"INBOX"', False)


def test_delete_moves_message_to_trash(client, fake_imap, configured):
    response = client.delete("/api/gmail/messages/3")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "trashed": True}
    connection = fake_imap.instances[0]
    assert connection.selected == ('"INBOX"', False)
    assert ("FETCH", ("3", "(FLAGS)")) in connection.calls
    assert ("LIST", ('""', '"*"')) in connection.calls
    assert ("COPY", ("3", '"[Gmail]/Trash"')) in connection.calls
    assert ("STORE", ("3", "+FLAGS", "(\\Deleted)")) in connection.calls
    assert connection.expunged is True
    assert ("EXPUNGE", ()) in connection.calls


def test_delete_missing_message_is_404(client, fake_imap, configured):
    response = client.delete("/api/gmail/messages/99")

    assert response.status_code == 404
    connection = fake_imap.instances[0]
    assert not any(
        command in {"COPY", "STORE", "EXPUNGE"} for command, _ in connection.calls
    )
    assert connection.expunged is False


def test_delete_rejects_unsafe_folder(client, fake_imap, configured):
    response = client.delete(
        "/api/gmail/messages/3", params={"folder": "[Gmail]/Trash"}
    )

    assert response.status_code == 400
    assert fake_imap.instances == []


def test_bulk_delete_moves_existing_messages_and_reports_missing(
    client, fake_imap, configured
):
    response = client.post(
        "/api/gmail/messages/bulk-delete",
        json={"uids": [1, 3, 99], "folder": "INBOX"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "trashed": 2, "missing": [99]}
    connection = fake_imap.instances[0]
    assert len(fake_imap.instances) == 1
    assert connection.selected == ('"INBOX"', False)
    assert ("FETCH", ("1,3,99", "(FLAGS)")) in connection.calls
    copy_calls = [args for command, args in connection.calls if command == "COPY"]
    assert copy_calls == [("1,3", '"[Gmail]/Trash"')]
    store_calls = [args for command, args in connection.calls if command == "STORE"]
    assert store_calls == [("1,3", "+FLAGS", "(\\Deleted)")]
    assert sum(command == "EXPUNGE" for command, _ in connection.calls) == 1
    assert connection.expunged is True


def test_bulk_delete_deduplicates_uids(client, fake_imap, configured):
    response = client.post(
        "/api/gmail/messages/bulk-delete", json={"uids": [3, 3, 1, 3]}
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "trashed": 2, "missing": []}
    connection = fake_imap.instances[0]
    assert connection.selected == ('"INBOX"', False)
    assert ("FETCH", ("3,1", "(FLAGS)")) in connection.calls
    copy_calls = [args for command, args in connection.calls if command == "COPY"]
    assert copy_calls == [("3,1", '"[Gmail]/Trash"')]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"uids": []},
        {"uids": "1,2"},
        {"uids": [1, "2"]},
        {"uids": [1, 2.5]},
        {"uids": [0]},
        {"uids": [-1]},
        {"uids": [True]},
        {"uids": list(range(1, 202))},
        {"uids": [1], "folder": 5},
    ],
)
def test_bulk_delete_rejects_invalid_payload(client, fake_imap, configured, payload):
    response = client.post("/api/gmail/messages/bulk-delete", json=payload)

    assert response.status_code == 400
    assert fake_imap.instances == []


def test_bulk_delete_rejects_invalid_json(client, fake_imap, configured):
    response = client.post(
        "/api/gmail/messages/bulk-delete",
        content=b"{not-json",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert fake_imap.instances == []


def test_bulk_delete_rejects_unsafe_folder(client, fake_imap, configured):
    response = client.post(
        "/api/gmail/messages/bulk-delete",
        json={"uids": [1], "folder": "[Gmail]/Trash"},
    )

    assert response.status_code == 400
    assert fake_imap.instances == []


def test_bulk_delete_all_missing_does_nothing(client, fake_imap, configured):
    response = client.post("/api/gmail/messages/bulk-delete", json={"uids": [98, 99]})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "trashed": 0, "missing": [98, 99]}
    connection = fake_imap.instances[0]
    assert ("FETCH", ("98,99", "(FLAGS)")) in connection.calls
    assert not any(
        command in {"COPY", "STORE", "EXPUNGE"} for command, _ in connection.calls
    )
    assert connection.expunged is False


def test_bulk_delete_not_configured_returns_503(client):
    response = client.post("/api/gmail/messages/bulk-delete", json={"uids": [1]})

    assert response.status_code == 503
    assert response.json()["detail"] == "Gmail non configurato"


def test_send_simple_email_uses_starttls_and_fixed_from(client, fake_smtp, configured):
    response = client.post(
        "/api/gmail/send",
        data={
            "to": "dest@example.com",
            "cc": "copy@example.com",
            "subject": "Ciao",
            "text": "Corpo",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["message_id"].startswith("<")
    assert body["message_id"].endswith(">")

    smtp = fake_smtp.instances[-1]
    assert smtp.starttls_called is True
    assert smtp.port == 587
    assert smtp.credentials == ("me@example.com", "app-secret")
    message = smtp.message
    assert message["From"] == "me@example.com"
    assert message["To"] == "dest@example.com"
    assert message["Cc"] == "copy@example.com"
    assert message["Subject"] == "Ciao"
    assert message.get_content().strip() == "Corpo"


def test_send_reply_sets_threading_headers_and_quotes_once(
    client, fake_imap, fake_smtp, configured
):
    response = client.post(
        "/api/gmail/send",
        data={"text": "Risposta", "reply_to_uid": "1"},
    )

    assert response.status_code == 200
    message = fake_smtp.instances[-1].message
    assert message["In-Reply-To"] == "<orig@example.com>"
    references = message["References"]
    assert "<root@example.com>" in references
    assert "<orig@example.com>" in references
    assert message["Subject"] == "Re: Hello"
    assert message["To"] == "mario@example.com"
    body = message.get_content()
    assert "Risposta" in body
    assert "> original line" in body
    assert "> second line" in body
    assert body.count("> original line") == 1


def test_send_reply_does_not_double_re_prefix(client, fake_imap, fake_smtp, configured):
    response = client.post(
        "/api/gmail/send",
        data={"text": "Risposta", "subject": "Re: Hello", "reply_to_uid": "1"},
    )

    assert response.status_code == 200
    assert fake_smtp.instances[-1].message["Subject"] == "Re: Hello"


def test_send_rejects_header_injection(client, fake_smtp, configured):
    response = client.post(
        "/api/gmail/send",
        data={
            "to": "dest@example.com\r\nBcc: attacker@example.com",
            "subject": "Ciao",
            "text": "Corpo",
        },
    )

    assert response.status_code == 400
    assert fake_smtp.instances == []


def test_send_rejects_missing_recipient(client, fake_smtp, configured):
    response = client.post("/api/gmail/send", data={"subject": "x", "text": "y"})

    assert response.status_code == 400


def test_send_rejects_too_many_attachments(client, fake_smtp, configured):
    files = [
        ("file[]", (f"file-{index}.txt", b"x", "text/plain")) for index in range(11)
    ]
    response = client.post(
        "/api/gmail/send",
        data={"to": "dest@example.com", "text": "Corpo"},
        files=files,
    )

    assert response.status_code == 400
    assert fake_smtp.instances == []


def test_send_rejects_oversized_attachment(client, fake_smtp, configured, monkeypatch):
    monkeypatch.setattr(gmail, "_MAX_ATTACHMENT_BYTES", 4)
    response = client.post(
        "/api/gmail/send",
        data={"to": "dest@example.com", "text": "Corpo"},
        files=[("file[]", ("big.txt", b"12345", "text/plain"))],
    )

    assert response.status_code == 413
    assert fake_smtp.instances == []


def test_send_with_attachment_sanitizes_filename(client, fake_smtp, configured):
    response = client.post(
        "/api/gmail/send",
        data={"to": "dest@example.com", "subject": "File", "text": "Corpo"},
        files=[("file[]", ("../../evil?.txt", b"hello", "text/plain"))],
    )

    assert response.status_code == 200
    message = fake_smtp.instances[-1].message
    attachments = list(message.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "evil_.txt"
    assert attachments[0].get_payload(decode=True) == b"hello"


def test_scenario_a_missing_secret_never_serves_a_warm_list(
    client, fake_imap, configured, monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG)
    assert client.get("/api/gmail/messages").status_code == 200
    assert _search_count(fake_imap) == 1

    monkeypatch.delenv("GMAIL_APP_PASSWORD")
    monkeypatch.setenv("GMAIL_APP_PASSWORD_CMD", "/bin/false")
    config.clear_gmail_secret_cache()

    status = client.get("/api/gmail/status")
    assert status.status_code == 200
    assert status.json()["configured"] is False

    response = client.get("/api/gmail/messages")
    assert response.status_code == 503
    assert _search_count(fake_imap) == 1
    assert "app-secret" not in caplog.text


def test_scenario_b_wrong_secret_serves_stale_then_502(
    client, fake_imap, configured, monkeypatch, caplog
):
    class AuthFailingIMAP(FakeIMAP):
        def login(self, user, password):
            if password != "app-secret":
                raise imaplib.IMAP4.error("AUTHENTICATIONFAILED wrong-secret")
            super().login(user, password)

    monkeypatch.setattr(gmail.imaplib, "IMAP4_SSL", AuthFailingIMAP)
    caplog.set_level(logging.DEBUG)

    assert client.get("/api/gmail/messages").status_code == 200
    assert _search_count(fake_imap) == 1

    monkeypatch.setenv("GMAIL_APP_PASSWORD", "wrong-secret")

    stale = client.get("/api/gmail/messages")
    assert stale.status_code == 200
    assert _search_count(fake_imap) == 1

    gmail.clear_inbox_cache()
    failed = client.get("/api/gmail/messages")

    assert failed.status_code == 502
    assert _search_count(fake_imap) == 1
    assert "wrong-secret" not in caplog.text
    assert "app-secret" not in caplog.text


def test_not_configured_returns_503(client):
    response = client.get("/api/gmail/messages")

    assert response.status_code == 503
    assert response.json()["detail"] == "Gmail non configurato"


def test_upstream_error_is_generic_and_does_not_leak(
    client, monkeypatch, configured, caplog
):
    class FailingIMAP(FakeIMAP):
        def uid(self, command, *args):
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED app-secret")

    monkeypatch.setattr(gmail.imaplib, "IMAP4_SSL", FailingIMAP)
    caplog.set_level(logging.DEBUG)

    response = client.get("/api/gmail/messages")

    assert response.status_code == 502
    assert "app-secret" not in response.text
    assert "AUTHENTICATIONFAILED" not in response.text
    assert "app-secret" not in caplog.text
