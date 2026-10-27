"""Remote Gmail images: background prefetch, shared fetches and cache.

Opening a message starts downloading its remote images on the server loop;
the browser's ``<img>`` requests then hit the cache or join the in-flight
fetch.  ``tests/conftest.py`` disables the prefetch (``GMAIL_IMAGE_PREFETCH``)
and empties the caches around every test; tests here turn it back on.
"""

from __future__ import annotations

import asyncio

import pytest

from server import gmail
from server.ops import gmail as ops

_PNG = b"\x89PNG\r\n\x1a\nfake"


def _html(*urls: str) -> str:
    return "".join(
        f'<img data-src="{gmail.sign_image_url(url).replace("&", "&amp;")}">'
        for url in urls
    )


@pytest.fixture
def fetches(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def fake_fetch(url, max_bytes, max_pixels=None):
        calls.append(url)
        await asyncio.sleep(0.01)
        return _PNG, "image/png"

    monkeypatch.setattr("server.link_preview.fetch_external_image", fake_fetch)
    return calls


def test_remote_image_urls_skip_inline_parts_and_duplicates(monkeypatch) -> None:
    part = gmail.sign_part_image_url("INBOX", 1, 0).replace("&", "&amp;")
    html = _html("https://a.example/1.png", "https://a.example/1.png")
    html += f'<img data-src="{part}">' + _html("https://a.example/2.png")
    html += '<img data-src="gmail-image?token=forged.0123">'

    assert ops._remote_image_urls(html) == [
        "https://a.example/1.png",
        "https://a.example/2.png",
    ]

    monkeypatch.setattr(ops, "_REMOTE_PREFETCH_MAX", 3)
    many = _html(*(f"https://a.example/{i}.png" for i in range(10)))
    assert len(ops._remote_image_urls(many)) == 3


async def test_concurrent_requests_share_one_fetch(fetches: list[str]) -> None:
    results = await asyncio.gather(
        *(ops._remote_image("https://a.example/x.png") for _ in range(5))
    )

    assert fetches == ["https://a.example/x.png"]
    assert all(result == (_PNG, "image/png") for result in results)
    assert ops._remote_inflight == {}
    assert await ops._remote_image("https://a.example/x.png") == (_PNG, "image/png")
    assert fetches == ["https://a.example/x.png"]


async def test_cancelled_request_does_not_cancel_the_shared_fetch(
    fetches: list[str],
) -> None:
    first = asyncio.create_task(ops._remote_image("https://a.example/x.png"))
    await asyncio.sleep(0)
    second = asyncio.create_task(ops._remote_image("https://a.example/x.png"))
    await asyncio.sleep(0)
    first.cancel()

    assert await second == (_PNG, "image/png")
    assert fetches == ["https://a.example/x.png"]


async def test_failed_fetch_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    results: list[tuple[bytes, str] | None] = [None, (_PNG, "image/png")]

    async def flaky(url, max_bytes, max_pixels=None):
        return results.pop(0)

    monkeypatch.setattr("server.link_preview.fetch_external_image", flaky)

    assert await ops._remote_image("https://a.example/x.png") is None
    assert await ops._remote_image("https://a.example/x.png") == (_PNG, "image/png")


def test_remote_cache_expires_and_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(ops.time, "monotonic", lambda: clock[0])
    ops._remote_cache_put("https://a.example/old.png", b"a" * 10, "image/png")
    clock[0] += ops._REMOTE_IMAGE_TTL_S + 1
    assert ops._remote_cache_get("https://a.example/old.png") is None

    monkeypatch.setattr(ops, "_REMOTE_IMAGE_CACHE_MAX_BYTES", 25)
    for index in range(3):
        ops._remote_cache_put(f"https://a.example/{index}.png", b"b" * 10, "image/png")
    assert ops._remote_cache_get("https://a.example/0.png") is None
    assert ops._remote_cache_get("https://a.example/2.png") == (b"b" * 10, "image/png")
    assert ops._remote_cache_bytes == 20


async def test_opening_a_message_prefetches_its_remote_images(
    monkeypatch: pytest.MonkeyPatch, fetches: list[str]
) -> None:
    monkeypatch.setenv("GMAIL_IMAGE_PREFETCH", "1")
    html = _html("https://a.example/1.png", "https://a.example/2.png")
    monkeypatch.setattr(
        gmail, "fetch_message", lambda uid, folder=None: {"uid": uid, "html": html}
    )

    detail = await ops.gmail_messages_get(
        None, ops.GmailMessageRef(uid=7, folder="INBOX")
    )
    assert detail["html"] == html
    await asyncio.gather(*ops._background)

    assert sorted(fetches) == ["https://a.example/1.png", "https://a.example/2.png"]
    assert await ops._remote_image("https://a.example/1.png") == (_PNG, "image/png")
    assert len(fetches) == 2


async def test_prefetch_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch, fetches: list[str]
) -> None:
    monkeypatch.setenv("GMAIL_IMAGE_PREFETCH", "0")
    html = _html("https://a.example/1.png")
    monkeypatch.setattr(
        gmail, "fetch_message", lambda uid, folder=None: {"uid": uid, "html": html}
    )

    await ops.gmail_messages_get(None, ops.GmailMessageRef(uid=7, folder="INBOX"))
    await asyncio.sleep(0.02)

    assert ops._background == set()
    assert fetches == []
