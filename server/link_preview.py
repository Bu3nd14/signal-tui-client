"""Link previews (Open Graph cards) for URLs shown in chat messages.

The server fetches the page on behalf of the browser so that the card shows up
without the browser contacting third-party sites, and so that the page's HTML
never reaches the SPA (only a sanitized ``{title, description, site_name}``).

SSRF guard: only ``http``/``https`` on the default ports, no credentials in
the URL, and every resolved address (initial request and each redirect hop)
must be a public, globally routable IP.  The check lives in the aiohttp
resolver, so the address that is validated is the one that is connected to
(no DNS-rebinding window).

Core module (moved from ``web/``): the ``links.*`` operations call it, so every
adapter gets the same protections.  Do not weaken ``validate_url`` or the
resolver without a security review.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import re
import socket
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from protocols.location_thumbs import load_thumbnail, parse_maps_url, thumbnail_path

logger = logging.getLogger(__name__)

PREVIEW_TTL_SECONDS = 6 * 3600
FAILURE_TTL_SECONDS = 30 * 60
_CACHE_LIMIT = 512
_TIMEOUT_SECONDS = 6
_MAX_REDIRECTS = 4
_MAX_HTML_BYTES = 512 * 1024
_MAX_IMAGE_BYTES = 3 * 1024 * 1024
_MAX_URL_LENGTH = 2048
_ALLOWED_PORTS = {80, 443}
_USER_AGENT = "Mozilla/5.0 (compatible; MegachatLinkPreview/1.0)"
_TITLE_LIMIT = 200
_DESCRIPTION_LIMIT = 300
_SESSION_CONNECTOR_LIMIT = 8

# url -> (expires_at, preview dict or None)
_CACHE: OrderedDict[str, tuple[float, dict[str, Any] | None]] = OrderedDict()
_INFLIGHT: dict[str, asyncio.Future] = {}

# Shared HTTP session, bound to the loop that created it.  ``_closing`` blocks
# new sessions while an in-flight close is running.
_shared_session: Any | None = None
_shared_session_loop: asyncio.AbstractEventLoop | None = None
_shared_resolver: Any | None = None
_closing = False
_SHARED_SESSION_ENABLED = True


class BlockedURLError(ValueError):
    """The URL (or a redirect hop) points somewhere previews may not go."""


def _is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def validate_url(url: str) -> str:
    """Return *url* if previews may fetch it, else raise ``BlockedURLError``."""
    if not isinstance(url, str) or not url or len(url) > _MAX_URL_LENGTH:
        raise BlockedURLError("invalid url")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise BlockedURLError("invalid url") from exc
    if parts.scheme not in ("http", "https"):
        raise BlockedURLError("scheme not allowed")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host or parts.username is not None or parts.password is not None:
        raise BlockedURLError("invalid host")
    if port is not None and port not in _ALLOWED_PORTS:
        raise BlockedURLError("port not allowed")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise BlockedURLError("host not allowed")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return url
    # aiohttp skips the resolver for IP literals: check them here.
    if not _is_public_ip(host):
        raise BlockedURLError("address not allowed")
    return url


def _public_resolver() -> Any:
    from aiohttp.abc import AbstractResolver
    from aiohttp.resolver import ThreadedResolver

    class PublicOnlyResolver(AbstractResolver):
        def __init__(self) -> None:
            self._inner = ThreadedResolver()

        async def resolve(
            self, host: str, port: int = 0, family: int = socket.AF_INET
        ) -> list[dict[str, Any]]:
            infos = await self._inner.resolve(host, port, family)
            if not infos or not all(_is_public_ip(i["host"]) for i in infos):
                raise BlockedURLError(f"non-public address for {host}")
            return infos

        async def close(self) -> None:
            await self._inner.close()

    return PublicOnlyResolver()


class _MetaParser(HTMLParser):
    """Collect ``<title>`` and the Open Graph / Twitter / description metas."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_title = False
        self.done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "body":
            self.done = True
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            values = {k.lower(): (v or "") for k, v in attrs}
            key = (values.get("property") or values.get("name") or "").lower()
            content = values.get("content", "").strip()
            if key and content and key not in self.meta:
                self.meta[key] = content

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "head":
            self.done = True

    def handle_data(self, data: str) -> None:
        if self._in_title and len(self.title) < 1000:
            self.title += data


def _clean(value: str | None, limit: int) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def parse_preview(html: str, page_url: str) -> dict[str, Any] | None:
    """Extract a preview dict from *html*, or ``None`` when there's no title."""
    parser = _MetaParser()
    try:
        parser.feed(html)
    except Exception:  # malformed markup: keep what was collected
        logger.debug("link preview: html parse error", exc_info=True)
    meta = parser.meta
    title = _clean(
        meta.get("og:title") or meta.get("twitter:title") or parser.title,
        _TITLE_LIMIT,
    )
    if not title:
        return None
    description = _clean(
        meta.get("og:description")
        or meta.get("twitter:description")
        or meta.get("description"),
        _DESCRIPTION_LIMIT,
    )
    site_name = _clean(meta.get("og:site_name"), 80) or (
        urlsplit(page_url).hostname or ""
    ).removeprefix("www.")
    image = None
    raw_image = (
        meta.get("og:image:secure_url")
        or meta.get("og:image")
        or meta.get("og:image:url")
        or meta.get("twitter:image")
        or meta.get("twitter:image:src")
    )
    if raw_image:
        candidate = urljoin(page_url, raw_image.strip())
        try:
            image = validate_url(candidate)
        except BlockedURLError:
            image = None
    return {
        "url": page_url,
        "title": title,
        "description": description,
        "site_name": site_name,
        "image_url": image,
    }


async def _get(
    session: Any, url: str, max_bytes: int
) -> tuple[str, str, bytes, str | None]:
    """GET *url* following validated redirects; return (final_url, type, body, charset)."""
    current = validate_url(url)
    for _ in range(_MAX_REDIRECTS + 1):
        async with session.get(current, allow_redirects=False) as response:
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    raise BlockedURLError("redirect without location")
                current = validate_url(urljoin(current, location))
                continue
            if response.status != 200:
                raise BlockedURLError(f"status {response.status}")
            length = response.content_length
            if length is not None and length > max_bytes:
                raise BlockedURLError("response too large")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise BlockedURLError("response too large")
                chunks.append(chunk)
            body = b"".join(chunks)
            return current, response.content_type or "", body, response.charset
    raise BlockedURLError("too many redirects")


def _make_session() -> tuple[Any, Any]:
    import aiohttp

    resolver = _public_resolver()
    session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(
            resolver=resolver, limit=_SESSION_CONNECTOR_LIMIT
        ),
        cookie_jar=aiohttp.DummyCookieJar(),
        timeout=aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS),
        headers={
            "User-Agent": _USER_AGENT,
            "Accept-Language": "it,en;q=0.8",
        },
        trust_env=False,
    )
    return session, resolver


@asynccontextmanager
async def _transient_session():
    session, resolver = _make_session()
    try:
        yield session
    finally:
        try:
            await session.close()
        finally:
            await resolver.close()


def _get_or_create_shared_session() -> Any:
    global _shared_resolver, _shared_session, _shared_session_loop

    loop = asyncio.get_running_loop()
    session = _shared_session
    if session is not None and _shared_session_loop is loop and not session.closed:
        return session
    if _closing:
        raise RuntimeError("shared session is closing")
    if session is not None and _shared_session_loop is not loop:
        logger.debug("link preview: replacing shared session owned by another loop")
    session, resolver = _make_session()
    _shared_resolver, _shared_session, _shared_session_loop = (
        resolver,
        session,
        loop,
    )
    return session


@asynccontextmanager
async def shared_session():
    if _closing:
        raise RuntimeError("shared session is closing")
    if not _SHARED_SESSION_ENABLED:
        async with _transient_session() as session:
            yield session
        return
    yield _get_or_create_shared_session()


async def close_shared_session() -> None:
    """Close the shared session/resolver on their owning loop; idempotent."""
    global _closing, _shared_resolver, _shared_session, _shared_session_loop

    _closing = True
    try:
        loop = asyncio.get_running_loop()
        session = _shared_session
        resolver = _shared_resolver
        owner = _shared_session_loop
        if session is None:
            return
        if owner is not loop:
            # Leave it to its owner: with two adapters, each closes on its
            # own loop, and the first one must not orphan the other's session.
            logger.debug(
                "link preview: shared session owned by another loop; skip close"
            )
            return
        _shared_session = _shared_resolver = _shared_session_loop = None
        try:
            if not session.closed:
                await session.close()
        finally:
            if resolver is not None:
                await resolver.close()
    finally:
        _closing = False


async def _fetch_preview_uncached(url: str) -> dict[str, Any] | None:
    async with shared_session() as session:
        final_url, content_type, body, charset = await _get(
            session, url, _MAX_HTML_BYTES
        )
    if "html" not in content_type:
        return None
    # Cookie walls (e.g. Google's consent.google.com in the EU) aren't the page.
    if (urlsplit(final_url).hostname or "").startswith("consent."):
        return None
    if not charset:
        match = re.search(
            rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], re.IGNORECASE
        )
        charset = match.group(1).decode("ascii") if match else "utf-8"
    try:
        html = body.decode(charset, errors="replace")
    except LookupError:
        html = body.decode("utf-8", errors="replace")
    preview = parse_preview(html, final_url)
    if preview is not None:
        preview["url"] = url
    return preview


def _cache_get(url: str) -> tuple[bool, dict[str, Any] | None]:
    entry = _CACHE.get(url)
    if entry is None:
        return False, None
    expires_at, value = entry
    if expires_at < time.monotonic():
        _CACHE.pop(url, None)
        return False, None
    _CACHE.move_to_end(url)
    return True, value


def _cache_put(url: str, value: dict[str, Any] | None) -> None:
    ttl = PREVIEW_TTL_SECONDS if value is not None else FAILURE_TTL_SECONDS
    _CACHE[url] = (time.monotonic() + ttl, value)
    _CACHE.move_to_end(url)
    while len(_CACHE) > _CACHE_LIMIT:
        _CACHE.popitem(last=False)


def _location_preview(url: str, coords: tuple[float, float]) -> dict[str, Any] | None:
    """Card for a shared-location link, using WhatsApp's own map thumbnail.

    Served from disk: no request to Google (whose EU consent wall would
    replace the page anyway).  ``None`` when no thumbnail was received.
    """
    if not thumbnail_path(*coords).is_file():
        return None
    return {
        "url": url,
        "title": "Apri in Google Maps",
        "description": "",
        "site_name": "Google Maps",
        "image_url": None,
        "local_image": True,
    }


async def get_preview(url: str) -> dict[str, Any] | None:
    """Return the (cached) preview for *url*; ``None`` if there is none.

    Raises ``BlockedURLError`` for URLs that may not be fetched at all.
    """
    coords = parse_maps_url(url)
    if coords is not None:
        return _location_preview(url, coords)
    validate_url(url)
    hit, value = _cache_get(url)
    if hit:
        return value
    pending = _INFLIGHT.get(url)
    if pending is not None:
        return await asyncio.shield(pending)
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    _INFLIGHT[url] = future
    try:
        try:
            value = await _fetch_preview_uncached(url)
        except Exception as exc:  # noqa: BLE001 — preview best-effort
            logger.debug("link preview failed for %s: %s", url, exc)
            value = None
        _cache_put(url, value)
        future.set_result(value)
        return value
    finally:
        _INFLIGHT.pop(url, None)


async def get_preview_image(url: str) -> tuple[bytes, str] | None:
    """Return ``(bytes, content_type)`` of the preview image of page *url*.

    Only the image advertised by the page's own (cached) preview is served,
    so this endpoint can't be used as a generic fetch proxy.
    """
    preview = await get_preview(url)
    if preview and preview.get("local_image"):
        coords = parse_maps_url(url)
        data = load_thumbnail(*coords) if coords else None
        return (data, "image/jpeg") if data else None
    image_url = (preview or {}).get("image_url")
    if not image_url:
        return None
    try:
        async with shared_session() as session:
            _final, _type, body, _charset = await _get(
                session, image_url, _MAX_IMAGE_BYTES
            )
    except Exception as exc:  # noqa: BLE001 — preview best-effort
        logger.debug("link preview image failed for %s: %s", image_url, exc)
        return None
    # Trust the magic bytes, not the header: only raster formats (no SVG).
    sniffed = _sniff_raster(body)
    return (body, sniffed) if sniffed else None


_IMAGE_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
_IMAGE_DIR_NAME = "link-preview-images"
_IMAGE_KEEP = 256


def materialize_image(data: bytes, content_type: str) -> Path | None:
    """Store preview image *data* as a content-addressed file; return its path.

    The blob handle of ``links.preview_image`` needs a path, but the bytes come
    from a (validated, size-capped) fetch or from the location thumbnail store.
    The newest ``_IMAGE_KEEP`` files are kept.  ``protocols.db.CACHE_DIR`` is
    looked up at call time, like the rest of the cache layout.
    """
    from protocols import db

    digest = hashlib.sha256(data).hexdigest()
    directory = Path(db.CACHE_DIR) / _IMAGE_DIR_NAME
    path = directory / f"{digest}{_IMAGE_EXT.get(content_type, '.img')}"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            path.touch()
            return path
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        files = sorted(
            (p for p in directory.iterdir() if p.is_file() and p.suffix != ".tmp"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in files[_IMAGE_KEEP:]:
            stale.unlink(missing_ok=True)
        return path
    except OSError:
        logger.debug("Unable to store link preview image", exc_info=True)
        return None


async def fetch_external_image(
    url: str,
    max_bytes: int = _MAX_IMAGE_BYTES,
    max_pixels: int | None = None,
) -> tuple[bytes, str] | None:
    """Fetch a public raster image through the SSRF-safe preview session.

    Used by the Gmail image proxy: redirects and DNS answers are validated by
    the same public-only resolver as link previews; SVG and non-image bodies
    are rejected by magic-byte sniffing.  When *max_pixels* is set, images
    whose header declares a larger raster are rejected; unparseable headers
    fail open (the byte cap remains the backstop).
    """
    try:
        async with shared_session() as session:
            _final, _type, body, _charset = await _get(session, url, max_bytes)
    except Exception as exc:  # noqa: BLE001 — proxy is best-effort
        logger.debug("external image failed for %s: %s", url, exc)
        return None
    sniffed = _sniff_raster(body)
    if not sniffed:
        return None
    if max_pixels is not None:
        dimensions = _raster_dimensions(body, sniffed)
        if dimensions is not None and dimensions[0] * dimensions[1] > max_pixels:
            logger.debug("external image over pixel cap: %s", url)
            return None
    return (body, sniffed)


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Walk JPEG segments (skipping APPn/COM payloads) to the first real SOF."""
    index = 2
    size = len(data)
    while index + 1 < size:
        if data[index] != 0xFF:
            return None
        while index < size and data[index] == 0xFF:
            index += 1
        if index >= size:
            return None
        marker = data[index]
        index += 1
        if marker == 0x00:
            continue
        if marker == 0xD9:
            return None
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            continue
        if marker == 0xDA or index + 2 > size:
            return None
        segment_length = int.from_bytes(data[index : index + 2], "big")
        if segment_length < 2 or index + segment_length > size:
            return None
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if segment_length < 7:
                return None
            height = int.from_bytes(data[index + 3 : index + 5], "big")
            width = int.from_bytes(data[index + 5 : index + 7], "big")
            return (width, height)
        index += segment_length
    return None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 20 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    kind = data[12:16]
    chunk_size = int.from_bytes(data[16:20], "little")
    if 20 + chunk_size > len(data):
        return None
    if kind == b"VP8X":
        if chunk_size < 10 or len(data) < 30:
            return None
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return (width, height)
    if kind == b"VP8 ":
        if chunk_size < 10 or len(data) < 30 or data[23:26] != b"\x9d\x01\x2a":
            return None
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return (width, height)
    if kind == b"VP8L":
        if chunk_size < 5 or len(data) < 25 or data[20] != 0x2F:
            return None
        packed = int.from_bytes(data[21:25], "little")
        width = (packed & 0x3FFF) + 1
        height = ((packed >> 14) & 0x3FFF) + 1
        return (width, height)
    return None


def _raster_dimensions(data: bytes, content_type: str) -> tuple[int, int] | None:
    """Best-effort ``(width, height)`` from a raster header.

    Returns ``None`` when the header is truncated or not parseable, so callers
    can fail open instead of rejecting valid images.
    """
    if content_type == "image/png":
        if len(data) < 24 or data[12:16] != b"IHDR":
            return None
        return (
            int.from_bytes(data[16:20], "big"),
            int.from_bytes(data[20:24], "big"),
        )
    if content_type == "image/jpeg":
        return _jpeg_dimensions(data)
    if content_type == "image/gif":
        if len(data) < 10:
            return None
        return (
            int.from_bytes(data[6:8], "little"),
            int.from_bytes(data[8:10], "little"),
        )
    if content_type == "image/webp":
        return _webp_dimensions(data)
    return None


def _sniff_raster(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None
