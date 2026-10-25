"""Blob operations (``media.*``, ``contacts.avatar``) and their HTTP routes.

Each route answers with the bytes and headers of the file the operation hands
out; attachment paths never leave the protocol's media root.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import protocols.db as backend
import protocols.rpc as signal_rpc
from server.api import ApiError
from web.api import create_api_router
from web.ops import get_api

JPEG = b"\xff\xd8\xff\xe0contract-avatar"


@pytest.fixture
def media(monkeypatch, tmp_path: Path):
    root = tmp_path / "media"
    root.mkdir()
    monkeypatch.setattr(signal_rpc, "SIGNAL_CLI_ATTACHMENTS_DIR", root)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    files: dict[str, Path] = {}
    manager = SimpleNamespace(
        get_attachment_path=lambda _proto, attachment_id: files.get(attachment_id),
        get=lambda _proto: None,
    )
    app = FastAPI()
    app.state.manager = manager
    app.include_router(create_api_router())
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client, api=get_api(app), root=root, files=files, tmp=tmp_path
        )


def _png(path: Path, size=(1200, 800)) -> Path:
    Image.new("RGB", size, "#336699").save(path)
    return path


def test_attachment_route_equals_operation(media):
    photo = _png(media.root / "photo.png")
    media.files["photo.png"] = photo

    blob = media.api.call_json(
        "media.attachment", {"protocol": "signal", "attachment_id": "photo.png"}
    )
    response = media.client.get("/api/media/signal/photo.png")

    assert blob["path"] == str(photo.resolve())
    assert blob["content_type"] == "image/png"
    assert blob["size"] == photo.stat().st_size
    assert blob["version"] == f"{photo.stat().st_mtime_ns}-{photo.stat().st_size}"
    assert blob["immutable"] is False
    assert response.status_code == 200
    assert response.content == photo.read_bytes()
    assert response.headers["content-type"] == blob["content_type"]
    assert response.headers["cache-control"] == "private, max-age=86400"
    assert response.headers["content-length"] == str(blob["size"])


def test_thumbnail_route_equals_operation(media):
    photo = _png(media.root / "photo.png")
    media.files["photo.png"] = photo

    blob = media.api.call_json(
        "media.thumbnail",
        {"protocol": "signal", "attachment_id": "photo.png", "width": 96},
    )
    response = media.client.get("/api/media/signal/photo.png?w=96")

    assert blob["content_type"] == "image/jpeg"
    assert blob["immutable"] is True
    assert Path(blob["path"]).parent.name == "signal"
    assert response.status_code == 200
    assert response.content == Path(blob["path"]).read_bytes()
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "private, max-age=31536000, immutable"


def test_unusual_width_serves_the_original(media):
    photo = _png(media.root / "photo.png")
    media.files["photo.png"] = photo

    blob = media.api.call_json(
        "media.thumbnail",
        {"protocol": "signal", "attachment_id": "photo.png", "width": 100},
    )

    assert blob["path"] == str(photo.resolve())
    assert media.client.get("/api/media/signal/photo.png?w=100").content == (
        photo.read_bytes()
    )


def test_missing_attachment_is_not_found_everywhere(media):
    with pytest.raises(ApiError) as exc_info:
        media.api.call_json(
            "media.attachment", {"protocol": "signal", "attachment_id": "nope"}
        )
    assert exc_info.value.code == "not_found"
    for url in ("/api/media/signal/nope", "/api/media/signal/nope?w=96"):
        response = media.client.get(url)
        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}


def test_unknown_protocol_is_not_found(media):
    with pytest.raises(ApiError) as exc_info:
        media.api.call_json(
            "media.attachment", {"protocol": "irc", "attachment_id": "a"}
        )
    assert exc_info.value.code == "not_found"


@pytest.mark.parametrize(
    "attachment_id",
    [
        "../secret.txt",
        "sub/../../secret.txt",
        "/etc/passwd",
        "..",
        "/",
        "link-out",
        "absolute",
    ],
)
def test_attachment_never_leaves_the_media_root(media, attachment_id):
    secret = media.tmp / "secret.txt"
    secret.write_text("top secret")
    (media.root / "link-out").symlink_to(secret)
    # A backend that resolves ids naively, plus ids that hand back files
    # outside the root directly: the operation must refuse every one.
    media.files["absolute"] = secret
    media.files["../secret.txt"] = media.root / ".." / "secret.txt"
    media.files["sub/../../secret.txt"] = (
        media.root / "sub" / ".." / ".." / "secret.txt"
    )
    media.files["/etc/passwd"] = Path("/etc/passwd")
    media.files[".."] = media.root.parent
    media.files["/"] = Path("/")
    media.files["link-out"] = media.root / "link-out"

    for payload in (
        {"protocol": "signal", "attachment_id": attachment_id},
        {"protocol": "signal", "attachment_id": attachment_id, "width": 96},
    ):
        name = "media.thumbnail" if "width" in payload else "media.attachment"
        with pytest.raises(ApiError) as exc_info:
            media.api.call_json(name, payload)
        assert exc_info.value.code == "not_found"
    # The route says 404 (or the router refuses the URL): never the secret.
    response = media.client.get(f"/api/media/signal/{quote(attachment_id, safe='')}")
    assert response.status_code in {404, 422}
    assert b"top secret" not in response.content


def test_real_signal_resolver_rejects_traversal_ids(media):
    secret = media.tmp / "secret.txt"
    secret.write_text("top secret")
    media_manager = SimpleNamespace(
        get_attachment_path=lambda _proto, attachment_id: (
            signal_rpc.get_attachment_path(attachment_id)
        )
    )
    api = type(media.api)(SimpleNamespace(manager=media_manager, bus=None))
    for attachment_id in ("../secret.txt", str(secret), "a/../../secret.txt"):
        with pytest.raises(ApiError) as exc_info:
            api.call_json(
                "media.attachment",
                {"protocol": "signal", "attachment_id": attachment_id},
            )
        assert exc_info.value.code == "not_found"


def test_quote_media_route_equals_operation(media, monkeypatch):
    db_file = media.tmp / "quotes.db"
    monkeypatch.setattr(backend, "DB_FILE", db_file)
    thumbs = Path(backend.CACHE_DIR) / "quote-thumbs"
    thumbs.mkdir(parents=True)
    quote = _png(thumbs / "q.png", (40, 40))
    outside = _png(media.tmp / "outside.png", (40, 40))
    with sqlite3.connect(db_file) as connection:
        connection.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, protocol TEXT, "
            "quote_attachment_path TEXT)"
        )
        connection.execute(
            "INSERT INTO messages VALUES (1, 'signal', ?)", (str(quote),)
        )
        connection.execute(
            "INSERT INTO messages VALUES (2, 'signal', ?)", (str(outside),)
        )

    blob = media.api.call_json(
        "media.quote", {"protocol": "signal", "message_row_id": 1}
    )
    response = media.client.get("/api/quote-media/signal/1")

    assert blob["path"] == str(quote.resolve())
    assert blob["content_type"] == "image/png"
    assert response.content == quote.read_bytes()
    assert response.headers["cache-control"] == "private, max-age=86400"
    thumb = media.api.call_json(
        "media.quote", {"protocol": "signal", "message_row_id": 1, "width": 96}
    )
    assert thumb["immutable"] is True and thumb["content_type"] == "image/jpeg"
    # Only files directly inside <CACHE_DIR>/quote-thumbs are served.
    for row_id in (2, 99):
        with pytest.raises(ApiError) as exc_info:
            media.api.call_json(
                "media.quote", {"protocol": "signal", "message_row_id": row_id}
            )
        assert exc_info.value.code == "not_found"
        assert media.client.get(f"/api/quote-media/signal/{row_id}").status_code == 404


def test_avatar_route_equals_operation(media, monkeypatch):
    from server import avatar_cache

    monkeypatch.setattr(avatar_cache, "CACHE_DIR", media.tmp / "avatars")
    rest = SimpleNamespace(
        get_profile_picture_url=lambda _cid: "https://example.test/a.jpg",
        last_status=200,
    )
    wa = SimpleNamespace(_rest=rest)
    media.api._host.manager.get = lambda proto: wa if proto == "whatsapp" else None
    monkeypatch.setattr(avatar_cache, "_download", lambda _url: JPEG)

    blob = media.api.call_json(
        "contacts.avatar", {"protocol": "whatsapp", "contact_id": "1@c.us"}
    )
    response = media.client.get(
        "/api/contact-avatar", params={"proto": "whatsapp", "contact_id": "1@c.us"}
    )

    assert blob["content_type"] == "image/jpeg"
    assert Path(blob["path"]).read_bytes() == JPEG
    assert response.content == JPEG
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "private, max-age=86400"
    for protocol, contact_id, code in (
        ("irc", "x", "not_found"),
        ("whatsapp", "  ", "invalid_request"),
    ):
        with pytest.raises(ApiError) as exc_info:
            media.api.call_json(
                "contacts.avatar", {"protocol": protocol, "contact_id": contact_id}
            )
        assert exc_info.value.code == code


def test_signal_avatar_blob_is_a_content_addressed_file(media, monkeypatch):
    avatars = media.tmp / "avatars"
    avatars.mkdir()
    (avatars / "contact-+39123").write_bytes(JPEG)
    monkeypatch.setattr(signal_rpc, "SIGNAL_CLI_AVATARS_DIR", avatars)
    from protocols.signal import SignalBackend

    media.api._host.manager.get = lambda proto: (
        SignalBackend() if proto == "signal" else None
    )

    blob = media.api.call_json(
        "contacts.avatar", {"protocol": "signal", "contact_id": "+39123"}
    )

    assert blob["content_type"] == "image/jpeg"
    assert Path(blob["path"]).read_bytes() == JPEG
    assert Path(blob["path"]).is_relative_to(Path(backend.CACHE_DIR))


def test_quoted_attachment_lookup(media):
    db_file = Path(backend.DB_FILE)
    with sqlite3.connect(db_file) as connection:
        connection.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, protocol TEXT, "
            "contact_number TEXT, timestamp INTEGER, attachment_id TEXT, "
            "content_type TEXT, msg_id TEXT, attachment_info TEXT)"
        )
        connection.execute(
            "INSERT INTO messages VALUES (1, 'signal', 'alice', 1000, 'img.png', "
            "'image/png', 'm1', 'IMG_9.png')"
        )
        connection.execute(
            "INSERT INTO messages VALUES (2, 'signal', 'alice', 5000, 'doc.pdf', "
            "'application/pdf', 'm2', 'doc.pdf')"
        )
    base = {"protocol": "signal", "contact_key": "alice"}

    def lookup(**extra):
        return media.api.call_json("media.quoted_attachment", {**base, **extra})

    assert lookup(quote_timestamp=1000) == {"attachment_id": "img.png"}
    assert lookup(quote_timestamp=1500) == {"attachment_id": "img.png"}  # drift
    assert lookup(quote_timestamp=5000) == {"attachment_id": None}  # not an image
    assert lookup(reply_to_message_id="m1") == {"attachment_id": "img.png"}
    assert lookup(quote_text="IMG_9.png — photo", content_type="image/png") == {
        "attachment_id": "img.png"
    }
    assert lookup() == {"attachment_id": None}


# -- TUI call sites go through the client -------------------------------------


def test_tui_resolves_attachments_through_the_client(tmp_path, monkeypatch):
    from client_harness import client_for

    from tui.blobs import resolve_attachment_path

    root = tmp_path / "media"
    root.mkdir()
    monkeypatch.setattr(signal_rpc, "SIGNAL_CLI_ATTACHMENTS_DIR", root)
    photo = _png(root / "photo.png", (8, 8))
    outside = tmp_path / "outside.png"
    _png(outside, (8, 8))
    paths = {"photo.png": photo, "outside": outside}
    calls: list[tuple[str, str]] = []

    def get_attachment_path(proto, attachment_id):
        calls.append((proto, attachment_id))
        return paths.get(attachment_id)

    client = client_for(SimpleNamespace(get_attachment_path=get_attachment_path))

    assert resolve_attachment_path(client, "signal", "photo.png") == photo.resolve()
    assert calls == [("signal", "photo.png")]
    # not_found (missing, outside the root, unknown protocol) is None, like the
    # old backend lookup.
    assert resolve_attachment_path(client, "signal", "missing") is None
    assert resolve_attachment_path(client, "signal", "outside") is None
    assert resolve_attachment_path(client, "irc", "photo.png") is None


def test_adapters_do_not_resolve_attachment_files_themselves():
    root = Path(__file__).resolve().parent.parent
    for name in (
        "tui/chat_view.py",
        "tui/download.py",
        "tui/send.py",
        "web/api.py",
    ):
        source = (root / name).read_text(encoding="utf-8")
        assert "get_attachment_path" not in source, name
        assert "get_attachment_chunk" not in source, name
        assert "sqlite3" not in source or name == "tui/send.py", name
    for name in ("tui/chat_view.py", "tui/download.py", "web/api.py"):
        assert "avatar_cache" not in (root / name).read_text(encoding="utf-8")
