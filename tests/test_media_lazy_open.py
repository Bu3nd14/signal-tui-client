"""Lazy WhatsApp media on chat open (S1, issue #310).

Opening a chat is metadata-only on the WAHA side (``downloadMedia=false``);
media rows without a local file are exposed as presentation-only "pending"
attachments (lazy token) and resolved one message at a time, serially, when
the UI asks for them.  Deterministic fakes only: no live WAHA, isolated
``DB_FILE``/``CACHE_DIR`` (conftest), no timing asserts.
"""

from __future__ import annotations

import sqlite3
import subprocess
import threading
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import protocols.db as backend_db
from models import (
    PROTOCOL_WHATSAPP,
    ChatContact,
    is_lazy_media_token,
    lazy_media_token,
    parse_lazy_media_token,
)
from protocols.db import _add_message_to_cache
from protocols.whatsapp import WhatsAppBackend
from server.messages import _messages, _refresh_history, messages_page

CHAT = "393331112222@c.us"
LID = "191023082934508@lid"
URL = "http://127.0.0.1:3005/api/files/default/true_393331112222@c.us_ABC.jpeg"


@pytest.fixture(autouse=True)
def _lazy_media_defaults(monkeypatch):
    monkeypatch.delenv("WA_LAZY_MEDIA", raising=False)
    monkeypatch.delenv("WA_LAZY_MEDIA_NEGATIVE_TTL_S", raising=False)
    monkeypatch.setattr("protocols.config._load_config", dict)


def _backend() -> WhatsAppBackend:
    backend = WhatsAppBackend(api_url="http://api.test", media_dir="")
    backend._rest = MagicMock()
    backend._connected = True
    return backend


def _media_row(msg_id: str | None = "ABC", *, contact: str = CHAT, **extra) -> None:
    fields = {
        "msg_type": "image",
        "attachment_info": None,
        "attachment_id": None,
        "content_type": "image/jpeg",
        "media_kind": "image",
    }
    fields.update(extra)
    _add_message_to_cache(
        contact,
        "",
        is_mine=False,
        sender=contact,
        timestamp=1_700_000_000_000,
        protocol=PROTOCOL_WHATSAPP,
        msg_id=msg_id,
        **fields,
    )


def _waha_message(msg_id: str = "ABC", url: str | None = URL) -> dict:
    return {
        "id": msg_id,
        "from": CHAT,
        "fromMe": False,
        "hasMedia": True,
        "body": "",
        "timestamp": 1_700_000_000,
        "media": {"url": url, "mimetype": "image/jpeg", "filename": None},
    }


def _db_attachment(msg_id: str, contact: str = CHAT) -> str | None:
    with closing(sqlite3.connect(backend_db.DB_FILE)) as conn:
        row = conn.execute(
            "SELECT attachment_id FROM messages WHERE contact_number = ? AND msg_id = ?",
            (contact, msg_id),
        ).fetchone()
    return row[0] if row else None


# ── 1. Pending payload ──────────────────────────────────────────────────────


def test_pending_payload_for_media_rows_without_attachment():
    _media_row("ABC")
    _add_message_to_cache(
        CHAT,
        "ciao",
        is_mine=False,
        sender=CHAT,
        timestamp=1_700_000_001_000,
        protocol=PROTOCOL_WHATSAPP,
        msg_id="TXT",
    )
    _add_message_to_cache(
        CHAT,
        "",
        is_mine=False,
        sender=CHAT,
        timestamp=1_700_000_002_000,
        protocol=PROTOCOL_WHATSAPP,
        msg_id="REAL",
        msg_type="image",
        attachment_info="photo.jpg",
        attachment_id="true_real.jpeg",
        content_type="image/jpeg",
        media_kind="image",
    )

    pending, text, real = _messages(PROTOCOL_WHATSAPP, CHAT)

    attachment = pending["attachment"]
    assert attachment["pending"] is True
    assert attachment["type"] == "image/jpeg"
    assert attachment["media_kind"] == "image"
    assert attachment["name"] == "Immagine"
    assert "/" not in attachment["attachment_id"]
    assert parse_lazy_media_token(attachment["attachment_id"]) == (CHAT, "ABC")
    assert text["attachment"] is None and text["text"] == "ciao"
    assert real["attachment"]["attachment_id"] == "true_real.jpeg"
    assert "pending" not in real["attachment"]
    # The token is presentation-only: never written to SQLite.
    assert _db_attachment("ABC") is None


def test_pending_payload_keeps_caption_and_document_name():
    _media_row("CAP", attachment_info="Guarda che tramonto")
    _media_row(
        "DOC",
        msg_type="attachment",
        attachment_info="report.pdf",
        content_type="application/pdf",
        media_kind="document",
    )

    image, document = _messages(PROTOCOL_WHATSAPP, CHAT)

    assert image["attachment"]["pending"] is True
    assert image["text"] == "Guarda che tramonto"
    assert image["attachment"]["name"] == "Immagine"
    assert document["attachment"]["pending"] is True
    assert document["attachment"]["name"] == "report.pdf"
    assert document["attachment"]["media_kind"] == "document"


def test_media_row_without_msg_id_gets_a_visible_placeholder():
    _media_row(None, msg_type="attachment", media_kind="video", content_type=None)

    (message,) = _messages(PROTOCOL_WHATSAPP, CHAT)

    assert message["attachment"] is None
    assert message["media_placeholder"] == "Video"


def test_other_protocols_never_get_pending_tokens():
    _add_message_to_cache(
        "alice",
        "",
        is_mine=False,
        sender="alice",
        timestamp=1_000,
        msg_type="image",
        media_kind="image",
        msg_id="S1",
    )

    (message,) = _messages("signal", "alice")

    assert message["attachment"] is None
    assert "media_placeholder" not in message


# ── 2. Token round-trip and validation ──────────────────────────────────────


def test_token_round_trip_and_rejections():
    token = lazy_media_token(LID, "true_191023082934508@lid_3EB0A1")
    assert token is not None and is_lazy_media_token(token)
    assert "/" not in token and "?" not in token
    assert parse_lazy_media_token(token) == (LID, "true_191023082934508@lid_3EB0A1")

    assert lazy_media_token(CHAT, "") is None
    assert lazy_media_token("", "ABC") is None
    assert lazy_media_token(CHAT, "../../etc/passwd") is None
    assert lazy_media_token(CHAT, "a/b") is None
    assert lazy_media_token("a..b@c.us", "ABC") is None

    assert parse_lazy_media_token("true_real.jpeg") is None
    assert parse_lazy_media_token("lazy-wa-") is None
    assert parse_lazy_media_token("lazy-wa-!!!~???") is None
    assert parse_lazy_media_token(token + "~eHg") is None
    assert parse_lazy_media_token(token + "==") is None
    assert parse_lazy_media_token(None) is None


def test_invalid_token_is_not_found_without_waha_calls():
    backend = _backend()
    bad = [
        "lazy-wa-garbage",
        "lazy-wa-!!!~???",
        lazy_media_token("status@broadcast", "ABC"),
    ]
    for token in bad:
        assert backend.get_attachment_path(str(token)) is None
    backend._rest.get_message_media.assert_not_called()
    backend._rest.download_media.assert_not_called()


# ── 3. Lazy resolve success + fast path ─────────────────────────────────────


def test_lazy_resolve_downloads_once_persists_and_heals_in_place():
    _media_row("ABC")
    backend = _backend()
    backend.cache[CHAT] = [{"id": "ABC", "timestamp": 1_700_000_000_000}]
    backend._media_pending[(CHAT, "ABC")] = {"attempts": 0, "next": 0, "ts": 0}
    backend._rest.get_message_media.return_value = _waha_message()
    backend._rest.download_media.return_value = b"jpeg-bytes"
    token = lazy_media_token(CHAT, "ABC")
    assert token is not None

    path = backend.get_attachment_path(token)

    assert isinstance(path, Path) and path.read_bytes() == b"jpeg-bytes"
    assert path.parent == backend._ensure_media_dir()
    assert path.name == Path(URL).name
    assert not list(path.parent.glob("*.part"))
    backend._rest.get_message_media.assert_called_once_with(CHAT, "ABC")
    backend._rest.download_media.assert_called_once_with(URL)
    assert _db_attachment("ABC") == URL
    assert (CHAT, "ABC") not in backend._media_pending
    event = backend._events.get_nowait()
    assert event.type == "message" and event.contact_id == CHAT
    assert event.payload["attachment_id"] == URL
    assert event.payload["timestamp"] == 1_700_000_000_000

    # Second open/scroll: local fast path, zero WAHA calls.
    assert backend.get_attachment_path(token) == path
    assert backend.get_attachment_path(URL) == path
    backend._rest.get_message_media.assert_called_once()
    backend._rest.download_media.assert_called_once()

    # The next page carries the real attachment: the token is gone.
    (message,) = _messages(PROTOCOL_WHATSAPP, CHAT)
    assert message["attachment"]["attachment_id"] == URL
    assert "pending" not in message["attachment"]


def test_resolved_event_heals_the_cache_row_as_changed():
    _media_row("ABC")
    backend = _backend()
    backend.cache[CHAT] = [
        {
            "id": "ABC",
            "text": "",
            "is_mine": False,
            "timestamp": 1_700_000_000_000,
            "msg_type": "image",
            "attachment_id": None,
            "media_kind": "image",
        }
    ]
    backend._rest.get_message_media.return_value = _waha_message()
    backend._rest.download_media.return_value = b"jpeg"

    backend.get_attachment_path(str(lazy_media_token(CHAT, "ABC")))
    event = backend._events.get_nowait()
    result = backend.ingest_message(
        event.contact_id, event.payload, event.payload["timestamp"]
    )

    assert result == "changed"
    assert backend.cache[CHAT][0]["attachment_id"] == URL
    assert len(backend.cache[CHAT]) == 1


def test_lazy_resolve_finds_the_row_under_an_alias_storage_key():
    _media_row("ABC", contact=CHAT)
    backend = _backend()
    contact = ChatContact(
        id=LID,
        display_name="Ada",
        protocol=PROTOCOL_WHATSAPP,
        extras={"phone": CHAT.split("@", 1)[0]},
    )
    backend.contacts = [contact]
    backend._contacts_by_jid = {LID: contact, CHAT: contact}
    backend._rest.get_message_media.return_value = _waha_message()
    backend._rest.download_media.return_value = b"jpeg"

    path = backend.get_attachment_path(str(lazy_media_token(LID, "ABC")))

    assert path is not None
    backend._rest.get_message_media.assert_called_once_with(CHAT, "ABC")
    assert _db_attachment("ABC", CHAT) == URL


# ── 4. Coalescing ───────────────────────────────────────────────────────────


class _RecordingInflight(dict):
    """Signals when a second request found the media already in flight."""

    def __init__(self):
        super().__init__()
        self.joined = threading.Event()

    def get(self, key, default=None):
        value = super().get(key, default)
        if value is not None:
            self.joined.set()
        return value


def test_concurrent_requests_for_the_same_media_are_coalesced():
    _media_row("ABC")
    backend = _backend()
    inflight = _RecordingInflight()
    backend._lazy_media_inflight = inflight
    entered = threading.Event()
    release = threading.Event()

    def slow_get(chat_id, msg_id):
        entered.set()
        assert release.wait(5)
        return _waha_message()

    backend._rest.get_message_media.side_effect = slow_get
    backend._rest.download_media.return_value = b"jpeg"
    token = str(lazy_media_token(CHAT, "ABC"))
    results: list[Path | None] = []
    first = threading.Thread(
        target=lambda: results.append(backend.get_attachment_path(token))
    )
    second = threading.Thread(
        target=lambda: results.append(backend.get_attachment_path(token))
    )

    first.start()
    assert entered.wait(5)
    second.start()
    assert inflight.joined.wait(5)
    release.set()
    first.join(5)
    second.join(5)

    assert backend._rest.get_message_media.call_count == 1
    assert backend._rest.download_media.call_count == 1
    assert len(results) == 2 and results[0] == results[1] and results[0] is not None


# ── 5. Serialisation ────────────────────────────────────────────────────────


class _RecordingGate:
    """``Semaphore(1)`` that signals when a second request queues on it."""

    def __init__(self):
        self._sem = threading.Semaphore(1)
        self.attempts = 0
        self.second_waiting = threading.Event()
        self._lock = threading.Lock()

    def acquire(self, timeout=None):
        with self._lock:
            self.attempts += 1
            if self.attempts >= 2:
                self.second_waiting.set()
        return self._sem.acquire(timeout=timeout)

    def release(self):
        self._sem.release()


def test_different_media_are_downloaded_one_at_a_time():
    _media_row("ONE")
    _media_row("TWO")
    backend = _backend()
    gate = _RecordingGate()
    backend._lazy_media_gate = gate  # type: ignore[assignment]
    active = 0
    peak = 0
    counter = threading.Lock()
    entered = threading.Event()
    release = threading.Event()

    def get_media(chat_id, msg_id):
        nonlocal active, peak
        with counter:
            active += 1
            peak = max(peak, active)
        entered.set()
        if msg_id == "ONE":
            assert release.wait(5)
        with counter:
            active -= 1
        return _waha_message(msg_id, URL.replace("ABC", msg_id))

    backend._rest.get_message_media.side_effect = get_media
    backend._rest.download_media.return_value = b"jpeg"
    one = threading.Thread(
        target=backend.get_attachment_path, args=(str(lazy_media_token(CHAT, "ONE")),)
    )
    two = threading.Thread(
        target=backend.get_attachment_path, args=(str(lazy_media_token(CHAT, "TWO")),)
    )

    one.start()
    assert entered.wait(5)
    two.start()
    assert gate.second_waiting.wait(5)
    assert backend._rest.get_message_media.call_count == 1
    release.set()
    one.join(5)
    two.join(5)

    assert backend._rest.get_message_media.call_count == 2
    assert peak == 1
    # Rows the core does not hold are healed in the DB only (no re-ingest).
    assert backend._events.empty()
    assert _db_attachment("ONE") and _db_attachment("TWO")


# ── 6. Failures + negative cache ────────────────────────────────────────────


@pytest.mark.parametrize(
    "response",
    [None, _waha_message(url=None), {"id": "ABC", "media": None}],
)
def test_failure_returns_none_and_is_negatively_cached(response):
    _media_row("ABC")
    backend = _backend()
    backend._rest.get_message_media.return_value = response
    token = str(lazy_media_token(CHAT, "ABC"))

    assert backend.get_attachment_path(token) is None
    assert backend.get_attachment_path(token) is None

    backend._rest.get_message_media.assert_called_once()
    backend._rest.download_media.assert_not_called()
    assert _db_attachment("ABC") is None
    assert backend._events.empty()


def test_failed_download_is_retryable_after_the_negative_ttl(monkeypatch):
    monkeypatch.setenv("WA_LAZY_MEDIA_NEGATIVE_TTL_S", "0")
    _media_row("ABC")
    backend = _backend()
    backend._rest.get_message_media.return_value = _waha_message()
    backend._rest.download_media.side_effect = [None, b"jpeg"]
    token = str(lazy_media_token(CHAT, "ABC"))

    assert backend.get_attachment_path(token) is None
    assert _db_attachment("ABC") is None
    assert backend.get_attachment_path(token) is not None
    assert backend._rest.get_message_media.call_count == 2


def test_stopped_backend_does_not_resolve_and_releases_waiters():
    _media_row("ABC")
    backend = _backend()
    waiter = {"done": threading.Event(), "path": None}
    backend._lazy_media_inflight[(CHAT, "OTHER")] = waiter
    backend.disconnect_sync()

    assert waiter["done"].is_set()
    assert backend.get_attachment_path(str(lazy_media_token(CHAT, "ABC"))) is None
    backend._rest.get_message_media.assert_not_called()


def test_pending_token_has_no_remote_chunk():
    _media_row("ABC")
    backend = _backend()
    token = str(lazy_media_token(CHAT, "ABC"))

    assert backend.get_attachment_chunk(token, 0, 10) is None
    backend._rest.get_message_media.assert_not_called()

    backend._rest.get_message_media.return_value = _waha_message()
    backend._rest.download_media.return_value = b"0123456789abcdef"
    assert backend.get_attachment_path(token) is not None
    assert backend.get_attachment_chunk(token, 2, 4) == b"2345"


# ── 7. Chat open fetch ──────────────────────────────────────────────────────


def test_refresh_history_on_chat_open_is_metadata_only_for_whatsapp():
    backend = MagicMock()
    backend.fetch_history = MagicMock(return_value=[])

    assert _refresh_history(backend, PROTOCOL_WHATSAPP, CHAT, 20) is True
    backend.fetch_history.assert_called_once_with(
        CHAT,
        20,
        download_media=False,
        resolve_media=False,
        ingest_pending_media=True,
    )


def test_refresh_history_for_telegram_is_unchanged():
    backend = MagicMock()
    backend.fetch_history = MagicMock(return_value=[])

    assert _refresh_history(backend, "telegram", "42", 20) is True
    backend.fetch_history.assert_called_once_with("42", 20)


def test_fetch_history_ingests_pending_media_without_scheduling_the_resolver():
    backend = _backend()
    backend._schedule_media_resolve = MagicMock()  # type: ignore[method-assign]
    backend._rest.list_messages.return_value = [
        _waha_message("M1", url=None),
        _waha_message("M2", url=None),
    ]

    backend.fetch_history(
        CHAT,
        20,
        download_media=False,
        resolve_media=False,
        ingest_pending_media=True,
    )

    backend._rest.list_messages.assert_called_once_with(
        CHAT, limit=20, download_media=False
    )
    backend._schedule_media_resolve.assert_not_called()
    assert {m["id"] for m in backend.cache[CHAT]} == {"M1", "M2"}
    assert all(not m.get("attachment_id") for m in backend.cache[CHAT])
    assert {m["msg_type"] for m in backend.cache[CHAT]} == {"image"}
    page = _messages(PROTOCOL_WHATSAPP, CHAT)
    assert [m["attachment"]["media_kind"] for m in page] == ["image", "image"]
    assert all(m["attachment"]["pending"] is True for m in page)


def test_chat_open_page_triggers_zero_waha_media_downloads():
    backend = _backend()
    backend._rest.list_messages.return_value = [
        _waha_message(f"M{i}", url=None) | {"timestamp": 1_700_000_000 + i}
        for i in range(50)
    ]
    manager = MagicMock()
    manager.get.return_value = backend
    manager.list_contacts.return_value = []

    page = messages_page(manager, PROTOCOL_WHATSAPP, CHAT, refresh=True)

    assert len(page) == 50
    assert all(m["attachment"]["pending"] is True for m in page)
    assert all(
        c.kwargs["download_media"] is False
        for c in backend._rest.list_messages.call_args_list
    )
    backend._rest.get_message_media.assert_not_called()
    backend._rest.download_media.assert_not_called()
    assert backend._media_pending == {}


# ── 8. Startup unchanged (#309) ─────────────────────────────────────────────


def test_startup_resync_keeps_skipping_pending_media(monkeypatch):
    monkeypatch.setattr(
        "protocols.whatsapp.get_wa_resync_download_media", lambda: False
    )
    monkeypatch.setattr("protocols.whatsapp.time.sleep", lambda _s: None)
    backend = _backend()
    backend.cache = {CHAT: [{"id": "seed", "timestamp": 1}]}
    backend._rest._request.return_value = []
    backend._rest.list_messages.return_value = [_waha_message("M1", url=None)]

    backend.resync_history()

    assert [m["id"] for m in backend.cache[CHAT]] == ["seed"]
    backend._rest.get_message_media.assert_not_called()


# ── Kill switch ─────────────────────────────────────────────────────────────


def test_kill_switch_reverts_fetch_and_pending_payload(monkeypatch):
    monkeypatch.setenv("WA_LAZY_MEDIA", "false")
    _media_row("ABC")
    backend = MagicMock()
    backend.fetch_history = MagicMock(return_value=[])

    _refresh_history(backend, PROTOCOL_WHATSAPP, CHAT, 20)
    (message,) = _messages(PROTOCOL_WHATSAPP, CHAT)

    backend.fetch_history.assert_called_once_with(CHAT, 20)
    assert message["attachment"] is None
    assert "media_placeholder" not in message


def test_lazy_media_config(monkeypatch):
    from protocols.config import get_wa_lazy_media, get_wa_lazy_media_negative_ttl_s

    assert get_wa_lazy_media() is True
    for value, expected in (("false", False), (" OFF ", False), ("0", False)):
        monkeypatch.setenv("WA_LAZY_MEDIA", value)
        assert get_wa_lazy_media() is expected
    monkeypatch.setenv("WA_LAZY_MEDIA", "garbage")
    assert get_wa_lazy_media() is True

    assert get_wa_lazy_media_negative_ttl_s() == 30
    monkeypatch.setenv("WA_LAZY_MEDIA_NEGATIVE_TTL_S", "-4")
    assert get_wa_lazy_media_negative_ttl_s() == 30
    monkeypatch.setenv("WA_LAZY_MEDIA_NEGATIVE_TTL_S", "0")
    assert get_wa_lazy_media_negative_ttl_s() == 0


# ── 9. Web DOM ──────────────────────────────────────────────────────────────


def _run_node(source: str) -> None:
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_web_pending_image_loads_only_when_visible():
    token = lazy_media_token(CHAT, "ABC")
    _run_node(
        r"""
const assert = require("node:assert/strict")
const fs = require("node:fs")
const vm = require("node:vm")
const app = fs.readFileSync("./web/static/app.js", "utf8")
const start = app.indexOf("function mediaUrl(")
const end = app.indexOf("\nfunction attachmentName(", start)
function node(tag) {
  return {
    tag, className: "", children: [], attributes: {}, listeners: {},
    append(...children) { this.children.push(...children) },
    setAttribute(name, value) { this.attributes[name] = value },
    addEventListener(name, callback) { this.listeners[name] = callback },
  }
}
globalThis.document = { createElement: node }
const observed = []
const loads = []
globalThis.state = { mediaCache: new Map() }
globalThis.mediaObserver = () => ({ observe: (target) => observed.push(target) })
globalThis.loadThumbnail = (...args) => loads.push(args)
globalThis.openImageModal = () => {}
globalThis.captureMediaDims = () => {}
globalThis.showImageFallback = () => {}
vm.runInThisContext(app.slice(start, end))
const token = "TOKEN"
const container = imageAttachment(
  { attachment_id: token, name: "Immagine", type: "image/jpeg", media_kind: "image", pending: true },
  "whatsapp", "in", null,
)
assert.equal(container.children[0].className, "attachment-loading")
assert.equal(loads.length, 0)
assert.deepEqual(observed, [container])
container._loadMedia()
assert.equal(loads.length, 1)
assert.equal(loads[0][2], `api/media/whatsapp/${token}?w=480`)
""".replace("TOKEN", str(token))
    )


def test_web_media_placeholder_is_never_an_empty_bubble():
    _run_node(r"""
const assert = require("node:assert/strict")
const fs = require("node:fs")
const vm = require("node:vm")
const app = fs.readFileSync("./web/static/app.js", "utf8")
const start = app.indexOf("function buildMessageNode(")
const end = app.indexOf("\nfunction renderMessages(", start)
function node(tag) {
  return {
    tag, className: "", textContent: "", children: [], attributes: {}, listeners: {},
    childElementCount: 0,
    append(...children) { this.children.push(...children); this.childElementCount += children.length },
    replaceChildren(...children) { this.children = children },
    setAttribute(name, value) { this.attributes[name] = value },
    addEventListener(name, callback) { this.listeners[name] = callback },
  }
}
globalThis.document = { createElement: node }
globalThis.window = { SignalTuiReconcile: { messageDisplayText: (item) => item.text || "" } }
globalThis.state = {}
globalThis.appendRenderedQuote = () => {}
globalThis.formatTimestamp = () => "12:00"
globalThis.setMessageTick = () => {}
globalThis.linkifyText = (text) => text
globalThis.linkPreviewSlot = () => null
vm.runInThisContext(app.slice(start, end))
const { el } = buildMessageNode(
  { id: "x", direction: "in", text: "", timestamp: 1, attachment: null, media_placeholder: "Video" },
  "whatsapp",
  null,
)
const bubble = el.children[0]
const placeholder = bubble.children.find((child) => child.className === "attachment-error")
assert.ok(placeholder, "placeholder missing")
assert.equal(placeholder.textContent, "▧  Video non disponibile")
""")


# ── 10. TUI ─────────────────────────────────────────────────────────────────


class _FakeTui:
    """Just the state ``_apply_resolved_media`` and the widget builder use."""

    def __init__(self, selected: ChatContact | None):
        self.selected_contact = selected
        self._cache: dict[str, list[dict]] = {}
        self.calls: list[tuple] = []

    def call_from_thread(self, fn, *args):
        self.calls.append((fn, args))

    def _rerender_open_chat(self, contact):  # pragma: no cover - only referenced
        raise AssertionError("must be scheduled via call_from_thread")


def _contact(cid: str = LID) -> ChatContact:
    return ChatContact(id=cid, display_name="Ada", protocol=PROTOCOL_WHATSAPP)


def test_tui_changed_media_event_updates_the_existing_entry_and_rerenders():
    from tui.events import EventHandlingMixin

    contact = _contact()
    tui = _FakeTui(contact)
    entry = {"id": "ABC", "msg_type": "image", "attachment_id": None, "timestamp": 1}
    tui._cache[contact.cache_key] = [entry]

    EventHandlingMixin._apply_resolved_media(
        tui,  # type: ignore[arg-type]
        contact,
        {
            "id": "ABC",
            "attachment_id": URL,
            "msg_type": "image",
            "media_kind": "image",
            "content_type": "image/jpeg",
        },
    )

    assert entry["attachment_id"] == URL and entry["media_kind"] == "image"
    assert len(tui._cache[contact.cache_key]) == 1
    assert [(fn.__name__, args) for fn, args in tui.calls] == [
        ("_rerender_open_chat", (contact,))
    ]


def test_tui_changed_media_event_never_swaps_a_real_attachment():
    from tui.events import EventHandlingMixin

    contact = _contact()
    tui = _FakeTui(None)
    entry = {"id": "ABC", "attachment_id": "true_old.jpeg", "timestamp": 1}
    tui._cache[contact.cache_key] = [entry]

    EventHandlingMixin._apply_resolved_media(
        tui,  # type: ignore[arg-type]
        contact,
        {"id": "ABC", "attachment_id": URL},
    )

    assert entry["attachment_id"] == "true_old.jpeg"
    assert tui.calls == []


def test_tui_pending_media_widget_gets_a_lazy_token_not_stored_in_cache():
    from tui.chat_view import ChatViewMixin

    tui = _FakeTui(_contact())
    msg = {"id": "ABC", "msg_type": "image", "attachment_id": None}

    token = ChatViewMixin._lazy_media_attachment_id(
        tui,  # type: ignore[arg-type]
        PROTOCOL_WHATSAPP,
        msg,
        "image",
    )

    assert parse_lazy_media_token(token) == (LID, "ABC")
    assert msg["attachment_id"] is None
    for protocol, row, kind in (
        ("signal", msg, "image"),
        (PROTOCOL_WHATSAPP, {"id": "T", "msg_type": "text"}, None),
        (PROTOCOL_WHATSAPP, {"id": "R", "attachment_id": "real.jpg"}, "image"),
        (PROTOCOL_WHATSAPP, {"msg_type": "image"}, "image"),
    ):
        assert (
            ChatViewMixin._lazy_media_attachment_id(
                tui,  # type: ignore[arg-type]
                protocol,
                row,
                kind,
            )
            is None
        )
