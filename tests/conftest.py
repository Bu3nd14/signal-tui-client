"""
Shared fixtures for regression tests.
All tests use in-memory / tmp_path to avoid touching real files or daemons.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolate_backend_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate every test from the user's persistent cache and database."""
    import backend

    cache_dir = tmp_path / "backend-cache"
    cache_dir.mkdir()
    monkeypatch.setattr(backend, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(backend, "DB_FILE", cache_dir / "messages.db")
    monkeypatch.setattr(backend, "CACHE_FILE", cache_dir / "messages.json")
    return cache_dir


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
    # they are defined (``backends`` / ``signal_tui`` shim).
    with (
        patch("tui.app.BackendManager"),
        patch("tui.app.SignalBackend"),
        patch("tui.app.whatsapp_enabled", return_value=False),
        patch("tui.app.telegram_enabled", return_value=False),
        patch.object(SignalTUI, "on_mount", _noop_on_mount),
    ):
        app = SignalTUI()
        # Route manager.get(protocol) to the mocked Signal backend so selection
        # and send paths resolve to the same mock (easy to assert on).
        app.signal_backend.protocol = PROTOCOL_SIGNAL
        app.manager.get.return_value = app.signal_backend
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
        yield app, app.signal_backend
