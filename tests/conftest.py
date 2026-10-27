"""
Shared fixtures for regression tests.
All tests use in-memory / tmp_path to avoid touching real files or daemons.
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def fail_on_unclosed_sqlite(monkeypatch: pytest.MonkeyPatch):
    """Fail any test that leaves a SQLite connection open.

    ``ResourceWarning: unclosed database`` is only emitted when the garbage
    collector finalises a leaked connection, i.e. inside an unrelated later
    test, so ``-W error`` cannot attribute or reliably fail it. Instead every
    connection opened through ``sqlite3.connect`` during the test is recorded
    (with strong references, so none is finalised early) and checked at
    teardown. ``with closing(sqlite3.connect(...)) as conn, conn`` does not close: use
    ``contextlib.closing`` or an explicit ``close()``.
    """
    real_connect = sqlite3.connect
    opened: list[tuple[sqlite3.Connection, str]] = []

    def recording_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        frame = sys._getframe(1)
        site = f"{frame.f_code.co_filename}:{frame.f_lineno} in {frame.f_code.co_name}"
        opened.append((conn, site))
        return conn

    monkeypatch.setattr(sqlite3, "connect", recording_connect)
    yield
    leaked = []
    for conn, site in opened:
        try:
            conn.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            continue  # closed
        except sqlite3.Error:
            pass  # open but unusable: still a leak
        leaked.append(site)
        conn.close()
    if leaked:
        pytest.fail(
            "SQLite connection(s) left open (use contextlib.closing / close()):\n  "
            + "\n  ".join(leaked),
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def isolate_gmail_imap_pool():
    """Reset the process-wide IMAP pool around every test.

    The pool lives at module level in :mod:`server.gmail`, so a connection left
    idle by one test would otherwise be reused by the next one, bypassing its
    freshly patched ``IMAP4_SSL`` fake.  Clearing before *and* after every test
    keeps the Gmail/link-preview suites isolated from module-level state.
    """
    from server import gmail

    gmail.clear_imap_pool()
    try:
        yield
    finally:
        gmail.clear_imap_pool()


@pytest.fixture(autouse=True)
def isolate_gmail_image_caches(monkeypatch: pytest.MonkeyPatch):
    """Empty the Gmail image caches and disable the remote image prefetch.

    Opening a message would otherwise start real network fetches for the
    images of the test HTML, and a cached image from one test would answer the
    next test's request instead of its patched fetcher.
    """
    from server import gmail
    from server.ops import gmail as gmail_ops

    monkeypatch.setenv("GMAIL_IMAGE_PREFETCH", "0")
    gmail.clear_inline_image_cache()
    gmail_ops.clear_remote_image_cache()
    try:
        yield
    finally:
        gmail.clear_inline_image_cache()
        gmail_ops.clear_remote_image_cache()


@pytest.fixture(autouse=True)
def isolate_backend_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate every test from the user's persistent cache and database."""
    import protocols.db as backend
    import protocols.download as download_mod
    import protocols.signal as signal_mod
    import protocols.whatsapp as whatsapp_mod
    from protocols import config, rpc

    cache_dir = tmp_path / "backend-cache"
    cache_dir.mkdir()
    monkeypatch.setattr(backend, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(backend, "DB_FILE", cache_dir / "messages.db")
    monkeypatch.setattr(backend, "CACHE_FILE", cache_dir / "messages.json")
    monkeypatch.setattr(rpc, "SIGNAL_CLI_ATTACHMENTS_DIR", cache_dir / "signal-media")
    monkeypatch.setattr(config, "get_whatsapp_media_dir", lambda: "")

    # Modules that bind these names by value at import time (no local import).
    monkeypatch.setattr(signal_mod, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(
        signal_mod, "SIGNAL_CLI_ATTACHMENTS_DIR", cache_dir / "signal-media"
    )
    monkeypatch.setattr(download_mod, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(whatsapp_mod, "get_whatsapp_media_dir", lambda: "")

    # Process-wide download globals: reset so the first test to use them
    # resolves under tmp_path. Resetting _DOWNLOAD_SERVER does not close a real
    # server; tests either mock it or stop it explicitly.
    monkeypatch.setattr(download_mod, "_TEMP_DOWNLOAD_DIR", None)
    monkeypatch.setattr(download_mod, "_DOWNLOAD_SERVER", None)
    monkeypatch.setattr(download_mod, "_DOWNLOAD_URL_BASE", None)
    return cache_dir


@pytest.fixture(autouse=True)
def isolate_server_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No test may bind (or probe) the user's real server socket: ``megachat
    serve`` and the socket adapter resolve their default path from this."""
    path = tmp_path / "megachat-run" / "server.sock"
    monkeypatch.setenv("MEGACHAT_SOCKET", str(path))
    return path


@pytest.fixture(autouse=True)
def isolate_instance_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Child processes (spawned servers, shell aliases) resolve the instance
    lock from this: the real ``/tmp/signal-tui.lock`` is never touched.
    (In-process code reads ``server.lock.LOCK_FILE``, which tests patch.)"""
    path = tmp_path / "signal-tui.lock"
    monkeypatch.setenv("MEGACHAT_LOCK_FILE", str(path))
    return path


@pytest.fixture
def open_media_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let ``media.*`` operations hand out any path a test's fake manager returns.

    The real operations confine attachments to the protocol's media root; tests
    of *callers* (TUI, adapters) that return arbitrary tmp files from a fake
    manager open the root.  Confinement itself is covered by
    ``tests/test_blob_ops.py``.
    """
    monkeypatch.setattr(
        "server.media._allowed_media_root", lambda _manager, _proto: Path("/")
    )


@pytest.fixture(autouse=True)
def bus_wire_guard(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Every event published on any ``EventBus`` must be JSON-serializable.

    The Phase 2 socket adapter forwards payloads unchanged, so a live object
    (``ChatContact``, set, Path, ...) on the bus is a bug.  Violations are
    collected (a publisher thread may swallow exceptions) and asserted at
    teardown.  Opt out with ``@pytest.mark.allow_unserializable_events``.
    """
    import json

    from server.bus import EventBus

    if request.node.get_closest_marker("allow_unserializable_events"):
        yield
        return
    original = EventBus.publish
    violations: list[str] = []

    def guarded(self, type, payload=None):
        try:
            json.dumps(payload)
        except (TypeError, ValueError) as exc:
            violations.append(f"{type}: {exc}")
        return original(self, type, payload)

    monkeypatch.setattr(EventBus, "publish", guarded)
    yield
    assert not violations, f"non-JSON event payloads published: {violations}"


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear SIGNAL_TUI_WEB_TOKEN so web-server tests are deterministic.

    Only this variable is cleared: SIGNAL_USER_NUMBER, WHATSAPP_API_URL and the
    LIVE_* flags must stay visible so the opt-in live tests remain runnable.
    """
    monkeypatch.delenv("SIGNAL_TUI_WEB_TOKEN", raising=False)


@pytest.fixture
def tmp_cache_dir(isolate_backend_cache: Path) -> Path:
    """Return the isolated cache directory for the current test."""
    return isolate_backend_cache


@pytest.fixture
def tmp_cache_file(tmp_cache_dir: Path) -> Path:
    """Return the path to the cache file inside the temp directory."""
    return tmp_cache_dir / "messages.json"


@pytest.fixture
def sample_messages() -> dict[str, list[dict]]:
    """Return a sample in-memory cache with recent timestamps."""
    import time

    now_ms = int(time.time() * 1000)
    return {
        "+391234567890": [
            {
                "text": "Ciao!",
                "is_mine": False,
                "sender": "Mario",
                "timestamp": now_ms,
                "quote_text": None,
                "msg_type": "text",
                "attachment_info": None,
                "attachment_id": None,
                "read": False,
                "status": "read",
            },
            {
                "text": "Come stai?",
                "is_mine": True,
                "sender": "You",
                "timestamp": now_ms + 1,
                "quote_text": None,
                "msg_type": "text",
                "attachment_info": None,
                "attachment_id": None,
                "read": True,
                "status": "sent",
            },
        ],
        "+391111111111": [
            {
                "text": "Messaggio recente",
                "is_mine": False,
                "sender": "Luigi",
                "timestamp": now_ms - 1000,
                "quote_text": None,
                "msg_type": "text",
                "attachment_info": None,
                "attachment_id": None,
                "read": False,
                "status": "read",
            },
        ],
    }


@pytest.fixture
def sample_envelope_text() -> dict:
    """Return a sample envelope with a text dataMessage."""
    return {
        "source": "+391234567890",
        "sourceNumber": "+391234567890",
        "sourceName": "Mario",
        "timestamp": 2000000,
        "dataMessage": {
            "message": "Hello!",
            "timestamp": 2000000,
            "quote": {},
        },
    }


@pytest.fixture
def sample_envelope_image() -> dict:
    """Return a sample envelope with an image attachment."""
    return {
        "source": "+391234567890",
        "sourceNumber": "+391234567890",
        "sourceName": "Mario",
        "timestamp": 3000000,
        "dataMessage": {
            "message": "",
            "timestamp": 3000000,
            "attachments": [
                {
                    "contentType": "image/jpeg",
                    "filename": "photo.jpg",
                    "id": "att-123",
                    "caption": "Guarda!",
                },
            ],
            "quote": {},
        },
    }


@pytest.fixture
def sample_envelope_quoting_image() -> dict:
    """Return a Signal envelope whose ``dataMessage`` quotes an image (no text).

    Sister fixture of ``sample_envelope_image``, used by bug #37 tests to
    exercise the media-quote fallback on the ``dataMessage`` path.
    """
    return {
        "source": "+391234567890",
        "sourceNumber": "+391234567890",
        "sourceName": "Mario",
        "timestamp": 5000000,
        "dataMessage": {
            "message": "Guarda!",
            "timestamp": 5000000,
            "quote": {
                "id": 4999000,
                "author": "+391234567890",
                "attachments": [{"contentType": "image/jpeg"}],
            },
        },
    }


@pytest.fixture
def wa_event_quoting_sticker() -> dict:
    """Return a raw WAHA message that quotes a sticker (nested ``quotedMessage``)."""
    return {
        "chatId": "391234567890@c.us",
        "from": "391234567890@c.us",
        "text": "guarda!",
        "timestamp": 1700000000,
        "quotedMessage": {"stickerMessage": {"id": "sticker-id-1"}},
    }


@pytest.fixture
def cached_media_target() -> dict:
    """Return a Telegram cache entry for a photo without a caption."""
    return {
        "id": "12",
        "text": "",
        "is_mine": False,
        "sender": "Ada",
        "timestamp": 1735787045000,
        "quote_text": None,
        "msg_type": "image",
        "attachment_info": "Photo",
        "attachment_id": "tgref:42:12",
    }


@pytest.fixture
def sample_envelope_receipt() -> dict:
    """Return a sample receiptMessage envelope."""
    return {
        "source": "+391234567890",
        "sourceNumber": "+391234567890",
        "timestamp": 4000000,
        "receiptMessage": {
            "isDelivery": True,
            "isRead": False,
            "timestamps": [1000001],
        },
    }


@pytest.fixture
def sample_contacts_rpc_output() -> list[dict]:
    """Return sample contact list as returned by RPC."""
    return [
        {"number": "+391234567890", "name": "Mario Rossi", "uuid": "uuid-123"},
        {"number": "+391111111111", "name": "Luigi Verdi", "uuid": "uuid-456"},
    ]


@pytest.fixture
def sample_contacts_subprocess_output() -> str:
    """Return sample contact list as returned by signal-cli subprocess."""
    return (
        "Number:+391234567890 Name:Mario ACI:uuid-123\n"
        "Number:+391111111111 Name:Luigi Verdi ACI:uuid-456 Profile name:Luigi\n"
    )


@contextmanager
def _make_test_app():
    """Build a ``SignalTUI`` with mocked, I/O-free backends and neutralized workers.

    Kept as a shared context manager so both ``app_for_test`` and
    ``app_for_test_with_mocks`` reuse the same setup.
    """
    from unittest.mock import MagicMock, patch

    from textual.containers import Vertical

    from models import PROTOCOL_SIGNAL, ChatContact
    from tui.app import SignalTUI

    def _noop_on_mount(self: SignalTUI) -> None:
        """Mount the UI without starting poll/connect workers, then render contacts."""
        self._chat_log = self.query_one("#chat-log", Vertical)
        self._render_contact_list(list(self.contacts))

    # Patch the symbols where they are IMPORTED/USED (``tui.app``), not where
    # they are defined (``protocols`` / ``signal_tui`` shim).
    with (
        patch("server.core.BackendManager"),
        patch("server.core.SignalBackend"),
        patch("server.core.whatsapp_enabled", return_value=False),
        patch("server.core.telegram_enabled", return_value=False),
        patch.object(SignalTUI, "on_mount", _noop_on_mount),
    ):
        app = SignalTUI()
        # Route manager.get(protocol) to the mocked Signal backend so selection
        # and send paths resolve to the same mock (easy to assert on).
        app.core.signal_backend.protocol = PROTOCOL_SIGNAL
        app.core.manager.get.return_value = app.core.signal_backend
        # Neutralize background workers (load-messages / send) to keep tests
        # deterministic and thread-free.
        app.run_worker = MagicMock()
        app.contacts = [
            ChatContact(
                id="+391234567890", display_name="Mario", protocol=PROTOCOL_SIGNAL
            ),
            ChatContact(
                id="+391111111111", display_name="Luigi", protocol=PROTOCOL_SIGNAL
            ),
            ChatContact(
                id="+392222222222", display_name="Giulia", protocol=PROTOCOL_SIGNAL
            ),
        ]
        yield app


@pytest.fixture
def app_for_test():
    """Create a ``SignalTUI`` instance ready for ``App.run_test()`` headless.

    No real I/O happens: the backends are mocked, WhatsApp/Telegram are
    disabled, the ``on_mount`` workers (poll loop + per-protocol connections)
    are neutralized, background workers are no-ops, and a small set of fake
    contacts is injected and rendered.
    """
    with _make_test_app() as app:
        yield app


@pytest.fixture
def app_for_test_with_mocks():
    """Like ``app_for_test`` but also yields the mocked Signal backend.

    Useful for tests that must assert on backend calls (e.g. message send).
    """
    with _make_test_app() as app:
        yield app, app.core.signal_backend
