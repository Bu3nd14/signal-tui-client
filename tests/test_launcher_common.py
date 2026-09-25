"""Coverage for launcher/common.py's network helpers and output functions."""

from __future__ import annotations

import socket

import pytest

from launcher import common


class TestPortListening:
    def test_true_when_something_accepts_connections(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            assert common.port_listening(port) is True
        finally:
            server.close()

    def test_false_when_nothing_listens(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        server.close()  # freed immediately, nothing bound anymore
        assert common.port_listening(port, timeout=0.2) is False

    def test_false_on_os_error(self, monkeypatch):
        class _BoomSocket:
            def __init__(self, *a, **k):
                raise OSError("boom")

        monkeypatch.setattr(common.socket, "socket", _BoomSocket)
        assert common.port_listening(9999) is False


class TestHttpGet:
    def test_returns_status_and_body_on_success(self, monkeypatch):
        class _Resp:
            status = 200

            def read(self):
                return b"hello"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(
            common.urllib.request, "urlopen", lambda req, timeout: _Resp()
        )
        assert common.http_get("http://x") == (200, b"hello")

    def test_returns_none_on_url_error(self, monkeypatch):
        def _raise(req, timeout):
            raise common.urllib.error.URLError("nope")

        monkeypatch.setattr(common.urllib.request, "urlopen", _raise)
        assert common.http_get("http://x") is None

    def test_returns_code_and_body_on_http_error(self, monkeypatch):
        def _raise(req, timeout):
            raise common.urllib.error.HTTPError(
                "http://x", 404, "not found", None, None
            )

        monkeypatch.setattr(common.urllib.request, "urlopen", _raise)
        # HTTPError.read() needs a readable fp; simulate one without a real fp.
        err = common.urllib.error.HTTPError("http://x", 404, "not found", None, None)
        err.fp = None

        def _raise2(req, timeout):
            raise err

        monkeypatch.setattr(common.urllib.request, "urlopen", _raise2)
        monkeypatch.setattr(type(err), "read", lambda self: b"missing", raising=False)
        assert common.http_get("http://x") == (404, b"missing")


class TestHttpPost:
    def test_returns_status_and_body_on_success(self, monkeypatch):
        class _Resp:
            status = 201

            def read(self):
                return b"created"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(
            common.urllib.request, "urlopen", lambda req, timeout: _Resp()
        )
        assert common.http_post("http://x", b"{}") == (201, b"created")

    def test_returns_none_on_timeout(self, monkeypatch):
        def _raise(req, timeout):
            raise TimeoutError()

        monkeypatch.setattr(common.urllib.request, "urlopen", _raise)
        assert common.http_post("http://x", b"{}") is None


class TestCommandExists:
    def test_true_for_python3(self):
        assert common.command_exists("python3") is True

    def test_false_for_nonsense_binary(self):
        assert common.command_exists("this-binary-does-not-exist-xyz") is False


class TestOutputHelpers:
    def test_die_prints_and_exits(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            common.die("boom")
        assert exc_info.value.code == 1
        assert "boom" in capsys.readouterr().err

    def test_info_ok_warn_print_to_stdout(self, capsys):
        common.info("hello")
        common.ok("world")
        common.warn("careful")
        out = capsys.readouterr().out
        assert "hello" in out
        assert "world" in out
        assert "careful" in out
