from __future__ import annotations

import io
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import quote

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import backend
import web.api as web_api
from web import video_thumbs
from web.api import _is_video_candidate, create_api_router
from web.video_thumbs import _mp4_has_moov, _video_thumbnail


def _box(box_type: bytes, payload: bytes = b"", *, large: bool = False) -> bytes:
    if large:
        return (
            b"\x00\x00\x00\x01"
            + box_type
            + (16 + len(payload)).to_bytes(8, "big")
            + payload
        )
    return (8 + len(payload)).to_bytes(4, "big") + box_type + payload


def _client(monkeypatch, tmp_path: Path, files: dict[str, Path]) -> TestClient:
    root = tmp_path / "media"
    root.mkdir(exist_ok=True)
    monkeypatch.setattr(backend, "SIGNAL_CLI_ATTACHMENTS_DIR", root)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    manager = SimpleNamespace(
        get_attachment_path=lambda _proto, attachment_id: files.get(attachment_id)
    )
    app = FastAPI()
    app.state.manager = manager
    app.include_router(create_api_router())
    return TestClient(app)


def _url(attachment_id: str, width: int = 480) -> str:
    return f"/api/media/signal/{quote(attachment_id, safe='')}?w={width}"


def _fake_ffmpeg(calls: list[tuple[list[str], dict]]):
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        Image.new("RGB", (800, 450), "navy").save(argv[-1], "JPEG")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    return run


def test_mp4_box_parser_detects_faststart_and_tail_moov(tmp_path):
    ftyp = _box(b"ftyp", b"isom")
    moov = _box(b"moov", b"metadata", large=True)
    mdat = _box(b"mdat", b"frame-data")
    faststart = ftyp + moov + mdat
    tail_moov = ftyp + mdat + moov

    faststart_path = tmp_path / "faststart.mp4"
    tail_path = tmp_path / "tail.mp4"
    faststart_path.write_bytes(faststart)
    tail_path.write_bytes(tail_moov)

    assert _mp4_has_moov(faststart_path.read_bytes()[: len(ftyp) + len(moov)])
    assert not _mp4_has_moov(tail_path.read_bytes()[: len(ftyp) + len(mdat)])
    assert _mp4_has_moov(tail_path.read_bytes())
    assert not _mp4_has_moov(b"\x00\x00\x00\x00mdatignored")


def test_video_thumbnail_invokes_ffmpeg_and_caches_jpeg(monkeypatch, tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(video_thumbs, "_ffmpeg_executable", lambda: "/usr/bin/ffmpeg")
    calls: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(video_thumbs.subprocess, "run", _fake_ffmpeg(calls))

    thumbnail = _video_thumbnail(source, "signal", "clip.mp4", 240)

    assert thumbnail is not None
    assert thumbnail.suffix == ".jpg"
    with Image.open(thumbnail) as image:
        assert image.format == "JPEG"
        assert max(image.size) <= 240
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[:7] == [
        "/usr/bin/ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        "0",
        "-i",
    ]
    assert argv[7] == str(source)
    assert argv[argv.index("-map") + 1] == "0:v:0"
    assert argv[argv.index("-frames:v") + 1] == "1"
    assert kwargs == {"capture_output": True, "check": False, "timeout": 15}

    assert _video_thumbnail(source, "signal", "clip.mp4", 240) == thumbnail
    assert len(calls) == 1


def test_is_video_candidate_uses_content_type_and_extension(monkeypatch, tmp_path):
    content_types = {
        "typed.bin": "video/mp4",
        "picture.png": "image/png",
        "document.pdf": "application/pdf",
    }
    monkeypatch.setattr(
        web_api,
        "_attachment_content_type",
        lambda _proto, attachment_id: content_types[attachment_id],
    )

    assert _is_video_candidate(tmp_path / "typed.bin", "signal", "typed.bin")
    assert not _is_video_candidate(tmp_path / "picture.png", "signal", "picture.png")
    assert not _is_video_candidate(tmp_path / "document.pdf", "signal", "document.pdf")


def test_media_video_thumbnail_returns_jpeg(monkeypatch, tmp_path):
    source = tmp_path / "media" / "clip.mp4"
    source.parent.mkdir()
    source.write_bytes(b"video")
    client = _client(monkeypatch, tmp_path, {source.name: source})
    monkeypatch.setattr(video_thumbs, "_ffmpeg_executable", lambda: "/usr/bin/ffmpeg")
    calls: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(video_thumbs.subprocess, "run", _fake_ffmpeg(calls))

    response = client.get(_url(source.name, 96))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.headers["cache-control"].endswith("immutable")
    with Image.open(io.BytesIO(response.content)) as image:
        assert max(image.size) <= 96
    assert len(calls) == 1


def test_media_video_thumbnail_unavailable_returns_422(monkeypatch, tmp_path):
    source = tmp_path / "media" / "unsupported.mp4"
    source.parent.mkdir()
    source.write_bytes(b"video-original")
    client = _client(monkeypatch, tmp_path, {source.name: source})
    monkeypatch.setattr(video_thumbs, "_ffmpeg_executable", lambda: None)

    response = client.get(_url(source.name))

    assert response.status_code == 422
    assert response.json() == {"detail": "Video thumbnail unavailable"}
    assert response.content != source.read_bytes()


def test_chunk_pipeline_uses_head_and_tail_and_stable_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(video_thumbs, "_ffmpeg_executable", lambda: "/usr/bin/ffmpeg")
    calls: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(video_thumbs.subprocess, "run", _fake_ffmpeg(calls))
    ranges = []
    head = _box(b"ftyp", b"isom") + _box(b"mdat", b"frame")
    tail = _box(b"moov", b"metadata")

    def chunk(_proto, _attachment_id, start, length):
        ranges.append((start, length))
        return head if start == 0 else tail

    manager = SimpleNamespace(get_attachment_chunk=chunk)
    thumbnail, available = video_thumbs._chunk_video_thumbnail(
        manager, "whatsapp", "video.mp4", 480
    )

    assert available is True
    assert thumbnail is not None and thumbnail.is_file()
    assert ranges == [(0, 512 * 1024), (None, 2 * 1024 * 1024)]
    assert len(calls) == 1

    cached, available = video_thumbs._chunk_video_thumbnail(
        manager, "whatsapp", "video.mp4", 480
    )
    assert available is True
    assert cached == thumbnail
    assert len(ranges) == 2


def test_chunk_pipeline_faststart_uses_only_head(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(video_thumbs, "_ffmpeg_executable", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(video_thumbs.subprocess, "run", _fake_ffmpeg([]))
    ranges = []
    head = _box(b"ftyp", b"isom") + _box(b"moov", b"metadata")

    def chunk(_proto, _attachment_id, start, length):
        ranges.append((start, length))
        return head

    thumbnail, available = video_thumbs._chunk_video_thumbnail(
        SimpleNamespace(get_attachment_chunk=chunk),
        "telegram",
        "tgref:42:99",
        96,
    )

    assert available is True
    assert thumbnail is not None
    assert ranges == [(0, 512 * 1024)]


def test_whatsapp_media_endpoint_uses_chunk_without_full_download(
    monkeypatch, tmp_path
):
    media_root = tmp_path / "whatsapp-media"
    media_root.mkdir()
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(video_thumbs, "_ffmpeg_executable", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(video_thumbs.subprocess, "run", _fake_ffmpeg([]))
    head = _box(b"ftyp", b"isom") + _box(b"moov", b"metadata")
    backend_instance = SimpleNamespace(_ensure_media_dir=lambda: media_root)
    manager = SimpleNamespace(
        get=lambda _proto: backend_instance,
        get_attachment_chunk=lambda *_args: head,
        get_attachment_path=MagicMock(side_effect=AssertionError("full download")),
    )
    app = FastAPI()
    app.state.manager = manager
    app.include_router(create_api_router())

    response = TestClient(app).get("/api/media/whatsapp/video.mp4?w=480")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
    manager.get_attachment_path.assert_not_called()


def test_chunk_failure_falls_back_to_complete_file(monkeypatch, tmp_path):
    media_root = tmp_path / "telegram-media"
    media_root.mkdir()
    complete = media_root / "complete.mp4"
    complete.write_bytes(b"complete")
    monkeypatch.setattr("backends.telegram._media_dir", lambda: media_root)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(web_api, "_attachment_content_type", lambda *_args: "video/mp4")
    monkeypatch.setattr(video_thumbs, "_ffmpeg_executable", lambda: "/usr/bin/ffmpeg")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 2:
            Image.new("RGB", (320, 180), "navy").save(argv[-1], "JPEG")
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return subprocess.CompletedProcess(argv, 1, b"", b"invalid chunk")

    monkeypatch.setattr(video_thumbs.subprocess, "run", run)
    backend_instance = SimpleNamespace()
    manager = SimpleNamespace(
        get=lambda _proto: backend_instance,
        get_attachment_chunk=lambda *_args: _box(b"ftyp", b"isom") + _box(b"moov"),
        get_attachment_path=MagicMock(return_value=complete),
    )
    app = FastAPI()
    app.state.manager = manager
    app.include_router(create_api_router())

    response = TestClient(app).get("/api/media/telegram/tgref%3A42%3A99?w=240")

    assert response.status_code == 200
    assert len(calls) == 2
    manager.get_attachment_path.assert_called_once_with("telegram", "tgref:42:99")


def test_insufficient_chunk_without_complete_file_returns_422(monkeypatch, tmp_path):
    media_root = tmp_path / "whatsapp-media"
    media_root.mkdir()
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(video_thumbs, "_ffmpeg_executable", lambda: None)
    backend_instance = SimpleNamespace(_ensure_media_dir=lambda: media_root)
    manager = SimpleNamespace(
        get=lambda _proto: backend_instance,
        get_attachment_chunk=lambda *_args: _box(b"ftyp", b"isom") + _box(b"moov"),
        get_attachment_path=lambda *_args: None,
    )
    app = FastAPI()
    app.state.manager = manager
    app.include_router(create_api_router())

    response = TestClient(app).get("/api/media/whatsapp/video.mp4?w=480")

    assert response.status_code == 422
    assert response.json() == {"detail": "Video thumbnail unavailable"}
