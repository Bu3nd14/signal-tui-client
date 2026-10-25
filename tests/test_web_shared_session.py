"""P3a — shared HTTP session: lifecycle, isolation, cookies and SSRF.

The shared session is module-level state in :mod:`server.link_preview` and is
bound to the event loop that created it.  Coroutine tests request the
``_closed_session`` fixture, which closes the session *inside that same loop*;
the few sync tests that use explicit ``asyncio.run`` blocks own their cleanup.
"""

from __future__ import annotations

import asyncio
import sys
import types
from contextlib import asynccontextmanager
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web as aioweb
from client_harness import api_for

import server.link_preview as lp
from web import gmail

PAGE = "<html><head><title>t</title></head><body>x</body></html>"


def _reset_state() -> None:
    lp._CACHE.clear()
    lp._INFLIGHT.clear()
    lp._shared_session = None
    lp._shared_resolver = None
    lp._shared_session_loop = None
    lp._SHARED_SESSION_ENABLED = True
    lp._closing = False


@pytest.fixture(autouse=True)
def _isolate_link_preview_state():
    _reset_state()
    yield
    lp._CACHE.clear()
    lp._INFLIGHT.clear()


@pytest.fixture
async def _closed_session():
    """Close the shared session from the same loop that created it."""
    try:
        yield
    finally:
        await lp.close_shared_session()
        _reset_state()


def _spy_make_session(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, object]]:
    created: list[tuple[object, object]] = []
    original = lp._make_session

    def spy():
        session, resolver = original()
        created.append((session, resolver))
        return session, resolver

    monkeypatch.setattr(lp, "_make_session", spy)
    return created


class _TrackingResolver:
    """Resolver double that records ``close()`` without touching the network."""

    def __init__(self) -> None:
        self.close_calls = 0
        self.closed = False

    async def resolve(self, *_args, **_kwargs):
        raise AssertionError("test must not perform DNS resolution")

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True


def _patch_tracking_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> list[_TrackingResolver]:
    resolvers: list[_TrackingResolver] = []

    def factory() -> _TrackingResolver:
        resolver = _TrackingResolver()
        resolvers.append(resolver)
        return resolver

    monkeypatch.setattr(lp, "_public_resolver", factory)
    return resolvers


@asynccontextmanager
async def _local_site(monkeypatch: pytest.MonkeyPatch, routes):
    """Serve *routes* on 127.0.0.1 and allow that non-standard port for the test."""
    monkeypatch.setattr(lp, "_is_public_ip", lambda _value: True)
    app = aioweb.Application()
    for method, path, handler in routes:
        app.router.add_route(method, path, handler)
    runner = aioweb.AppRunner(app)
    await runner.setup()
    site = aioweb.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(lp, "_ALLOWED_PORTS", {port})
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


# ── Session reuse ────────────────────────────────────────────────────────────


async def test_two_fetches_in_one_loop_share_one_client_session(
    _closed_session, monkeypatch
):
    created = _spy_make_session(monkeypatch)

    async def page(_request):
        return aioweb.Response(text=PAGE, content_type="text/html")

    async with _local_site(
        monkeypatch, [("GET", "/one", page), ("GET", "/two", page)]
    ) as base:
        first = await lp.get_preview(f"{base}/one")
        second = await lp.get_preview(f"{base}/two")

    assert first is not None and second is not None
    assert len(created) == 1
    assert lp._shared_session is created[0][0]
    assert not created[0][0].closed


# ── Cookies ──────────────────────────────────────────────────────────────────


async def test_shared_session_does_not_persist_cookies(_closed_session, monkeypatch):
    seen: list[str | None] = []

    async def set_cookie(request):
        seen.append(request.headers.get("Cookie"))
        response = aioweb.Response(text="<title>one</title>", content_type="text/html")
        response.headers["Set-Cookie"] = "sid=secret; Path=/"
        return response

    async def read_cookie(request):
        seen.append(request.headers.get("Cookie"))
        return aioweb.Response(text="<title>two</title>", content_type="text/html")

    async with _local_site(
        monkeypatch,
        [
            ("GET", "/set-cookie", set_cookie),
            ("GET", "/read-cookie", read_cookie),
        ],
    ) as base:
        assert await lp.get_preview(f"{base}/set-cookie") is not None
        assert await lp.get_preview(f"{base}/read-cookie") is not None

    assert seen == [None, None]
    assert isinstance(lp._shared_session.cookie_jar, aiohttp.DummyCookieJar)


# ── SSRF guard is unchanged ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "http://localhost/",
        "http://example.com:8080/",
        "http://example.com:21/",
        "http://user:pw@example.com/",
        "http://10.1.2.3/",
    ],
)
async def test_ssrf_guard_blocks_before_any_request(url):
    class _ExplodingSession:
        async def get(self, *_args, **_kwargs):
            raise AssertionError("network request attempted")

    with pytest.raises(lp.BlockedURLError):
        await lp._get(_ExplodingSession(), url, 1024)


# ── Loop isolation ───────────────────────────────────────────────────────────


def test_two_asyncio_run_calls_create_distinct_sessions(monkeypatch):
    created = _spy_make_session(monkeypatch)

    async def use_and_close():
        try:
            async with lp.shared_session() as session:
                return session
        finally:
            await lp.close_shared_session()

    first = asyncio.run(use_and_close())
    second = asyncio.run(use_and_close())

    assert first is not second
    assert len(created) == 2
    assert first.closed
    assert second.closed


def test_loop_isolation_creates_per_loop_sessions_and_skips_cross_loop_close(
    monkeypatch,
):
    created = _spy_make_session(monkeypatch)
    loop1 = asyncio.new_event_loop()
    loop2 = asyncio.new_event_loop()

    async def create():
        async with lp.shared_session() as session:
            return session, lp._shared_resolver

    try:
        first, first_resolver = loop1.run_until_complete(create())
        second, second_resolver = loop2.run_until_complete(create())

        assert first is not second
        assert len(created) == 2
        # Replacing the session of another loop must not close it.
        assert not first.closed

        # A close issued on the wrong loop is skipped: the owner keeps its session.
        loop1.run_until_complete(lp.close_shared_session())
        assert not second.closed

        # The next creation on loop1 is a fresh session, not the old one.
        third, _third_resolver = loop1.run_until_complete(create())
        assert third is not first
        loop1.run_until_complete(lp.close_shared_session())
        assert third.closed

        # Close every session/resolver on its own loop.
        loop1.run_until_complete(first.close())
        loop1.run_until_complete(first_resolver.close())
        loop2.run_until_complete(second.close())
        loop2.run_until_complete(second_resolver.close())
    finally:
        lp._shared_session = lp._shared_resolver = lp._shared_session_loop = None
        loop1.close()
        loop2.close()


# ── Deterministic close ──────────────────────────────────────────────────────


async def test_close_shared_session_closes_session_connector_and_resolver(
    _closed_session, monkeypatch
):
    resolvers = _patch_tracking_resolver(monkeypatch)

    async with lp.shared_session() as session:
        connector = session.connector
        assert connector is not None
        assert connector.limit == 8

    await lp.close_shared_session()

    assert session.closed
    assert connector.closed
    assert resolvers[0].close_calls == 1
    assert lp._shared_session is None

    # Idempotent: nothing is closed twice and no exception is raised.
    await lp.close_shared_session()
    assert resolvers[0].close_calls == 1

    # After close the next use lazily creates a fresh session.
    async with lp.shared_session() as replacement:
        assert replacement is not session
    await lp.close_shared_session()
    assert replacement.closed
    assert len(resolvers) == 2
    assert resolvers[1].close_calls == 1


async def test_close_shared_session_handles_already_closed_session(
    _closed_session, monkeypatch
):
    resolvers = _patch_tracking_resolver(monkeypatch)

    async with lp.shared_session() as session:
        pass
    await session.close()

    await lp.close_shared_session()

    assert session.closed
    assert resolvers[0].close_calls == 1


async def test_close_shared_session_closes_resolver_when_session_close_raises(
    _closed_session, monkeypatch
):
    resolver = _TrackingResolver()

    class _BrokenSession:
        closed = False

        async def close(self):
            raise RuntimeError("close failed")

    lp._shared_session = _BrokenSession()
    lp._shared_resolver = resolver
    lp._shared_session_loop = asyncio.get_running_loop()

    with pytest.raises(RuntimeError, match="close failed"):
        await lp.close_shared_session()

    assert resolver.close_calls == 1
    assert lp._shared_session is None
    assert lp._closing is False


# ── Rollback flag: transient sessions ────────────────────────────────────────


async def test_disabled_shared_session_uses_transient_closed_in_finally(
    _closed_session, monkeypatch
):
    monkeypatch.setattr(lp, "_SHARED_SESSION_ENABLED", False)
    resolvers = _patch_tracking_resolver(monkeypatch)

    async with lp.shared_session() as session:
        assert lp._shared_session is None

    assert session.closed
    assert resolvers[0].close_calls == 1
    assert lp._shared_session is None


async def test_disabled_shared_session_closes_transient_on_error(
    _closed_session, monkeypatch
):
    monkeypatch.setattr(lp, "_SHARED_SESSION_ENABLED", False)
    resolvers = _patch_tracking_resolver(monkeypatch)
    session = None

    with pytest.raises(RuntimeError, match="boom"):
        async with lp.shared_session() as session:
            raise RuntimeError("boom")

    assert session is not None
    assert session.closed
    assert resolvers[0].close_calls == 1


async def test_disabled_shared_session_during_shutdown_creates_nothing(
    _closed_session, monkeypatch
):
    monkeypatch.setattr(lp, "_SHARED_SESSION_ENABLED", False)
    monkeypatch.setattr(lp, "_closing", True)
    created = _spy_make_session(monkeypatch)

    with pytest.raises(RuntimeError, match="closing"):
        async with lp.shared_session():
            pass

    assert created == []


# ── Concurrency ──────────────────────────────────────────────────────────────


async def test_concurrent_fetches_share_session_and_respect_connector_limit(
    _closed_session, monkeypatch
):
    created = _spy_make_session(monkeypatch)
    active = 0
    max_active = 0

    async def slow(request):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.2)
            return aioweb.Response(
                text=f"<title>{request.path}</title>", content_type="text/html"
            )
        finally:
            active -= 1

    async with _local_site(monkeypatch, [("GET", "/{name}", slow)]) as base:
        results = await asyncio.gather(
            *(lp.get_preview(f"{base}/p{index}") for index in range(16))
        )

    assert all(result is not None for result in results)
    assert len(created) == 1
    assert created[0][0].connector.limit == 8
    assert max_active == 8


# ── Server shutdown wiring ───────────────────────────────────────────────────


def test_web_server_shutdown_closes_session_and_imap_pool_on_same_loop(monkeypatch):
    import web.server as server_mod

    calls: list[tuple[str, asyncio.AbstractEventLoop, bool, bool]] = []

    async def fake_close_shared_session() -> None:
        loop = asyncio.get_running_loop()
        calls.append(("shared", loop, loop.is_running(), loop.is_closed()))

    def fake_close_imap_pool() -> None:
        loop = asyncio.get_event_loop()
        calls.append(("imap", loop, loop.is_running(), loop.is_closed()))

    class _FakeConfig:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class _FakeServer:
        def __init__(self, config):
            self.config = config
            self.started = False
            self.should_exit = False

        async def serve(self) -> None:
            self.started = True
            await asyncio.sleep(0.05)

    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.Config = _FakeConfig
    fake_uvicorn.Server = _FakeServer

    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setattr(lp, "close_shared_session", fake_close_shared_session)
    monkeypatch.setattr(gmail, "close_imap_pool", fake_close_imap_pool)

    try:
        handle = server_mod.start_web_server(
            api_for(SimpleNamespace()), require_auth=False
        )
        assert handle is not None
        handle.thread.join(2)
        assert not handle.thread.is_alive()
    finally:
        server_mod._active_server = None

    assert [call[0] for call in calls] == ["shared", "imap"]
    shared_loop = calls[0][1]
    imap_loop = calls[1][1]
    assert shared_loop is imap_loop
    # close_shared_session runs inside run_until_complete; close_imap_pool runs
    # synchronously afterwards, both before loop.close().
    assert calls[0][2] is True
    assert calls[1][2] is False
    assert calls[0][3] is False
    assert calls[1][3] is False
