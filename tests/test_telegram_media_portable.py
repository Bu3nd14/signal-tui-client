from pathlib import Path
from unittest.mock import MagicMock

from backends.telegram import TelegramBackend


def _backend() -> TelegramBackend:
    backend = TelegramBackend()
    backend._api_id = 123
    backend._api_hash = "hash"
    return backend


def test_resolves_foreign_absolute_path_by_basename(monkeypatch, tmp_path: Path):
    media_dir = tmp_path / "telegram-media"
    media_dir.mkdir()
    expected = media_dir / "123-456-sent.png"
    expected.write_bytes(b"image")
    monkeypatch.setattr("backends.telegram._media_dir", lambda: media_dir)

    attachment_id = (
        "/home/altrouser/.local/share/signal-tui-client/telegram-media/"
        "123-456-sent.png"
    )

    assert _backend().get_attachment_path(attachment_id) == expected


def test_does_not_resolve_path_traversal_by_basename(monkeypatch, tmp_path: Path):
    media_dir = tmp_path / "telegram-media"
    media_dir.mkdir()
    (media_dir / "passwd").write_text("not system passwd")
    monkeypatch.setattr("backends.telegram._media_dir", lambda: media_dir)

    assert _backend().get_attachment_path("../../etc/passwd") is None


def test_tgref_still_uses_lazy_download(monkeypatch, tmp_path: Path):
    media_dir = tmp_path / "telegram-media"
    media_dir.mkdir()
    monkeypatch.setattr("backends.telegram._media_dir", lambda: media_dir)
    backend = _backend()
    lazy_download = MagicMock(return_value=tmp_path / "downloaded.png")
    monkeypatch.setattr(backend, "_download_media_by_ref", lazy_download)

    assert backend.get_attachment_path("tgref:42:99") == tmp_path / "downloaded.png"
    lazy_download.assert_called_once_with(42, 99)
