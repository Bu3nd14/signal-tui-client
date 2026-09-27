"""Regression tests for the WhatsApp outgoing double-attachment bug.

An outgoing WhatsApp media (e.g. a PDF) used to be persisted twice, both rows
sharing the same ``msg_id``:

* the WAHA URL row (``attachment_id`` = ``http://.../files/...pdf``,
  ``text = "Media: <url>"``, lower timestamp);
* the durable client mirror row (``attachment_id = "sent-<uuid>.pdf"``,
  ``text = ""``, higher timestamp).

Two fixes are covered here:

1. live ingest XOR in ``WhatsAppBackend._message_already_cached`` (echo-first
   must match the already-cached URL row, while two distinct ``sent-*`` mirrors
   must NOT be collapsed);
2. boot fusion in ``protocols.db._dedup_outgoing_attachment_mirrors`` invoked
   by ``run_startup_maintenance``.

Each test maps to a design point (1..17 in the approved plan).
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from models import is_whatsapp_synthetic_media_text
from protocols import db
from protocols.whatsapp import WhatsAppBackend

CONTACT = "391234567890@c.us"
MSG_ID = "true_391234567890@c.us_ABC"
WA_URL = "http://localhost:3000/api/files/default/true_391234567890@c.us_ABC.pdf"
SENT_ID = "sent-8a44cb8499554832aa70afa2e0d998ca.pdf"
TS_URL = 1_700_000_000_000
TS_SENT = TS_URL + 1_500


def _wa_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WhatsAppBackend:
    media_dir = tmp_path / "wa-media"
    media_dir.mkdir(parents=True, exist_ok=True)
    backend = WhatsAppBackend(api_url="http://api.test", media_dir=str(media_dir))
    monkeypatch.setattr(backend, "_resolve_send_chat_id", lambda cid: cid)
    return backend


def _add(
    *,
    text: str,
    attachment_id: str,
    ts: int,
    msg_id: str = MSG_ID,
    contact: str = CONTACT,
    protocol: str = "whatsapp",
    is_mine: bool = True,
    info: str | None = None,
    status: str | None = "sent",
    msg_type: str = "attachment",
    content_type: str | None = "application/pdf",
    media_kind: str | None = "document",
    batch_id: str | None = None,
    batch_index: int | None = None,
    quote_text: str | None = None,
    quote_timestamp: int | None = None,
    quote_author: str | None = None,
    reply_to_message_id: str | None = None,
    quote_attachment_id: str | None = None,
    quote_attachment_path: str | None = None,
    quote_content_type: str | None = None,
):
    return db._add_message_to_cache(
        contact,
        text,
        is_mine,
        "You" if is_mine else "Mario",
        ts,
        quote_text=quote_text,
        msg_type=msg_type,
        attachment_info=info,
        attachment_id=attachment_id,
        content_type=content_type,
        protocol=protocol,
        msg_id=msg_id,
        status=status,
        quote_timestamp=quote_timestamp,
        quote_author=quote_author,
        reply_to_message_id=reply_to_message_id,
        quote_attachment_id=quote_attachment_id,
        quote_attachment_path=quote_attachment_path,
        quote_content_type=quote_content_type,
        media_kind=media_kind,
        batch_id=batch_id,
        batch_index=batch_index,
    )


def _url_and_mirror(
    *, text_sent: str = "", info_sent: str | None = None, info_url: str | None = None
):
    return [
        {
            "id": MSG_ID,
            "text": f"Media: {WA_URL}",
            "is_mine": True,
            "sender": "You",
            "msg_type": "attachment",
            "attachment_info": info_url,
            "attachment_id": WA_URL,
            "content_type": "application/pdf",
            "media_kind": "document",
        },
        {
            "id": MSG_ID,
            "text": text_sent,
            "is_mine": True,
            "sender": "You",
            "msg_type": "attachment",
            "attachment_info": info_sent,
            "attachment_id": SENT_ID,
            "content_type": "application/pdf",
            "media_kind": "document",
        },
    ]


def _all_rows() -> list[tuple]:
    with sqlite3.connect(db.DB_FILE) as conn:
        return conn.execute(
            "SELECT attachment_id, text, status, attachment_info, batch_id, "
            "batch_index, quote_text, quote_author, quote_timestamp, "
            "reply_to_message_id, quote_attachment_id, quote_attachment_path, "
            "quote_content_type, edited, content_type, media_kind "
            "FROM messages ORDER BY rowid"
        ).fetchall()


def _count() -> int:
    with sqlite3.connect(db.DB_FILE) as conn:
        return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


# ─── 1. Live ingest: echo-first ──────────────────────────────────────────────


def test_echo_first_url_then_sent_mirror_is_single_entry(tmp_path, monkeypatch):
    """URL row ingested first; the later ``sent-*`` echo upgrades it in place."""
    backend = _wa_backend(tmp_path, monkeypatch)
    (Path(backend.media_dir) / SENT_ID).write_bytes(b"pdf")
    url_row, sent_row = _url_and_mirror(info_url="report.pdf", info_sent="report.pdf")

    assert backend.ingest_message(CONTACT, url_row, TS_URL) is True
    backend.ingest_message(CONTACT, sent_row, TS_SENT)

    assert len(backend.cache[CONTACT]) == 1
    assert backend.cache[CONTACT][0]["attachment_id"] == SENT_ID
    # No second row was inserted in SQLite.
    assert _count() == 1
    assert _all_rows()[0][0] == SENT_ID


# ─── 2. Live ingest: mirror-first unchanged ──────────────────────────────────


def test_mirror_first_then_url_stays_single_entry(tmp_path, monkeypatch):
    backend = _wa_backend(tmp_path, monkeypatch)
    (Path(backend.media_dir) / SENT_ID).write_bytes(b"pdf")
    url_row, sent_row = _url_and_mirror()

    assert backend.ingest_message(CONTACT, sent_row, TS_SENT) is True
    backend.ingest_message(CONTACT, url_row, TS_URL)

    assert len(backend.cache[CONTACT]) == 1
    assert backend.cache[CONTACT][0]["attachment_id"] == SENT_ID
    assert _count() == 1


# ─── 3. Live ingest: ack / follow-up without attachment is unchanged ─────────


def test_ack_follow_up_without_attachment_is_ignored(tmp_path, monkeypatch):
    backend = _wa_backend(tmp_path, monkeypatch)
    url_row, _ = _url_and_mirror(info_url="Yes, nice")

    backend.ingest_message(CONTACT, url_row, TS_URL)
    ack = {
        "id": MSG_ID,
        "text": "Yes, nice",
        "is_mine": True,
        "sender": "You",
        "msg_type": "text",
        "attachment_info": None,
        "attachment_id": None,
    }
    backend.ingest_message(CONTACT, ack, TS_URL + 250)

    assert len(backend.cache[CONTACT]) == 1
    assert _count() == 1


# ─── 4. XOR: two distinct sent mirrors are never merged ──────────────────────


def test_two_distinct_sent_mirrors_same_msg_id_not_merged():
    _add(text="", attachment_id=SENT_ID, ts=TS_URL)
    _add(text="", attachment_id="sent-other.pdf", ts=TS_URL + 10)

    assert db._dedup_outgoing_attachment_mirrors() == 0
    assert _count() == 2


# ─── 5. Boot dedup: real URL + sent pair → durable survivor ──────────────────


def test_boot_dedup_fuses_url_and_sent_mirror():
    _add(text=f"Media: {WA_URL}", attachment_id=WA_URL, ts=TS_URL)
    _add(text="", attachment_id=SENT_ID, ts=TS_SENT)

    assert db._dedup_outgoing_attachment_mirrors() == 1

    rows = _all_rows()
    assert len(rows) == 1
    assert rows[0][0] == SENT_ID
    assert rows[0][1] == ""


# ─── 6. Status: max rank preserved, NULL never downgrades ────────────────────


def test_boot_dedup_keeps_highest_status():
    _add(
        text=f"Media: {WA_URL}",
        attachment_id=WA_URL,
        ts=TS_URL,
        status="read",
    )
    _add(text="", attachment_id=SENT_ID, ts=TS_SENT, status="sent")

    assert db._dedup_outgoing_attachment_mirrors() == 1
    assert _all_rows()[0][2] == "read"


def test_boot_dedup_null_status_does_not_downgrade():
    _add(text="", attachment_id=SENT_ID, ts=TS_SENT, status="sent")
    _add(
        text=f"Media: {WA_URL}",
        attachment_id=WA_URL,
        ts=TS_URL,
        status="delivered",
    )
    with sqlite3.connect(db.DB_FILE) as conn:
        conn.execute(
            "UPDATE messages SET status = NULL WHERE attachment_id = ?", (SENT_ID,)
        )

    assert db._dedup_outgoing_attachment_mirrors() == 1
    assert _all_rows()[0][2] == "delivered"


# ─── 7. Synthetic text vs real caption ───────────────────────────────────────


def test_boot_dedup_preserves_multiword_caption_text():
    _add(text="Media: bella foto", attachment_id=SENT_ID, ts=TS_SENT)
    _add(text=f"Media: {WA_URL}", attachment_id=WA_URL, ts=TS_URL)

    assert db._dedup_outgoing_attachment_mirrors() == 1
    assert _all_rows()[0][1] == "Media: bella foto"


def test_boot_dedup_clears_synthetic_url_text():
    _add(text="Media: http://x", attachment_id=SENT_ID, ts=TS_SENT)
    _add(text=f"Media: {WA_URL}", attachment_id=WA_URL, ts=TS_URL)

    assert db._dedup_outgoing_attachment_mirrors() == 1
    assert _all_rows()[0][1] == ""


# ─── 7bis. R1/EC11: real caption only on the non-mirror row is transferred ───


@pytest.mark.parametrize(
    ("survivor_text", "other_text", "expected"),
    [
        ("", "document caption", "document caption"),
        ("Media: http://x", "document caption", "document caption"),
        ("real caption", "Media: http://x", "real caption"),
        ("real caption", "other caption", "real caption"),
        ("", "Media: http://y", ""),
    ],
)
def test_boot_dedup_transfers_real_caption_from_other_row(
    survivor_text, other_text, expected
):
    _add(text=survivor_text, attachment_id=SENT_ID, ts=TS_SENT)
    _add(text=other_text, attachment_id=WA_URL, ts=TS_URL)

    assert db._dedup_outgoing_attachment_mirrors() == 1
    assert _all_rows()[0][1] == expected


# ─── 8. attachment_info: a real caption survives a technical filename ────────


def test_boot_dedup_does_not_lose_real_caption():
    _add(
        text="",
        attachment_id=SENT_ID,
        ts=TS_SENT,
        info="sent-8a44cb84.pdf",
    )
    _add(
        text=f"Media: {WA_URL}",
        attachment_id=WA_URL,
        ts=TS_URL,
        info="document caption",
    )

    assert db._dedup_outgoing_attachment_mirrors() == 1
    assert _all_rows()[0][3] == "document caption"


# ─── 9. batch/quote/edited fields preserved ──────────────────────────────────


def test_boot_dedup_preserves_batch_quote_and_edited():
    _add(
        text="",
        attachment_id=SENT_ID,
        ts=TS_SENT,
        batch_id="batch-7",
        batch_index=0,
        content_type=None,
        media_kind=None,
    )
    _add(
        text=f"Media: {WA_URL}",
        attachment_id=WA_URL,
        ts=TS_URL,
        content_type="application/pdf",
        media_kind="document",
        quote_text="domanda",
        quote_timestamp=123,
        quote_author="Mario",
        reply_to_message_id="q1",
        quote_attachment_id="att-q",
        quote_attachment_path="/tmp/q.png",
        quote_content_type="image/png",
    )
    with sqlite3.connect(db.DB_FILE) as conn:
        conn.execute(
            "UPDATE messages SET edited = 1 WHERE attachment_id = ?", (SENT_ID,)
        )

    assert db._dedup_outgoing_attachment_mirrors() == 1
    row = _all_rows()[0]
    assert row[4] == "batch-7"  # batch_id stays the survivor's
    assert row[5] == 0  # batch_index stays the survivor's
    assert row[6] == "domanda"
    assert row[7] == "Mario"
    assert row[8] == 123
    assert row[9] == "q1"
    assert row[10] == "att-q"
    assert row[11] == "/tmp/q.png"
    assert row[12] == "image/png"
    assert row[13] == 1  # edited = max
    assert row[14] == "application/pdf"
    assert row[15] == "document"


# ─── 10. Ambiguous 3-row group is skipped ────────────────────────────────────


def test_boot_dedup_skips_ambiguous_three_row_group():
    _add(text="", attachment_id=SENT_ID, ts=TS_URL)
    _add(text="", attachment_id=SENT_ID, ts=TS_URL + 10)
    _add(text=f"Media: {WA_URL}", attachment_id=WA_URL, ts=TS_URL + 20)

    assert db._dedup_outgoing_attachment_mirrors() == 0
    assert _count() == 3


# ─── 11. Two URLs sharing an attachment_id but different text → skip ──────────


def test_boot_dedup_skips_two_url_rows_same_attachment():
    _add(text="Media: url-a", attachment_id=WA_URL, ts=TS_URL)
    _add(text="Media: url-b", attachment_id=WA_URL, ts=TS_URL + 10)

    assert db._dedup_outgoing_attachment_mirrors() == 0
    assert _count() == 2


# ─── 12. Outside the echo window → no fusion ─────────────────────────────────


def test_boot_dedup_outside_window_not_fused(monkeypatch):
    _add(text=f"Media: {WA_URL}", attachment_id=WA_URL, ts=TS_URL)
    _add(text="", attachment_id=SENT_ID, ts=TS_URL + db._ECHO_MATCH_WINDOW_MS + 1)

    assert db._dedup_outgoing_attachment_mirrors() == 0
    assert _count() == 2


# ─── 13. Idempotency ─────────────────────────────────────────────────────────


def test_boot_dedup_is_idempotent():
    _add(text=f"Media: {WA_URL}", attachment_id=WA_URL, ts=TS_URL)
    _add(text="", attachment_id=SENT_ID, ts=TS_SENT)

    assert db._dedup_outgoing_attachment_mirrors() == 1
    assert db._dedup_outgoing_attachment_mirrors() == 0
    assert _count() == 1


# ─── 14. run_startup_maintenance force / once semantics (R3) ─────────────────


def test_run_startup_maintenance_force_and_once(monkeypatch):
    _add(text=f"Media: {WA_URL}", attachment_id=WA_URL, ts=TS_URL)
    _add(text="", attachment_id=SENT_ID, ts=TS_SENT)

    removed: list[int] = []
    real = db._dedup_outgoing_attachment_mirrors

    def spy() -> int:
        result = real()
        removed.append(result)
        return result

    monkeypatch.setattr(db, "_dedup_outgoing_attachment_mirrors", spy)

    db.run_startup_maintenance(force=True)
    db.run_startup_maintenance(force=True)
    db.run_startup_maintenance()
    db.run_startup_maintenance()

    assert removed == [1, 0]
    assert _count() == 1


# ─── 14bis. R2: callers block until maintenance completes (no cache race) ────


def test_run_startup_maintenance_concurrent_blocks_and_runs_once(monkeypatch):
    """Race R2: N worker concorrenti non devono seminare la cache prima che la
    manutenzione sia finita.  La cache letta da ogni worker dopo il ritorno è
    già deduplicata e il lavoro pesante gira una sola volta."""
    db.reset_startup_maintenance()
    try:
        _add(text=f"Media: {WA_URL}", attachment_id=WA_URL, ts=TS_URL)
        _add(text="", attachment_id=SENT_ID, ts=TS_SENT)

        real_dedup = db._dedup_outgoing_attachment_mirrors
        finished = threading.Event()
        counter_lock = threading.Lock()
        counter = {"n": 0}

        def slow_dedup() -> int:
            with counter_lock:
                counter["n"] += 1
            time.sleep(0.3)
            result = real_dedup()
            finished.set()
            return result

        monkeypatch.setattr(db, "_dedup_messages_by_id", lambda: 0)
        monkeypatch.setattr(db, "_dedup_outgoing_attachment_mirrors", slow_dedup)
        monkeypatch.setattr(db, "_detect_ghost_outgoing_text", lambda db_file=None: [])

        workers = 5
        barrier = threading.Barrier(workers)
        returned_before_finish: list[bool] = []
        seeded_counts: list[int] = []

        def worker() -> None:
            barrier.wait()
            db.run_startup_maintenance()
            returned_before_finish.append(not finished.is_set())
            cache = db._load_cache(protocol="whatsapp")
            seeded_counts.append(len(cache.get(CONTACT, [])))

        threads = [threading.Thread(target=worker) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive()

        assert counter["n"] == 1
        assert finished.is_set()
        assert returned_before_finish == [False] * workers
        assert seeded_counts == [1] * workers

        # force=True must rerun even after the once-marked path.
        db.run_startup_maintenance(force=True)
        assert counter["n"] == 2
        assert _count() == 1
    finally:
        db.reset_startup_maintenance()


def test_run_startup_maintenance_failure_does_not_wedge(monkeypatch):
    """R2: un'eccezione non lascia il path marcato; il chiamante successivo
    ritenta e completa."""
    db.reset_startup_maintenance()
    try:
        _add(text=f"Media: {WA_URL}", attachment_id=WA_URL, ts=TS_URL)
        _add(text="", attachment_id=SENT_ID, ts=TS_SENT)

        real_dedup = db._dedup_outgoing_attachment_mirrors
        calls = {"n": 0}

        def failing_once() -> int:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom during maintenance")
            return real_dedup()

        monkeypatch.setattr(db, "_dedup_messages_by_id", lambda: 0)
        monkeypatch.setattr(db, "_dedup_outgoing_attachment_mirrors", failing_once)
        monkeypatch.setattr(db, "_detect_ghost_outgoing_text", lambda db_file=None: [])

        with pytest.raises(RuntimeError, match="boom during maintenance"):
            db.run_startup_maintenance()

        # The failed run must not mark the path as done.
        db.run_startup_maintenance()

        assert calls["n"] == 2
        assert _count() == 1
    finally:
        db.reset_startup_maintenance()


# ─── 14ter. R3: connect_all is best-effort on maintenance failure ────────────


def test_connect_all_survives_maintenance_failure(monkeypatch):
    from protocols.manager import BackendManager

    class _FakeBackend:
        protocol = "signal"

        def __init__(self) -> None:
            self.connect = AsyncMock()

    backend = _FakeBackend()
    manager = BackendManager()
    manager.register(backend)
    monkeypatch.setattr(
        db, "run_startup_maintenance", MagicMock(side_effect=RuntimeError("boom"))
    )

    asyncio.run(manager.connect_all())

    backend.connect.assert_awaited_once()


# ─── 15. Path-form mirror classifier coherence Python/SQL (R2) ───────────────


def test_path_form_mirror_classifier_consistent():
    from models import is_sent_mirror_attachment_id

    assert is_sent_mirror_attachment_id("/x/sent-a.pdf") is True

    _add(text=f"Media: {WA_URL}", attachment_id=WA_URL, ts=TS_URL)
    _add(text="", attachment_id="/x/sent-a.pdf", ts=TS_SENT)

    assert db._dedup_outgoing_attachment_mirrors() == 1
    assert _all_rows()[0][0] == "/x/sent-a.pdf"


# ─── 16. Scope guard: Signal multi-attachment / incoming WhatsApp untouched ──


def test_signal_multi_attachment_same_msg_id_untouched():
    _add(
        text="Image: a.jpg",
        attachment_id="att-1",
        ts=TS_URL,
        protocol="signal",
        msg_type="image",
    )
    _add(
        text="Image: b.jpg",
        attachment_id="att-2",
        ts=TS_URL,
        protocol="signal",
        msg_type="image",
    )

    assert db._dedup_outgoing_attachment_mirrors() == 0
    assert _count() == 2


def test_incoming_whatsapp_multi_media_untouched():
    _add(
        text=f"Media: {WA_URL}",
        attachment_id=WA_URL,
        ts=TS_URL,
        is_mine=False,
    )
    _add(
        text="",
        attachment_id=SENT_ID,
        ts=TS_SENT,
        is_mine=False,
    )

    assert db._dedup_outgoing_attachment_mirrors() == 0
    assert _count() == 2


# ─── 17. Canonical predicate unit tests ──────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Media: bella foto", False),
        ("Media: http://x", True),
        ("MEDIA: x", True),
        ("", False),
        (None, False),
        ("media:", False),
    ],
)
def test_is_whatsapp_synthetic_media_text(text, expected):
    assert is_whatsapp_synthetic_media_text(text) is expected
