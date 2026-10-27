"""Gmail unread badge: STATUS polling worker, ``gmail.unread`` op, web client.

The server polls ``STATUS INBOX (UNSEEN UIDNEXT)`` and publishes a
``gmail_unread`` web-push event when it changes; the SPA shows a red dot on
the Gmail tab and adds the count to the Dock/app badge total.
"""

from __future__ import annotations

import re
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from client_harness import api_for

from server import gmail, gmail_unread
from server.gmail_unread import GmailUnreadWatcher

_ROOT = Path(__file__).resolve().parents[1]

# ── fetch_unread_status ──────────────────────────────────────────────────────


class _WindowConnection:
    """Fake IMAP: read-only SELECT (EXISTS, UIDNEXT) and SEARCH."""

    def __init__(
        self,
        exists: object = b"500",
        uidnext: object = b"9000",
        unseen: bytes = b"301 450 500",
        select_status: str = "OK",
        search_status: str = "OK",
    ) -> None:
        self.exists = exists
        self.uidnext = uidnext
        self.unseen = unseen
        self.select_status = select_status
        self.search_status = search_status
        self.calls: list[tuple[object, ...]] = []

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return self.select_status, [self.exists]

    def response(self, code):
        return code, [self.uidnext]

    def search(self, charset, *criteria):
        self.calls.append(("search", *criteria))
        return self.search_status, [self.unseen]


def _patch_connection(monkeypatch, connection: _WindowConnection) -> None:
    @contextmanager
    def fake_connect(settings=None):
        yield connection

    monkeypatch.setattr(gmail, "imap_connect", fake_connect)
    monkeypatch.setattr(gmail, "load_settings", lambda: SimpleNamespace(folder="INBOX"))


def test_unread_counts_only_the_200_listed_messages(monkeypatch) -> None:
    connection = _WindowConnection()
    _patch_connection(monkeypatch, connection)

    assert gmail.fetch_unread_status() == {"unread": 3, "uidnext": 9000}
    assert connection.calls == [
        ("select", '"INBOX"', True),
        ("search", "UNSEEN", "301:500"),
    ]


def test_small_mailbox_window_starts_at_one(monkeypatch) -> None:
    connection = _WindowConnection(exists=b"12", unseen=b"")
    _patch_connection(monkeypatch, connection)

    assert gmail.fetch_unread_status() == {"unread": 0, "uidnext": 9000}
    assert connection.calls[-1] == ("search", "UNSEEN", "1:12")


def test_empty_mailbox_skips_the_search(monkeypatch) -> None:
    connection = _WindowConnection(exists=b"0", uidnext=None)
    _patch_connection(monkeypatch, connection)

    assert gmail.fetch_unread_status() == {"unread": 0, "uidnext": None}
    assert [call[0] for call in connection.calls] == ["select"]


@pytest.mark.parametrize(
    "connection",
    [
        _WindowConnection(select_status="NO"),
        _WindowConnection(exists=None),
        _WindowConnection(search_status="NO"),
    ],
)
def test_fetch_unread_status_rejects_bad_replies(monkeypatch, connection) -> None:
    _patch_connection(monkeypatch, connection)

    with pytest.raises(gmail.GmailUpstreamError):
        gmail.fetch_unread_status()


# ── watcher ──────────────────────────────────────────────────────────────────


def _watcher(statuses: list[object], *, enabled: bool = True, interval: float = 60):
    published: list[dict] = []

    def fetch():
        value = statuses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    watcher = GmailUnreadWatcher(
        published.append,
        interval_s=interval,
        fetch=fetch,
        enabled=lambda: enabled,
    )
    return watcher, published


def test_watcher_publishes_changes_and_flags_new_mail() -> None:
    watcher, published = _watcher(
        [
            {"unread": 2, "uidnext": 10},
            {"unread": 2, "uidnext": 10},  # unchanged: nothing published
            {"unread": 1, "uidnext": 10},  # read elsewhere: not new mail
            {"unread": 2, "uidnext": 11},  # new mail arrived
            gmail.GmailUpstreamError("down"),  # error: keep last, no event
        ]
    )
    for _ in range(5):
        watcher.check_once()

    assert [event["payload"] for event in published] == [
        {"unread": 2, "new_mail": False},
        {"unread": 1, "new_mail": False},
        {"unread": 2, "new_mail": True},
    ]
    assert all(event["type"] == "gmail_unread" for event in published)
    assert watcher.snapshot() == {"unread": 2, "uidnext": 11}


def test_watcher_does_nothing_when_gmail_is_not_configured() -> None:
    watcher, published = _watcher([], enabled=False)

    watcher.check_once()

    assert published == []
    assert watcher.snapshot() is None


def test_zero_interval_disables_the_worker(monkeypatch) -> None:
    monkeypatch.setenv("GMAIL_UNREAD_POLL_S", "0")
    assert gmail_unread.poll_interval_s() == 0
    watcher = GmailUnreadWatcher(lambda event: None)

    watcher.start()

    assert watcher._thread is None
    assert gmail_unread.latest() is None


@pytest.mark.parametrize(("raw", "expected"), [("", 60.0), ("30", 30.0), ("x", 60.0)])
def test_poll_interval_from_environment(monkeypatch, raw, expected) -> None:
    monkeypatch.setenv("GMAIL_UNREAD_POLL_S", raw)

    assert gmail_unread.poll_interval_s() == expected


def test_running_worker_polls_and_wakes_on_poke() -> None:
    fetched = threading.Event()
    calls: list[int] = []

    def fetch():
        calls.append(1)
        fetched.set()
        return {"unread": len(calls), "uidnext": 1}

    published: list[dict] = []
    watcher = GmailUnreadWatcher(
        published.append, interval_s=3600, fetch=fetch, enabled=lambda: True
    )
    watcher.start()
    try:
        assert fetched.wait(2)  # first check right away
        fetched.clear()
        gmail_unread.poke()  # module-level poke reaches the running worker
        assert fetched.wait(2)
        assert gmail_unread.latest() == {"unread": 2, "uidnext": 1}
    finally:
        watcher.stop()
        watcher._thread.join(2)
    assert not watcher._thread.is_alive()
    assert gmail_unread.latest() is None
    assert len(published) == 2


# ── gmail.unread op and pokes ────────────────────────────────────────────────


@pytest.fixture
def api():
    return api_for(SimpleNamespace())


def test_unread_op_when_gmail_is_not_configured(api, monkeypatch) -> None:
    monkeypatch.setattr(gmail, "status_snapshot", lambda: {"configured": False})

    assert api.call("gmail.unread", {}) == {"configured": False, "unread": 0}


def test_unread_op_uses_the_worker_value_or_fetches(api, monkeypatch) -> None:
    monkeypatch.setattr(gmail, "status_snapshot", lambda: {"configured": True})
    monkeypatch.setattr(gmail_unread, "latest", lambda: {"unread": 4, "uidnext": 9})
    assert api.call("gmail.unread", {}) == {"configured": True, "unread": 4}

    monkeypatch.setattr(gmail_unread, "latest", lambda: None)
    monkeypatch.setattr(gmail, "fetch_unread_status", lambda: {"unread": 7})
    assert api.call("gmail.unread", {}) == {"configured": True, "unread": 7}


@pytest.mark.parametrize(
    ("operation", "function", "data"),
    [
        ("gmail.messages.mark_read", "mark_read", {"uid": 5}),
        ("gmail.messages.delete", "delete_message", {"uid": 5}),
        ("gmail.messages.bulk_delete", "delete_messages", {"uids": [5]}),
    ],
)
def test_actions_that_change_the_count_poke_the_worker(
    api, monkeypatch, operation, function, data
) -> None:
    pokes: list[int] = []
    monkeypatch.setattr(gmail_unread, "poke", lambda: pokes.append(1))
    monkeypatch.setattr(
        gmail, function, lambda *a, **k: {"ok": True, "trashed": [], "missing": []}
    )

    api.call(operation, data)

    assert pokes == [1]


def test_core_owns_the_watcher_without_starting_threads() -> None:
    from unittest.mock import patch

    from server.bus import EventBus
    from server.core import ChatCore

    with (
        patch("server.core.whatsapp_enabled", return_value=False),
        patch("server.core.telegram_enabled", return_value=False),
    ):
        core = ChatCore(EventBus())

    assert isinstance(core.gmail_unread, GmailUnreadWatcher)
    assert core.gmail_unread._thread is None


def test_published_event_reaches_the_legacy_websocket_frame() -> None:
    from server.api import ChatAPI
    from server.bus import Event
    from web.legacy_events import to_legacy

    published: list[tuple[str, dict]] = []
    bus = SimpleNamespace(
        publish=lambda type_, payload: published.append((type_, payload))
    )
    api = ChatAPI(SimpleNamespace(bus=bus))
    watcher = GmailUnreadWatcher(
        api.publish_web_push,
        interval_s=60,
        fetch=lambda: {"unread": 3, "uidnext": 5},
        enabled=lambda: True,
    )
    watcher.check_once()

    ((type_, payload),) = published
    assert to_legacy(Event(1, type_, payload, 0.0)) == {
        "type": "gmail_unread",
        "payload": {"unread": 3, "new_mail": False},
    }


# ── web client (Node) ────────────────────────────────────────────────────────


def _app_function(source: str, name: str) -> str:
    match = re.search(rf"^(?:async )?function {name}\(", source, re.MULTILINE)
    assert match, name
    following = re.search(
        r"^(?:(?:async )?function |const |let |// |window\.|if \()",
        source[match.end() :],
        re.MULTILINE,
    )
    end = match.end() + following.start() if following else len(source)
    return source[match.start() : end]


def _run_node(source: str) -> None:
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


_FAKE_DOM = r"""
const assert = require("node:assert/strict");
function dot() { return { hidden: true }; }
const tabs = {
  signal: { dataset: { protocol: "signal" }, dot: dot() },
  whatsapp: { dataset: { protocol: "whatsapp" }, dot: dot() },
};
for (const tab of Object.values(tabs)) tab.querySelector = () => tab.dot;
const gmailDot = dot();
const bodyClasses = new Set();
const document = { body: { classList: { contains: (c) => bodyClasses.has(c) } } };
const elements = {
  protocolTabs: {
    querySelectorAll: () => Object.values(tabs),
    querySelector: (selector) => (selector === "#gmail-tab .unread-dot" ? gmailDot : null),
  },
};
const badges = [];
const navigator = {
  setAppBadge: (n) => { badges.push(n); return Promise.resolve(); },
  clearAppBadge: () => { badges.push(0); return Promise.resolve(); },
};
let faviconRed = false;
document.querySelector = () => ({ href: "" });
document.createElement = () => ({});
document.head = { append() {} };
const state = {
  gmailUnread: 0,
  protocolFilter: "signal",
  contacts: [
    { protocol: "signal", unread: 2 },
    { protocol: "whatsapp", unread: 1 },
    { protocol: "whatsapp", unread: 5, extras: { archived: true } },
  ],
};
"""


def test_tab_dots_gmail_dot_and_app_badge_total() -> None:
    source = (_ROOT / "web/static/app.js").read_text(encoding="utf-8")
    functions = "\n".join(
        _app_function(source, name)
        for name in (
            "unreadOf",
            "isArchived",
            "unreadOutsideArchive",
            "isGmailViewActive",
            "updateTabDots",
            "setGmailUnread",
            "updateFavicon",
        )
    )
    _run_node(
        _FAKE_DOM
        + functions
        + r"""
// Signal list on screen: dot only on WhatsApp (archived chats do not count).
updateTabDots();
assert.equal(tabs.signal.dot.hidden, true);
assert.equal(tabs.whatsapp.dot.hidden, false);
assert.equal(gmailDot.hidden, true);

// Gmail unread: its dot lights up and the Dock badge includes it (2+1+4).
setGmailUnread(4);
assert.equal(gmailDot.hidden, false);
assert.equal(badges.at(-1), 7);

// Gmail view open: no chat list on screen, so every backend shows its dot,
// while the Gmail dot hides (the list is on screen).
bodyClasses.add("gmail-active");
updateTabDots();
assert.equal(tabs.signal.dot.hidden, false);
assert.equal(tabs.whatsapp.dot.hidden, false);
assert.equal(gmailDot.hidden, true);

// Everything read: badge cleared.
bodyClasses.delete("gmail-active");
state.contacts = [];
setGmailUnread(0);
assert.equal(gmailDot.hidden, true);
assert.equal(badges.at(-1), 0);
setGmailUnread("garbage");
assert.equal(state.gmailUnread, 0);
"""
    )


def test_static_wiring() -> None:
    index = (_ROOT / "web/static/index.html").read_text(encoding="utf-8")
    app = (_ROOT / "web/static/app.js").read_text(encoding="utf-8")
    gmail_js = (_ROOT / "web/static/gmail.js").read_text(encoding="utf-8")

    assert re.search(
        r'id="gmail-tab"[^>]*>Gmail<span class="unread-dot" hidden>', index
    )
    assert 'case "gmail_unread":' in app
    assert 'apiFetch("api/gmail/unread")' in app
    assert 'window.addEventListener("gmail:view", updateTabDots);' in app
    assert gmail_js.count('new CustomEvent("gmail:view")') == 2
    assert 'window.addEventListener("gmail:new-mail"' in gmail_js
