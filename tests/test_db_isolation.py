from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

REAL_DB = Path.home() / ".local" / "share" / "signal-tui-client" / "messages.db"
# Contact numbers only used by tests: if they appear in the real DB a test
# leaked outside tmp_path.
TEST_MARKERS = ("fixture-isolation@invalid", "test-contact")


def _real_db_marker_count() -> int:
    """Count leaked test markers in the real DB, skipping if it is unavailable."""
    if not REAL_DB.exists():
        pytest.skip("Real DB does not exist")
    try:
        with sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True) as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM messages WHERE contact_number IN (?, ?)",
                TEST_MARKERS,
            ).fetchone()[0]
    except sqlite3.Error:
        pytest.skip("Real DB is not accessible")


def test_autouse_fixture_routes_ingest_away_from_real_database(tmp_path):
    import protocols.db as backend

    assert backend.DB_FILE != REAL_DB
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
    assert _real_db_marker_count() == 0


def test_autouse_fixture_patches_all_value_imports(tmp_path):
    import protocols.db as backend
    import protocols.download as download_mod
    import protocols.signal as signal_mod
    import protocols.whatsapp as whatsapp_mod
    from protocols import config, rpc

    assert Path(backend.CACHE_DIR).is_relative_to(tmp_path)
    assert Path(signal_mod.CACHE_DIR).is_relative_to(tmp_path)
    assert Path(download_mod.CACHE_DIR).is_relative_to(tmp_path)
    assert Path(rpc.SIGNAL_CLI_ATTACHMENTS_DIR).is_relative_to(tmp_path)
    assert (
        Path(signal_mod.SIGNAL_CLI_ATTACHMENTS_DIR)
        == tmp_path / "backend-cache" / "signal-media"
    )

    assert config.get_whatsapp_media_dir() == ""
    assert whatsapp_mod.get_whatsapp_media_dir() == ""


def test_autouse_fixture_resets_download_globals(tmp_path):
    import protocols.download as download_mod

    assert download_mod._TEMP_DOWNLOAD_DIR is None
    assert download_mod._DOWNLOAD_SERVER is None
    assert download_mod._DOWNLOAD_URL_BASE is None

    temp_dir = download_mod._get_temp_download_dir()
    assert temp_dir.is_relative_to(tmp_path)
    assert temp_dir.name == "downloads"
    assert temp_dir.is_dir()


def test_autouse_fixture_real_db_unchanged(tmp_path):
    import protocols.db as backend

    assert backend.DB_FILE != REAL_DB
    assert _real_db_marker_count() == 0

    backend._add_message_to_cache(
        "test-contact",
        "test message",
        False,
        "tester",
        12345,
        protocol="signal",
    )

    assert Path(backend.DB_FILE).is_relative_to(tmp_path)
    assert _real_db_marker_count() == 0
