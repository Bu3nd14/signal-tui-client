from __future__ import annotations

import io
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import backend
from backend.db import _init_db
from web.api import create_api_router
from web.auth import install_auth

TOKEN = "correct-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def web_client(monkeypatch, tmp_path: Path):
    db_file = tmp_path / "messages.db"
    monkeypatch.setattr(backend, "DB_FILE", db_file)
    monkeypatch.setattr(backend, "CACHE_DIR", tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    _init_db()
    app = FastAPI()
    app.state.manager = SimpleNamespace(get_attachment_path=lambda *_args: None)
    install_auth(app, TOKEN)
    app.include_router(create_api_router())
    with TestClient(app) as client:
        yield client, db_file, tmp_path


def _insert_message(db_file: Path, **values) -> int:
    fields = {
        "protocol": "signal",
        "contact_number": "alice",
        "text": "reply",
        "is_mine": 0,
        "timestamp": 1,
        **values,
    }
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    with sqlite3.connect(db_file) as connection:
        cursor = connection.execute(
            f"INSERT INTO messages ({columns}) VALUES ({placeholders})",
            tuple(fields.values()),
        )
        return cursor.lastrowid


def test_message_payload_maps_quote_thumbnails_and_raw_fields(web_client):
    client, db_file, tmp_path = web_client
    local_path = tmp_path / "quote-thumbs" / "local.jpg"
    local_id = _insert_message(
        db_file,
        protocol="telegram",
        timestamp=1,
        quote_attachment_id="local-id",
        quote_attachment_path=str(local_path),
        quote_content_type="image/jpeg",
    )
    remote_id = "tgref:folder/photo id"
    _insert_message(
        db_file,
        protocol="telegram",
        timestamp=2,
        quote_attachment_id=remote_id,
        quote_content_type="image/jpeg",
    )
    _insert_message(
        db_file,
        protocol="telegram",
        timestamp=3,
        quote_attachment_id="video-id",
        quote_content_type="video/mp4",
    )
    _insert_message(db_file, protocol="telegram", timestamp=4)

    response = client.get("/api/messages?proto=telegram&contact_id=alice", headers=AUTH)

    assert response.status_code == 200
    messages = response.json()
    assert messages[0]["quote_attachment_id"] == "local-id"
    assert messages[0]["quote_content_type"] == "image/jpeg"
    assert messages[0]["quote_thumb_url"] == (
        f"/api/quote-media/telegram/{local_id}?w=96"
    )
    encoded_id = "/".join(quote(part, safe="") for part in remote_id.split("/"))
    assert messages[1]["quote_attachment_id"] == remote_id
    assert messages[1]["quote_content_type"] == "image/jpeg"
    assert messages[1]["quote_thumb_url"] == (f"/api/media/telegram/{encoded_id}?w=96")
    assert messages[2]["quote_thumb_url"] is None
    assert messages[3]["quote_attachment_id"] is None
    assert messages[3]["quote_content_type"] is None
    assert messages[3]["quote_thumb_url"] is None


def test_quote_media_thumbnail_is_cached(web_client):
    client, db_file, tmp_path = web_client
    quote_root = tmp_path / "quote-thumbs"
    quote_root.mkdir()
    source = quote_root / "large.png"
    Image.new("RGB", (1200, 800), "#3578a8").save(source)
    row_id = _insert_message(db_file, quote_attachment_path=str(source))
    url = f"/api/quote-media/signal/{row_id}?w=96"

    first = client.get(url, headers=AUTH)

    assert first.status_code == 200
    assert first.headers["content-type"].startswith("image/jpeg")
    assert first.headers["cache-control"] == ("private, max-age=31536000, immutable")
    with Image.open(io.BytesIO(first.content)) as thumbnail:
        assert max(thumbnail.size) <= 96
    with patch("PIL.Image.open", side_effect=AssertionError("cache miss")):
        second = client.get(url, headers=AUTH)
    assert second.content == first.content


def test_quote_media_rejects_missing_and_unsafe_paths(web_client):
    client, db_file, tmp_path = web_client
    quote_root = tmp_path / "quote-thumbs"
    quote_root.mkdir()
    missing_id = _insert_message(
        db_file, quote_attachment_path=str(quote_root / "missing.jpg")
    )
    passwd_id = _insert_message(
        db_file, timestamp=2, quote_attachment_path="/etc/passwd"
    )
    outside = tmp_path / "outside.jpg"
    Image.new("RGB", (10, 10)).save(outside)
    outside_id = _insert_message(
        db_file, timestamp=3, quote_attachment_path=str(outside)
    )

    for url in (
        f"/api/quote-media/signal/{missing_id}",
        f"/api/quote-media/signal/{passwd_id}",
        f"/api/quote-media/signal/{outside_id}",
        "/api/quote-media/signal/999999",
        f"/api/quote-media/telegram/{missing_id}",
    ):
        assert client.get(url, headers=AUTH).status_code == 404


def test_quote_media_requires_bearer(web_client):
    client, _, _ = web_client

    response = client.get("/api/quote-media/signal/1")

    assert response.status_code == 401


def test_quote_media_database_error_returns_500(web_client):
    client, _, _ = web_client

    with patch("web.api.sqlite3.connect", side_effect=sqlite3.OperationalError):
        response = client.get("/api/quote-media/signal/1", headers=AUTH)

    assert response.status_code == 500
    assert response.json() == {"detail": "Database error"}
