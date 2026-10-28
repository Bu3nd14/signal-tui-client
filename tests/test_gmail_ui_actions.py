"""Gmail reader actions: mark unread, archive, reply all, forward.

Server functions are exercised with a fake IMAP connection / SMTP session;
the client compose helpers run in Node with the real ``gmail.js`` functions
(``test_gmail_images_default.py`` helpers).
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from email import message_from_bytes
from pathlib import Path
from types import SimpleNamespace

import pytest
from client_harness import api_for

from server import gmail, gmail_unread
from tests.test_gmail_images_default import _js_functions, _run_node

_ROOT = Path(__file__).resolve().parents[1]
_SETTINGS = SimpleNamespace(address="me@example.com", folder="INBOX")


class _FakeImap:
    def __init__(self, *, fetch_data=None, status: str = "OK") -> None:
        self.calls: list[tuple[object, ...]] = []
        self.fetch_data = (
            [b"5 (UID 5 FLAGS (\\Seen))"] if fetch_data is None else fetch_data
        )
        self.status = status

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return "OK", [b"1"]

    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        if command == "FETCH":
            return "OK", self.fetch_data
        return self.status, [b""]


@pytest.fixture
def fake_imap(monkeypatch):
    connection = _FakeImap()

    @contextmanager
    def fake_connect(settings=None):
        yield connection

    monkeypatch.setattr(gmail, "imap_connect", fake_connect)
    monkeypatch.setattr(gmail, "load_settings", lambda: _SETTINGS)
    monkeypatch.setattr(gmail, "_account_fingerprint", lambda settings: "fp")
    return connection


# ── mark unread / archive ────────────────────────────────────────────────────


def test_mark_unread_clears_seen_and_patches_the_cache(fake_imap, monkeypatch) -> None:
    patched: list[tuple[object, ...]] = []
    monkeypatch.setattr(gmail, "_set_seen_cached", lambda *args: patched.append(args))

    gmail.mark_unread(5)

    assert ("uid", "STORE", "5", "-FLAGS", "(\\Seen)") in fake_imap.calls
    assert ("select", '"INBOX"', False) in fake_imap.calls
    assert patched == [("INBOX", "fp", 5, False)]


def test_remove_flag_is_case_insensitive() -> None:
    assert gmail._remove_flag(["\\Answered", "\\seen", "\\Flagged"], "\\Seen") == [
        "\\Answered",
        "\\Flagged",
    ]


def test_archive_removes_the_inbox_label(fake_imap, monkeypatch) -> None:
    invalidated: list[int] = []
    monkeypatch.setattr(gmail, "_invalidate_inbox_all", lambda: invalidated.append(1))

    gmail.archive_message(5)

    assert ("uid", "STORE", "5", "-X-GM-LABELS", "(\\Inbox)") in fake_imap.calls
    assert invalidated == [1]


def test_archive_only_applies_to_the_inbox(fake_imap) -> None:
    with pytest.raises(gmail.GmailValidationError):
        gmail.archive_message(5, folder="Lavoro")
    assert fake_imap.calls == []


def test_archive_missing_message_is_not_found(fake_imap) -> None:
    fake_imap.fetch_data = []

    with pytest.raises(gmail.GmailNotFound):
        gmail.archive_message(5)
    assert not any(call[:2] == ("uid", "STORE") for call in fake_imap.calls)


@pytest.mark.parametrize(
    ("operation", "function"),
    [
        ("gmail.messages.mark_unread", "mark_unread"),
        ("gmail.messages.archive", "archive_message"),
    ],
)
def test_ops_call_the_action_and_poke_the_unread_count(
    monkeypatch, operation, function
) -> None:
    calls: list[tuple[object, ...]] = []
    pokes: list[int] = []
    monkeypatch.setattr(
        gmail, function, lambda uid, folder=None: calls.append((uid, folder))
    )
    monkeypatch.setattr(gmail_unread, "poke", lambda: pokes.append(1))

    assert api_for(SimpleNamespace()).call(operation, {"uid": 5}) == {"ok": True}
    assert calls == [(5, None)]
    assert pokes == [1]


# ── forward ──────────────────────────────────────────────────────────────────

_ORIGINAL = {
    "from": '"Mario Rossi" <mario@example.com>',
    "to": "me@example.com, anna@example.com",
    "cc": "ufficio@example.com",
    "subject": "Verbale",
    "date_ts": 1_793_000_000,
    "text": "Ciao a tutti,\nin allegato il verbale.",
    "message_id": "<orig@example.com>",
    "references": "",
    "attachments": [
        {"index": 0, "filename": "verbale.pdf", "inline": False},
        {"index": 1, "filename": "logo.png", "inline": True},
        {"index": 2, "filename": "azioni.xlsx", "inline": False},
    ],
}


@pytest.fixture
def sent(monkeypatch) -> list[object]:
    messages: list[object] = []

    @contextmanager
    def fake_smtp(settings=None):
        yield SimpleNamespace(send_message=messages.append)

    downloads = {
        0: gmail.AttachmentDownload("verbale.pdf", "application/pdf", b"%PDF"),
        2: gmail.AttachmentDownload("azioni.xlsx", "application/vnd.ms-excel", b"XLS"),
    }
    monkeypatch.setattr(gmail, "smtp_connect", fake_smtp)
    monkeypatch.setattr(gmail, "load_settings", lambda: _SETTINGS)
    monkeypatch.setattr(
        gmail, "fetch_message", lambda uid, folder=None: dict(_ORIGINAL)
    )
    monkeypatch.setattr(
        gmail, "fetch_attachment", lambda uid, index, folder=None: downloads[index]
    )
    return messages


def test_forward_adds_header_block_text_and_original_files(sent) -> None:
    gmail.send_email(to="luigi@example.com", text="Guarda qui sotto.", forward_uid=7)

    (message,) = sent
    parsed = message_from_bytes(message.as_bytes())
    assert parsed["Subject"] == "Fwd: Verbale"
    assert parsed["To"] == "luigi@example.com"
    assert parsed["In-Reply-To"] is None  # a forward starts a new thread
    body = next(
        part for part in parsed.walk() if part.get_content_type() == "text/plain"
    )
    text = body.get_payload(decode=True).decode("utf-8")
    assert text.startswith(
        "Guarda qui sotto.\n\n---------- Messaggio inoltrato ----------"
    )
    for line in (
        'Da: "Mario Rossi" <mario@example.com>',
        "Oggetto: Verbale",
        "A: me@example.com, anna@example.com",
        "Cc: ufficio@example.com",
        "in allegato il verbale.",
    ):
        assert line in text
    files = [part.get_filename() for part in parsed.walk() if part.get_filename()]
    assert files == ["verbale.pdf", "azioni.xlsx"]  # not the inline logo


def test_forward_keeps_an_existing_prefix(sent) -> None:
    gmail.send_email(to="a@example.com", subject="Fwd: già inoltrato", forward_uid=7)

    assert sent[0]["Subject"] == "Fwd: già inoltrato"


def test_forward_respects_the_attachment_limits(sent, monkeypatch) -> None:
    monkeypatch.setattr(gmail, "_MAX_TOTAL_ATTACHMENT_BYTES", 5)

    with pytest.raises(gmail.GmailValidationError):
        gmail.send_email(to="a@example.com", forward_uid=7)
    assert sent == []


def test_reply_and_forward_cannot_be_combined(sent) -> None:
    with pytest.raises(gmail.GmailValidationError):
        gmail.send_email(to="a@example.com", reply_to_uid=1, forward_uid=7)


def test_send_op_passes_the_forward_fields(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(
        gmail, "send_email", lambda **kwargs: calls.append(kwargs) or "<id>"
    )

    api_for(SimpleNamespace()).call(
        "gmail.messages.send",
        {"to": "a@example.com", "forward_uid": 7, "forward_folder": "INBOX"},
    )

    assert calls[0]["forward_uid"] == 7
    assert calls[0]["forward_folder"] == "INBOX"


# ── client ───────────────────────────────────────────────────────────────────


def _run(body: str, state: dict) -> None:
    js = (
        'const assert = require("node:assert/strict");\n'
        f"const state = {json.dumps(state)};\n"
        + _js_functions(
            "displayAddress",
            "joinAddresses",
            "splitAddresses",
            "extractEmail",
            "prefixedSubject",
            "replyAllRecipients",
            "forwardedFileCount",
        )
        + body
    )
    _run_node(js)


def test_reply_all_recipients_skip_own_address_and_duplicates() -> None:
    _run(
        r"""
const detail = {
  from: '"Mario Rossi" <mario@example.com>',
  to: 'Me <ME@example.com>, "Bianchi, Anna" <anna@example.com>, mario@example.com',
  cc: "ufficio@example.com, anna@example.com",
};
assert.deepEqual(replyAllRecipients(detail), {
  to: ["mario@example.com", "anna@example.com"],
  cc: ["ufficio@example.com"],
});
// A mail sent only to ourselves: reply to the sender.
assert.deepEqual(
  replyAllRecipients({ from: "me@example.com", to: "me@example.com", cc: "" }),
  { to: ["me@example.com"], cc: [] },
);
""",
        {"status": {"address": "me@example.com"}},
    )


def test_subject_prefixes_and_forwarded_file_count() -> None:
    _run(
        r"""
assert.equal(prefixedSubject("Verbale", "Re:", /^re:/i), "Re: Verbale");
assert.equal(prefixedSubject("RE: Verbale", "Re:", /^re:/i), "RE: Verbale");
assert.equal(prefixedSubject("", "Fwd:", /^(fwd?|i):/i), "Fwd:");
assert.equal(prefixedSubject("I: Verbale", "Fwd:", /^(fwd?|i):/i), "I: Verbale");
assert.equal(forwardedFileCount({ attachments: [
  { inline: false }, { inline: true }, { inline: false },
] }), 2);
assert.equal(forwardedFileCount({}), 0);
""",
        {"status": None},
    )


def test_static_wiring() -> None:
    index = (_ROOT / "web/static/index.html").read_text(encoding="utf-8")
    script = (_ROOT / "web/static/gmail.js").read_text(encoding="utf-8")
    css = (_ROOT / "web/static/gmail.css").read_text(encoding="utf-8")

    # Icon actions with accessible names.
    for element_id, label in (
        ("gmail-reply", "Rispondi"),
        ("gmail-reply-all", "Rispondi a tutti"),
        ("gmail-forward", "Inoltra"),
        ("gmail-archive", "Archivia"),
        ("gmail-delete", "Elimina"),
        ("gmail-more", "Altre azioni"),
        ("gmail-refresh", "Ricarica"),
    ):
        assert re.search(
            rf'id="{element_id}"[^>]*aria-label="{label}"[^>]*title="{label}"', index
        ), element_id
    # Refresh lives in the list header; the text toggle in the "more" menu.
    list_top = index[
        index.index('class="gmail-list-top"') : index.index('id="gmail-not-configured"')
    ]
    assert 'id="gmail-refresh"' in list_top
    menu = index[index.index('id="gmail-more-menu"') : index.index("</article>")]
    assert 'id="gmail-render-toggle"' in menu and 'id="gmail-mark-unread"' in menu
    # Compose keeps its label next to the icon.
    assert re.search(
        r'id="gmail-compose-new"[^>]*>\s*<svg[^>]*>.*?</svg>\s*<span>Scrivi</span>',
        index,
        re.DOTALL,
    )
    # Behaviour hooks.
    assert 'form.append("forward_uid"' in script
    assert (
        'messagePath(uid, "archive")' in script
        and 'messagePath(uid, "unread")' in script
    )
    assert "pageCount() > 1" in script
    assert 'const parts = ["Configurato"]' not in script
    assert "#gmail-view.gmail-detail-open .gmail-back-button { display: none; }" in css
