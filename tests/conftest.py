"""
Shared fixtures for regression tests.
All tests use in-memory / tmp_path to avoid touching real files or daemons.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_cache_dir(tmp_path: Path) -> Path:
    """Create a temporary cache directory and patch backend constants."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    return cache_dir


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
            {"text": "Ciao!", "is_mine": False, "sender": "Mario",
             "timestamp": now_ms, "quote_text": None, "msg_type": "text",
             "attachment_info": None, "attachment_id": None, "read": False,
             "status": "read"},
            {"text": "Come stai?", "is_mine": True, "sender": "You",
             "timestamp": now_ms + 1, "quote_text": None, "msg_type": "text",
             "attachment_info": None, "attachment_id": None, "read": True,
             "status": "sent"},
        ],
        "+391111111111": [
            {"text": "Messaggio recente", "is_mine": False, "sender": "Luigi",
             "timestamp": now_ms - 1000, "quote_text": None, "msg_type": "text",
             "attachment_info": None, "attachment_id": None, "read": False,
             "status": "read"},
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
                {"contentType": "image/jpeg", "filename": "photo.jpg",
                 "id": "att-123", "caption": "Guarda!"},
            ],
            "quote": {},
        },
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
        {"number": "+391234567890", "name": "Mario Rossi",
         "uuid": "uuid-123"},
        {"number": "+391111111111", "name": "Luigi Verdi",
         "uuid": "uuid-456"},
    ]


@pytest.fixture
def sample_contacts_subprocess_output() -> str:
    """Return sample contact list as returned by signal-cli subprocess."""
    return (
        "Number:+391234567890 Name:Mario ACI:uuid-123\n"
        "Number:+391111111111 Name:Luigi Verdi ACI:uuid-456 Profile name:Luigi\n"
    )
