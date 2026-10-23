"""API tests for ``/api/gmail/*`` with mocked IMAP/SMTP (no network)."""

from __future__ import annotations

import imaplib
import logging
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


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(create_gmail_router())
    return TestClient(app)


def _build_messages() -> dict[int, bytes]:
    messages: dict[int, bytes] = {}

    reply = EmailMessage()
    reply["From"] = "Mario Rossi <mario@example.com>"
    reply["To"] = "me@example.com"
    reply["Subject"] = "Hello"
    reply["Date"] = "Fri, 23 Oct 2026 10:00:00 +0000"
    reply["Message-ID"] = "<orig@example.com>"
    reply["References"] = "<root@example.com>"
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
    html.set_content("<p>HTML body</p>", subtype="html")
    messages[2] = html.as_bytes()

    for uid, subject in ((3, "Third"), (4, "Latest")):
        message = EmailMessage()
        message["From"] = f"Sender {uid} <sender{uid}@example.com>"
        message["To"] = "me@example.com"
        message["Subject"] = subject
        message["Date"] = f"Wed, 21 Oct 2026 1{uid}:00:00 +0000"
        message["Message-ID"] = f"<msg-{uid}@example.com>"
        message.set_content(f"{subject.lower()} body")
        messages[uid] = message.as_bytes()

    no_date = EmailMessage()
    no_date["From"] = "No Date <nodate@example.com>"
    no_date["To"] = "me@example.com"
    no_date["Subject"] = "No date"
    no_date["Message-ID"] = "<no-date@example.com>"
    no_date.set_content("no date body")
    messages[5] = no_date.as_bytes()

    return messages


class FakeIMAP:
    instances: ClassVar[list[FakeIMAP]] = []
    messages: ClassVar[dict[int, bytes]] = {}
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
                flags = "\\Seen" if uid == 1 else ""
                if "BODY.PEEK[HEADER]" in fetch_item:
                    raw = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
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
        if command in {"STORE", "COPY"}:
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
    # The list fetches headers only: no body, no attachments, no snippet.
    assert first["snippet"] == ""
    assert fake_imap.instances[0].selected == ('"INBOX"', True)
    calls = fake_imap.instances[0].calls
    assert ("SEARCH", (None, "ALL")) in calls
    assert not any(command == "SEARCH" and "UNSEEN" in args for command, args in calls)
    fetch_calls = [str(args) for command, args in calls if command == "FETCH"]
    assert fetch_calls
    assert all("BODY.PEEK[HEADER]" in call for call in fetch_calls)
    assert all("BODY.PEEK[]" not in call for call in fetch_calls)


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


def test_detail_prefers_plain_text_and_hides_payload(client, fake_imap, configured):
    response = client.get("/api/gmail/messages/1")

    assert response.status_code == 200
    body = response.json()
    assert body["uid"] == 1
    assert body["subject"] == "Hello"
    assert body["text"] == "original line\nsecond line"
    assert body["message_id"] == "<orig@example.com>"
    assert body["references"] == "<root@example.com>"
    assert [attachment["index"] for attachment in body["attachments"]] == [0, 1]
    assert body["attachments"][0]["filename"] == "evil_.pdf"
    assert body["attachments"][1]["filename"] == "città.txt"
    assert "data" not in body["attachments"][0]


def test_detail_html_only_is_text(client, fake_imap, configured):
    response = client.get("/api/gmail/messages/2")

    assert response.status_code == 200
    assert response.json()["text"] == "HTML body"
    assert "<p>" not in response.json()["text"]


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
