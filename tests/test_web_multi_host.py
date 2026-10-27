"""The web server listens on every address of a multi-host ``--web-host``."""

from __future__ import annotations

import socket
import urllib.request

import pytest

from protocols import config
from web.server import format_hosts, parse_hosts, start_web_server, stop_web_server


@pytest.mark.parametrize(
    ("value", "hosts"),
    [
        ("127.0.0.1", ["127.0.0.1"]),
        ("127.0.0.1,192.168.1.10", ["127.0.0.1", "192.168.1.10"]),
        (" 127.0.0.1 , ::1 ,127.0.0.1", ["127.0.0.1", "::1"]),
        (["127.0.0.1", "10.0.0.1,10.0.0.2"], ["127.0.0.1", "10.0.0.1", "10.0.0.2"]),
        ("", ["0.0.0.0"]),
    ],
)
def test_parse_hosts(value, hosts):
    assert parse_hosts(value) == hosts


def test_format_hosts_brackets_ipv6():
    assert format_hosts(["127.0.0.1", "::1"], 80) == (
        "http://127.0.0.1:80, http://[::1]:80"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (["127.0.0.1", "192.168.1.10"], "127.0.0.1,192.168.1.10"),
        ("127.0.0.1,192.168.1.10", "127.0.0.1,192.168.1.10"),
        (None, "127.0.0.1"),
    ],
)
def test_config_web_host_accepts_a_list(monkeypatch, value, expected):
    monkeypatch.setattr(config, "_web_config", lambda: {"host": value})
    assert config.web_host() == expected


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _API:
    bus = None

    async def close_loop_resources(self, _timeout: float) -> None:
        return None


def _health(host: str, port: int) -> int:
    with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=5) as resp:
        return resp.status


def test_listens_on_every_host(monkeypatch):
    monkeypatch.setattr("web.uploads.prepare_upload_directory", lambda: None)
    port = _free_port()
    handle = start_web_server(
        _API(), port, "tok", host="127.0.0.1,127.0.0.2", require_auth=True
    )
    try:
        assert handle is not None and handle.status == "up"
        assert _health("127.0.0.1", port) == 200
        assert _health("127.0.0.2", port) == 200
        # Not bound on the wildcard: another loopback address is refused.
        with pytest.raises(OSError):
            _health("127.0.0.3", port)
    finally:
        stop_web_server(handle)


def test_one_unbindable_host_brings_the_web_down(monkeypatch):
    monkeypatch.setattr("web.uploads.prepare_upload_directory", lambda: None)
    port = _free_port()
    with socket.socket() as busy:
        busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        busy.bind(("127.0.0.2", port))
        busy.listen()
        handle = start_web_server(
            _API(), port, "tok", host="127.0.0.1,127.0.0.2", require_auth=True
        )
        try:
            assert handle is not None and handle.status == "down"
        finally:
            stop_web_server(handle)
    # The socket bound for 127.0.0.1 was released.
    with socket.socket() as again:
        again.bind(("127.0.0.1", port))
