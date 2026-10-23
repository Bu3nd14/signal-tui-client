"""MIME/header/validation tests for ``web.gmail`` (no network)."""

from __future__ import annotations

from datetime import UTC, datetime
from email.message import EmailMessage

import pytest

from web import gmail


def _raw(message: EmailMessage) -> bytes:
    return message.as_bytes()


def _plain_message(body: str = "Ciao dal corpo") -> EmailMessage:
    message = EmailMessage()
    message["From"] = "Mario Rossi <mario@example.com>"
    message["To"] = "user@example.com"
    message["Subject"] = "Oggetto"
    message["Date"] = "Fri, 23 Oct 2026 10:00:00 +0000"
    message["Message-ID"] = "<orig@example.com>"
    message.set_content(body)
    return message


def test_parse_plain_message():
    parsed = gmail.parse_message(_raw(_plain_message()))

    assert parsed["from"] == "Mario Rossi <mario@example.com>"
    assert parsed["to"] == "user@example.com"
    assert parsed["subject"] == "Oggetto"
    assert parsed["message_id"] == "<orig@example.com>"
    assert parsed["text"] == "Ciao dal corpo"
    assert parsed["date_ts"] == int(datetime(2026, 10, 23, 10, tzinfo=UTC).timestamp())
    assert parsed["attachments"] == []


def test_parse_summary_uses_headers_without_body():
    raw = _raw(_plain_message())
    header_only = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"

    parsed = gmail.parse_summary(header_only)

    assert parsed["from"] == "Mario Rossi <mario@example.com>"
    assert parsed["subject"] == "Oggetto"
    assert parsed["snippet"] == ""
    assert parsed["has_attachments"] is False


def test_parse_summary_hints_multipart_attachments():
    message = _plain_message()
    message.add_attachment(
        b"data", maintype="application", subtype="pdf", filename="a.pdf"
    )

    parsed = gmail.parse_summary(_raw(message))

    assert parsed["has_attachments"] is True


def test_prefers_plain_text_over_html_alternative():
    message = _plain_message("versione testo")
    message.add_alternative("<p>versione <b>html</b></p>", subtype="html")

    parsed = gmail.parse_message(_raw(message))

    assert parsed["text"] == "versione testo"


def test_html_only_is_converted_to_text():
    message = EmailMessage()
    message["Subject"] = "HTML"
    message.set_content(
        "<p>Ciao <b>mondo</b></p><script>alert('x')</script><p>Linea 2</p>",
        subtype="html",
    )

    parsed = gmail.parse_message(_raw(message))

    assert "Ciao mondo" in parsed["text"]
    assert "Linea 2" in parsed["text"]
    assert "<" not in parsed["text"]
    assert "alert" not in parsed["text"]


def test_encoded_headers_are_decoded():
    raw = (
        b"From: =?UTF-8?Q?Mario_Rossi?= <mario@example.com>\r\n"
        b"To: user@example.com\r\n"
        b"Subject: =?utf-8?q?Ciao_=C3=A8?=\r\n"
        b"Date: Fri, 23 Oct 2026 10:00:00 +0000\r\n"
        b"\r\n"
        b"body\r\n"
    )

    parsed = gmail.parse_message(raw)

    assert parsed["from"] == "Mario Rossi <mario@example.com>"
    assert parsed["subject"] == "Ciao è"


def test_unknown_charset_header_does_not_crash():
    raw = (
        b"Subject: =?x-unknown?q?hello?=\r\n"
        b"Date: Fri, 23 Oct 2026 10:00:00 +0000\r\n"
        b"\r\n"
        b"body\r\n"
    )

    parsed = gmail.parse_message(raw)

    assert "hello" in parsed["subject"]


@pytest.mark.parametrize(
    "date_header,expected",
    [
        ("Fri, 23 Oct 2026 10:00:00 +0000", 1792749600),
        ("not a date", None),
        ("", None),
    ],
)
def test_date_parsing_is_tolerant(date_header, expected):
    message = _plain_message()
    del message["Date"]
    if date_header:
        message["Date"] = date_header

    parsed = gmail.parse_message(_raw(message))

    assert parsed["date_ts"] == expected


def test_attachments_have_deterministic_indices_and_sanitized_names():
    message = _plain_message("corpo")
    message.add_attachment(
        b"primo", maintype="text", subtype="plain", filename="../../evil?.txt"
    )
    message.add_attachment(
        b"secondo", maintype="application", subtype="pdf", filename="report.pdf"
    )
    attachments = message.get_payload()[1:]
    attachments[0]["Content-ID"] = "<cid-1@example.com>"

    first = gmail.parse_message(_raw(message))
    second = gmail.parse_message(_raw(message))

    assert first["text"] == "corpo"
    assert [item["index"] for item in first["attachments"]] == [0, 1]
    assert [item["index"] for item in second["attachments"]] == [0, 1]
    assert first["attachments"][0]["filename"] == "evil_.txt"
    assert first["attachments"][1]["filename"] == "report.pdf"
    assert first["attachments"][0]["size"] == len(b"primo")
    assert first["attachments"][0]["content_id"] == "cid-1@example.com"
    assert first["attachments"][1]["content_type"] == "application/pdf"


def test_inline_image_without_filename_is_indexed_with_content_id():
    message = EmailMessage()
    message.set_content("corpo")
    message.add_related(
        b"PNGDATA", maintype="image", subtype="png", cid="<img1@example.com>"
    )

    parsed = gmail.parse_message(_raw(message))

    assert len(parsed["attachments"]) == 1
    attachment = parsed["attachments"][0]
    assert attachment["content_type"] == "image/png"
    assert attachment["content_id"] == "img1@example.com"
    assert attachment["filename"].startswith("attachment-")
    assert parsed["text"] == "corpo"


def test_plain_text_attachment_is_not_used_as_body():
    message = _plain_message("corpo reale")
    message.add_attachment(
        b"allegato testuale",
        maintype="text",
        subtype="plain",
        filename="note.txt",
    )

    parsed = gmail.parse_message(_raw(message))

    assert parsed["text"] == "corpo reale"
    assert len(parsed["attachments"]) == 1


def test_public_attachments_strips_payload():
    message = _plain_message()
    message.add_attachment(b"data", maintype="text", subtype="plain", filename="a.txt")

    parsed = gmail.parse_message(_raw(message))
    public = gmail.public_attachments(parsed["attachments"])

    assert "data" not in public[0]
    assert set(public[0]) == {
        "index",
        "filename",
        "content_type",
        "size",
        "content_id",
    }


def test_append_quote_prefixes_every_line_once():
    quoted = gmail.append_quote("Risposta", "prima riga\nseconda riga")

    assert quoted == "Risposta\n\n> prima riga\n> seconda riga"
    assert quoted.count("> prima riga") == 1


def test_append_quote_without_user_text():
    assert gmail.append_quote("", "originale") == "> originale"


def test_validate_header_value_rejects_crlf():
    with pytest.raises(gmail.GmailValidationError):
        gmail.validate_header_value("ok\r\nBcc: attacker@example.com", "subject")
    with pytest.raises(gmail.GmailValidationError):
        gmail.validate_header_value("ok\nX-Evil: 1", "to")
    assert gmail.validate_header_value("  ciao  ", "subject") == "ciao"


@pytest.mark.parametrize(
    "folder",
    ["INBOX", "Archive", "Projects/2026", "My Folder", "a.b-c_d"],
)
def test_validate_folder_accepts_safe_names(folder):
    assert gmail.validate_folder(folder) == folder


@pytest.mark.parametrize(
    "folder",
    ["", "   ", "INBOX*", "100%", 'foo"bar', "..", "bad\\name", "[Gmail]/All Mail"],
)
def test_validate_folder_rejects_unsafe_names(folder):
    with pytest.raises(gmail.GmailValidationError):
        gmail.validate_folder(folder)


def test_validate_folder_uses_configured_default(monkeypatch):
    monkeypatch.setattr(gmail.config, "get_gmail_folder", lambda: "Archive")
    assert gmail.validate_folder(None) == "Archive"


def test_parse_fetch_data_handles_flags_only_bytes():
    records = gmail._parse_fetch_data(
        [
            b"3 (UID 3 FLAGS (\\Seen))",
            b"4 (UID 4 FLAGS ())",
        ]
    )

    assert [record["uid"] for record in records] == [3, 4]
    assert records[0]["flags"] == {"\\Seen"}
    assert records[1]["flags"] == set()


class _ListConnection:
    def __init__(self, data, status="OK"):
        self.data = data
        self.status = status
        self.calls = []

    def list(self, directory='""', pattern="*"):
        self.calls.append((directory, pattern))
        return self.status, self.data


@pytest.mark.parametrize(
    "data,expected",
    [
        (
            [
                b'(\\HasNoChildren \\Inbox) "/" "INBOX"',
                b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"',
            ],
            "[Gmail]/Trash",
        ),
        ([b'(\\HasNoChildren \\Trash) "/" "[GoogleMail]/Trash"'], "[GoogleMail]/Trash"),
        ([b'(\\HasNoChildren \\Trash) "/" "Il mio cestino"'], "Il mio cestino"),
        (
            [b'(\\HasNoChildren \\Trash) "/" "Cestino \\"speciale\\""'],
            'Cestino "speciale"',
        ),
        ([b'(\\HasNoChildren \\trash) "/" "Trash"'], "Trash"),
        ([b'(\\HasNoChildren \\Inbox) "/" "INBOX"'], "[Gmail]/Trash"),
        ([], "[Gmail]/Trash"),
    ],
)
def test_find_trash_mailbox_is_robust(data, expected):
    connection = _ListConnection(data)

    assert gmail._find_trash_mailbox(connection) == expected
    assert connection.calls == [('""', '"*"')]


def test_find_trash_mailbox_falls_back_on_list_error():
    connection = _ListConnection([], status="NO")

    assert gmail._find_trash_mailbox(connection) == "[Gmail]/Trash"
