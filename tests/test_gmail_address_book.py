"""Compose address autocomplete: server address book and client matching.

The address book is built from the latest Sent and Inbox headers (read-only);
the client filters it as the user types (real ``gmail.js`` functions in Node).
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from client_harness import api_for

from server import gmail
from tests.test_gmail_images_default import _js_functions, _run_node

_SETTINGS = SimpleNamespace(address="Me@Example.com", folder="INBOX")


def _headers(**fields: str) -> bytes:
    return (
        "".join(f"{name}: {value}\r\n" for name, value in fields.items()).encode()
        + b"\r\n"
    )


class _FakeImap:
    def __init__(self, folders: dict[str, list[bytes]]) -> None:
        self.folders = folders
        self.selected = ""
        self.calls: list[tuple[object, ...]] = []

    def list(self, reference, pattern):
        return "OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Posta inviata"',
        ]

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        self.selected = mailbox.strip('"')
        return "OK", [str(len(self.folders.get(self.selected, []))).encode()]

    def fetch(self, sequence, item):
        self.calls.append(("fetch", self.selected, sequence, item))
        return "OK", [
            (f"{i + 1} (BODY[HEADER.FIELDS (FROM TO CC DATE)] {{1}}".encode(), raw)
            for i, raw in enumerate(self.folders.get(self.selected, []))
        ]


@pytest.fixture
def imap(monkeypatch):
    gmail.clear_address_book_cache()
    holder: dict[str, _FakeImap] = {}

    @contextmanager
    def fake_connect(settings=None):
        yield holder["connection"]

    monkeypatch.setattr(gmail, "imap_connect", fake_connect)
    monkeypatch.setattr(gmail, "load_settings", lambda: _SETTINGS)
    monkeypatch.setattr(gmail, "_account_fingerprint", lambda settings: "fp")

    def install(folders: dict[str, list[bytes]]) -> _FakeImap:
        holder["connection"] = _FakeImap(folders)
        return holder["connection"]

    yield install
    gmail.clear_address_book_cache()


def test_address_book_weighs_sent_over_inbox_and_skips_noise(imap) -> None:
    connection = imap(
        {
            "[Gmail]/Posta inviata": [
                _headers(
                    From="me@example.com",
                    To='"Rossi, Mario" <mario@example.com>, anna@example.com',
                    Cc="=?UTF-8?B?Q2hpYXJhIEzDoA==?= <chiara@example.com>",
                    Date="Mon, 26 Oct 2026 10:00:00 +0000",
                ),
            ],
            "INBOX": [
                _headers(
                    From="Anna Bianchi <anna@example.com>",
                    To="me@example.com, team@example.com",
                    Date="Tue, 27 Oct 2026 10:00:00 +0000",
                ),
                _headers(
                    From="Newsletter <no-reply@shop.example>", To="me@example.com"
                ),
                _headers(From="GitHub <notifications@github.com>", To="ME@example.com"),
            ],
        }
    )

    book = gmail.fetch_address_book()

    by_email = {entry["email"]: entry for entry in book}
    assert "me@example.com" not in by_email  # own address, any case
    assert "no-reply@shop.example" not in by_email
    assert by_email["mario@example.com"]["name"] == "Rossi, Mario"
    assert by_email["chiara@example.com"]["name"] == "Chiara Là"  # MIME-decoded
    assert by_email["anna@example.com"]["name"] == "Anna Bianchi"  # latest name
    assert by_email["anna@example.com"]["score"] == 4.0  # sent To 3 + inbox From 1
    assert [entry["email"] for entry in book][:2] == [
        "anna@example.com",
        "mario@example.com",
    ]
    assert book[-1]["email"] == "team@example.com"  # inbox Cc/To weighs little
    assert all(call[2] is True for call in connection.calls if call[0] == "select")
    assert all(
        "BODY.PEEK" in call[3] for call in connection.calls if call[0] == "fetch"
    )


def test_address_book_reads_only_the_latest_window(imap, monkeypatch) -> None:
    monkeypatch.setattr(gmail, "_ADDRESS_BOOK_WINDOW", 2)
    connection = imap({"INBOX": [_headers(From=f"p{i}@example.com") for i in range(5)]})

    gmail.fetch_address_book()

    fetches = [call for call in connection.calls if call[0] == "fetch"]
    assert [call[2] for call in fetches] == ["4:5"]


def test_address_book_is_cached_until_refresh(imap) -> None:
    first = imap({"INBOX": [_headers(From="a@example.com")]})
    assert [entry["email"] for entry in gmail.fetch_address_book()] == ["a@example.com"]

    second = imap({"INBOX": [_headers(From="b@example.com")]})
    assert [entry["email"] for entry in gmail.fetch_address_book()] == ["a@example.com"]
    assert second.calls == []
    assert [entry["email"] for entry in gmail.fetch_address_book(refresh=True)] == [
        "b@example.com"
    ]
    assert first.calls


def test_address_book_op(monkeypatch) -> None:
    monkeypatch.setattr(
        gmail, "fetch_address_book", lambda: [{"email": "a@example.com", "name": "A"}]
    )

    result = api_for(SimpleNamespace()).call("gmail.address_book", {})

    assert result == {"contacts": [{"email": "a@example.com", "name": "A"}]}


# ── client matching (Node) ───────────────────────────────────────────────────


def _run(body: str) -> None:
    _run_node(
        'const assert = require("node:assert/strict");\n'
        "const ADDRESS_SUGGESTIONS_MAX = 8;\n"
        + _js_functions(
            "normalizeSearch",
            "addressTokenRange",
            "suggestAddresses",
            "formatAddress",
            "applySuggestion",
        )
        + body
    )


def test_suggestions_match_names_and_addresses_without_accents() -> None:
    _run(
        r"""
const book = [
  { email: "anna@example.com", name: "Anna Bianchi", score: 4, last_ts: 2 },
  { email: "nicola@example.com", name: "Nicolò Verdi", score: 1, last_ts: 9 },
  { email: "mario.rossi@example.com", name: "Mario Rossi", score: 3, last_ts: 1 },
  { email: "giovanna@example.com", name: "", score: 9, last_ts: 1 },
];
const emails = (q, exclude = new Set()) => suggestAddresses(q, book, exclude).map((c) => c.email);
assert.deepEqual(emails("an"), ["anna@example.com", "giovanna@example.com"]);
assert.deepEqual(emails("nicolo"), ["nicola@example.com"]);       // accent-insensitive
assert.deepEqual(emails("ROSSI"), ["mario.rossi@example.com"]);    // word / address part
assert.deepEqual(emails("an", new Set(["anna@example.com"])), ["giovanna@example.com"]);
assert.deepEqual(emails("   "), []);
const many = Array.from({ length: 20 }, (_, i) => ({ email: `p${i}@x.example`, name: "", score: i }));
assert.equal(suggestAddresses("p", many, new Set()).length, 8);
assert.equal(suggestAddresses("p", many, new Set())[0].email, "p19@x.example");
"""
    )


def test_formatting_and_inserting_a_suggestion() -> None:
    _run(
        r"""
assert.equal(formatAddress({ email: "a@example.com", name: "" }), "a@example.com");
assert.equal(formatAddress({ email: "a@example.com", name: "Anna" }), "Anna <a@example.com>");
assert.equal(formatAddress({ email: "m@example.com", name: "Rossi, Mario" }), '"Rossi, Mario" <m@example.com>');
assert.equal(formatAddress({ email: "x@example.com", name: 'Evil "<x>"' }), "Evil x <x@example.com>");

assert.deepEqual(addressTokenRange("a@x.it, an", 10), { start: 7, end: 10 });
const anna = { email: "anna@example.com", name: "Anna" };
// At the end of the field.
assert.deepEqual(applySuggestion("a@x.it, an", 10, anna), {
  value: "a@x.it, Anna <anna@example.com>, ",
  caret: "a@x.it, Anna <anna@example.com>, ".length,
});
// In the middle: the following addresses are kept.
const middle = applySuggestion("an, b@x.it", 2, anna);
assert.equal(middle.value, "Anna <anna@example.com>, b@x.it");
assert.equal(middle.caret, "Anna <anna@example.com>, ".length);
"""
    )
