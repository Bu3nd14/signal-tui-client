"""The ``gmail.*`` operations (``server/ops/gmail.py``), called through the API.

IMAP/SMTP are faked at the ``server.gmail`` function level; the transport
itself is covered by ``test_gmail_api.py`` and the HTTP translation by
``test_web_gmail_contract.py``.
"""

from __future__ import annotations

import imaplib
import os
import smtplib
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from client_harness import api_for

import protocols.db as backend_db
from server import gmail
from server.api import ApiError
from server.models_api import AttachmentIn


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(backend_db, "CACHE_DIR", tmp_path / "cache")
    gmail.clear_inbox_cache()
    yield
    gmail.clear_inbox_cache()


@pytest.fixture
def api():
    return api_for(SimpleNamespace())


def _blobs() -> list[Path]:
    directory = gmail.blob_directory()
    return sorted(directory.iterdir()) if directory.is_dir() else []


def test_operations_are_registered(api):
    names = {op.name for op in api.operations() if op.name.startswith("gmail.")}
    assert names == {
        "gmail.status",
        "gmail.messages.list",
        "gmail.messages.get",
        "gmail.messages.delete",
        "gmail.messages.bulk_delete",
        "gmail.messages.mark_read",
        "gmail.messages.mark_unread",
        "gmail.messages.archive",
        "gmail.messages.send",
        "gmail.messages.send.check",
        "gmail.attachment",
        "gmail.image",
        "gmail.unread",
        "gmail.address_book",
    }
    assert api.operation("gmail.image").is_async
    assert not api.operation("gmail.messages.send").is_async


@pytest.mark.parametrize(
    ("error", "code", "message"),
    [
        (gmail.GmailNotConfigured("x"), "backend_down", "Gmail non configurato"),
        (
            gmail.GmailValidationError("Cartella non valida"),
            "invalid_request",
            "Cartella non valida",
        ),
        (gmail.GmailValidationError("Grande", status_code=413), "too_large", "Grande"),
        (gmail.GmailNotFound("secret"), "not_found", "Risorsa non trovata"),
        (
            gmail.GmailUpstreamError("secret"),
            "backend_error",
            "Errore di comunicazione con Gmail",
        ),
        (
            imaplib.IMAP4.error("secret"),
            "backend_error",
            "Errore di comunicazione con Gmail",
        ),
        (
            smtplib.SMTPException("secret"),
            "backend_error",
            "Errore di comunicazione con Gmail",
        ),
        (OSError("secret"), "backend_error", "Errore di comunicazione con Gmail"),
        (ValueError("secret"), "backend_error", "Errore Gmail"),
    ],
)
def test_error_mapping(api, monkeypatch, error, code, message):
    def boom(*args, **kwargs):
        raise error

    monkeypatch.setattr(gmail, "fetch_message", boom)
    with pytest.raises(ApiError) as exc:
        api.call("gmail.messages.get", {"uid": 1})
    assert (exc.value.code, exc.value.message) == (code, message)


def test_requests_are_validated(api):
    with pytest.raises(ApiError) as exc:
        api.call("gmail.messages.get", {"uid": "abc"})
    assert exc.value.code == "invalid_request"


def test_attachment_returns_a_private_temporary_blob(api, monkeypatch):
    monkeypatch.setattr(
        gmail,
        "fetch_attachment",
        lambda uid, index, folder=None: gmail.AttachmentDownload(
            filename="a.pdf", content_type="application/pdf", data=b"%PDF body"
        ),
    )
    blob = api.call_json("gmail.attachment", {"uid": 1, "index": 0})
    path = Path(blob["path"])
    assert path.parent == gmail.blob_directory()
    assert path.read_bytes() == b"%PDF body"
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(path.parent).st_mode & 0o777 == 0o700
    assert (blob["filename"], blob["content_type"], blob["size"]) == (
        "a.pdf",
        "application/pdf",
        9,
    )


def test_attachment_download_limit_is_kept(api, monkeypatch):
    def too_big(*args, **kwargs):
        raise gmail.GmailValidationError("Allegato troppo grande", status_code=413)

    monkeypatch.setattr(gmail, "fetch_attachment", too_big)
    with pytest.raises(ApiError) as exc:
        api.call("gmail.attachment", {"uid": 1, "index": 0})
    assert (exc.value.code, exc.value.message) == (
        "too_large",
        "Allegato troppo grande",
    )
    assert _blobs() == []


def test_stale_blobs_are_pruned_and_close_resources_wipes_them(api):
    old = gmail.write_blob(b"old")
    stale = time.time() - gmail.BLOB_MAX_AGE_S - 5
    os.utime(old, (stale, stale))
    fresh = gmail.write_blob(b"fresh")
    assert not old.exists()
    assert fresh.exists()
    gmail.close_resources()
    assert _blobs() == []


# -- image proxy ----------------------------------------------------------------


def _token(url: str) -> str:
    return gmail.sign_image_url(url).split("token=")[1].split("&")[0]


def test_image_fetches_a_signed_url_into_a_blob(api, monkeypatch):
    seen: list[tuple[str, int]] = []
    pixels: list[int | None] = []

    async def fake_fetch(url, max_bytes, max_pixels=None):
        seen.append((url, max_bytes))
        pixels.append(max_pixels)
        return b"\x89PNG\r\n\x1a\nxx", "image/png"

    monkeypatch.setattr("server.link_preview.fetch_external_image", fake_fetch)
    blob = api.call_json("gmail.image", {"token": _token("https://img.example/a.png")})
    assert Path(blob["path"]).read_bytes() == b"\x89PNG\r\n\x1a\nxx"
    assert (blob["content_type"], blob["size"]) == ("image/png", 10)
    assert seen == [("https://img.example/a.png", 8 * 1024 * 1024)]
    assert pixels == [25_000_000]


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-token",
        "x" * 9000,
        "aHR0cHM6Ly9ldmlsLmV4YW1wbGUv.0123456789abcdef0123456789abcdef",
    ],
)
def test_image_unsigned_or_forged_token_is_never_fetched(api, monkeypatch, token):
    async def fail(url, max_bytes, max_pixels=None):
        raise AssertionError("fetched an unsigned URL")

    monkeypatch.setattr("server.link_preview.fetch_external_image", fail)
    with pytest.raises(ApiError) as exc:
        api.call("gmail.image", {"token": token})
    assert (exc.value.code, exc.value.message) == ("not_found", "Immagine non trovata")
    assert _blobs() == []


def test_image_signature_does_not_transfer_to_another_url(api, monkeypatch):
    async def fail(url, max_bytes, max_pixels=None):
        raise AssertionError("fetched an unsigned URL")

    monkeypatch.setattr("server.link_preview.fetch_external_image", fail)
    _, _, signature = _token("https://img.example/a.png").partition(".")
    forged_payload = _token("http://169.254.169.254/latest").partition(".")[0]
    with pytest.raises(ApiError) as exc:
        api.call("gmail.image", {"token": f"{forged_payload}.{signature}"})
    assert exc.value.code == "not_found"


def test_image_fetch_failure_is_a_backend_error(api, monkeypatch):
    async def none(url, max_bytes, max_pixels=None):
        return None

    monkeypatch.setattr("server.link_preview.fetch_external_image", none)
    with pytest.raises(ApiError) as exc:
        api.call("gmail.image", {"token": _token("https://img.example/a.png")})
    assert (exc.value.code, exc.value.message) == (
        "backend_error",
        "Immagine non disponibile",
    )


# -- send -------------------------------------------------------------------


@pytest.fixture
def sent(monkeypatch):
    calls: list[dict[str, Any]] = []

    def fake_send(**kwargs):
        kwargs["attachments"] = list(kwargs["attachments"])
        calls.append(kwargs)
        return "<id@example.com>"

    monkeypatch.setattr(gmail, "send_email", fake_send)
    return calls


def test_send_reads_the_stored_files(api, sent, tmp_path):
    one = tmp_path / "x1"
    one.write_bytes(b"one")
    two = tmp_path / "x2"
    two.write_bytes(b"two")
    result = api.call_json(
        "gmail.messages.send",
        {
            "to": "a@example.com",
            "text": "hi",
            "subject": "S",
            "reply_to_uid": 9,
            "reply_to_folder": "",
            "attachments": [
                AttachmentIn(path=str(one), filename="a.txt"),
                AttachmentIn(path=str(two)),
            ],
        },
    )
    assert result == {"ok": True, "message_id": "<id@example.com>"}
    assert sent == [
        {
            "to": "a@example.com",
            "cc": "",
            "bcc": "",
            "subject": "S",
            "text": "hi",
            "reply_to_uid": 9,
            "reply_to_folder": None,
            "forward_uid": None,
            "forward_folder": None,
            "attachments": [("a.txt", b"one"), ("attachment", b"two")],
        }
    ]


def test_send_check_is_side_effect_free(api, sent):
    assert api.call_json("gmail.messages.send.check", {"attachment_count": 10}) == {
        "ok": True
    }
    with pytest.raises(ApiError) as many:
        api.call("gmail.messages.send.check", {"attachment_count": 11})
    assert (many.value.code, many.value.message) == (
        "invalid_request",
        "Troppi allegati",
    )
    with pytest.raises(ApiError) as slot:
        api.call(
            "gmail.messages.send.check", {"attachment_count": 1, "slots_ok": False}
        )
    assert (slot.value.code, slot.value.message) == (
        "invalid_request",
        "Richiesta non valida",
    )
    assert sent == []


def test_send_sizes_are_checked_in_order_before_reading(
    api, sent, tmp_path, monkeypatch
):
    monkeypatch.setattr(gmail, "_MAX_ATTACHMENT_BYTES", 4)
    monkeypatch.setattr(gmail, "_MAX_TOTAL_ATTACHMENT_BYTES", 6)
    files = []
    for name, data in (("a", b"123"), ("b", b"1234"), ("c", b"12345")):
        path = tmp_path / name
        path.write_bytes(data)
        files.append(AttachmentIn(path=str(path), filename=name))

    def attempt(*slots):
        with pytest.raises(ApiError) as exc:
            api.call(
                "gmail.messages.send",
                {"to": "a@example.com", "attachments": list(slots)},
            )
        return exc.value.code, exc.value.message

    assert attempt(files[0], files[1]) == ("too_large", "Allegati troppo grandi")
    assert attempt(files[2], files[0]) == ("too_large", "Allegato troppo grande")
    assert attempt(files[0], AttachmentIn(rejected="too_large"), files[2]) == (
        "too_large",
        "Allegato troppo grande",
    )
    assert sent == []


def test_send_rejects_unreadable_or_missing_slots(api, sent, tmp_path):
    for slots in (
        [{"path": str(tmp_path / "nope")}],
        [{"path": ""}],
    ):
        with pytest.raises(ApiError) as exc:
            api.call(
                "gmail.messages.send", {"to": "a@example.com", "attachments": slots}
            )
        assert (exc.value.code, exc.value.message) == (
            "invalid_request",
            "Richiesta non valida",
        )
    assert sent == []
