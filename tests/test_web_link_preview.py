from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web as aioweb
from fastapi import FastAPI
from fastapi.testclient import TestClient

import web.link_preview as lp
from web.api import create_api_router

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32

PAGE = """<!doctype html><html><head>
<meta charset="utf-8"><title>Fallback title</title>
<meta property="og:title" content="Caff&egrave; &amp; cornetti">
<meta property="og:description" content="  La   migliore colazione  ">
<meta property="og:site_name" content="Bar Sport">
<meta property="og:image" content="/img/cover.png">
</head><body><meta property="og:title" content="ignored"></body></html>"""


@pytest.fixture(autouse=True)
def _clear_cache():
    lp._CACHE.clear()
    lp._INFLIGHT.clear()
    yield
    lp._CACHE.clear()


def test_parse_preview_prefers_open_graph_and_resolves_image():
    preview = lp.parse_preview(PAGE, "https://example.com/menu")
    assert preview == {
        "url": "https://example.com/menu",
        "title": "Caffè & cornetti",
        "description": "La migliore colazione",
        "site_name": "Bar Sport",
        "image_url": "https://example.com/img/cover.png",
    }


def test_parse_preview_falls_back_to_title_and_host():
    preview = lp.parse_preview(
        "<html><head><title> Hello </title></head></html>", "https://www.example.org/"
    )
    assert preview["title"] == "Hello"
    assert preview["site_name"] == "example.org"
    assert preview["image_url"] is None
    assert lp.parse_preview("<html><body>x</body></html>", "https://a.com") is None


def test_parse_preview_drops_private_image():
    html = '<title>t</title><meta property="og:image" content="http://10.0.0.1/x.png">'
    assert lp.parse_preview(html, "https://a.com")["image_url"] is None


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/",
        "file:///etc/passwd",
        "http://localhost/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data",
        "http://10.1.2.3/",
        "http://192.168.1.1/",
        "http://[::ffff:127.0.0.1]/",
        "http://example.com:8080/",
        "http://user:pw@example.com/",
        "http://router.local/",
        "https://example.com/" + "a" * 3000,
    ],
)
def test_validate_url_blocks(url):
    with pytest.raises(lp.BlockedURLError):
        lp.validate_url(url)


def test_validate_url_allows_public():
    assert lp.validate_url("https://example.com/a?b=c") == "https://example.com/a?b=c"
    assert lp.validate_url("http://8.8.8.8/") == "http://8.8.8.8/"


def test_resolver_rejects_private_answers(monkeypatch):
    async def fake_resolve(host, port=0, family=0):
        return [{"host": "127.0.0.1", "port": port}]

    async def run():
        resolver = lp._public_resolver()
        monkeypatch.setattr(resolver._inner, "resolve", fake_resolve)
        try:
            with pytest.raises(lp.BlockedURLError):
                await resolver.resolve("evil.example", 80)
        finally:
            await resolver.close()

    asyncio.run(run())


def _serve_and_run(monkeypatch, coro_factory):
    """Run *coro_factory(base_url)* against a local aiohttp site."""
    monkeypatch.setattr(lp, "_is_public_ip", lambda _v: True)

    async def page(_request):
        return aioweb.Response(text=PAGE, content_type="text/html")

    async def redirect(_request):
        raise aioweb.HTTPFound("/page")

    async def bounce_private(_request):
        raise aioweb.HTTPFound("http://localhost/admin")

    async def cover(_request):
        return aioweb.Response(body=PNG, content_type="image/png")

    async def main():
        app = aioweb.Application()
        app.router.add_get("/page", page)
        app.router.add_get("/redirect", redirect)
        app.router.add_get("/private", bounce_private)
        app.router.add_get("/img/cover.png", cover)
        runner = aioweb.AppRunner(app)
        await runner.setup()
        site = aioweb.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setattr(lp, "_ALLOWED_PORTS", {port})
        try:
            return await coro_factory(f"http://127.0.0.1:{port}")
        finally:
            await runner.cleanup()

    return asyncio.run(main())


def test_fetch_follows_redirect_and_serves_image(monkeypatch):
    async def scenario(base):
        preview = await lp.get_preview(f"{base}/redirect")
        image = await lp.get_preview_image(f"{base}/redirect")
        return preview, image

    preview, image = _serve_and_run(monkeypatch, scenario)
    assert preview["title"] == "Caffè & cornetti"
    assert preview["url"].endswith("/redirect")
    assert image == (PNG, "image/png")


def test_redirect_to_blocked_host_gives_no_preview(monkeypatch):
    async def scenario(base):
        return await lp.get_preview(f"{base}/private")

    assert _serve_and_run(monkeypatch, scenario) is None


def _client() -> TestClient:
    app = FastAPI()
    app.state.manager = SimpleNamespace()
    app.include_router(create_api_router())
    return TestClient(app)


def test_endpoint_returns_preview_without_image_url(monkeypatch):
    async def fake_get_preview(url):
        return {
            "url": url,
            "title": "T",
            "description": "D",
            "site_name": "S",
            "image_url": "https://cdn.example.com/x.png",
        }

    monkeypatch.setattr(lp, "get_preview", fake_get_preview)
    response = _client().get("/api/link-preview", params={"url": "https://a.com/x"})
    assert response.status_code == 200
    assert response.json() == {
        "url": "https://a.com/x",
        "title": "T",
        "description": "D",
        "site_name": "S",
        "has_image": True,
    }


def test_endpoint_rejects_blocked_and_missing():
    client = _client()
    assert (
        client.get("/api/link-preview", params={"url": "http://127.0.0.1/"}).status_code
        == 400
    )
    assert (
        client.get(
            "/api/link-preview/image", params={"url": "http://10.0.0.1/"}
        ).status_code
        == 400
    )


def test_endpoint_404_when_no_preview(monkeypatch):
    async def none(_url):
        return None

    monkeypatch.setattr(lp, "get_preview", none)
    monkeypatch.setattr(lp, "get_preview_image", none)
    client = _client()
    assert (
        client.get("/api/link-preview", params={"url": "https://a.com"}).status_code
        == 404
    )
    assert (
        client.get(
            "/api/link-preview/image", params={"url": "https://a.com"}
        ).status_code
        == 404
    )
