from __future__ import annotations

import sqlite3
from pathlib import Path

import purge_orphan_media as cli
from protocols.media_prune import MediaScope


def _setup(tmp_path, monkeypatch, *, create_db=True, create_dir=True):
    db_file = tmp_path / "messages.db"
    if create_db:
        with sqlite3.connect(db_file) as conn:
            conn.execute(
                "CREATE TABLE messages (protocol TEXT, attachment_id TEXT, "
                "quote_attachment_id TEXT, quote_attachment_path TEXT)"
            )
            conn.execute("INSERT INTO messages VALUES ('signal', 'keep', NULL, NULL)")
    media = tmp_path / "media"
    if create_dir:
        media.mkdir()
        (media / "keep").write_bytes(b"k")
        (media / "orphan").write_bytes(b"xx")
    monkeypatch.setattr(cli, "DB_FILE", db_file)
    monkeypatch.setattr(
        cli, "default_scopes", lambda: [MediaScope("signal", "signal", media)]
    )
    return media


def test_default_is_dry_run(tmp_path, monkeypatch, capsys):
    media = _setup(tmp_path, monkeypatch)
    assert cli.main(["--older-than-days", "0"]) == 0
    assert (media / "orphan").exists()
    assert "dry-run" in capsys.readouterr().out


def test_execute_deletes_only_orphan(tmp_path, monkeypatch, capsys):
    media = _setup(tmp_path, monkeypatch)
    assert cli.main(["--execute", "--older-than-days", "0"]) == 0
    assert (media / "keep").exists()
    assert not (media / "orphan").exists()
    assert "Rimossi 1 file" in capsys.readouterr().out


def test_protocol_limits_scopes(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    assert cli.main(["--protocol", "telegram", "--older-than-days", "0"]) == 1
    assert "signal" not in capsys.readouterr().out


def test_missing_db_exits_two(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, create_db=False)
    assert cli.main([]) == 2
    assert "ABORT: DB non trovato" in capsys.readouterr().out


def test_missing_selected_directory_exits_one(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, create_dir=False)
    assert cli.main(["--protocol", "signal"]) == 1
    assert "directory mancante" in capsys.readouterr().out


def test_execute_deletion_error_exits_three(tmp_path, monkeypatch, capsys):
    media = _setup(tmp_path, monkeypatch)
    orphan = media / "orphan"
    original_unlink = Path.unlink

    def fail_orphan(path, *args, **kwargs):
        if path == orphan:
            raise OSError("denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_orphan)
    assert cli.main(["--execute", "--older-than-days", "0"]) == 3
    assert orphan.exists()
    assert "errori: 1" in capsys.readouterr().out
