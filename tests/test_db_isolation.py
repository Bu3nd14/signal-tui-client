from __future__ import annotations

from pathlib import Path


def test_autouse_fixture_routes_ingest_away_from_real_database(tmp_path):
    import protocols.db as backend

    real_db = Path.home() / ".local" / "share" / "signal-tui-client" / "messages.db"
    before = (
        (real_db.stat().st_size, real_db.stat().st_mtime_ns)
        if real_db.exists()
        else None
    )

    assert backend.DB_FILE != real_db
    assert Path(backend.DB_FILE).is_relative_to(tmp_path)

    backend._add_message_to_cache(
        "fixture-isolation@invalid",
        "isolated",
        False,
        "tester",
        1,
        protocol="whatsapp",
        msg_id="fixture-isolation",
    )

    assert Path(backend.DB_FILE).is_file()
    after = (
        (real_db.stat().st_size, real_db.stat().st_mtime_ns)
        if real_db.exists()
        else None
    )
    assert after == before
