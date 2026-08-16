"""Unit tests for temporary download helpers without starting an HTTP server."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend import download as dl


class TestLocalIp:
    def test_uses_server_ip_from_ssh_connection(self, monkeypatch):
        monkeypatch.setenv("SSH_CONNECTION", "10.0.0.2 50000 192.168.1.10 22")
        assert dl.get_local_ip() == "192.168.1.10"

    def test_short_ssh_connection_falls_back_to_socket(self, monkeypatch):
        class _Socket:
            def connect(self, address):
                pass

            def getsockname(self):
                return ("192.168.1.20", 12345)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        monkeypatch.setenv("SSH_CONNECTION", "too short")
        monkeypatch.setattr(dl.socket, "socket", lambda *args: _Socket())
        assert dl.get_local_ip() == "192.168.1.20"

    def test_socket_failure_falls_back_to_loopback(self, monkeypatch):
        monkeypatch.delenv("SSH_CONNECTION", raising=False)

        def _raise(*args, **kwargs):
            raise OSError("unavailable")

        monkeypatch.setattr(dl.socket, "socket", _raise)
        assert dl.get_local_ip() == "127.0.0.1"


class TestDownloadDirectory:
    def test_get_temp_download_dir_creates_and_caches_directory(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(dl, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(dl, "_TEMP_DOWNLOAD_DIR", None)

        result = dl._get_temp_download_dir()

        assert result == tmp_path / "downloads"
        assert result.is_dir()
        assert dl._get_temp_download_dir() is result

    def test_get_temp_download_dir_uses_existing_cache(self, monkeypatch, tmp_path):
        existing = tmp_path / "existing"
        monkeypatch.setattr(dl, "_TEMP_DOWNLOAD_DIR", existing)

        assert dl._get_temp_download_dir() == existing
        assert not existing.exists()

    def test_clean_download_dir_removes_entries_except_keep(
        self, monkeypatch, tmp_path
    ):
        nested = tmp_path / "nested"
        nested.mkdir()
        (nested / "child").write_text("x")
        removable = tmp_path / "remove.txt"
        removable.write_text("x")
        keep = tmp_path / "keep.txt"
        keep.write_text("keep")
        symlink = tmp_path / "link.txt"
        symlink.symlink_to(removable)
        monkeypatch.setattr(dl, "_get_temp_download_dir", lambda: tmp_path)

        dl._clean_download_dir(keep="keep.txt")

        assert keep.exists()
        assert not removable.exists()
        assert not nested.exists()
        assert not symlink.exists()

    def test_clean_download_dir_ignores_oserror(self, monkeypatch, tmp_path):
        child = tmp_path / "cannot-remove.txt"
        child.write_text("x")
        monkeypatch.setattr(dl, "_get_temp_download_dir", lambda: tmp_path)
        monkeypatch.setattr(Path, "unlink", MagicMock(side_effect=OSError("denied")))

        dl._clean_download_dir()

        assert child.exists()


class TestDownloadServer:
    def test_handler_initializes_with_temp_directory(self, monkeypatch, tmp_path):
        init = MagicMock()
        monkeypatch.setattr(dl, "_get_temp_download_dir", lambda: tmp_path)
        monkeypatch.setattr(dl.http.server.SimpleHTTPRequestHandler, "__init__", init)

        dl._DownloadHTTPHandler("request", "address", "server")

        assert init.call_args.kwargs["directory"] == str(tmp_path)

    def test_ensure_server_returns_existing_url(self, monkeypatch):
        monkeypatch.setattr(dl, "_DOWNLOAD_SERVER", object())
        monkeypatch.setattr(dl, "_DOWNLOAD_URL_BASE", "http://existing:10042")

        assert dl._ensure_download_server() == "http://existing:10042"

    def test_ensure_server_starts_mocked_server(self, monkeypatch):
        server = MagicMock()
        thread = MagicMock()
        monkeypatch.setattr(dl, "_DOWNLOAD_SERVER", None)
        monkeypatch.setattr(dl, "_DOWNLOAD_URL_BASE", None)
        monkeypatch.setattr(dl, "get_local_ip", lambda: "1.2.3.4")
        monkeypatch.setattr(
            dl.socketserver, "TCPServer", MagicMock(return_value=server)
        )
        monkeypatch.setattr(dl.threading, "Thread", MagicMock(return_value=thread))

        assert dl._ensure_download_server() == "http://1.2.3.4:10042"
        dl.socketserver.TCPServer.assert_called_once_with(
            ("0.0.0.0", dl.DOWNLOAD_PORT), dl._DownloadHTTPHandler
        )
        dl.threading.Thread.assert_called_once_with(
            target=server.serve_forever, daemon=True
        )
        thread.start.assert_called_once()


class TestServingFiles:
    def test_serve_file_path_symlinks_source(self, monkeypatch, tmp_path):
        source = tmp_path / "source.txt"
        source.write_text("data")
        served = tmp_path / "served"
        served.mkdir()
        clean = MagicMock()
        monkeypatch.setattr(
            dl, "_ensure_download_server", lambda: "http://1.2.3.4:10042"
        )
        monkeypatch.setattr(dl, "_get_temp_download_dir", lambda: served)
        monkeypatch.setattr(dl, "_clean_download_dir", clean)

        result = dl._serve_file_path(source)

        assert result == "http://1.2.3.4:10042/source.txt"
        assert (served / "source.txt").is_symlink()
        assert (served / "source.txt").resolve() == source
        clean.assert_called_once()

    def test_serve_file_path_copies_when_symlink_fails(self, monkeypatch, tmp_path):
        source = tmp_path / "source.txt"
        source.write_text("data")
        served = tmp_path / "served"
        served.mkdir()
        monkeypatch.setattr(dl, "_ensure_download_server", lambda: "http://x")
        monkeypatch.setattr(dl, "_get_temp_download_dir", lambda: served)
        monkeypatch.setattr(dl, "_clean_download_dir", lambda: None)
        monkeypatch.setattr(
            Path, "symlink_to", MagicMock(side_effect=OSError("no link"))
        )

        result = dl._serve_file_path(source)

        assert result == "http://x/source.txt"
        assert (served / "source.txt").read_text() == "data"

    def test_serve_attachment_handles_missing_and_existing_paths(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(dl, "get_attachment_path", lambda attachment_id: None)
        assert dl.serve_attachment_for_download("missing").startswith("ERROR:")

        source = tmp_path / "photo.jpg"
        source.write_text("image")
        monkeypatch.setattr(dl, "get_attachment_path", lambda attachment_id: source)
        monkeypatch.setattr(dl, "_serve_file_path", lambda path: "http://x/photo.jpg")
        assert dl.serve_attachment_for_download("att-1") == "http://x/photo.jpg"

    def test_serve_text_as_file_writes_and_returns_url(self, monkeypatch, tmp_path):
        clean = MagicMock()
        monkeypatch.setattr(dl, "_ensure_download_server", lambda: "http://x")
        monkeypatch.setattr(dl, "_get_temp_download_dir", lambda: tmp_path)
        monkeypatch.setattr(dl, "_clean_download_dir", clean)

        assert dl.serve_text_as_file("hello", "note.txt") == "http://x/note.txt"
        assert (tmp_path / "note.txt").read_text() == "hello"
        clean.assert_called_once()

    def test_serve_text_as_file_returns_error_on_write_failure(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(dl, "_ensure_download_server", lambda: "http://x")
        monkeypatch.setattr(dl, "_get_temp_download_dir", lambda: tmp_path)
        monkeypatch.setattr(dl, "_clean_download_dir", lambda: None)
        monkeypatch.setattr(
            Path, "write_text", MagicMock(side_effect=OSError("denied"))
        )

        assert dl.serve_text_as_file("hello").startswith(
            "ERROR: Cannot write temp file"
        )
