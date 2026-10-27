"""MIME/header/validation tests for ``server.gmail`` (no network)."""

from __future__ import annotations

import builtins
import email
import imaplib
from datetime import UTC, datetime
from email.message import EmailMessage

import pytest

from server import gmail


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
    # The attachment is text/plain with base64 CTE: the contract exposes the
    # encoded wire size (base64 "cHJpbW8=" normalised to CRLF = 10 bytes).
    assert first["attachments"][0]["size"] == 10
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
        "inline",
    }
    # base64("data") = "ZGF0YQ==" + line break -> encoded wire size 10.
    assert public[0]["size"] == 10


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


# ── P3b: BODYSTRUCTURE parser, walk indices and encoded sizes ───────────────


def _binary(*parts: bytes) -> bytes:
    return b"(" + b" ".join(parts) + b")"


def _text_node(*, octets: int = 1, subtype: bytes = b"plain") -> bytes:
    return (
        b'("text" "'
        + subtype
        + b'" ("charset" "utf-8") NIL NIL "7bit" '
        + str(octets).encode()
        + b" 1)"
    )


def _pdf_node(*, octets: int = 10) -> bytes:
    return (
        b'("application" "pdf" ("name" "in.pdf") NIL NIL "base64" '
        + str(octets).encode()
        + b' NIL ("attachment" ("filename" "in.pdf")))'
    )


def _attachment_structure(
    disposition_params: bytes | None,
    *,
    ctype_params: bytes = b"NIL",
) -> bytes:
    if disposition_params is None:
        disposition = b'("attachment")'
    else:
        disposition = b'("attachment" (' + disposition_params + b"))"
    return (
        b'("application" "pdf" '
        + ctype_params
        + b' NIL NIL "base64" 10 NIL '
        + disposition
        + b")"
    )


def test_bodystructure_parses_quotes_escapes_nil_and_numbers():
    block = (
        b'("text" "plain" ("charset" "utf-8" "name" "a\\"b\\\\c") NIL NIL "7bit" 42 3)'
    )

    root = gmail.parse_bodystructure(block)

    assert root is not None
    assert root.maintype == "text"
    assert root.subtype == "plain"
    assert root.params == (("charset", "utf-8"), ("name", 'a"b\\c'))
    assert root.content_id is None
    assert root.encoding == "7bit"
    assert root.octets == 42


def test_bodystructure_parses_nested_multipart():
    block = _binary(
        _text_node(),
        _binary(_text_node(subtype=b"html"), _pdf_node(), b'"alternative"'),
        b'"mixed"',
    )

    root = gmail.parse_bodystructure(block)

    assert root is not None
    assert root.maintype == "multipart"
    assert root.subtype == "mixed"
    assert len(root.children) == 2
    assert root.children[1].maintype == "multipart"
    assert root.children[1].subtype == "alternative"
    assert [child.subtype for child in root.children[1].children] == ["html", "pdf"]


@pytest.mark.parametrize(
    "block",
    [
        b'("text" "plain" ("name" {5}) NIL NIL "7bit" 1 1)',
        b'("text" "plain" NIL NIL NIL "7bit" {5} 1)',
        b'({5} "plain" NIL NIL NIL "7bit" 1 1)',
        b'("application" "pdf" NIL NIL NIL "base64" 1 NIL ("attachment" ("filename" {5})))',
    ],
    ids=["param", "octets", "maintype", "disposition-param"],
)
def test_bodystructure_rejects_literals_anywhere(block):
    assert gmail.parse_bodystructure(block) is None
    assert (
        gmail._extract_bodystructure(b"1 (UID 1 BODYSTRUCTURE " + block + b")") is None
    )


def test_extract_bodystructure_is_quote_aware_and_balanced():
    meta = (
        b"1 (UID 7 FLAGS () BODYSTRUCTURE "
        b'("text" "plain" ("name" "a)b\\"c") NIL NIL "7bit" 1 1) '
        b"BODY[HEADER.FIELDS (FROM)] {2}"
    )

    assert gmail._extract_bodystructure(meta) == (
        b'("text" "plain" ("name" "a)b\\"c") NIL NIL "7bit" 1 1)'
    )


@pytest.mark.parametrize(
    "meta",
    [
        b"1 (UID 1 FLAGS ())",
        b'1 (BODYSTRUCTURE "not-a-list")',
        b'1 (BODYSTRUCTURE ("text" "plain" NIL NIL NIL "7bit" 1 1',
        b"1 (BODYSTRUCTURE NIL)",
        b"1 (BODYSTRUCTURE)",
    ],
    ids=["missing", "not-list", "unbalanced", "nil", "empty"],
)
def test_extract_bodystructure_rejects_malformed_metadata(meta):
    assert gmail._extract_bodystructure(meta) is None


def test_extract_bodystructure_respects_the_byte_limit(monkeypatch):
    monkeypatch.setattr(gmail, "_MAX_BODYSTRUCTURE_BYTES", 10)
    meta = b'1 (BODYSTRUCTURE ("text" "plain" NIL NIL NIL "7bit" 1 1))'

    assert gmail._extract_bodystructure(meta) is None


def test_bodystructure_tokenizer_limits(monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(gmail, "_MAX_BODYSTRUCTURE_DEPTH", 2)
        with pytest.raises(gmail._BodystructureParseError):
            gmail._bodystructure_tokens(b'((("x")))')

    with monkeypatch.context() as patch:
        patch.setattr(gmail, "_MAX_BODYSTRUCTURE_TOKENS", 2)
        with pytest.raises(gmail._BodystructureParseError):
            gmail._bodystructure_tokens(b'("a" "b" "c")')

    with monkeypatch.context() as patch:
        patch.setattr(gmail, "_MAX_BODYSTRUCTURE_TOKEN_BYTES", 3)
        with pytest.raises(gmail._BodystructureParseError):
            gmail._bodystructure_tokens(b'("abcd")')

    with monkeypatch.context() as patch:
        patch.setattr(gmail, "_MAX_BODYSTRUCTURE_BYTES", 3)
        with pytest.raises(gmail._BodystructureParseError):
            gmail._bodystructure_tokens(b'("abcd")')


def test_parse_bodystructure_never_evaluates(monkeypatch):
    def fail_eval(*args, **kwargs):
        raise AssertionError("BODYSTRUCTURE parsing must not call eval()")

    monkeypatch.setattr(builtins, "eval", fail_eval)
    block = (
        b'("text" "plain" ("name" "__import__(\'os\').system(\'id\')") '
        b'NIL NIL "7bit" 1 1)'
    )

    root = gmail.parse_bodystructure(block)

    assert root is not None
    assert root.params[0][1] == "__import__('os').system('id')"


def test_index_bodystructure_single_part_root_uses_1():
    root = gmail.parse_bodystructure(_text_node(octets=4))
    assert root is not None

    index = gmail._index_bodystructure(root)

    assert [part.walk_index for part in index.parts] == ["1"]
    assert [part.walk_index for part in index.render_parts] == ["1"]
    assert index.attachments == ()


def test_index_bodystructure_nested_multipart_numbering():
    block = _binary(
        _text_node(),
        _binary(_text_node(subtype=b"html"), _text_node(), b'"alternative"'),
        b'"mixed"',
    )
    root = gmail.parse_bodystructure(block)
    assert root is not None

    index = gmail._index_bodystructure(root)

    assert [part.walk_index for part in index.parts] == ["1", "2.1", "2.2"]
    assert [part.walk_index for part in index.render_parts] == ["1", "2.1", "2.2"]
    assert index.attachments == ()


def test_index_bodystructure_forward_skips_the_rfc822_container():
    nested = _binary(_text_node(), _pdf_node(), b'"mixed"')
    message_node = (
        b'("message" "rfc822" NIL NIL NIL "7bit" 100 NIL '
        + nested
        + b' 2 NIL ("attachment"))'
    )
    block = _binary(_text_node(), message_node, b'"mixed"')
    root = gmail.parse_bodystructure(block)
    assert root is not None

    index = gmail._index_bodystructure(root)

    assert [part.walk_index for part in index.parts] == ["1", "2.1", "2.2"]
    assert [part.walk_index for part in index.render_parts] == ["1", "2.1"]
    assert [
        (part.walk_index, part.filename, part.attachment_index)
        for part in index.attachments
    ] == [("2.2", "in.pdf", 0)]


def test_index_bodystructure_root_rfc822_forward_numbers_nested_parts():
    nested = _binary(_text_node(), _pdf_node(), b'"mixed"')
    block = (
        b'("message" "rfc822" NIL NIL NIL "7bit" 100 NIL '
        + nested
        + b' 2 NIL ("attachment"))'
    )
    root = gmail.parse_bodystructure(block)
    assert root is not None

    index = gmail._index_bodystructure(root)

    assert [part.walk_index for part in index.parts] == ["1.1", "1.2"]
    assert [part.walk_index for part in index.render_parts] == ["1.1"]
    assert [(part.walk_index, part.filename) for part in index.attachments] == [
        ("1.2", "in.pdf")
    ]


@pytest.mark.parametrize(
    ("disposition_params", "expected"),
    [
        (b'"filename*" "utf-8\'\'caf%C3%A9.txt"', "café.txt"),
        (
            b'"filename*0*" "utf-8\'\'caf%C3%A9" "filename*1*" ".txt"',
            "café.txt",
        ),
        (b'"filename*" "utf-8\'\'a%20b%2Etxt"', "a b.txt"),
        (b'"filename" "=?utf-8?q?caf=C3=A9.txt?="', "café.txt"),
        (b'"filename" "plain.txt"', "plain.txt"),
        (
            b'"filename*0" "O\'Brien\'s report" "filename*1" ".pdf"',
            "O'Brien's report.pdf",
        ),
    ],
    ids=[
        "rfc2231",
        "continuations",
        "percent-decode",
        "rfc2047",
        "plain",
        "continuation-plain-apostrophes",
    ],
)
def test_attachment_filenames_decode_rfc2231_and_rfc2047(disposition_params, expected):
    root = gmail.parse_bodystructure(_attachment_structure(disposition_params))
    assert root is not None

    index = gmail._index_bodystructure(root)

    assert index.attachments[0].filename == expected


def test_disposition_filename_precedes_content_type_name():
    root = gmail.parse_bodystructure(
        _attachment_structure(
            b'"filename" "disp.pdf"',
            ctype_params=b'("name" "name.pdf")',
        )
    )
    assert root is not None

    index = gmail._index_bodystructure(root)

    assert index.attachments[0].filename == "disp.pdf"


def test_content_type_name_is_the_filename_fallback():
    root = gmail.parse_bodystructure(
        _attachment_structure(None, ctype_params=b'("name" "name.pdf")')
    )
    assert root is not None

    index = gmail._index_bodystructure(root)

    assert index.attachments[0].filename == "name.pdf"


def test_nil_filename_falls_back_to_the_content_type_name():
    root = gmail.parse_bodystructure(
        _attachment_structure(
            b'"filename" NIL',
            ctype_params=b'("name" "name.pdf")',
        )
    )
    assert root is not None

    index = gmail._index_bodystructure(root)

    assert index.attachments[0].filename == "name.pdf"


def test_attachment_without_a_filename_is_still_indexed():
    root = gmail.parse_bodystructure(_attachment_structure(None))
    assert root is not None

    index = gmail._index_bodystructure(root)

    assert index.attachments[0].filename is None
    assert index.attachments[0].attachment_index == 0


def test_parse_part_fetch_inherits_the_uid_from_the_first_tuple():
    data = [
        (b"1 (UID 7 BODY[1] {5}", b"hello"),
        (b" BODY[2] {3}", b"abc"),
        b")",
    ]

    sections = gmail._parse_part_fetch(data, 7, ["1", "2"])

    assert sections == {"1": b"hello", "2": b"abc"}


def test_parse_part_fetch_accepts_a_uid_on_the_trailer():
    data = [(b"1 (BODY[1] {5}", b"hello"), b") UID 7"]

    assert gmail._parse_part_fetch(data, 7, ["1"]) == {"1": b"hello"}


@pytest.mark.parametrize(
    "data",
    [
        [(b"1 (BODY[1] {5}", b"hello"), b")"],
        [(b"1 (UID 8 BODY[1] {5}", b"hello"), b")"],
        [(b"1 (UID 7 BODY[2] {5}", b"hello"), b")"],
        [
            (b"1 (UID 7 BODY[1] {5}", b"hello"),
            (b" BODY[1] {3}", b"abc"),
            b")",
        ],
        [(b"1 (UID 7 BODY[1] {5}", b"hello"), b")"],
    ],
    ids=["no-uid", "wrong-uid", "unexpected-label", "duplicate", "missing-part"],
)
def test_parse_part_fetch_rejects_inconsistent_responses(data):
    with pytest.raises(gmail._PartFetchError):
        gmail._parse_part_fetch(data, 7, ["1", "2"])


@pytest.mark.parametrize("expected", [[], ["0"], ["1.0"], ["x"], ["1..2"]])
def test_parse_part_fetch_rejects_invalid_part_numbers(expected):
    with pytest.raises(gmail._PartFetchError):
        gmail._parse_part_fetch([(b"1 (UID 7 BODY[1] {5}", b"hello")], 7, expected)


class _PartConnection:
    def __init__(self, status="NO", error: BaseException | None = None):
        self.status = status
        self.error = error
        self.calls: list[tuple] = []

    def uid(self, command, *args):
        self.calls.append((command, args))
        if self.error is not None:
            raise self.error
        return self.status, []


def test_fetch_part_payloads_wraps_status_errors_as_part_fetch_errors():
    with pytest.raises(gmail._PartFetchError):
        gmail._fetch_part_payloads(_PartConnection("NO"), 7, ["1"])

    with pytest.raises(gmail._PartFetchError):
        gmail._fetch_part_payloads(_PartConnection("BAD"), 7, ["1"])


@pytest.mark.parametrize(
    "error",
    [imaplib.IMAP4.abort("aborted"), TimeoutError("timeout"), OSError("reset")],
    ids=["abort", "timeout", "oserror"],
)
def test_fetch_part_payloads_propagates_transport_errors(error):
    with pytest.raises(type(error)):
        gmail._fetch_part_payloads(_PartConnection(error=error), 7, ["1"])


def test_fetch_part_payloads_rejects_invalid_parts_before_fetching():
    connection = _PartConnection(status="OK")

    with pytest.raises(gmail._PartFetchError):
        gmail._fetch_part_payloads(connection, 7, ["0"])

    assert connection.calls == []


def test_fetch_structure_returns_none_for_logical_errors():
    assert gmail._fetch_structure(_PartConnection("NO"), 7) is None
    assert gmail._fetch_structure(_PartConnection("BAD"), 7) is None


@pytest.mark.parametrize(
    "error",
    [imaplib.IMAP4.abort("aborted"), TimeoutError("timeout"), OSError("reset")],
    ids=["abort", "timeout", "oserror"],
)
def test_fetch_structure_propagates_transport_errors(error):
    with pytest.raises(type(error)):
        gmail._fetch_structure(_PartConnection(error=error), 7)


def _single_part(raw: bytes) -> email.message.Message:
    message = email.message_from_bytes(raw)
    return next(part for part in message.walk() if not part.is_multipart())


@pytest.mark.parametrize(
    ("cte", "payload", "expected"),
    [
        (b"base64", b"cHJpbW8=\r\n", 10),
        (b"base64", b"cHJpbW8=\n", 10),
        (b"quoted-printable", b"caf=C3=A9=20=E2=82=AC\r\n", 23),
        (b"quoted-printable", b"caf=C3=A9=20=E2=82=AC\n", 23),
        (b"7bit", b"hello", 5),
        (b"8bit", b"caf\xc3\xa9 \xe2\x82\xac", 9),
    ],
    ids=[
        "base64-crlf",
        "base64-lf",
        "qp-crlf",
        "qp-lf",
        "7bit",
        "8bit",
    ],
)
def test_encoded_size_matches_the_wire_transfer_encoding(cte, payload, expected):
    raw = (
        b'Content-Type: text/plain; charset="utf-8"\r\n'
        b"Content-Transfer-Encoding: " + cte + b"\r\n\r\n" + payload
    )

    assert gmail._encoded_size(_single_part(raw)) == expected


def test_encoded_size_is_none_for_an_unknown_transfer_encoding():
    raw = (
        b"Content-Type: text/plain\r\n"
        b"Content-Transfer-Encoding: x-unknown\r\n\r\n"
        b"payload"
    )

    assert gmail._encoded_size(_single_part(raw)) is None


def test_encoded_size_handles_a_missing_transfer_encoding_as_7bit():
    raw = b"Content-Type: text/plain\r\n\r\nhello"

    assert gmail._encoded_size(_single_part(raw)) == 5


# ── P3b: sanitized Gmail-style golden BODYSTRUCTURE fixtures ────────────────
#
# The repository has no captured real Gmail fixture (no user data is checked
# in), so these are sanitized, hand-authored Gmail-shaped blocks.  They keep
# the exact RFC 3501 field order used by Gmail: multipart/mixed, a nested
# multipart/alternative, a message/rfc822 forward and an inline cid image.

GMAIL_GOLDEN_BODYSTRUCTURES = {
    "mixed-alternative-pdf": (
        b'(("text" "plain" ("charset" "UTF-8") NIL NIL "7bit" 7 1)'
        b'(("text" "plain" ("charset" "UTF-8") NIL NIL "7bit" 7 1)'
        b'("text" "html" ("charset" "UTF-8") NIL NIL "7bit" 24 1)'
        b'"alternative" NIL)'
        b'("application" "pdf" ("name" "report.pdf") NIL NIL "base64" 8 NIL '
        b'("attachment" ("filename" "report.pdf"))) "mixed" '
        b'("boundary" "gmail-boundary"))'
    ),
    "inline-cid": (
        b'(("text" "plain" ("charset" "UTF-8") NIL NIL "7bit" 12 1)'
        b'("image" "png" ("name" "logo.png") "<cid@example.com>" NIL "base64" 10 NIL '
        b'("inline" ("filename" "logo.png"))) "related" '
        b'("boundary" "gmail-rel"))'
    ),
    "forward-pdf": (
        b'("message" "rfc822" NIL NIL NIL "7bit" 100 NIL '
        b'(("text" "plain" ("charset" "UTF-8") NIL NIL "7bit" 7 1)'
        b'("application" "pdf" ("name" "in.pdf") NIL NIL "base64" 10 NIL '
        b'("attachment" ("filename" "in.pdf"))) "mixed") 2 NIL ("attachment"))'
    ),
}


def test_golden_gmail_bodystructure_indices():
    mixed = gmail.parse_bodystructure(
        GMAIL_GOLDEN_BODYSTRUCTURES["mixed-alternative-pdf"]
    )
    assert mixed is not None
    mixed_index = gmail._index_bodystructure(mixed)
    assert [part.walk_index for part in mixed_index.parts] == ["1", "2.1", "2.2", "3"]
    assert [part.walk_index for part in mixed_index.render_parts] == [
        "1",
        "2.1",
        "2.2",
    ]
    assert [(part.walk_index, part.filename) for part in mixed_index.attachments] == [
        ("3", "report.pdf")
    ]

    inline = gmail.parse_bodystructure(GMAIL_GOLDEN_BODYSTRUCTURES["inline-cid"])
    assert inline is not None
    inline_index = gmail._index_bodystructure(inline)
    assert [
        (part.walk_index, part.content_id) for part in inline_index.attachments
    ] == [("2", "cid@example.com")]

    forward = gmail.parse_bodystructure(GMAIL_GOLDEN_BODYSTRUCTURES["forward-pdf"])
    assert forward is not None
    forward_index = gmail._index_bodystructure(forward)
    assert [part.walk_index for part in forward_index.parts] == ["1.1", "1.2"]
    assert [(part.walk_index, part.filename) for part in forward_index.attachments] == [
        ("1.2", "in.pdf")
    ]


def test_golden_gmail_base64_octets_match_encoded_size():
    raw = (
        b"Subject: golden\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="gmail-boundary"\r\n\r\n'
        b"--gmail-boundary\r\n"
        b'Content-Type: text/plain; charset="UTF-8"\r\n'
        b"Content-Transfer-Encoding: 7bit\r\n\r\n"
        b"hello\r\n"
        b"--gmail-boundary\r\n"
        b'Content-Type: application/pdf; name="report.pdf"\r\n'
        b"Content-Transfer-Encoding: base64\r\n"
        b'Content-Disposition: attachment; filename="report.pdf"\r\n\r\n'
        b"JVBERi0=\r\n"
        b"--gmail-boundary--\r\n"
    )
    message = email.message_from_bytes(raw)
    attachment = next(
        part for part in message.walk() if part.get_filename() == "report.pdf"
    )

    assert gmail._encoded_size(attachment) == 8

    root = gmail.parse_bodystructure(
        b'(("text" "plain" ("charset" "UTF-8") NIL NIL "7bit" 7 1)'
        b'("application" "pdf" ("name" "report.pdf") NIL NIL "base64" 8 NIL '
        b'("attachment" ("filename" "report.pdf"))) "mixed" '
        b'("boundary" "gmail-boundary"))'
    )
    assert root is not None
    index = gmail._index_bodystructure(root)
    parsed = gmail._parse_full(raw, index)

    assert parsed["attachments"][0]["size"] == 8


def test_attachment_filename_is_sanitized_by_the_partial_path():
    root = gmail.parse_bodystructure(
        _attachment_structure(b'"filename" "../../evil?.txt"')
    )
    assert root is not None
    index = gmail._index_bodystructure(root)
    part = index.attachments[0]

    assert part.filename == "../../evil?.txt"
    assert gmail._safe_part_filename(part, 0) == "evil_.txt"


def test_extract_bodystructure_only_rejects_syntactic_literals():
    meta = b'1 (BODYSTRUCTURE ("text" "plain" ("name" "{5}") NIL NIL "7bit" 1 1))'

    block = gmail._extract_bodystructure(meta)

    assert block is not None
    root = gmail.parse_bodystructure(block)
    assert root is not None
    assert root.params == (("name", "{5}"),)


def test_parse_structure_fetch_builds_headers_and_index():
    structure = b'("text" "plain" NIL NIL NIL "7bit" 4 1)'
    headers = b"From: sender@example.com\r\nSubject: hello\r\n\r\n"
    data = [
        (
            b"1 (UID 7 FLAGS () BODYSTRUCTURE "
            + structure
            + b" BODY[HEADER.FIELDS] {"
            + str(len(headers)).encode()
            + b"}",
            headers,
        ),
        b")",
    ]

    record = gmail._parse_structure_fetch(data, 7)

    assert record is not None
    assert record.uid == 7
    assert record.headers == headers
    assert [part.walk_index for part in record.index.parts] == ["1"]


def test_parse_structure_fetch_accepts_headers_in_a_later_tuple():
    structure = b'("text" "plain" NIL NIL NIL "7bit" 4 1)'
    headers = b"From: sender@example.com\r\n\r\n"
    data = [
        (b"1 (UID 7 BODYSTRUCTURE " + structure + b")", b""),
        (
            b" BODY[HEADER.FIELDS] {" + str(len(headers)).encode() + b"}",
            headers,
        ),
        b")",
    ]

    record = gmail._parse_structure_fetch(data, 7)

    assert record is not None
    assert record.headers == headers


@pytest.mark.parametrize(
    "data",
    [
        [
            (
                (
                    b'1 (UID 8 BODYSTRUCTURE ("text" "plain" NIL NIL NIL "7bit" 4 1) '
                    b"BODY[HEADER.FIELDS] {2}"
                ),
                b"\r\n",
            ),
            b")",
        ],
        [
            (
                b'1 (UID 7 BODYSTRUCTURE ("text" "plain" NIL NIL NIL "7bit" 4 1))',
                b"",
            ),
            b")",
        ],
        [
            (
                (
                    b'1 (UID 7 BODYSTRUCTURE ("text" "plain" ("n" {5}) NIL NIL "7bit" 4 1) '
                    b"BODY[HEADER.FIELDS] {2}"
                ),
                b"\r\n",
            ),
            b")",
        ],
        [(b"1 (UID 7 FLAGS ())", b""), b")"],
        [("not-bytes", b""), b")"],
    ],
    ids=["wrong-uid", "missing-headers", "literal", "missing-structure", "bad-item"],
)
def test_parse_structure_fetch_returns_none_for_logical_failures(data):
    assert gmail._parse_structure_fetch(data, 7) is None


def test_plain_filename_with_two_apostrophes_is_preserved():
    root = gmail.parse_bodystructure(
        _attachment_structure(b'"filename" "O\'Brien\'s report.pdf"')
    )
    assert root is not None

    index = gmail._index_bodystructure(root)

    assert index.attachments[0].filename == "O'Brien's report.pdf"


def test_attachment_size_is_null_when_the_transfer_encoding_is_unknown():
    raw = (
        b"Subject: unknown\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
        b"--b\r\nContent-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: x-unknown\r\n"
        b'Content-Disposition: attachment; filename="x.bin"\r\n\r\n'
        b"payload\r\n"
        b"--b--\r\n"
    )

    parsed = gmail.parse_message(raw)
    public = gmail.public_attachments(parsed["attachments"])

    assert public[0]["size"] is None
