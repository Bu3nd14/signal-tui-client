"""Voice note upload classification (design v2, gating R1/R3).

Copre il riconoscimento backend di una nota vocale registrata dal browser:
WebM (``A_OPUS`` vs ``V_VP8``), MP4/M4A (box-walk ``moov``/``hdlr``), l'hint
per-file ``upload.content_type`` e il servizio media come audio (non video).
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

import web.api as web_api
from web.api import (
    _is_video_candidate,
    _media_content_type,
)
from web.uploads import (
    UploadValidationError,
    _classify_upload,
    _mp4_has_video_track,
    _sniff_webm_kind,
    _store_upload_sync,
)

# ── Fixture builder ───────────────────────────────────────────────────────────


def _box(box_type: bytes, payload: bytes = b"", *, large: bool = False) -> bytes:
    if large:
        return (
            b"\x00\x00\x00\x01"
            + box_type
            + (16 + len(payload)).to_bytes(8, "big")
            + payload
        )
    return (8 + len(payload)).to_bytes(4, "big") + box_type + payload


def _hdlr(handler: bytes) -> bytes:
    # hdlr: fullbox(4) + pre_defined(4) + handler_type(4) + reserved(12).
    return _box(b"hdlr", b"\x00" * 8 + handler + b"\x00" * 12)


def _trak(handler: bytes) -> bytes:
    return _box(b"trak", _box(b"mdia", _hdlr(handler)))


def _moov(*tracks: bytes) -> bytes:
    return _box(b"moov", b"".join(tracks))


def _ftyp(brand: bytes) -> bytes:
    return _box(b"ftyp", brand + b"\x00\x00\x00\x00")


#: Prefisso EBML: identifica il container WebM/Matroska per ``_sniff_media``.
EBML = b"\x1aE\xdf\xa3" + b"\x00" * 20


def _upload(name: str, data: bytes, content_type: str | None = None) -> SimpleNamespace:
    upload = SimpleNamespace(filename=name, file=BytesIO(data))
    if content_type is not None:
        upload.content_type = content_type
    return upload


def _store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    data: bytes,
    content_type: str | None = None,
):
    monkeypatch.setattr("web.uploads.upload_directory", lambda: tmp_path)
    return _store_upload_sync(_upload(name, data, content_type))


# ── _sniff_webm_kind ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"A_OPUS", "audio"),
        (b"A_VORBIS", "audio"),
        (b"V_VP8", "video"),
        (b"V_VP9", "video"),
        # Any video codec wins over an audio track in the same container.
        (b"V_VP8A_OPUS", "video"),
        (b"", None),
        (b"random-bytes-without-codec", None),
    ],
)
def test_sniff_webm_kind_codec_ids(payload: bytes, expected: str | None):
    assert _sniff_webm_kind(EBML + payload) == expected


# ── _mp4_has_video_track ──────────────────────────────────────────────────────


def test_mp4_has_video_track_audio_only_returns_audio():
    assert _mp4_has_video_track(_ftyp(b"isom") + _moov(_trak(b"soun"))) == "audio"


def test_mp4_has_video_track_video_returns_video():
    assert _mp4_has_video_track(_ftyp(b"isom") + _moov(_trak(b"vide"))) == "video"


def test_mp4_has_video_track_r1_audio_first_video_second_returns_video():
    """R1: with both tracks present, any video track wins."""
    data = _ftyp(b"isom") + _moov(_trak(b"soun"), _trak(b"vide"))
    assert _mp4_has_video_track(data) == "video"


def test_mp4_has_video_track_moov_in_tail_returns_none():
    # `moov` beyond the sniffed bytes (tail): nessuna prova → None.
    data = _ftyp(b"mp42") + _box(b"mdat", b"\x00" * 100)
    assert _mp4_has_video_track(data) is None


def test_mp4_has_video_track_extended_size_moov_is_a_known_gap():
    """Finding (low): il box-walk annidato assume header a 8 byte.

    Un ``moov`` con size estesa a 64 bit (``size == 1``) non viene scompattato:
    i figli partono da ``box_start + 8`` invece di ``+ 16``, quindi la traccia
    video non viene trovata (None). Documentato qui, non bloccante per le note
    vocali (``moov`` è tipicamente < 4 GiB e usa size a 32 bit).
    """
    data = _ftyp(b"isom") + _box(b"moov", _trak(b"vide"), large=True)
    assert _mp4_has_video_track(data) is None


# ── _classify_upload / _store_upload_sync ─────────────────────────────────────


def test_classify_upload_is_pure_and_uses_content_type_hint():
    assert _classify_upload(
        EBML + b"A_OPUS", declared_content_type=None, filename="voice.webm"
    ) == ("audio/webm", "audio")
    # CodecID assente ma hint + suffisso coerente: l'hint decide.
    assert _classify_upload(
        EBML, declared_content_type="audio/webm;codecs=opus", filename="voice.webm"
    ) == ("audio/webm", "audio")
    # R1: un video codec nel WebM vince sempre sull'audio.
    assert _classify_upload(
        EBML + b"V_VP8A_OPUS", declared_content_type="audio/webm", filename="v.webm"
    ) == ("video/webm", "video")
    with pytest.raises(UploadValidationError) as error:
        _classify_upload(b"not-media", declared_content_type=None, filename="x.bin")
    assert error.value.status_code == 400


def test_store_webm_audio_only_without_hint(monkeypatch, tmp_path):
    stored = _store(monkeypatch, tmp_path, "voice.webm", EBML + b"A_OPUS" + b"payload")
    try:
        assert (stored.mime_type, stored.media_kind) == ("audio/webm", "audio")
        assert stored.path.suffix == ".webm"
    finally:
        stored.cleanup()


def test_store_webm_video(monkeypatch, tmp_path):
    stored = _store(monkeypatch, tmp_path, "clip.webm", EBML + b"V_VP8" + b"payload")
    try:
        assert (stored.mime_type, stored.media_kind) == ("video/webm", "video")
    finally:
        stored.cleanup()


def test_store_webm_audio_with_content_type_hint(monkeypatch, tmp_path):
    # CodecID assente (refinement None) ma hint dichiarato e suffisso coerente.
    stored = _store(
        monkeypatch,
        tmp_path,
        "voice.webm",
        EBML + b"payload",
        content_type="audio/webm",
    )
    try:
        assert (stored.mime_type, stored.media_kind) == ("audio/webm", "audio")
    finally:
        stored.cleanup()


def test_store_m4a_mp42_tail_with_audio_hint(monkeypatch, tmp_path):
    # iOS: brand `mp42`, `moov` in coda oltre i 4096 byte sniffati, hint
    # audio/mp4 + estensione `.m4a` coerente → audio.
    data = _ftyp(b"mp42") + _box(b"mdat", b"\x00" * 5000) + _moov(_trak(b"soun"))
    stored = _store(monkeypatch, tmp_path, "voice.m4a", data, "audio/mp4")
    try:
        assert (stored.mime_type, stored.media_kind) == ("audio/mp4", "audio")
        assert stored.path.suffix == ".m4a"
    finally:
        stored.cleanup()


def test_store_mp4_audio_hint_incoherent_suffix_stays_video(monkeypatch, tmp_path):
    # `.mp4` + hint audio/mp4: suffisso NON coerente → nessun 400, resta video.
    data = _ftyp(b"isom") + b"\x00" * 8
    stored = _store(monkeypatch, tmp_path, "clip.mp4", data, "audio/mp4")
    try:
        assert (stored.mime_type, stored.media_kind) == ("video/mp4", "video")
    finally:
        stored.cleanup()


def test_store_png_ignores_audio_hint(monkeypatch, tmp_path):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
    stored = _store(monkeypatch, tmp_path, "foto.png", png, "audio/mp4")
    try:
        assert (stored.mime_type, stored.media_kind) == ("image/png", "image")
    finally:
        stored.cleanup()


@pytest.mark.parametrize("brand", [b"iso5", b"iso6", b"mp42"])
@pytest.mark.parametrize("name", ["voice.m4a", "clip.mp4"])
def test_store_r3_iso_brands_do_not_400(monkeypatch, tmp_path, brand: bytes, name: str):
    """R3: i brand frammentati iOS (iso*/mp42) non producono 400.

    Con `.m4a` + hint audio/mp4 diventano audio/mp4; con `.mp4` restano video.
    """
    data = _ftyp(brand) + b"\x00" * 8
    content_type = "audio/mp4" if name.endswith(".m4a") else "video/mp4"
    stored = _store(monkeypatch, tmp_path, name, data, content_type)
    try:
        if name.endswith(".m4a"):
            assert (stored.mime_type, stored.media_kind) == ("audio/mp4", "audio")
        else:
            assert (stored.mime_type, stored.media_kind) == ("video/mp4", "video")
    finally:
        stored.cleanup()


def test_store_unknown_ftyp_brand_rejects_400(monkeypatch, tmp_path):
    with pytest.raises(UploadValidationError) as error:
        _store(monkeypatch, tmp_path, "clip.mp4", _ftyp(b"xxxx") + b"\x00" * 8)
    assert error.value.status_code == 400


def test_store_mixed_voice_batch_no_400_and_mime(monkeypatch, tmp_path):
    """Batch misto voce+video: ogni file conserva il proprio mime, nessun 400."""
    cases = [
        ("voice.webm", EBML + b"A_OPUS", "audio/webm", "audio/webm", "audio"),
        ("video.mp4", _ftyp(b"isom") + b"\x00" * 8, "video/mp4", "video/mp4", "video"),
        ("clip.webm", EBML + b"V_VP8", "video/webm", "video/webm", "video"),
    ]
    stored = []
    try:
        for name, data, declared, mime, kind in cases:
            item = _store(monkeypatch, tmp_path, name, data, declared)
            stored.append(item)
            assert (item.mime_type, item.media_kind) == (mime, kind), name
    finally:
        for item in stored:
            item.cleanup()


# ── API: audio servito come audio (non video) ─────────────────────────────────


@pytest.mark.parametrize(
    ("suffix", "prefer_audio", "expected"),
    [
        (".webm", False, "video/webm"),
        (".webm", True, "audio/webm"),
        (".m4a", False, "audio/mp4"),
        (".m4a", True, "audio/mp4"),
        (".mp4", True, "video/mp4"),
    ],
)
def test_media_content_type_prefer_audio_only_for_webm(
    tmp_path: Path, suffix: str, prefer_audio: bool, expected: str
):
    assert (
        _media_content_type(tmp_path / f"clip{suffix}", prefer_audio=prefer_audio)
        == expected
    )


def test_is_video_candidate_rejects_audio_content_type(monkeypatch, tmp_path):
    monkeypatch.setattr(
        web_api, "_attachment_content_type", lambda *_args: "audio/webm"
    )
    assert not _is_video_candidate(tmp_path / "voice.webm", "signal", "voice.webm")


def test_is_video_candidate_rejects_audio_media_kind_without_content_type(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(web_api, "_attachment_content_type", lambda *_args: None)
    monkeypatch.setattr(web_api, "_attachment_media_kind", lambda *_args: "audio")
    assert not _is_video_candidate(tmp_path / "voice.webm", "signal", "voice.webm")


def test_is_video_candidate_keeps_video_webm_and_mp4(monkeypatch, tmp_path):
    monkeypatch.setattr(web_api, "_attachment_content_type", lambda *_args: None)
    monkeypatch.setattr(web_api, "_attachment_media_kind", lambda *_args: "video")
    assert _is_video_candidate(tmp_path / "clip.webm", "signal", "clip.webm")
    assert _is_video_candidate(tmp_path / "clip.mp4", "signal", "clip.mp4")


# ── Backend: una nota vocale non deve passare come video ──────────────────────


def test_whatsapp_audio_media_kind_routes_to_file_not_video(tmp_path, monkeypatch):
    """ACC-1: con media_kind 'audio' WhatsApp usa sendFile (non sendVideo)."""
    from unittest.mock import MagicMock

    from protocols.whatsapp import WhatsAppBackend

    backend = WhatsAppBackend(api_url="http://api.test", media_dir=str(tmp_path / "wa"))
    monkeypatch.setattr(backend, "_resolve_send_chat_id", lambda contact_id: contact_id)
    voice = tmp_path / "voice.webm"
    voice.write_bytes(EBML + b"A_OPUS")
    backend._rest.send_file = MagicMock(return_value={"id": "wa-audio"})
    backend._rest.send_video = MagicMock(return_value={"id": "wa-video"})

    message_ids = backend.send_attachments_sync(
        "39333@c.us",
        [voice],
        captions=[None],
        mime_types=["audio/webm"],
        media_kinds=["audio"],
        filenames=["voice.webm"],
    )

    assert message_ids == ["wa-audio"]
    backend._rest.send_file.assert_called_once()
    backend._rest.send_video.assert_not_called()


@pytest.mark.parametrize(
    ("media_kind", "content_type", "expected_prefix", "query"),
    [
        ("audio", "audio/webm", "audio/webm", ""),
        # Un audio richiesto come thumbnail NON deve passare per il video path.
        ("audio", "audio/webm", "audio/webm", "?w=96"),
        ("video", "video/webm", "video/webm", ""),
    ],
)
def test_media_endpoint_serves_webm_by_persisted_kind(
    monkeypatch,
    tmp_path,
    media_kind: str,
    content_type: str,
    expected_prefix: str,
    query: str,
):
    """ACC-1/R7: il media_kind persistito decide audio vs video per `.webm`."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import protocols.rpc as signal_rpc
    from protocols.db import _add_message_to_cache
    from web.api import create_api_router

    media_root = tmp_path / "signal-media"
    media_root.mkdir()
    monkeypatch.setattr(signal_rpc, "SIGNAL_CLI_ATTACHMENTS_DIR", media_root)
    attachment_id = "voice-1.webm"
    source = media_root / attachment_id
    source.write_bytes(EBML + b"A_OPUS")
    _add_message_to_cache(
        "alice",
        "",
        is_mine=True,
        sender="You",
        timestamp=1_000,
        msg_type="attachment",
        attachment_info=attachment_id,
        attachment_id=attachment_id,
        content_type=content_type,
        media_kind=media_kind,
    )
    manager = SimpleNamespace(
        get_attachment_path=lambda _proto, aid: source if aid == attachment_id else None
    )
    app = FastAPI()
    app.state.manager = manager
    app.include_router(create_api_router())

    response = TestClient(app).get(f"/api/media/signal/{attachment_id}{query}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(expected_prefix)
