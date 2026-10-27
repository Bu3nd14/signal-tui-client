"""Phase 4 Gmail HTML rendering tests: inline ``cid:`` images.

Inline image parts get an expiring ``gmail-part:`` token signed like remote
image URLs; the sanitizer rewrites ``<img src="cid:…">`` to it and the image
proxy serves the MIME part only when its bytes are a raster image.
"""

from __future__ import annotations

import base64
import struct
import time
import zlib
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from client_harness import api_for
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import gmail
from web.gmail_api import create_gmail_image_router


def _token(url: str) -> str:
    return parse_qs(urlsplit(url).query)["token"][0]


def _png(width: int = 2, height: int = 2) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IEND", b"")


def _client() -> TestClient:
    app = FastAPI()
    app.state.api = api_for(SimpleNamespace())
    app.include_router(create_gmail_image_router())
    return TestClient(app)


# ── tokens ───────────────────────────────────────────────────────────────────


def test_part_token_roundtrip() -> None:
    url = gmail.sign_part_image_url("Lavoro/Progetti 2026", 42, 3)
    payload = gmail.verify_image_token(_token(url))

    assert url.startswith("gmail-image?token=")
    assert gmail.parse_part_image(payload) == ("Lavoro/Progetti 2026", 42, 3)


def test_part_token_expires() -> None:
    now = time.time()
    payload = gmail.verify_image_token(
        _token(gmail.sign_part_image_url("INBOX", 1, 0, now=now))
    )

    assert gmail.parse_part_image(payload, now=now + gmail._PART_IMAGE_TTL_S - 1)
    assert (
        gmail.parse_part_image(payload, now=now + gmail._PART_IMAGE_TTL_S + 1) is None
    )


@pytest.mark.parametrize(
    "payload",
    [
        "https://images.example/a.png",
        "gmail-part:",
        "gmail-part:9999999999:1:2",
        "gmail-part:x:1:2:INBOX",
        "gmail-part:9999999999:-1:2:INBOX",
        "gmail-part:9999999999:1:2:IN*BOX",
        "gmail-part:9999999999:1:2:..",
    ],
)
def test_part_token_rejects_malformed_payloads(payload: str) -> None:
    assert gmail.parse_part_image(payload) is None


def test_remote_image_urls_are_never_signed_as_parts() -> None:
    cleaned = gmail.sanitize_html('<img src="gmail-part:9999999999:1:0:INBOX">')

    assert "data-src" not in cleaned


# ── sanitizer ────────────────────────────────────────────────────────────────


def test_cid_images_are_rewritten_to_signed_part_urls() -> None:
    url = gmail.sign_part_image_url("INBOX", 42, 3)
    cleaned = gmail.sanitize_html(
        '<p>x</p><img src="cid:Logo@X" alt="logo">'
        '<img src="CID:%3Clogo@x%3E"><img src="cid:other@x">',
        {"logo@x": url},
    )

    sources = [part.split('"')[0] for part in cleaned.split('data-src="')[1:]]
    assert [source.replace("&amp;", "&") for source in sources] == [url, url]
    assert "cid:" not in cleaned
    assert gmail.sanitize_html(cleaned) == cleaned


def test_cid_images_without_mapping_are_dropped() -> None:
    cleaned = gmail.sanitize_html('<img src="cid:logo@x" alt="logo">')

    assert "data-src" not in cleaned
    assert "cid:" not in cleaned


def _message_with_inline_logo() -> bytes:
    message = EmailMessage()
    message["From"] = "a@example.com"
    message["To"] = "b@example.com"
    message["Subject"] = "s"
    message.set_content("testo")
    message.add_alternative('<p>ciao</p><img src="cid:logo123">', subtype="html")
    message.get_payload()[1].add_related(
        _png(), maintype="image", subtype="png", cid="<logo123>"
    )
    return message.as_bytes()


def test_parse_full_with_folder_and_uid_links_inline_images() -> None:
    parsed = gmail._parse_full(_message_with_inline_logo(), folder="INBOX", uid=7)

    (attachment,) = parsed["attachments"]
    assert attachment["inline"] is True
    source = parsed["html"].split('data-src="')[1].split('"')[0].replace("&amp;", "&")
    payload = gmail.verify_image_token(_token(source))
    assert gmail.parse_part_image(payload) == ("INBOX", 7, attachment["index"])


def test_parse_message_without_location_drops_inline_images() -> None:
    parsed = gmail.parse_message(_message_with_inline_logo())

    assert "data-src" not in parsed["html"]


# ── proxy ────────────────────────────────────────────────────────────────────


def _download(data: bytes, content_type: str = "image/png") -> gmail.AttachmentDownload:
    return gmail.AttachmentDownload(
        filename="logo.png", content_type=content_type, data=data
    )


def test_proxy_serves_inline_part_as_raster_image() -> None:
    token = _token(gmail.sign_part_image_url("INBOX", 42, 3))
    calls: list[tuple[object, ...]] = []

    def fake_fetch(uid, index, folder=None):
        calls.append((uid, index, folder))
        return _download(_png(), content_type="application/octet-stream")

    with patch.object(gmail, "fetch_attachment", new=fake_fetch):
        response = _client().get("/gmail-image", params={"token": token})

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.content.startswith(b"\x89PNG")
    assert calls == [(42, 3, "INBOX")]


@pytest.mark.parametrize(
    "data",
    [
        b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
        b"<html><script>alert(1)</script></html>",
        b"",
    ],
)
def test_proxy_refuses_non_raster_parts(data: bytes) -> None:
    token = _token(gmail.sign_part_image_url("INBOX", 42, 3))

    with patch.object(
        gmail, "fetch_attachment", return_value=_download(data, "image/png")
    ):
        response = _client().get("/gmail-image", params={"token": token})

    assert response.status_code == 502


def test_proxy_refuses_oversized_parts() -> None:
    token = _token(gmail.sign_part_image_url("INBOX", 42, 3))
    huge = _png(6000, 6000)  # 36 MP > 25 MP

    with patch.object(gmail, "fetch_attachment", return_value=_download(huge)):
        response = _client().get("/gmail-image", params={"token": token})

    assert response.status_code == 502


def test_proxy_rejects_expired_part_token() -> None:
    old = time.time() - gmail._PART_IMAGE_TTL_S - 10
    token = _token(gmail.sign_part_image_url("INBOX", 42, 3, now=old))

    with patch.object(gmail, "fetch_attachment") as fetch:
        response = _client().get("/gmail-image", params={"token": token})

    assert response.status_code == 404
    fetch.assert_not_called()


def test_proxy_maps_missing_part_to_404() -> None:
    token = _token(gmail.sign_part_image_url("INBOX", 42, 99))

    with patch.object(
        gmail,
        "fetch_attachment",
        side_effect=gmail.GmailNotFound("Allegato non trovato"),
    ):
        response = _client().get("/gmail-image", params={"token": token})

    assert response.status_code == 404


# ── prefetch cache ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _empty_inline_cache():
    gmail.clear_inline_image_cache()
    yield
    gmail.clear_inline_image_cache()


def _part(
    walk_index: str,
    maintype: str = "image",
    *,
    content_id: str | None = "logo@x",
    disposition: str | None = "inline",
    octets: int | None = 1000,
) -> gmail._MimePart:
    return gmail._MimePart(
        walk_index=walk_index,
        maintype=maintype,
        subtype="png",
        params=(),
        disposition=disposition,
        disposition_params=(),
        content_id=content_id,
        encoding="base64",
        octets=octets,
        filename=None,
    )


def test_inline_candidates_are_small_cid_images_not_marked_as_attachments() -> None:
    big = gmail._INLINE_PREFETCH_PART_MAX + 1
    parts = (
        _part("2"),
        _part("3", disposition=None),
        _part("4", "application"),
        _part("5", content_id=None),
        _part("6", disposition="attachment"),
        _part("7", octets=None),
        _part("8", octets=big),
    )
    index = gmail._PartMap(parts=parts, render_parts=(), attachments=parts)

    assert [p.walk_index for p in gmail._inline_image_candidates(index)] == ["2", "3"]


def test_inline_candidates_respect_the_total_budget() -> None:
    size = gmail._INLINE_PREFETCH_PART_MAX
    count = gmail._INLINE_PREFETCH_TOTAL_MAX // size
    parts = tuple(_part(str(i + 2), octets=size) for i in range(count + 1))
    index = gmail._PartMap(parts=parts, render_parts=(), attachments=parts)

    assert len(gmail._inline_image_candidates(index)) == count


def test_cache_only_inline_attachments_from_payloads() -> None:
    parts = (_part("2"), _part("3", content_id="other@x"))
    index = gmail._PartMap(parts=parts, render_parts=(), attachments=parts)
    payloads = {"2": base64.b64encode(_png()), "3": base64.b64encode(_png())}
    attachments = [{"index": 0, "inline": True}, {"index": 1, "inline": False}]

    gmail._cache_inline_images("INBOX", 5, index, attachments, payloads)

    assert gmail.cached_inline_image("INBOX", 5, 0) == _png()
    assert gmail.cached_inline_image("INBOX", 5, 1) is None


def test_inline_cache_expires_and_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(gmail.time, "monotonic", lambda: clock[0])
    gmail._inline_cache_put("INBOX", 1, 0, b"a" * 10)
    clock[0] += gmail._INLINE_CACHE_TTL_S + 1
    assert gmail.cached_inline_image("INBOX", 1, 0) is None

    monkeypatch.setattr(gmail, "_INLINE_CACHE_MAX_BYTES", 25)
    for index in range(3):
        gmail._inline_cache_put("INBOX", 2, index, b"b" * 10)
    assert gmail.cached_inline_image("INBOX", 2, 0) is None
    assert gmail.cached_inline_image("INBOX", 2, 1) == b"b" * 10
    assert gmail.cached_inline_image("INBOX", 2, 2) == b"b" * 10
    assert gmail._inline_cache_bytes == 20


def test_proxy_serves_prefetched_image_without_imap() -> None:
    gmail._inline_cache_put("INBOX", 42, 3, _png())
    token = _token(gmail.sign_part_image_url("INBOX", 42, 3))

    with patch.object(gmail, "fetch_attachment") as fetch:
        response = _client().get("/gmail-image", params={"token": token})

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    fetch.assert_not_called()
