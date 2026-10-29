"""Regression tests for the startup history/media burst design v3.1.

Scope B + S2: the startup bulk resync fetches metadata/text only
(``downloadMedia=false``) and skips media without a real attachment, while the
chat-open/live paths keep their previous defaults.  No network, no timing
asserts: ``time.sleep`` and ``ThreadPoolExecutor`` are patched where relevant.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import protocols.db as backend_mod
from protocols.whatsapp import WhatsAppBackend


def _make_backend() -> WhatsAppBackend:
    backend = WhatsAppBackend(api_url="http://api.test", media_dir="")
    backend._rest = MagicMock()
    backend._connected = True
    backend._chats_last_refresh = 0.0
    backend._CHATS_REFRESH_INTERVAL = 15.0
    return backend


@pytest.fixture(autouse=True)
def _deterministic_resync_config(monkeypatch):
    monkeypatch.setattr(
        "protocols.whatsapp.get_wa_resync_download_media", lambda: False
    )
    monkeypatch.setattr("protocols.whatsapp.get_wa_resync_limit", lambda: 30)
    monkeypatch.setattr("protocols.whatsapp.get_wa_resync_workers", lambda: 2)
    monkeypatch.setattr("protocols.whatsapp.get_wa_resync_max_chats", lambda: 20)


def _media(chat_id: str, msg_id: str, *, url: str | None, is_mine=False, ack=None):
    payload = {
        "id": msg_id,
        "chatId": chat_id,
        "from": chat_id,
        "fromMe": is_mine,
        "hasMedia": True,
        "media": {"url": url, "mimetype": "image/jpeg"},
        "body": "",
        "timestamp": 1_700_000_000,
    }
    if ack is not None:
        payload["ack"] = ack
    return payload


def test_t1_bulk_query_forces_download_media_false():
    backend = _make_backend()
    backend.cache = {"123@c.us": [{"id": "a", "timestamp": 1}]}
    backend._rest._request.return_value = []
    backend._rest.list_messages.return_value = []

    assert backend.resync_history() == 1

    calls = backend._rest.list_messages.call_args_list
    assert calls
    assert all(c.kwargs.get("download_media") is False for c in calls)


def test_t2_bulk_never_schedules_the_media_resolver():
    backend = _make_backend()
    backend.cache = {"123@c.us": [{"id": "a", "timestamp": 1}]}
    backend._rest._request.return_value = []
    backend._rest.list_messages.return_value = [
        _media("123@c.us", "m1", url=None),
        _media("123@c.us", "m2", url=None),
    ]

    backend.resync_history()

    assert backend._media_pending == {}
    backend._rest.get_message_media.assert_not_called()


def test_t2b_media_none_without_text_is_not_ingested():
    backend = _make_backend()
    backend.cache = {"123@c.us": [{"id": "a", "timestamp": 1}]}
    backend._rest.list_messages.return_value = [
        {
            "id": "m1",
            "from": "123@c.us",
            "fromMe": False,
            "hasMedia": True,
            "media": None,
            "body": "",
            "timestamp": 1_700_000_000,
        }
    ]
    backend.ingest_message = MagicMock(return_value=True)

    backend.fetch_history("123@c.us", resolve_media=False)

    backend.ingest_message.assert_not_called()


def test_t2b_media_none_with_text_is_ingested_as_text():
    backend = _make_backend()
    backend._rest.list_messages.return_value = [
        {
            "id": "m1",
            "from": "123@c.us",
            "fromMe": False,
            "hasMedia": True,
            "media": None,
            "body": "ciao",
            "timestamp": 1_700_000_000,
        }
    ]
    backend.ingest_message = MagicMock(return_value=True)

    result = backend.fetch_history("123@c.us", resolve_media=False)

    assert len(result) == 1
    backend.ingest_message.assert_called_once()
    data = backend.ingest_message.call_args.args[1]
    assert data["text"] == "ciao"
    assert data["media_kind"] is None


def test_t2d_skipped_mine_media_still_emits_read_receipt():
    backend = _make_backend()
    backend._rest.list_messages.return_value = [
        _media("123@c.us", "mine-media", url=None, is_mine=True, ack=3)
    ]
    backend.ingest_message = MagicMock(return_value=True)

    result = backend.fetch_history("123@c.us", resolve_media=False)

    assert result
    backend.ingest_message.assert_not_called()
    events = backend.poll_once()
    receipts = [e for e in events if e.type == "receipt"]
    assert len(receipts) == 1
    assert receipts[0].payload["message_ids"] == ["mine-media"]
    assert receipts[0].payload["is_read"] is True


def test_t2c_mixed_bulk_ingests_text_and_url_media_only():
    backend = _make_backend()
    backend._rest.list_messages.return_value = [
        {
            "id": "t1",
            "from": "123@c.us",
            "fromMe": False,
            "body": "ciao",
            "timestamp": 1_700_000_000,
        },
        _media("123@c.us", "m-null", url=None),
        _media("123@c.us", "m-url", url="https://wa.test/m-url.jpg"),
    ]
    backend.ingest_message = MagicMock(return_value=True)

    backend.fetch_history("123@c.us", resolve_media=False)

    ingested = [call.args[1] for call in backend.ingest_message.call_args_list]
    assert [data["id"] for data in ingested] == ["t1", "m-url"]
    assert ingested[1]["attachment_id"] == "https://wa.test/m-url.jpg"
    assert ingested[1]["media_kind"] == "image"
    assert backend._media_pending == {}
    backend._rest.get_message_media.assert_not_called()


def test_t3_cap_and_tie_break_unread_then_ts_then_jid():
    backend = _make_backend()
    backend.cache = {
        "111@c.us": [{"id": "a", "timestamp": 9000}],
        "222@c.us": [{"id": "b", "timestamp": 9000}],
        "555@c.us": [{"id": "c", "timestamp": 8000}],
    }
    backend._discover_active_chats = MagicMock(
        return_value=[("400@c.us", 2, 2), ("300@c.us", 1, 1)]
    )

    assert backend._select_resync_targets(3) == [
        "400@c.us",
        "300@c.us",
        "111@c.us",
    ]


def test_cache_last_ts_skips_invalid_rows_and_returns_max():
    from protocols.whatsapp import _cache_last_ts

    rows = [
        "x",
        {"timestamp": "bad"},
        {"timestamp": 5},
        {"timestamp": 9},
    ]

    assert _cache_last_ts(rows) == 9
    assert _cache_last_ts([]) == 0


def test_select_resync_targets_filters_non_string_and_broadcast_jids():
    backend = _make_backend()
    backend.cache = {
        "status@broadcast": [{"id": "status", "timestamp": 1}],
        "123@c.us": [{"id": "ok", "timestamp": 5}],
    }
    backend._discover_active_chats = MagicMock(
        return_value=[
            (None, 1, 1),
            ("status@broadcast", 1, 2),
            ("false_status@broadcast_123@c.us", 1, 3),
            ("456@c.us", 0, 4),
        ]
    )

    with patch("protocols.whatsapp._is_fetchable_jid", return_value=True):
        targets = backend._select_resync_targets(20)

    assert set(targets) == {"123@c.us", "456@c.us"}


def test_t5_single_flag_rollback_restores_download_and_resolution():
    backend = _make_backend()
    backend.cache = {"123@c.us": [{"id": "a", "timestamp": 1}]}
    backend._rest._request.return_value = []
    backend._rest.list_messages.return_value = [_media("123@c.us", "m1", url=None)]
    backend._start_media_resolver = MagicMock()

    with patch("protocols.whatsapp.get_wa_resync_download_media", return_value=True):
        backend.resync_history()

    assert backend._rest.list_messages.call_args.kwargs["download_media"] is True
    assert backend._media_pending


class _InlineExecutor:
    instances: ClassVar[list[int]] = []

    def __init__(self, max_workers=1):
        self.max_workers = max_workers
        _InlineExecutor.instances.append(max_workers)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def map(self, fn, iterable):
        return [fn(item) for item in iterable]


def test_t5b_full_rollback_uses_download_limit_workers_and_no_cap():
    backend = _make_backend()
    backend.cache = {
        f"{1000 + i}@c.us": [{"id": f"a{i}", "timestamp": i}] for i in range(25)
    }
    backend._rest._request.return_value = []
    backend._rest.list_messages.side_effect = lambda cid, limit=1, download_media=True: [
        _media(cid, f"m-{cid}", url=None)
    ]
    backend._start_media_resolver = MagicMock()
    _InlineExecutor.instances.clear()

    with (
        patch("protocols.whatsapp.get_wa_resync_download_media", return_value=True),
        patch("protocols.whatsapp.get_wa_resync_limit", return_value=50),
        patch("protocols.whatsapp.get_wa_resync_workers", return_value=4),
        patch("protocols.whatsapp.get_wa_resync_max_chats", return_value=0),
        patch("protocols.whatsapp.ThreadPoolExecutor", _InlineExecutor),
        patch("protocols.whatsapp.time.sleep"),
    ):
        assert backend.resync_history() == 25

    assert _InlineExecutor.instances == [4]
    calls = backend._rest.list_messages.call_args_list
    assert len(calls) == 25
    assert all(c.kwargs["download_media"] is True for c in calls)
    assert all(c.kwargs["limit"] == 50 for c in calls)
    assert backend._media_pending


def test_t4_zero_max_chats_is_unlimited_and_broadcast_excluded():
    backend = _make_backend()
    valid = {f"{1000 + i}@c.us": [{"id": f"a{i}", "timestamp": i}] for i in range(22)}
    invalid = {
        "status@broadcast": [{"id": "status", "timestamp": 99}],
        "false_status@broadcast_123@c.us": [{"id": "status2", "timestamp": 99}],
        "42@newsletter": [{"id": "news", "timestamp": 99}],
    }
    backend.cache = {**valid, **invalid}
    backend._rest._request.return_value = []
    backend._rest.list_messages.return_value = []
    _InlineExecutor.instances.clear()

    with (
        patch("protocols.whatsapp.get_wa_resync_max_chats", return_value=0),
        patch("protocols.whatsapp.ThreadPoolExecutor", _InlineExecutor),
        patch("protocols.whatsapp.time.sleep"),
    ):
        assert backend.resync_history() == 22

    called = {call.args[0] for call in backend._rest.list_messages.call_args_list}
    assert called == set(valid)
    assert not called & set(invalid)


def test_t6_waha_down_stays_on_db_chats_without_raising():
    backend = _make_backend()
    backend.cache = {"111@c.us": [{"id": "a", "timestamp": 1}]}
    backend._rest._request.side_effect = RuntimeError("waha down")
    backend._rest.list_messages.return_value = []

    assert backend.resync_history() == 1

    backend._rest.list_messages.assert_called_once_with(
        "111@c.us", limit=30, download_media=False
    )


def test_t7_limit_workers_and_constant_stagger():
    backend = _make_backend()
    backend.cache = {
        f"{2000 + i}@c.us": [{"id": f"a{i}", "timestamp": i}] for i in range(7)
    }
    backend._rest._request.return_value = []
    backend._rest.list_messages.return_value = []
    _InlineExecutor.instances.clear()

    with (
        patch("protocols.whatsapp.get_wa_resync_workers", return_value=3),
        patch("protocols.whatsapp.ThreadPoolExecutor", _InlineExecutor),
        patch("protocols.whatsapp.time.sleep") as sleep,
    ):
        assert backend.resync_history(limit=7) == 7

    assert _InlineExecutor.instances == [3]
    calls = backend._rest.list_messages.call_args_list
    assert len(calls) == 7
    assert all(call.kwargs["limit"] == 7 for call in calls)
    assert sleep.call_count == 2


def test_t8_chat_open_refresh_keeps_positional_call_without_kwargs():
    from server.messages import _refresh_history

    backend = MagicMock()
    backend.fetch_history = MagicMock(return_value=[])

    assert _refresh_history(backend, "whatsapp", "123@c.us", 20) is True
    backend.fetch_history.assert_called_once_with("123@c.us", 20)


def test_t9_out_of_window_media_are_absent_not_empty_bubbles(tmp_path: Path):
    db_file = tmp_path / "messages.db"
    with (
        patch.object(backend_mod, "DB_FILE", db_file),
        patch.object(backend_mod, "CACHE_DIR", tmp_path),
    ):
        backend = _make_backend()
        backend._rest._request.return_value = []
        backend.cache = {"123@c.us": [{"id": "seed", "timestamp": 1}]}

        def fake_list(cid, limit=30, download_media=True):
            count = 30 if not download_media else 20
            return [
                {
                    "id": f"m{i}",
                    "from": cid,
                    "fromMe": False,
                    "hasMedia": True,
                    "timestamp": 1_700_000_000 - i,
                    "body": "",
                    "media": (
                        {
                            "url": f"https://wa.test/m{i}.jpg",
                            "mimetype": "image/jpeg",
                        }
                        if download_media
                        else {"url": None, "mimetype": "image/jpeg"}
                    ),
                }
                for i in range(count)
            ]

        backend._rest.list_messages.side_effect = fake_list
        backend.resync_history()

        assert any(
            call.kwargs.get("download_media") is False
            for call in backend._rest.list_messages.call_args_list
        )

        manager = MagicMock()
        manager.get.return_value = backend
        manager.list_contacts.return_value = []

        from server.messages import messages_page

        page = messages_page(manager, "whatsapp", "123@c.us", refresh=True, limit=200)

    assert isinstance(page, list)
    assert len(page) == 20
    for row in page:
        assert isinstance(row["attachment"], dict)
        assert row["attachment"]["attachment_id"]
        assert row["attachment"]["media_kind"]
    assert not any(r["attachment"] is None and r["text"] == "" for r in page)


def test_t10_config_validation_and_negative_max_chats(monkeypatch):
    from protocols.config import (
        get_wa_resync_download_media,
        get_wa_resync_limit,
        get_wa_resync_max_chats,
        get_wa_resync_workers,
    )

    monkeypatch.setenv("WA_RESYNC_MAX_CHATS", "-5")
    assert get_wa_resync_max_chats() == 20
    monkeypatch.setenv("WA_RESYNC_MAX_CHATS", "0")
    assert get_wa_resync_max_chats() == 0
    monkeypatch.setenv("WA_RESYNC_MAX_CHATS", "7")
    assert get_wa_resync_max_chats() == 7

    monkeypatch.setenv("WA_RESYNC_LIMIT", "0")
    assert get_wa_resync_limit() == 30
    monkeypatch.setenv("WA_RESYNC_LIMIT", "-2")
    assert get_wa_resync_limit() == 30
    monkeypatch.setenv("WA_RESYNC_LIMIT", "7")
    assert get_wa_resync_limit() == 7

    monkeypatch.setenv("WA_RESYNC_WORKERS", "0")
    assert get_wa_resync_workers() == 2
    monkeypatch.setenv("WA_RESYNC_WORKERS", "-1")
    assert get_wa_resync_workers() == 2

    monkeypatch.setenv("WA_RESYNC_DOWNLOAD_MEDIA", " TRUE ")
    assert get_wa_resync_download_media() is True
    monkeypatch.setenv("WA_RESYNC_DOWNLOAD_MEDIA", "false")
    assert get_wa_resync_download_media() is False
