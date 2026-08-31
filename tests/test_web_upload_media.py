from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import pytest

from web.uploads import UploadValidationError, _store_upload_sync


def _upload(name: str, data: bytes):
    return SimpleNamespace(filename=name, file=BytesIO(data))


@pytest.mark.parametrize(
    "name,data,mime,kind",
    [
        ("report.pdf", b"%PDF-1.7\n", "application/pdf", "document"),
        ("song.mp3", b"ID3\x04\x00\x00payload", "audio/mpeg", "audio"),
        ("voice.ogg", b"OggS\x00payload", "audio/ogg", "audio"),
        (
            "clip.mp4",
            b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00",
            "video/mp4",
            "video",
        ),
    ],
)
def test_store_upload_sniffs_media_kind(monkeypatch, tmp_path, name, data, mime, kind):
    monkeypatch.setattr("web.uploads.upload_directory", lambda: tmp_path)

    stored = _store_upload_sync(_upload(name, data))
    try:
        assert stored.mime_type == mime
        assert stored.media_kind == kind
        assert stored.path.read_bytes() == data
    finally:
        stored.cleanup()


def test_store_upload_applies_kind_limit(monkeypatch, tmp_path):
    monkeypatch.setattr("web.uploads.upload_directory", lambda: tmp_path)
    monkeypatch.setitem(
        __import__("web.uploads", fromlist=["_MAX_BYTES_BY_KIND"])._MAX_BYTES_BY_KIND,
        "image",
        8,
    )

    with pytest.raises(UploadValidationError) as error:
        _store_upload_sync(_upload("large.png", b"\x89PNG\r\n\x1a\nextra"))

    assert error.value.status_code == 413


def test_store_upload_rejects_incoherent_extension(monkeypatch, tmp_path):
    monkeypatch.setattr("web.uploads.upload_directory", lambda: tmp_path)

    with pytest.raises(UploadValidationError) as error:
        _store_upload_sync(_upload("report.mp3", b"%PDF-1.7\n"))

    assert error.value.status_code == 400


def test_store_upload_rejects_unknown_signature(monkeypatch, tmp_path):
    monkeypatch.setattr("web.uploads.upload_directory", lambda: tmp_path)

    with pytest.raises(UploadValidationError) as error:
        _store_upload_sync(_upload("notes.txt", b"plain text"))

    assert error.value.status_code == 400
