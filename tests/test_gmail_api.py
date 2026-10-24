"""API tests for ``/api/gmail/*`` with mocked IMAP/SMTP (no network)."""

from __future__ import annotations

import base64
import email
import email.policy
import imaplib
import logging
import re
import socket
import threading
import time
from contextlib import ExitStack
from email.message import EmailMessage
from typing import ClassVar
from urllib.parse import quote

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


# ── P3b: synthetic BODYSTRUCTURE corpus and fake IMAP parts ─────────────────

P3B_SIMPLE = 101
P3B_ALTERNATIVE = 102
P3B_MIXED = 103
P3B_INLINE = 104
P3B_LATIN = 105
P3B_BASE64_TEXT = 106
P3B_MALFORMED = 107
P3B_FORWARD = 108
P3B_LARGE = 109
P3B_BIG_7BIT = 110
P3B_BIG_BASE64 = 111
P3B_BIG_QP = 112
P3B_UNKNOWN_CTE = 113
P3B_NIL_OCTETS = 114
P3B_FALLBACK_OCTETS = 115
P3B_PRIMO = 116
P3B_HTML_ONLY = 117
P3B_ATTACHMENT_ONLY = 118


def _wire_bytes(message: EmailMessage) -> bytes:
    return message.as_bytes(policy=email.policy.SMTP)


def _quote_imap(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _imap_param(name: str, value: str) -> str:
    if all(ord(character) < 128 for character in value):
        return f"{_quote_imap(name)} {_quote_imap(value)}"
    # Gmail emits RFC 2231 for non-ASCII names instead of raw 8-bit bytes.
    encoded = "utf-8''" + quote(value, safe="")
    return f"{_quote_imap(name + '*')} {_quote_imap(encoded)}"


def _content_type_params(part) -> str:
    params: list[str] = []
    boundary = part.get_boundary()
    if boundary:
        params.append(_imap_param("boundary", boundary))
    charset = part.get_content_charset()
    if charset:
        params.append(_imap_param("charset", charset))
    name = part.get_param("name")
    if isinstance(name, str) and name:
        params.append(_imap_param("name", name))
    return "(" + " ".join(params) + ")" if params else "NIL"


def _disposition(part) -> str | None:
    disposition = part.get_content_disposition()
    if not disposition:
        return None
    filename = part.get_filename()
    params = f"({_imap_param('filename', filename)})" if filename else "NIL"
    return f"({_quote_imap(disposition)} {params})"


def _wire_part_payload(part) -> bytes:
    if part.get_content_type() == "message/rfc822":
        return _wire_bytes(part.get_payload()[0])
    cte = (part.get("Content-Transfer-Encoding") or "").strip().lower()
    if cte in ("base64", "quoted-printable"):
        payload = part.get_payload(decode=False)
        if isinstance(payload, bytes):
            return payload
        return payload.encode("ascii")
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        if isinstance(raw, str):
            return raw.encode("utf-8", errors="replace")
        return b""
    if isinstance(payload, str):
        return payload.encode("utf-8", errors="replace")
    return bytes(payload)


def _bodystructure_node(part) -> str:
    maintype = part.get_content_maintype()
    subtype = part.get_content_subtype()
    if maintype == "multipart":
        children = " ".join(_bodystructure_node(child) for child in part.get_payload())
        fields = f"{children} {_quote_imap(subtype)} {_content_type_params(part)}"
        disposition = _disposition(part)
        if disposition:
            fields += f" {disposition}"
        return f"({fields})"

    payload = _wire_part_payload(part)
    line_count = payload.count(b"\n")
    content_id = part.get("Content-ID")
    description = part.get("Content-Description")
    encoding = part.get("Content-Transfer-Encoding")
    fields = (
        f"{_quote_imap(maintype)} {_quote_imap(subtype)} "
        f"{_content_type_params(part)} "
        f"{_quote_imap(content_id) if content_id else 'NIL'} "
        f"{_quote_imap(description) if description else 'NIL'} "
        f"{_quote_imap(encoding) if encoding else 'NIL'} {len(payload)}"
    )
    if maintype == "text":
        fields += f" {line_count}"
    elif part.get_content_type() == "message/rfc822":
        nested = part.get_payload()[0]
        fields += f" NIL {_bodystructure_node(nested)} {line_count}"
    disposition = _disposition(part)
    if disposition:
        fields += f" NIL {disposition}"
    return f"({fields})"


def _bodystructure_from_raw(raw: bytes) -> bytes:
    return _bodystructure_node(email.message_from_bytes(raw)).encode("latin-1")


def _leaf_part_payloads(message) -> dict[str, bytes]:
    payloads: dict[str, bytes] = {}

    def descend(part, index: str) -> None:
        if part.get_content_maintype() == "multipart":
            for position, child in enumerate(part.get_payload(), 1):
                descend(child, f"{index}.{position}" if index else str(position))
            return
        if part.get_content_type() == "message/rfc822":
            nested = part.get_payload()[0]
            base = index or "1"
            if nested.get_content_maintype() == "multipart":
                for position, child in enumerate(nested.get_payload(), 1):
                    descend(child, f"{base}.{position}")
            else:
                descend(nested, f"{base}.1")
            return
        payloads[index or "1"] = _wire_part_payload(part)

    descend(message, "")
    return payloads


def _p3b_message(uid: int, subject: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = "Mario Rossi <mario@example.com>"
    message["To"] = "me@example.com"
    message["Cc"] = "cc@example.com"
    message["Subject"] = subject
    message["Date"] = "Fri, 23 Oct 2026 10:00:00 +0000"
    message["Message-ID"] = f"<p3b-{uid}@example.com>"
    message["In-Reply-To"] = "<parent@example.com>"
    message["References"] = "<root@example.com>"
    return message


def _raw_multipart(*, subject: str, text_body: bytes, attachment: bytes) -> bytes:
    return (
        f"Subject: {subject}\r\n".encode()
        + b"MIME-Version: 1.0\r\n"
        + b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
        + b"--b\r\nContent-Type: text/plain\r\n\r\n"
        + text_body
        + b"\r\n--b\r\n"
        + attachment
        + b"--b--\r\n"
    )


def _build_p3b_corpus() -> dict[int, bytes]:
    corpus: dict[int, bytes] = {}

    simple = _p3b_message(P3B_SIMPLE, "Simple")
    simple.set_content("simple body")
    corpus[P3B_SIMPLE] = _wire_bytes(simple)

    alternative = _p3b_message(P3B_ALTERNATIVE, "Alternative")
    alternative.set_content("plain body")
    alternative.add_alternative("<p>html body</p>", subtype="html")
    corpus[P3B_ALTERNATIVE] = _wire_bytes(alternative)

    mixed = _p3b_message(P3B_MIXED, "Mixed")
    mixed.set_content("mixed body")
    mixed.add_attachment(
        b"%PDF-1.4\r\npdf",
        maintype="application",
        subtype="pdf",
        filename="report.pdf",
    )
    mixed.add_attachment(
        b"note\r\ntext",
        maintype="text",
        subtype="plain",
        filename="città.txt",
    )
    mixed.add_attachment(
        b"evil",
        maintype="text",
        subtype="plain",
        filename="../../evil?.txt",
    )
    corpus[P3B_MIXED] = _wire_bytes(mixed)

    inline = _p3b_message(P3B_INLINE, "Inline")
    inline.set_content("inline body")
    inline.add_related(
        b"PNGDATA", maintype="image", subtype="png", cid="<cid@example.com>"
    )
    corpus[P3B_INLINE] = _wire_bytes(inline)

    latin = _p3b_message(P3B_LATIN, "Latin")
    latin.set_content("café €\r\nsecond", charset="iso-8859-15")
    corpus[P3B_LATIN] = _wire_bytes(latin)

    html = _p3b_message(P3B_HTML_ONLY, "HTML only")
    html.set_content(
        "<p>HTML body</p><script>alert('x')</script><p>line 2</p>",
        subtype="html",
    )
    corpus[P3B_HTML_ONLY] = _wire_bytes(html)

    corpus[P3B_BASE64_TEXT] = (
        b"Subject: Base64 body\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: text/plain; charset="utf-8"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        b"dGV4dCBiYXNlNjQNCmJvZHk=\r\n"
    )

    corpus[P3B_MALFORMED] = (
        b"Subject: Malformed\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
        b"--b\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
        b"--b\r\nContent-Type: application/pdf\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b'Content-Disposition: attachment; filename="bad.pdf"\r\n\r\n'
        b"!!!not-base64!!!\r\n"
        b"--b--\r\n"
    )

    inner = _p3b_message(0, "Inner")
    inner.set_content("inner body")
    inner.add_attachment(
        b"%PDF-1.4\r\npdf", maintype="application", subtype="pdf", filename="in.pdf"
    )
    forward = _p3b_message(P3B_FORWARD, "Forward")
    forward.set_content("outer body")
    forward.add_attachment(inner)
    corpus[P3B_FORWARD] = _wire_bytes(forward)

    large = _p3b_message(P3B_LARGE, "Large")
    large.set_content("large body")
    large.add_attachment(
        b"X" * 131072,
        maintype="application",
        subtype="pdf",
        filename="large.pdf",
    )
    corpus[P3B_LARGE] = _wire_bytes(large)

    corpus[P3B_BIG_7BIT] = _raw_multipart(
        subject="Big 7bit",
        text_body=b"body",
        attachment=(
            b"Content-Type: application/octet-stream\r\n"
            b"Content-Transfer-Encoding: 7bit\r\n"
            b'Content-Disposition: attachment; filename="big.bin"\r\n\r\n'
            + b"X" * 20
            + b"\r\n"
        ),
    )

    corpus[P3B_BIG_BASE64] = _raw_multipart(
        subject="Big base64",
        text_body=b"body",
        attachment=(
            b"Content-Type: application/octet-stream\r\n"
            b"Content-Transfer-Encoding: base64\r\n"
            b'Content-Disposition: attachment; filename="big.b64"\r\n\r\n'
            + base64.b64encode(b"Y" * 20)
            + b"\r\n"
        ),
    )

    corpus[P3B_BIG_QP] = _raw_multipart(
        subject="Big quoted-printable",
        text_body=b"body",
        attachment=(
            b"Content-Type: application/octet-stream\r\n"
            b"Content-Transfer-Encoding: quoted-printable\r\n"
            b'Content-Disposition: attachment; filename="big.qp"\r\n\r\n'
            + b"Y" * 20
            + b"\r\n"
        ),
    )

    corpus[P3B_UNKNOWN_CTE] = _raw_multipart(
        subject="Unknown encoding",
        text_body=b"body",
        attachment=(
            b"Content-Type: application/octet-stream\r\n"
            b"Content-Transfer-Encoding: x-unknown\r\n"
            b'Content-Disposition: attachment; filename="unknown.bin"\r\n\r\n'
            + b"Z" * 20
            + b"\r\n"
        ),
    )

    corpus[P3B_ATTACHMENT_ONLY] = (
        b"Subject: Attachment only\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
        b"--b\r\nContent-Type: application/pdf\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b'Content-Disposition: attachment; filename="only.pdf"\r\n\r\n'
        b"JVBERi0=\r\n"
        b"--b--\r\n"
    )

    for uid, subject in (
        (P3B_NIL_OCTETS, "Nil octets"),
        (P3B_FALLBACK_OCTETS, "Fallback octets"),
        (P3B_PRIMO, "Primo"),
    ):
        message = _p3b_message(uid, subject)
        message.set_content("body")
        message.add_attachment(
            b"primo",
            maintype="application",
            subtype="pdf",
            filename="primo.pdf",
        )
        corpus[uid] = _wire_bytes(message)

    return corpus


NIL_OCTETS_STRUCTURE = (
    b'(("text" "plain" ("charset" "utf-8") NIL NIL "7bit" NIL 1)'
    b'("application" "pdf" ("name" "primo.pdf") NIL NIL "base64" NIL NIL '
    b'("attachment" ("filename" "primo.pdf"))) "mixed")'
)

FALLBACK_OCTETS_STRUCTURE = (
    b'(("text" "plain" ("charset" "utf-8") NIL NIL "7bit" NIL 1)'
    b'("application" "pdf" ("name" "primo.pdf") NIL NIL "base64" 999 NIL '
    b'("attachment" ("filename" "primo.pdf"))) "mixed")'
)

NIL_7BIT_STRUCTURE = (
    b'(("text" "plain" ("charset" "utf-8") NIL NIL "7bit" NIL 1)'
    b'("application" "octet-stream" ("name" "big.bin") NIL NIL "7bit" NIL NIL '
    b'("attachment" ("filename" "big.bin"))) "mixed")'
)


def _plain_big_structure(cte: str) -> bytes:
    return (
        b'(("text" "plain" ("charset" "utf-8") NIL NIL "7bit" NIL 1)'
        b'("application" "pdf" ("name" "primo.pdf") NIL NIL "'
        + cte.encode()
        + b'" 999 NIL ("attachment" ("filename" "primo.pdf"))) "mixed")'
    )


class FakeSocket:
    """Minimal socket stand-in used by the IMAP pool close/NOOP paths.

    ``logout`` can block on ``close_event`` so tests can emulate an
    ``imaplib`` logout that is unblocked by ``sock.close()``, or that is
    bounded by the ``settimeout`` value installed by ``_close_conn``.
    """

    def __init__(self) -> None:
        self.timeout: float | None = None
        self.timeouts: list[float | None] = []
        self.shutdown_calls: list[int] = []
        self.closed = False
        self.close_event = threading.Event()

    def settimeout(self, value: float | None) -> None:
        self.timeout = value
        self.timeouts.append(value)

    def gettimeout(self) -> float | None:
        return self.timeout

    def shutdown(self, how: int) -> None:
        self.shutdown_calls.append(how)

    def close(self) -> None:
        self.closed = True
        self.close_event.set()

    def wait_closed(self, timeout: float | None = None) -> bool:
        return self.close_event.wait(timeout)


class FakeIMAP:
    instances: ClassVar[list[FakeIMAP]] = []
    messages: ClassVar[dict[int, bytes]] = {}
    bodystructures: ClassVar[dict[int, bytes]] = {}
    bodystructure_status: ClassVar[dict[int, str]] = {}
    part_fetch_status: ClassVar[dict[int, str]] = {}
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
        self.logged_in = False
        self.login_attempts = 0
        self.noops = 0
        self.shutdown_called = False
        self.closed = False
        self.expunged = False
        self.fetched_headers: dict[int, bytes] = {}
        self.structure_fetches = 0
        self.full_body_fetches = 0
        self.full_body_bytes = 0
        self.part_fetch_calls: list[tuple[int, tuple[str, ...]]] = []
        self.part_bytes = 0
        self.sock = FakeSocket()
        self.messages = type(self).messages
        FakeIMAP.instances.append(self)

    def login(self, user, password):
        self.login_attempts += 1
        self.credentials = (user, password)
        self.logged_in = True

    def noop(self):
        self.noops += 1
        return "OK", [b""]

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
                if "BODYSTRUCTURE" in fetch_item:
                    status = type(self).bodystructure_status.get(uid)
                    if status:
                        return status, []
                    self.structure_fetches += 1
                    header = _filter_header_fields(raw, fetch_item)
                    self.fetched_headers[uid] = header
                    structure = type(self).bodystructures.get(uid)
                    if structure is None:
                        meta = (
                            f"{uid} (UID {uid} FLAGS ({flags}) "
                            f"BODY[HEADER.FIELDS] {{{len(header)}}}"
                        ).encode()
                        payload.extend([(meta, header), b")"])
                    else:
                        meta = (
                            f"{uid} (UID {uid} FLAGS ({flags}) BODYSTRUCTURE ".encode()
                            + structure
                            + f" BODY[HEADER.FIELDS] {{{len(header)}}}".encode()
                        )
                        payload.extend([(meta, header), b")"])
                elif "HEADER.FIELDS" in fetch_item:
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
                    self.full_body_fetches += 1
                    self.full_body_bytes += len(raw)
                    meta = (
                        f"{uid} (UID {uid} FLAGS ({flags}) BODY[] {{{len(raw)}}}"
                    ).encode()
                    payload.extend([(meta, raw), b")"])
                else:
                    labels = tuple(
                        label
                        for label in re.findall(r"BODY\.PEEK\[([^\]]*)\]", fetch_item)
                        if label and not label.upper().startswith("HEADER")
                    )
                    if labels:
                        self.part_fetch_calls.append((uid, labels))
                        status = type(self).part_fetch_status.get(uid)
                        if status:
                            return status, []
                        part_payloads = _leaf_part_payloads(
                            email.message_from_bytes(raw)
                        )
                        for offset, label in enumerate(labels):
                            literal = part_payloads.get(label)
                            if literal is None:
                                return "NO", []
                            self.part_bytes += len(literal)
                            if offset == 0:
                                meta = (
                                    f"{uid} (UID {uid} BODY[{label}] {{{len(literal)}}}"
                                ).encode()
                            else:
                                meta = f" BODY[{label}] {{{len(literal)}}}".encode()
                            payload.append((meta, literal))
                        payload.append(b")")
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

    def shutdown(self):
        self.shutdown_called = True
        self.sock.shutdown(socket.SHUT_RDWR)

    def close(self):
        self.closed = True
        self.sock.close()


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
    FakeIMAP.bodystructures = {}
    FakeIMAP.bodystructure_status = {}
    FakeIMAP.part_fetch_status = {}
    FakeIMAP.seen_uids = set()
    FakeIMAP.list_data = [
        b'(\\HasNoChildren \\Inbox) "/" "INBOX"',
        b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"',
    ]
    monkeypatch.setattr(gmail.imaplib, "IMAP4_SSL", FakeIMAP)
    return FakeIMAP


@pytest.fixture
def p3b_corpus(fake_imap):
    """Install the synthetic CRLF corpus and its generated BODYSTRUCTUREs."""
    corpus = _build_p3b_corpus()
    FakeIMAP.instances = []
    FakeIMAP.messages = corpus
    FakeIMAP.bodystructures = {
        uid: _bodystructure_from_raw(raw) for uid, raw in corpus.items()
    }
    FakeIMAP.bodystructure_status = {}
    FakeIMAP.part_fetch_status = {}
    return corpus


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


def _login_count(fake_imap) -> int:
    """Number of successful logins across every fake connection."""
    return sum(1 for instance in fake_imap.instances if instance.logged_in)


def _noop_count(fake_imap) -> int:
    return sum(instance.noops for instance in fake_imap.instances)


def _make_settings(**overrides):
    values = {
        "address": "me@example.com",
        "password": "app-secret",
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "folder": "INBOX",
        "timeout_s": 30,
    }
    values.update(overrides)
    return gmail.GmailSettings(**values)


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
    # The pooled connection is reused: one login, one connection.
    assert _login_count(fake_imap) == 1
    assert len(fake_imap.instances) == 1

    body = client.get("/api/gmail/messages").json()

    assert _login_count(fake_imap) == 1
    assert len(fake_imap.instances) == 1
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

    # INBOX list, Archive list and mark_read all share the pooled connection.
    assert _login_count(fake_imap) == 1
    assert len(fake_imap.instances) == 1
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


# ── P2: IMAP connection pool ────────────────────────────────────────────────


def _pool_state() -> tuple[int, int]:
    with gmail._pool_cond:
        return len(gmail._pool_idle), len(gmail._pool_active)


def test_imap_pool_reuses_one_login_for_two_operations(fake_imap):
    settings = _make_settings()

    with gmail.imap_connect(settings) as first:
        assert first.logged_in is True
    with gmail.imap_connect(settings) as second:
        assert second is first
        status, _ = second.noop()
        assert status == "OK"

    assert len(fake_imap.instances) == 1
    assert _login_count(fake_imap) == 1
    assert _pool_state() == (1, 0)
    # A warm acquire within the NOOP threshold must not send NOOP.
    assert _noop_count(fake_imap) == 1  # the explicit noop above only


def test_imap_pool_never_exceeds_max_with_four_idle_and_five_requests(
    fake_imap, monkeypatch
):
    settings = _make_settings()
    monkeypatch.setattr(gmail, "_IMAP_POOL_ACQUIRE_TIMEOUT_S", 10.0)

    # Build four idle connections.
    with ExitStack() as stack:
        held = [stack.enter_context(gmail.imap_connect(settings)) for _ in range(4)]
    assert len(held) == 4
    assert _pool_state() == (4, 0)
    assert _login_count(fake_imap) == 4

    barrier = threading.Barrier(5)
    errors: list[BaseException] = []
    used: list[object] = []

    def request() -> None:
        try:
            barrier.wait(5)
            with gmail.imap_connect(settings) as connection:
                used.append(connection)
                time.sleep(0.05)
        except BaseException as exc:  # noqa: BLE001 - surfaced in the main thread
            errors.append(exc)

    threads = [threading.Thread(target=request) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert len(used) == 5
    assert {id(connection) for connection in used} == set(map(id, held))
    assert len(fake_imap.instances) == 4
    assert _login_count(fake_imap) == 4
    assert _pool_state() == (4, 0)


def test_imap_pool_acquire_timeout_is_busy_and_keeps_the_slot(fake_imap, monkeypatch):
    settings = _make_settings()
    monkeypatch.setattr(gmail, "_IMAP_POOL_MAX_CONNECTIONS", 1)
    monkeypatch.setattr(gmail, "_IMAP_POOL_ACQUIRE_TIMEOUT_S", 0.05)

    with gmail.imap_connect(settings) as held:
        with (
            pytest.raises(gmail.GmailUpstreamError, match="busy"),
            gmail.imap_connect(settings),
        ):
            pytest.fail("a fifth connection must not be opened")
        assert _pool_state() == (0, 1)
        assert len(fake_imap.instances) == 1

    # Releasing the held connection restores the single slot.
    assert _pool_state() == (1, 0)
    with gmail.imap_connect(settings) as reused:
        assert reused is held
    assert _login_count(fake_imap) == 1


def test_imap_pool_busy_maps_to_502_and_recovers(
    client, fake_imap, configured, monkeypatch
):
    settings = gmail.load_settings()
    monkeypatch.setattr(gmail, "_IMAP_POOL_MAX_CONNECTIONS", 1)
    monkeypatch.setattr(gmail, "_IMAP_POOL_ACQUIRE_TIMEOUT_S", 0.05)

    with gmail.imap_connect(settings) as held:
        response = client.get("/api/gmail/messages")
        assert response.status_code == 502
        assert _pool_state() == (0, 1)
        assert len(fake_imap.instances) == 1

    response = client.get("/api/gmail/messages")

    assert response.status_code == 200
    assert _login_count(fake_imap) == 1
    assert _pool_state() == (1, 0)
    assert held.logged_out is False


def test_imap_pool_noop_only_after_the_idle_threshold(fake_imap, monkeypatch):
    settings = _make_settings()
    clock = {"value": 1000.0}
    monkeypatch.setattr(gmail, "_now", lambda: clock["value"])

    with gmail.imap_connect(settings) as first:
        pass
    clock["value"] += gmail._IMAP_POOL_NOOP_IDLE_S
    with gmail.imap_connect(settings) as second:
        assert second is first
    assert _noop_count(fake_imap) == 0

    clock["value"] += gmail._IMAP_POOL_NOOP_IDLE_S + 1
    with gmail.imap_connect(settings) as third:
        assert third is first
    assert _noop_count(fake_imap) == 1
    # NOOP installs a short socket timeout and restores the previous one.
    assert first.sock.timeouts == [gmail._IMAP_POOL_NOOP_TIMEOUT_S, None]
    assert _login_count(fake_imap) == 1


def test_imap_pool_reaps_idle_connections_past_max_idle(fake_imap, monkeypatch):
    settings = _make_settings()
    clock = {"value": 1000.0}
    monkeypatch.setattr(gmail, "_now", lambda: clock["value"])
    monkeypatch.setattr(gmail, "_IMAP_POOL_MAX_IDLE_S", 10)

    with gmail.imap_connect(settings) as stale:
        pass
    clock["value"] += 11

    with gmail.imap_connect(settings) as fresh:
        assert fresh is not stale

    assert stale.logged_out is True
    assert stale.sock.closed is True
    assert len(fake_imap.instances) == 2
    assert _login_count(fake_imap) == 2
    # Reaped before the NOOP threshold logic: no NOOP on a stale connection.
    assert _noop_count(fake_imap) == 0


def test_imap_pool_dead_peer_noop_discards_and_reconnects(fake_imap, monkeypatch):
    class DeadNoopIMAP(FakeIMAP):
        def noop(self):
            self.noops += 1
            raise OSError("peer black hole")

    monkeypatch.setattr(gmail.imaplib, "IMAP4_SSL", DeadNoopIMAP)
    settings = _make_settings()
    clock = {"value": 1000.0}
    monkeypatch.setattr(gmail, "_now", lambda: clock["value"])

    with gmail.imap_connect(settings) as stale:
        pass
    clock["value"] += gmail._IMAP_POOL_NOOP_IDLE_S + 1

    with gmail.imap_connect(settings) as fresh:
        assert fresh is not stale
        assert isinstance(fresh, DeadNoopIMAP)

    assert stale.noops == 1
    assert stale.logged_out is True
    assert stale.sock.closed is True

    # A second NOOP failure (on the fresh connection) must once again
    # discard and reconnect: _acquire_fresh never logs in over a dead socket.
    clock["value"] += gmail._IMAP_POOL_NOOP_IDLE_S + 1
    with gmail.imap_connect(settings) as second_fresh:
        assert second_fresh is not fresh

    assert fresh.noops == 1
    assert fresh.logged_out is True
    assert fresh.sock.closed is True
    assert len(DeadNoopIMAP.instances) == 3
    assert _login_count(DeadNoopIMAP) == 3


def test_imap_pool_releases_the_slot_after_five_failed_logins(fake_imap, monkeypatch):
    class FlakyLoginIMAP(FakeIMAP):
        failures_left = 5

        def login(self, user, password):
            self.login_attempts += 1
            if type(self).failures_left:
                type(self).failures_left -= 1
                raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
            super().login(user, password)

    monkeypatch.setattr(gmail.imaplib, "IMAP4_SSL", FlakyLoginIMAP)
    settings = _make_settings()

    for _ in range(5):
        with pytest.raises(imaplib.IMAP4.error), gmail.imap_connect(settings):
            pytest.fail("login must not succeed")
        assert _pool_state() == (0, 0)

    with gmail.imap_connect(settings) as recovered:
        assert recovered.logged_in is True

    # No restart and no leaked slot: the sixth attempt succeeds and is pooled.
    assert len(FlakyLoginIMAP.instances) == 6
    assert _login_count(FlakyLoginIMAP) == 1
    assert _pool_state() == (1, 0)


def test_imap_pool_fingerprint_change_with_idle_closes_the_old_account(
    fake_imap, monkeypatch
):
    settings_a = _make_settings()
    settings_b = _make_settings(address="other@example.com")
    clock = {"value": 1000.0}
    monkeypatch.setattr(gmail, "_now", lambda: clock["value"])

    with gmail.imap_connect(settings_a) as first:
        pass
    # Past the NOOP threshold: the fingerprint change must close the idle
    # connection before _needs_noop() can send a command on it (BL4).
    clock["value"] += gmail._IMAP_POOL_NOOP_IDLE_S + 1
    with gmail.imap_connect(settings_b) as second:
        assert second is not first
        assert second.credentials == ("other@example.com", "app-secret")
    assert first.noops == 0
    assert first.logged_out is True
    assert first.sock.closed is True

    # Switching back must not resurrect the closed connection of account A.
    with gmail.imap_connect(settings_a) as third:
        assert third is not first
        assert third.credentials == ("me@example.com", "app-secret")
    assert second.logged_out is True
    assert second.sock.closed is True
    assert len(fake_imap.instances) == 3
    assert _login_count(fake_imap) == 3


def test_imap_pool_fingerprint_change_with_active_request_never_uses_old_conn(
    fake_imap,
):
    settings_a = _make_settings()
    settings_b = _make_settings(password="new-secret")

    with gmail.imap_connect(settings_a) as active_a:
        with gmail.imap_connect(settings_b) as active_b:
            assert active_b is not active_a
            assert active_b.credentials == ("me@example.com", "new-secret")
            assert active_a.logged_out is False
        # B is now idle; A is still in use and must not be touched.
        assert active_a.logged_out is False

    # Releasing the old account closes it instead of returning it to the pool.
    assert active_a.logged_out is True
    assert active_a.sock.closed is True
    assert _pool_state() == (1, 0)
    with gmail._pool_cond:
        idle_accounts = {pc.fingerprint for pc in gmail._pool_idle}
        assert idle_accounts == {gmail._pool_fingerprint(settings_b)}

    with gmail.imap_connect(settings_b) as reused:
        assert reused is active_b
    assert _login_count(fake_imap) == 2


def test_imap_pool_clear_marks_active_but_closes_only_on_release(fake_imap):
    settings = _make_settings()

    with gmail.imap_connect(settings) as active:
        gmail.clear_imap_pool()
        assert active.logged_out is False
        assert active.sock.closed is False
        # The in-use connection is still usable after an invalidation.
        assert active.noop()[0] == "OK"

    assert active.logged_out is True
    assert active.sock.closed is True
    assert _pool_state() == (0, 0)


def test_imap_pool_close_is_idempotent_and_reusable(fake_imap):
    settings = _make_settings()

    with gmail.imap_connect(settings) as first:
        pass
    gmail.close_imap_pool()
    assert first.logged_out is True
    assert first.sock.closed is True
    assert _pool_state() == (0, 0)

    gmail.close_imap_pool()  # idempotent no-op

    with gmail.imap_connect(settings) as second:
        assert second is not first
    gmail.close_imap_pool()
    assert second.logged_out is True
    assert second.sock.closed is True
    assert _pool_state() == (0, 0)
    assert len(fake_imap.instances) == 2
    assert _login_count(fake_imap) == 2


@pytest.mark.parametrize(
    "error_factory",
    [
        lambda: imaplib.IMAP4.error("BAD command"),
        lambda: imaplib.IMAP4.abort("connection aborted"),
        lambda: TimeoutError("timed out"),
        lambda: OSError("connection reset"),
    ],
    ids=["bad", "abort", "timeout", "oserror"],
)
def test_imap_pool_discards_on_transport_errors(fake_imap, error_factory):
    settings = _make_settings()

    with pytest.raises(type(error_factory())), gmail.imap_connect(settings) as broken:
        raise error_factory()

    assert broken.logged_out is True
    assert broken.sock.closed is True
    with gmail.imap_connect(settings) as fresh:
        assert fresh is not broken
    assert len(fake_imap.instances) == 2
    assert _login_count(fake_imap) == 2


@pytest.mark.parametrize(
    "error_factory",
    [
        lambda: gmail.GmailNotFound("missing"),
        lambda: gmail.GmailValidationError("invalid"),
        lambda: gmail.GmailNotConfigured("not configured"),
    ],
    ids=["not-found", "validation", "not-configured"],
)
def test_imap_pool_keeps_on_logical_errors(fake_imap, error_factory):
    settings = _make_settings()

    with pytest.raises(type(error_factory())), gmail.imap_connect(settings) as kept:
        raise error_factory()

    assert kept.logged_out is False
    assert kept.sock.closed is False
    with gmail.imap_connect(settings) as reused:
        assert reused is kept
    assert len(fake_imap.instances) == 1
    assert _login_count(fake_imap) == 1


def test_imap_pool_disabled_uses_one_connection_per_operation(fake_imap, monkeypatch):
    monkeypatch.setattr(gmail, "_IMAP_POOL_ENABLED", False)
    settings = _make_settings()

    with gmail.imap_connect(settings) as first:
        pass
    assert first.logged_out is True
    assert first.sock.closed is True

    with gmail.imap_connect(settings) as second:
        assert second is not first
    assert second.logged_out is True
    assert second.sock.closed is True

    assert len(fake_imap.instances) == 2
    assert _login_count(fake_imap) == 2
    assert _pool_state() == (0, 0)


def test_close_many_empty_is_a_noop():
    started = time.monotonic()

    gmail._close_many([])

    assert time.monotonic() - started < 0.2


def test_close_many_deadline_respected_by_a_socket_bound_logout(fake_imap, monkeypatch):
    class SlowLogoutIMAP(FakeIMAP):
        def logout(self):
            # Emulate imaplib logout bounded by the socket timeout installed
            # by _close_conn; it fails rather than hanging forever.
            self.sock.wait_closed(self.sock.gettimeout())
            raise OSError("logout timed out")

    monkeypatch.setattr(gmail.imaplib, "IMAP4_SSL", SlowLogoutIMAP)
    monkeypatch.setattr(gmail, "_IMAP_POOL_CLOSE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(gmail, "_IMAP_POOL_CLOSE_TOTAL_S", 0.5)
    settings = _make_settings()

    with gmail.imap_connect(settings) as conn:
        pass

    started = time.monotonic()
    gmail.close_imap_pool()
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert conn.shutdown_called is True
    assert conn.sock.closed is True


def test_close_many_returns_at_the_deadline_when_logout_hangs(fake_imap, monkeypatch):
    class HangingLogoutIMAP(FakeIMAP):
        release = threading.Event()

        def logout(self):
            type(self).release.wait(5)

    monkeypatch.setattr(gmail.imaplib, "IMAP4_SSL", HangingLogoutIMAP)
    monkeypatch.setattr(gmail, "_IMAP_POOL_CLOSE_TOTAL_S", 0.05)
    settings = _make_settings()

    with gmail.imap_connect(settings) as conn:
        pass

    started = time.monotonic()
    try:
        gmail.close_imap_pool()
        elapsed = time.monotonic() - started
    finally:
        HangingLogoutIMAP.release.set()

    assert elapsed < 0.5
    # The abandoned worker still finishes and closes its socket.
    assert conn.sock.close_event.wait(1)
    assert conn.sock.closed is True


def test_pool_fingerprint_is_opaque_and_tracks_credentials():
    settings = _make_settings()
    other_password = _make_settings(password="different-secret")
    other_host = _make_settings(imap_host="imap.other.example")

    fingerprint = gmail._pool_fingerprint(settings)

    assert fingerprint != gmail._pool_fingerprint(other_password)
    assert fingerprint != gmail._pool_fingerprint(other_host)
    assert "app-secret" not in fingerprint
    assert "me@example.com" not in fingerprint
    assert "imap.example.com" not in fingerprint


def test_imap_pool_busy_after_fingerprint_detach_closes_idle_connections(
    fake_imap, monkeypatch
):
    """BLK1: to_close is drained even when the acquire ends in ``busy``."""
    settings_a = _make_settings()
    settings_b = _make_settings(address="other@example.com")
    monkeypatch.setattr(gmail, "_IMAP_POOL_MAX_CONNECTIONS", 2)

    # Two active connections fill the pool; releasing one makes it idle.
    with gmail.imap_connect(settings_a) as active_old:
        with gmail.imap_connect(settings_a) as idle_old:
            assert _pool_state() == (0, 2)
        assert _pool_state() == (1, 1)
        # Shrinking the cap makes the fingerprint change leave no free slot:
        # the detached idle must still be closed on the busy exit.
        monkeypatch.setattr(gmail, "_IMAP_POOL_MAX_CONNECTIONS", 1)
        monkeypatch.setattr(gmail, "_IMAP_POOL_ACQUIRE_TIMEOUT_S", 0.05)
        with (
            pytest.raises(gmail.GmailUpstreamError, match="busy"),
            gmail.imap_connect(settings_b),
        ):
            pytest.fail("no slot is available for account B")

        assert idle_old.logged_out is True
        assert idle_old.sock.closed is True
        assert active_old.logged_out is False
        assert _pool_state() == (0, 1)

    # The invalidated active connection is closed on release: no orphan.
    assert active_old.logged_out is True
    assert active_old.sock.closed is True
    assert _pool_state() == (0, 0)


# ── P3b: partial BODYSTRUCTURE fetch / on-demand attachments ────────────────


@pytest.mark.parametrize(
    "uid",
    [
        P3B_SIMPLE,
        P3B_ALTERNATIVE,
        P3B_MIXED,
        P3B_INLINE,
        P3B_LATIN,
        P3B_BASE64_TEXT,
        P3B_MALFORMED,
        P3B_FORWARD,
        P3B_PRIMO,
        P3B_HTML_ONLY,
        P3B_ATTACHMENT_ONLY,
        P3B_BIG_BASE64,
        P3B_BIG_QP,
    ],
)
def test_p3b_detail_matches_the_full_parser(client, p3b_corpus, configured, uid):
    expected = gmail.parse_message(p3b_corpus[uid])

    response = client.get(f"/api/gmail/messages/{uid}")

    assert response.status_code == 200
    body = response.json()
    assert body["uid"] == uid
    for key in (
        "from",
        "to",
        "cc",
        "subject",
        "date_ts",
        "message_id",
        "in_reply_to",
        "references",
        "text",
        "html",
    ):
        assert body[key] == expected[key]
    assert body["attachments"] == gmail.public_attachments(expected["attachments"])


def test_p3b_detail_does_not_download_attachment_parts(client, p3b_corpus, configured):
    response = client.get(f"/api/gmail/messages/{P3B_LARGE}")

    assert response.status_code == 200
    body = response.json()
    assert body["attachments"][0]["filename"] == "large.pdf"
    assert body["attachments"][0]["size"] > 100_000

    instance = FakeIMAP.instances[-1]
    labels = [label for _uid, group in instance.part_fetch_calls for label in group]
    assert labels == ["1"]
    assert instance.full_body_fetches == 0
    assert instance.full_body_bytes == 0
    assert instance.part_bytes < 1024


def test_p3b_attachment_download_fetches_only_its_part(client, p3b_corpus, configured):
    assert client.get(f"/api/gmail/messages/{P3B_LARGE}").status_code == 200
    before = sum(instance.part_bytes for instance in FakeIMAP.instances)

    response = client.get(f"/api/gmail/messages/{P3B_LARGE}/attachments/0")

    assert response.status_code == 200
    assert response.content == b"X" * 131072
    after = sum(instance.part_bytes for instance in FakeIMAP.instances)
    message = email.message_from_bytes(p3b_corpus[P3B_LARGE])
    attachment = next(
        part for part in message.walk() if part.get_filename() == "large.pdf"
    )
    assert after - before == gmail._encoded_size(attachment)
    calls = [
        call for instance in FakeIMAP.instances for call in instance.part_fetch_calls
    ]
    assert calls[-1] == (P3B_LARGE, ("2",))
    assert all(instance.full_body_fetches == 0 for instance in FakeIMAP.instances)


def test_p3b_attachment_out_of_range_does_not_download_the_full_body(
    client, p3b_corpus, configured
):
    response = client.get(f"/api/gmail/messages/{P3B_MIXED}/attachments/9")

    assert response.status_code == 404
    assert all(instance.full_body_fetches == 0 for instance in FakeIMAP.instances)


def test_p3b_413_7bit_is_an_early_reject_without_part_fetch(
    client, p3b_corpus, configured, monkeypatch
):
    monkeypatch.setattr(gmail, "_MAX_DOWNLOAD_BYTES", 10)

    response = client.get(f"/api/gmail/messages/{P3B_BIG_7BIT}/attachments/0")

    assert response.status_code == 413
    assert all(
        call[0] != P3B_BIG_7BIT
        for instance in FakeIMAP.instances
        for call in instance.part_fetch_calls
    )
    assert all(instance.full_body_fetches == 0 for instance in FakeIMAP.instances)


@pytest.mark.parametrize("uid", [P3B_BIG_BASE64, P3B_BIG_QP, P3B_UNKNOWN_CTE])
def test_p3b_413_base64_qp_unknown_are_post_checked(
    client, p3b_corpus, configured, monkeypatch, uid
):
    monkeypatch.setattr(gmail, "_MAX_DOWNLOAD_BYTES", 10)

    response = client.get(f"/api/gmail/messages/{uid}/attachments/0")

    assert response.status_code == 413
    calls = [
        call
        for instance in FakeIMAP.instances
        for call in instance.part_fetch_calls
        if call[0] == uid
    ]
    assert calls and calls[-1][1] == ("2",)
    assert all(instance.full_body_fetches == 0 for instance in FakeIMAP.instances)


def test_p3b_attachment_at_the_decoded_limit_is_allowed(
    client, p3b_corpus, configured, monkeypatch
):
    monkeypatch.setattr(gmail, "_MAX_DOWNLOAD_BYTES", 20)

    response = client.get(f"/api/gmail/messages/{P3B_BIG_BASE64}/attachments/0")

    assert response.status_code == 200
    assert response.content == b"Y" * 20


def test_p3b_octets_nil_with_structure_is_partial_with_a_null_size(
    client, p3b_corpus, configured
):
    FakeIMAP.bodystructures[P3B_NIL_OCTETS] = NIL_OCTETS_STRUCTURE

    detail = client.get(f"/api/gmail/messages/{P3B_NIL_OCTETS}")

    assert detail.status_code == 200
    body = detail.json()
    assert body["text"] == "body"
    assert body["attachments"] == [
        {
            "index": 0,
            "filename": "primo.pdf",
            "content_type": "application/pdf",
            "size": None,
            "content_id": None,
        }
    ]
    assert all(instance.full_body_fetches == 0 for instance in FakeIMAP.instances)

    download = client.get(f"/api/gmail/messages/{P3B_NIL_OCTETS}/attachments/0")

    assert download.status_code == 200
    assert download.content == b"primo"
    assert all(instance.full_body_fetches == 0 for instance in FakeIMAP.instances)


def test_p3b_full_fallback_with_a_structure_uses_body_octets(
    client, p3b_corpus, configured
):
    FakeIMAP.bodystructures[P3B_FALLBACK_OCTETS] = FALLBACK_OCTETS_STRUCTURE
    FakeIMAP.part_fetch_status[P3B_FALLBACK_OCTETS] = "NO"

    response = client.get(f"/api/gmail/messages/{P3B_FALLBACK_OCTETS}")

    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "body"
    assert body["attachments"][0]["size"] == 999
    assert len(FakeIMAP.instances) == 1
    instance = FakeIMAP.instances[0]
    assert instance.full_body_fetches == 1
    assert [call[1] for call in instance.part_fetch_calls] == [("1",)]


def test_p3b_fallback_without_a_structure_uses_encoded_size(
    client, p3b_corpus, configured
):
    FakeIMAP.bodystructures[P3B_PRIMO] = (
        b'("text" "plain" ("name" {5}) NIL NIL "7bit" 1 1)'
    )

    response = client.get(f"/api/gmail/messages/{P3B_PRIMO}")

    assert response.status_code == 200
    assert response.json()["attachments"][0]["size"] == 10
    instance = FakeIMAP.instances[-1]
    assert instance.structure_fetches == 1
    assert instance.full_body_fetches == 1
    assert instance.part_fetch_calls == []


def test_p3b_bodystructure_status_no_falls_back_to_full(client, p3b_corpus, configured):
    FakeIMAP.bodystructure_status[P3B_PRIMO] = "NO"

    response = client.get(f"/api/gmail/messages/{P3B_PRIMO}")

    assert response.status_code == 200
    assert response.json()["attachments"][0]["size"] == 10
    instance = FakeIMAP.instances[-1]
    assert instance.full_body_fetches == 1
    assert instance.part_fetch_calls == []


def test_p3b_render_parts_over_the_limit_fall_back_to_full(
    client, p3b_corpus, configured, monkeypatch
):
    monkeypatch.setattr(gmail, "_MAX_RENDER_PARTS", 1)

    response = client.get(f"/api/gmail/messages/{P3B_ALTERNATIVE}")

    assert response.status_code == 200
    assert response.json()["text"] == "plain body"
    instance = FakeIMAP.instances[-1]
    assert instance.full_body_fetches == 1
    assert instance.part_fetch_calls == []


def test_p3b_flag_off_rolls_back_to_the_full_fetch(
    client, p3b_corpus, configured, monkeypatch
):
    monkeypatch.setattr(gmail, "_PARTIAL_FETCH_ENABLED", False)

    response = client.get(f"/api/gmail/messages/{P3B_MIXED}")

    assert response.status_code == 200
    expected = gmail.parse_message(p3b_corpus[P3B_MIXED])
    assert response.json()["attachments"] == gmail.public_attachments(
        expected["attachments"]
    )
    instance = FakeIMAP.instances[-1]
    assert instance.structure_fetches == 0
    assert instance.part_fetch_calls == []
    assert instance.full_body_fetches == 1


@pytest.mark.parametrize(
    "error_factory",
    [
        lambda: imaplib.IMAP4.abort("aborted"),
        lambda: TimeoutError("timeout"),
        lambda: OSError("reset"),
    ],
    ids=["abort", "timeout", "oserror"],
)
def test_p3b_structure_transport_errors_propagate_and_discard(
    p3b_corpus, configured, monkeypatch, error_factory
):
    error = error_factory()

    class ErrorIMAP(FakeIMAP):
        def uid(self, command, *args):
            if command == "FETCH" and "BODYSTRUCTURE" in str(args[1]):
                raise error
            return super().uid(command, *args)

    monkeypatch.setattr(gmail.imaplib, "IMAP4_SSL", ErrorIMAP)

    with pytest.raises(type(error)):
        gmail.fetch_message(P3B_SIMPLE)

    instance = FakeIMAP.instances[-1]
    assert instance.logged_out is True
    assert instance.sock.closed is True
    assert _pool_state() == (0, 0)


@pytest.mark.parametrize(
    "error_factory",
    [
        lambda: imaplib.IMAP4.abort("aborted"),
        lambda: TimeoutError("timeout"),
        lambda: OSError("reset"),
    ],
    ids=["abort", "timeout", "oserror"],
)
def test_p3b_part_transport_errors_propagate_and_discard(
    p3b_corpus, configured, monkeypatch, error_factory
):
    error = error_factory()

    class ErrorIMAP(FakeIMAP):
        def uid(self, command, *args):
            item = str(args[1]) if len(args) > 1 else ""
            if (
                command == "FETCH"
                and "BODY.PEEK[" in item
                and "HEADER" not in item
                and "BODY.PEEK[]" not in item
            ):
                raise error
            return super().uid(command, *args)

    monkeypatch.setattr(gmail.imaplib, "IMAP4_SSL", ErrorIMAP)

    with pytest.raises(type(error)):
        gmail.fetch_message(P3B_SIMPLE)

    instance = FakeIMAP.instances[-1]
    assert instance.logged_out is True
    assert instance.sock.closed is True
    assert _pool_state() == (0, 0)


def test_p3b_logical_part_error_falls_back_and_keeps_the_connection(
    p3b_corpus, configured
):
    FakeIMAP.part_fetch_status[P3B_SIMPLE] = "BAD"

    first = gmail.fetch_message(P3B_SIMPLE)
    second = gmail.fetch_message(P3B_SIMPLE)

    assert first["text"] == second["text"] == "simple body"
    assert len(FakeIMAP.instances) == 1
    instance = FakeIMAP.instances[0]
    assert instance.logged_out is False
    assert instance.full_body_fetches == 2
    assert _login_count(FakeIMAP) == 1


def test_p3b_single_acquisition_for_detail_and_attachment(
    p3b_corpus, configured, monkeypatch
):
    monkeypatch.setattr(gmail, "_IMAP_POOL_MAX_CONNECTIONS", 1)
    monkeypatch.setattr(gmail, "_IMAP_POOL_ACQUIRE_TIMEOUT_S", 1.0)

    detail = gmail.fetch_message(P3B_MIXED)
    download = gmail.fetch_attachment(P3B_MIXED, 0)

    assert detail["subject"] == "Mixed"
    assert download.data == b"%PDF-1.4\r\npdf"
    assert len(FakeIMAP.instances) == 1
    assert _login_count(FakeIMAP) == 1


def test_p3b_detail_and_attachment_can_run_concurrently(
    p3b_corpus, configured, monkeypatch
):
    monkeypatch.setattr(gmail, "_IMAP_POOL_MAX_CONNECTIONS", 2)
    monkeypatch.setattr(gmail, "_IMAP_POOL_ACQUIRE_TIMEOUT_S", 2.0)
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}
    errors: list[BaseException] = []

    def detail() -> None:
        try:
            barrier.wait(5)
            results["detail"] = gmail.fetch_message(P3B_MIXED)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    def attachment() -> None:
        try:
            barrier.wait(5)
            results["attachment"] = gmail.fetch_attachment(P3B_MIXED, 0)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=detail), threading.Thread(target=attachment)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert results["detail"]["subject"] == "Mixed"
    assert results["attachment"].data == b"%PDF-1.4\r\npdf"
    assert len(FakeIMAP.instances) <= 2


def test_close_many_returns_at_the_deadline_without_an_event(fake_imap, monkeypatch):
    class SleepingLogoutIMAP(FakeIMAP):
        def logout(self):
            time.sleep(0.2)
            self.logged_out = True

    monkeypatch.setattr(gmail.imaplib, "IMAP4_SSL", SleepingLogoutIMAP)
    monkeypatch.setattr(gmail, "_IMAP_POOL_CLOSE_TOTAL_S", 0.05)
    settings = _make_settings()

    with gmail.imap_connect(settings) as conn:
        pass

    started = time.monotonic()
    gmail.close_imap_pool()
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    deadline = time.monotonic() + 1
    while not conn.sock.closed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert conn.sock.closed is True


def test_p3b_attachment_part_fetch_failure_falls_back_to_full(
    client, p3b_corpus, configured
):
    FakeIMAP.bodystructures[P3B_FALLBACK_OCTETS] = FALLBACK_OCTETS_STRUCTURE
    FakeIMAP.part_fetch_status[P3B_FALLBACK_OCTETS] = "NO"

    response = client.get(f"/api/gmail/messages/{P3B_FALLBACK_OCTETS}/attachments/0")

    assert response.status_code == 200
    assert response.content == b"primo"
    instance = FakeIMAP.instances[-1]
    assert instance.full_body_fetches == 1
    assert [call[1] for call in instance.part_fetch_calls] == [("2",)]
    assert instance.logged_out is False


BAD_MAINTYPE_STRUCTURE = (
    b'(("text" "plain" ("charset" "utf-8") NIL NIL "7bit" NIL 1)'
    b'("bad/main" "pdf" ("name" "primo.pdf") NIL NIL "base64" NIL NIL '
    b'("attachment" ("filename" "primo.pdf"))) "mixed")'
)


def test_p3b_attachment_decode_failure_falls_back_to_full(
    client, p3b_corpus, configured
):
    FakeIMAP.bodystructures[P3B_PRIMO] = BAD_MAINTYPE_STRUCTURE

    response = client.get(f"/api/gmail/messages/{P3B_PRIMO}/attachments/0")

    assert response.status_code == 200
    assert response.content == b"primo"
    instance = FakeIMAP.instances[-1]
    assert instance.part_fetch_calls[-1][1] == ("2",)
    assert instance.full_body_fetches == 1
    assert instance.logged_out is False


def test_p3b_nil_octets_skips_the_7bit_early_reject_and_post_checks(
    client, p3b_corpus, configured, monkeypatch
):
    monkeypatch.setattr(gmail, "_MAX_DOWNLOAD_BYTES", 10)
    FakeIMAP.bodystructures[P3B_BIG_7BIT] = NIL_7BIT_STRUCTURE

    response = client.get(f"/api/gmail/messages/{P3B_BIG_7BIT}/attachments/0")

    assert response.status_code == 413
    calls = [
        call
        for instance in FakeIMAP.instances
        for call in instance.part_fetch_calls
        if call[0] == P3B_BIG_7BIT
    ]
    assert calls and calls[-1][1] == ("2",)
    assert all(instance.full_body_fetches == 0 for instance in FakeIMAP.instances)


@pytest.mark.parametrize("cte", ["8bit", "binary"])
def test_p3b_413_8bit_binary_are_early_rejects_without_part_fetch(
    client, p3b_corpus, configured, monkeypatch, cte
):
    monkeypatch.setattr(gmail, "_MAX_DOWNLOAD_BYTES", 10)
    FakeIMAP.bodystructures[P3B_PRIMO] = _plain_big_structure(cte)

    response = client.get(f"/api/gmail/messages/{P3B_PRIMO}/attachments/0")

    assert response.status_code == 413
    assert all(
        call[0] != P3B_PRIMO
        for instance in FakeIMAP.instances
        for call in instance.part_fetch_calls
    )
    assert all(instance.full_body_fetches == 0 for instance in FakeIMAP.instances)


def test_p3b_7bit_octets_at_the_limit_are_fetched_and_allowed(
    client, p3b_corpus, configured, monkeypatch
):
    monkeypatch.setattr(gmail, "_MAX_DOWNLOAD_BYTES", 20)

    response = client.get(f"/api/gmail/messages/{P3B_BIG_7BIT}/attachments/0")

    assert response.status_code == 200
    assert response.content == b"X" * 20
