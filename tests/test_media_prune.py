from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from protocols import db
from protocols.media_prune import (
    MediaScope,
    collect_refs,
    compute_orphans,
    prune_media,
    resolve_protected,
)


def _scope(label: str, directory: Path) -> MediaScope:
    protocol = "signal" if label == "quote-thumbs" else label
    return MediaScope(protocol, label, directory)


def _db(path: Path, rows=()) -> Path:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE messages (protocol TEXT, attachment_id TEXT, "
            "quote_attachment_id TEXT, quote_attachment_path TEXT)"
        )
        conn.executemany("INSERT INTO messages VALUES (?, ?, ?, ?)", rows)
    return path


def test_resolve_protected_basename_and_urls(tmp_path):
    assert resolve_protected(_scope("signal", tmp_path), ["/x/a.jpg"], []) == {"a.jpg"}
    assert resolve_protected(
        _scope("whatsapp", tmp_path),
        ["https://waha.local/media/photo.jpg"],
        [],
    ) == {"photo.jpg"}
    assert resolve_protected(
        _scope("quote-thumbs", tmp_path), ["/cache/hash.webp"], []
    ) == {"hash.webp"}


def test_resolve_telegram_refs(tmp_path):
    disk = {"c-m-sent.jpg", "c-m-photo.jpg", "c-mm-no.jpg"}
    protected = resolve_protected(
        _scope("telegram", tmp_path),
        ["/tmp/legacy.png", "name.jpg", "tgref:c:m", "tgref:bad"],
        disk,
    )
    assert protected == {"legacy.png", "name.jpg", "c-m-sent.jpg", "c-m-photo.jpg"}


def test_compute_orphans_protection_grace_and_symlink(tmp_path):
    protected = tmp_path / "keep"
    old = tmp_path / "old"
    recent = tmp_path / "recent"
    target = tmp_path / "target"
    for path in (protected, old, recent, target):
        path.write_bytes(path.name.encode())
    link = tmp_path / "link"
    link.symlink_to(target)
    now = time.time() + 10_000
    os.utime(recent, (now, now))

    orphans = compute_orphans(
        _scope("signal", tmp_path),
        ["keep", "target"],
        [protected, old, recent, link],
        now_s=now,
        grace_s=100,
    )
    assert {item.path.name for item in orphans} == {"old", "link"}


def test_collect_refs_single_protocol(tmp_path):
    db_file = _db(
        tmp_path / "db.sqlite",
        [("signal", "a", "q", "/x/thumb"), ("telegram", "t", None, None)],
    )
    assert collect_refs(db_file, ["signal"]) == {"signal": {"a", "q", "/x/thumb"}}
    assert collect_refs(tmp_path / "missing", ["signal"]) is None


def test_prune_media_missing_db_never_deletes_files(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    referenced = media / "referenced"
    referenced.write_text("must survive")

    report = prune_media(
        db_file=tmp_path / "missing.db",
        scopes=[_scope("signal", media)],
        grace_s=0,
    ).scopes[0]

    assert referenced.exists()
    assert report.skipped_db_error
    assert report.errors == 1
    assert report.orphans_found == 0


def test_prune_media_counts_per_file_classification_and_stat_errors(
    tmp_path, monkeypatch
):
    media = tmp_path / "media"
    media.mkdir()
    classify_error = media / "classify-error"
    stat_error = media / "stat-error"
    orphan = media / "orphan"
    for path in (classify_error, stat_error, orphan):
        path.write_text("x")
    db_file = _db(tmp_path / "db.sqlite")
    original_stat = Path.stat

    def selective_is_file(path):
        if path == classify_error:
            raise OSError("classification failed")
        return True

    def selective_stat(path, *args, **kwargs):
        if path == stat_error:
            raise OSError("stat failed")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", selective_is_file)
    monkeypatch.setattr(Path, "stat", selective_stat)
    report = prune_media(
        db_file=db_file,
        scopes=[_scope("signal", media)],
        grace_s=0,
        dry_run=True,
    ).scopes[0]

    assert report.errors == 2
    assert report.orphans_found == 1
    assert report.orphans_dryrun[0].path == orphan


def test_prune_media_dry_run_execute_metrics_and_grace(tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    keep = media / "keep"
    orphan = media / "orphan"
    recent = media / "recent"
    keep.write_bytes(b"k")
    orphan.write_bytes(b"orphan")
    recent.write_bytes(b"new")
    db_file = _db(tmp_path / "db.sqlite", [("signal", "keep", None, None)])
    now = time.time() + 10_000
    os.utime(recent, (now, now))
    monkeypatch.setattr("protocols.media_prune.time.time", lambda: now)
    scope = _scope("signal", media)

    report = prune_media(
        db_file=db_file, scopes=[scope], grace_s=60, dry_run=True
    ).scopes[0]
    assert report.orphans_found == 1
    assert report.skipped_grace == 1
    assert report.bytes_freed == 0
    assert orphan.exists()

    monkeypatch.setattr("protocols.media_prune.time.time", lambda: now + 10_000)
    report = prune_media(db_file=db_file, scopes=[scope], grace_s=0).scopes[0]
    assert report.orphans_deleted == 2
    assert report.bytes_freed == 9
    assert keep.exists()


def test_prune_media_missing_scope_filter_and_quote_thumbs(tmp_path):
    db_file = _db(tmp_path / "db.sqlite", [("signal", None, None, "/x/thumb")])
    missing = _scope("telegram", tmp_path / "missing")
    quote_dir = tmp_path / "quotes"
    quote_dir.mkdir()
    (quote_dir / "thumb").write_text("keep")
    (quote_dir / "orphan").write_text("remove")
    scopes = [missing, _scope("quote-thumbs", quote_dir)]

    report = prune_media(
        db_file=db_file,
        scopes=scopes,
        protocols=["signal"],
        grace_s=0,
        dry_run=True,
    )
    assert [item.scope for item in report.scopes] == ["quote-thumbs"]
    assert report.scopes[0].orphans_found == 1
    missing_report = prune_media(
        db_file=db_file, scopes=[missing], grace_s=0, dry_run=True
    ).scopes[0]
    assert missing_report.skipped_dir_missing


def test_prune_media_unlink_error_is_nonfatal(tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    orphan = media / "orphan"
    orphan.write_text("x")
    db_file = _db(tmp_path / "db.sqlite")
    original_unlink = Path.unlink

    def fail_selected(path, *args, **kwargs):
        if path == orphan:
            raise OSError("no")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_selected)
    report = prune_media(
        db_file=db_file, scopes=[_scope("signal", media)], grace_s=0
    ).scopes[0]
    assert report.errors == 1
    assert report.orphans_deleted == 0


@pytest.mark.parametrize("limit", [300, 0])
def test_prune_cache_always_calls_media_prune(tmp_path, monkeypatch, limit):
    monkeypatch.setattr(db, "DB_FILE", tmp_path / "messages.db")
    monkeypatch.setattr(db, "CACHE_DIR", tmp_path)
    called = []
    monkeypatch.setattr(
        "protocols.media_prune.prune_media", lambda: called.append(1) or _empty_report()
    )
    assert db._prune_cache(limit=limit) == 0
    assert called == [1]


def _empty_report():
    from protocols.media_prune import MediaPruneReport

    return MediaPruneReport()


def test_prune_cache_media_failure_does_not_propagate(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_FILE", tmp_path / "messages.db")
    monkeypatch.setattr(db, "CACHE_DIR", tmp_path)
    db._init_db()
    with sqlite3.connect(db.DB_FILE) as conn:
        conn.executemany(
            "INSERT INTO messages "
            "(protocol, contact_number, timestamp, status, msg_id) "
            "VALUES ('signal', 'contact', ?, 'read', ?)",
            [(index, f"id-{index}") for index in range(101)],
        )
    monkeypatch.setattr(
        "protocols.media_prune.prune_media",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert db._prune_cache(limit=100, now_ms=1_000_000) == 1
