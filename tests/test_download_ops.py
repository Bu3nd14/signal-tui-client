"""``download.serve_text`` / ``download.serve_attachment``: the core's download server."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from client_harness import client_for

import protocols.download as dl
from server.client import ApiError


@pytest.fixture
def served(tmp_path, monkeypatch):
    """Real file handling in a tmp dir; the HTTP server itself is not started."""
    monkeypatch.setattr(dl, "_TEMP_DOWNLOAD_DIR", tmp_path / "dl")
    (tmp_path / "dl").mkdir()
    monkeypatch.setattr(dl, "_ensure_download_server", lambda: "http://1.2.3.4:10042")
    return tmp_path / "dl"


class TestServeText:
    def test_url_and_file(self, served):
        out = client_for(MagicMock()).call(
            "download.serve_text", {"text": "héllo", "filename": "signal-message-5.txt"}
        )
        assert out == {"url": "http://1.2.3.4:10042/signal-message-5.txt"}
        assert (served / "signal-message-5.txt").read_text(encoding="utf-8") == "héllo"

    def test_default_filename(self, served):
        out = client_for(MagicMock()).call("download.serve_text", {"text": "x"})
        assert out["url"].endswith("/message.txt")

    def test_serves_one_file_at_a_time(self, served):
        client = client_for(MagicMock())
        client.call("download.serve_text", {"text": "a", "filename": "a.txt"})
        client.call("download.serve_text", {"text": "b", "filename": "b.txt"})
        assert [p.name for p in served.iterdir()] == ["b.txt"]

    @pytest.mark.parametrize(
        "name", ["", ".", "..", "../x.txt", "a/b.txt", "/etc/x", "x\0y"]
    )
    def test_rejects_names_that_escape_the_download_dir(self, served, name):
        with pytest.raises(ApiError) as exc:
            client_for(MagicMock()).call(
                "download.serve_text", {"text": "x", "filename": name}
            )
        assert exc.value.code == "invalid_request"
        assert list(served.iterdir()) == []

    def test_write_error_is_a_backend_error(self, served, monkeypatch):
        monkeypatch.setattr(dl, "_clean_download_dir", MagicMock())
        monkeypatch.setattr(
            "pathlib.Path.write_text", MagicMock(side_effect=OSError("disk full"))
        )
        with pytest.raises(ApiError) as exc:
            client_for(MagicMock()).call("download.serve_text", {"text": "x"})
        assert exc.value.code == "backend_error"
        assert exc.value.message == "ERROR: Cannot write temp file: disk full"


@pytest.mark.usefixtures("open_media_root")
class TestServeAttachment:
    def _client(self, path):
        manager = MagicMock()
        manager.get_attachment_path.return_value = path
        return client_for(manager)

    def test_symlinks_into_download_dir(self, served, tmp_path):
        att = tmp_path / "photo.jpg"
        att.write_text("image")
        out = self._client(att).call(
            "download.serve_attachment", {"protocol": "signal", "attachment_id": "a"}
        )
        assert out == {"url": "http://1.2.3.4:10042/photo.jpg"}
        assert (served / "photo.jpg").resolve() == att.resolve()

    def test_missing_attachment_keeps_the_error_text(self, served):
        with pytest.raises(ApiError) as exc:
            self._client(None).call(
                "download.serve_attachment",
                {"protocol": "signal", "attachment_id": "x" * 100},
            )
        assert exc.value.code == "not_found"
        assert exc.value.message == f"ERROR: Attachment file not found (id={'x' * 80})"

    def test_unknown_protocol_is_not_found(self, served, tmp_path):
        att = tmp_path / "photo.jpg"
        att.write_text("image")
        with pytest.raises(ApiError) as exc:
            self._client(att).call(
                "download.serve_attachment",
                {"protocol": "irc", "attachment_id": "a"},
            )
        assert exc.value.code == "not_found"

    def test_directory_is_not_served(self, served, tmp_path):
        with pytest.raises(ApiError) as exc:
            self._client(tmp_path).call(
                "download.serve_attachment",
                {"protocol": "signal", "attachment_id": "a"},
            )
        assert exc.value.code == "not_found"

    def test_server_start_failure_is_a_backend_error(self, tmp_path, monkeypatch):
        att = tmp_path / "photo.jpg"
        att.write_text("image")
        monkeypatch.setattr(dl, "_TEMP_DOWNLOAD_DIR", tmp_path)
        monkeypatch.setattr(
            dl,
            "_ensure_download_server",
            MagicMock(side_effect=OSError("address in use")),
        )
        with pytest.raises(ApiError) as exc:
            self._client(att).call(
                "download.serve_attachment",
                {"protocol": "signal", "attachment_id": "a"},
            )
        assert exc.value.code == "backend_error"
        assert "address in use" in exc.value.message

    def test_confined_to_media_root(self, served, tmp_path, monkeypatch):
        outside = tmp_path / "secret.txt"
        outside.write_text("s")
        monkeypatch.setattr(
            "server.media._allowed_media_root", lambda _m, _p: tmp_path / "root"
        )
        with pytest.raises(ApiError) as exc:
            self._client(outside).call(
                "download.serve_attachment",
                {"protocol": "signal", "attachment_id": "a"},
            )
        assert exc.value.code == "not_found"


def test_download_server_constants_unchanged():
    assert dl.DOWNLOAD_PORT == 10042
