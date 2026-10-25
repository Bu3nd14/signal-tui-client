"""Contract: the ``/api/gmail/*`` routes == the ``gmail.*`` operations.

Routes only translate: success bodies equal ``ChatAPI.call_json`` and every
failure has the status and ``detail`` of the matching ``ApiError`` (via
``web.ops.HTTP_STATUS``).  IMAP/SMTP are faked at the ``server.gmail``
function level (the transport itself is covered by ``test_gmail_api.py``).
"""

from __future__ import annotations

import imaplib
import os
import smtplib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from client_harness import api_for
from fastapi import FastAPI
from fastapi.testclient import TestClient

import protocols.db as backend_db
from server import gmail
from server.api import ApiError
from server.models_api import AttachmentIn
from server.uploads import upload_directory
from web.gmail_api import create_gmail_image_router, create_gmail_router
from web.ops import HTTP_STATUS

_PNG = b"\x89PNG\r\n\x1a\nfake-png"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(backend_db, "CACHE_DIR", tmp_path / "cache")
    gmail.clear_inbox_cache()
    yield
    gmail.clear_inbox_cache()


@pytest.fixture
def api():
    return api_for(SimpleNamespace())


@pytest.fixture
def client(api):
    app = FastAPI()
    app.state.api = api
    app.include_router(create_gmail_router())
    app.include_router(create_gmail_image_router())
    return TestClient(app)


def _leftover_uploads() -> list[Path]:
    directory = upload_directory()
    return sorted(directory.iterdir()) if directory.is_dir() else []


def _leftover_blobs() -> list[Path]:
    directory = gmail.blob_directory()
    return sorted(directory.iterdir()) if directory.is_dir() else []


def _same_error(response, exc: pytest.ExceptionInfo[ApiError]) -> None:
    assert response.status_code == HTTP_STATUS[exc.value.code]
    assert response.json() == {"detail": exc.value.message}


# -- JSON operations ----------------------------------------------------------


def test_status_matches_operation(client, api):
    response = client.get("/api/gmail/status")
    assert response.status_code == 200
    assert response.json() == api.call_json("gmail.status")
    assert response.json()["limits"]["max_attachments"] == 10


def test_list_matches_operation_and_passes_the_query(client, api, monkeypatch):
    calls: list[dict[str, Any]] = []

    def fake(**kwargs):
        calls.append(kwargs)
        return {"folder": "INBOX", "messages": [{"uid": 7}], "total": 1}

    monkeypatch.setattr(gmail, "fetch_message_list", fake)
    response = client.get(
        "/api/gmail/messages",
        params={"folder": "Work", "offset": 5, "limit": 10, "refresh": "true"},
    )
    expected = api.call_json(
        "gmail.messages.list",
        {"folder": "Work", "offset": 5, "limit": 10, "refresh": True},
    )
    assert response.status_code == 200
    assert response.json() == expected
    assert (
        calls[0]
        == calls[1]
        == {
            "folder": "Work",
            "offset": 5,
            "limit": 10,
            "refresh": True,
        }
    )
    client.get("/api/gmail/messages")
    assert calls[-1] == {"folder": None, "offset": 0, "limit": 50, "refresh": False}


def test_get_delete_mark_read_match_operations(client, api, monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(
        gmail,
        "fetch_message",
        lambda uid, folder=None: seen.append(("get", uid, folder)) or {"uid": uid},
    )
    monkeypatch.setattr(
        gmail,
        "delete_message",
        lambda uid, folder=None: seen.append(("del", uid, folder)),
    )
    monkeypatch.setattr(
        gmail, "mark_read", lambda uid, folder=None: seen.append(("read", uid, folder))
    )
    got = client.get("/api/gmail/messages/5", params={"folder": "X"})
    deleted = client.delete("/api/gmail/messages/5")
    read = client.post("/api/gmail/messages/5/read")
    assert got.json() == api.call_json("gmail.messages.get", {"uid": 5, "folder": "X"})
    assert (
        deleted.json()
        == api.call_json("gmail.messages.delete", {"uid": 5})
        == {
            "ok": True,
            "trashed": True,
        }
    )
    assert (
        read.json()
        == api.call_json("gmail.messages.mark_read", {"uid": 5})
        == {"ok": True}
    )
    assert seen[:3] == [("get", 5, "X"), ("del", 5, None), ("read", 5, None)]


def test_bulk_delete_matches_operation(client, api, monkeypatch):
    calls: list[tuple] = []

    def fake(uids, folder=None):
        calls.append((uids, folder))
        return {"ok": True, "trashed": [1, 2], "missing": []}

    monkeypatch.setattr(gmail, "delete_messages", fake)
    response = client.post(
        "/api/gmail/messages/bulk-delete", json={"uids": [1, 2], "folder": "INBOX"}
    )
    assert response.json() == api.call_json(
        "gmail.messages.bulk_delete", {"uids": [1, 2], "folder": "INBOX"}
    )
    assert calls == [([1, 2], "INBOX")] * 2


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"uids": [1], "folder": 3}, "Cartella non valida"),
        ([1, 2], "Richiesta non valida"),
    ],
)
def test_bulk_delete_request_errors(client, body, detail):
    response = client.post("/api/gmail/messages/bulk-delete", json=body)
    assert (response.status_code, response.json()) == (400, {"detail": detail})


def test_bulk_delete_malformed_json(client):
    response = client.post(
        "/api/gmail/messages/bulk-delete",
        content=b"{nope",
        headers={"content-type": "application/json"},
    )
    assert (response.status_code, response.json()) == (
        400,
        {"detail": "Richiesta non valida"},
    )


@pytest.mark.parametrize(
    ("error", "code", "status", "detail"),
    [
        (gmail.GmailNotConfigured("x"), "backend_down", 503, "Gmail non configurato"),
        (
            gmail.GmailValidationError("Cartella non valida"),
            "invalid_request",
            400,
            "Cartella non valida",
        ),
        (
            gmail.GmailValidationError("Troppo", status_code=413),
            "too_large",
            413,
            "Troppo",
        ),
        (gmail.GmailNotFound("secret"), "not_found", 404, "Risorsa non trovata"),
        (
            gmail.GmailUpstreamError("secret"),
            "backend_error",
            502,
            "Errore di comunicazione con Gmail",
        ),
        (
            imaplib.IMAP4.error("secret"),
            "backend_error",
            502,
            "Errore di comunicazione con Gmail",
        ),
        (
            smtplib.SMTPException("secret"),
            "backend_error",
            502,
            "Errore di comunicazione con Gmail",
        ),
        (
            TimeoutError("secret"),
            "backend_error",
            502,
            "Errore di comunicazione con Gmail",
        ),
        (RuntimeError("secret"), "backend_error", 502, "Errore Gmail"),
    ],
)
def test_error_mapping_matches_operation(
    client, api, monkeypatch, error, code, status, detail
):
    def boom(*args, **kwargs):
        raise error

    monkeypatch.setattr(gmail, "fetch_message", boom)
    response = client.get("/api/gmail/messages/1")
    with pytest.raises(ApiError) as exc:
        api.call("gmail.messages.get", {"uid": 1})
    assert (exc.value.code, exc.value.message) == (code, detail)
    assert (response.status_code, response.json()) == (status, {"detail": detail})
    _same_error(response, exc)
    assert "secret" not in response.text


# -- attachments (blobs) --------------------------------------------------------


def _download(monkeypatch, data=b"%PDF-1.4 body", name='città "x".pdf'):
    monkeypatch.setattr(
        gmail,
        "fetch_attachment",
        lambda uid, index, folder=None: gmail.AttachmentDownload(
            filename=name, content_type="application/pdf", data=data
        ),
    )


def test_attachment_operation_returns_a_private_temporary_blob(api, monkeypatch):
    _download(monkeypatch)
    blob = api.call_json("gmail.attachment", {"uid": 1, "index": 0})
    path = Path(blob["path"])
    assert path.parent == gmail.blob_directory()
    assert path.read_bytes() == b"%PDF-1.4 body"
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(path.parent).st_mode & 0o777 == 0o700
    assert (blob["filename"], blob["content_type"], blob["size"]) == (
        'città "x".pdf',
        "application/pdf",
        13,
    )


def test_attachment_route_bytes_headers_and_cleanup(client, monkeypatch):
    _download(monkeypatch)
    response = client.get("/api/gmail/messages/1/attachments/0")
    assert response.status_code == 200
    assert response.content == b"%PDF-1.4 body"
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-length"] == "13"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"] == (
        'attachment; filename="citt? _x_.pdf"; '
        "filename*=UTF-8''citt%C3%A0%20%22x%22.pdf"
    )
    assert _leftover_blobs() == []  # the adapter deletes what it delivered


def test_attachment_route_errors_match_operation(client, api, monkeypatch):
    def too_big(*args, **kwargs):
        raise gmail.GmailValidationError("Allegato troppo grande", status_code=413)

    monkeypatch.setattr(gmail, "fetch_attachment", too_big)
    response = client.get("/api/gmail/messages/1/attachments/0")
    with pytest.raises(ApiError) as exc:
        api.call("gmail.attachment", {"uid": 1, "index": 0})
    assert exc.value.code == "too_large"
    _same_error(response, exc)
    assert _leftover_blobs() == []


# -- image proxy ----------------------------------------------------------------


def _token(url: str) -> str:
    return gmail.sign_image_url(url).split("token=")[1].split("&")[0]


def test_image_route_matches_operation_and_cleans_up(client, api, monkeypatch):
    fetched: list[tuple[str, int]] = []

    async def fake_fetch(url, max_bytes):
        fetched.append((url, max_bytes))
        return _PNG, "image/png"

    monkeypatch.setattr("server.link_preview.fetch_external_image", fake_fetch)
    token = _token("https://images.example/a.png")
    response = client.get("/gmail-image", params={"token": token})
    assert response.status_code == 200
    assert response.content == _PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-security-policy"] == "default-src 'none'"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert _leftover_blobs() == []

    blob = api.call_json("gmail.image", {"token": token})
    assert Path(blob["path"]).read_bytes() == _PNG
    assert (blob["content_type"], blob["size"]) == ("image/png", len(_PNG))
    assert fetched == [("https://images.example/a.png", 8 * 1024 * 1024)] * 2


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-token",
        "x" * 9000,
        "aHR0cHM6Ly9ldmlsLmV4YW1wbGUv.0123456789abcdef0123456789abcdef",
    ],
)
def test_image_forged_token_never_reaches_the_fetcher(client, api, monkeypatch, token):
    async def fail(url, max_bytes):
        raise AssertionError("fetched an unsigned URL")

    monkeypatch.setattr("server.link_preview.fetch_external_image", fail)
    response = client.get("/gmail-image", params={"token": token})
    with pytest.raises(ApiError) as exc:
        api.call("gmail.image", {"token": token})
    assert exc.value.code == "not_found"
    assert (response.status_code, response.json()) == (
        404,
        {"detail": "Immagine non trovata"},
    )
    assert _leftover_blobs() == []


def test_image_tampered_payload_is_rejected(client, monkeypatch):
    async def fail(url, max_bytes):
        raise AssertionError("fetched an unsigned URL")

    monkeypatch.setattr("server.link_preview.fetch_external_image", fail)
    payload, _, signature = _token("https://images.example/a.png").partition(".")
    other = _token("http://169.254.169.254/latest").partition(".")[0]
    response = client.get("/gmail-image", params={"token": f"{other}.{signature}"})
    assert response.status_code == 404
    assert payload != other


def test_image_fetch_failure_is_502(client, api, monkeypatch):
    async def none(url, max_bytes):
        return None

    monkeypatch.setattr("server.link_preview.fetch_external_image", none)
    token = _token("https://images.example/a.png")
    response = client.get("/gmail-image", params={"token": token})
    with pytest.raises(ApiError) as exc:
        api.call("gmail.image", {"token": token})
    assert exc.value.code == "backend_error"
    assert (response.status_code, response.json()) == (
        502,
        {"detail": "Immagine non disponibile"},
    )
    _same_error(response, exc)


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


def _post(client, files=(), **data):
    return client.post(
        "/api/gmail/send",
        data={"to": "a@example.com", "text": "hi", **data},
        files=[("file[]", f) for f in files],
    )


def test_send_matches_operation(client, api, sent):
    response = _post(
        client,
        files=[("a.txt", b"one", "text/plain"), ("b.bin", b"two", "application/x")],
        subject="S",
        reply_to_uid="9",
        reply_to_folder="INBOX",
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "message_id": "<id@example.com>"}
    route_call = sent[0]
    assert route_call["attachments"] == [("a.txt", b"one"), ("b.bin", b"two")]
    assert (route_call["reply_to_uid"], route_call["reply_to_folder"]) == (9, "INBOX")
    assert _leftover_uploads() == []

    # The same request through the operation (files stored by the "adapter").
    directory = upload_directory()
    directory.mkdir(parents=True, exist_ok=True)
    one = directory / "x1"
    one.write_bytes(b"one")
    two = directory / "x2"
    two.write_bytes(b"two")
    result = api.call_json(
        "gmail.messages.send",
        {
            "to": "a@example.com",
            "text": "hi",
            "subject": "S",
            "reply_to_uid": 9,
            "reply_to_folder": "INBOX",
            "attachments": [
                AttachmentIn(path=str(one), filename="a.txt"),
                AttachmentIn(path=str(two), filename="b.bin"),
            ],
        },
    )
    assert result == response.json()
    assert sent[1] == route_call


def test_send_without_files_and_empty_optional_fields(client, sent):
    response = _post(client)
    assert response.status_code == 200
    assert sent[0]["reply_to_uid"] is None
    assert sent[0]["reply_to_folder"] is None
    assert sent[0]["attachments"] == []


def test_send_invalid_reply_uid(client, sent):
    response = _post(client, reply_to_uid="abc")
    assert (response.status_code, response.json()) == (
        400,
        {"detail": "reply_to_uid non valido"},
    )
    assert sent == []


def test_send_too_many_attachments_stores_nothing(client, api, sent):
    files = [(f"f{i}.txt", b"x", "text/plain") for i in range(11)]
    response = _post(client, files=files)
    with pytest.raises(ApiError) as exc:
        api.call("gmail.messages.send.check", {"attachment_count": 11})
    assert exc.value.code == "invalid_request"
    assert (response.status_code, response.json()) == (
        400,
        {"detail": "Troppi allegati"},
    )
    _same_error(response, exc)
    assert sent == []
    assert _leftover_uploads() == []


def test_send_check_is_side_effect_free(api, sent):
    assert api.call_json("gmail.messages.send.check", {"attachment_count": 10}) == {
        "ok": True
    }
    with pytest.raises(ApiError) as exc:
        api.call(
            "gmail.messages.send.check", {"attachment_count": 1, "slots_ok": False}
        )
    assert (exc.value.code, exc.value.message) == (
        "invalid_request",
        "Richiesta non valida",
    )
    assert sent == []


def test_send_oversized_file_is_413_in_file_order(client, sent, monkeypatch):
    monkeypatch.setattr(gmail, "_MAX_ATTACHMENT_BYTES", 4)
    response = _post(client, files=[("big.txt", b"12345", "text/plain")])
    assert (response.status_code, response.json()) == (
        413,
        {"detail": "Allegato troppo grande"},
    )
    assert sent == []
    assert _leftover_uploads() == []


def test_send_total_limit_is_413(client, sent, monkeypatch):
    monkeypatch.setattr(gmail, "_MAX_TOTAL_ATTACHMENT_BYTES", 5)
    response = _post(
        client,
        files=[("a.txt", b"123", "text/plain"), ("b.txt", b"456", "text/plain")],
    )
    assert (response.status_code, response.json()) == (
        413,
        {"detail": "Allegati troppo grandi"},
    )
    assert sent == []
    assert _leftover_uploads() == []


def test_send_transport_cuts_off_a_file_over_the_limit(client, sent, monkeypatch):
    """The route's copy limit is the Gmail one: it never stores a huge file."""
    import web.gmail_api as route_module

    monkeypatch.setattr(route_module, "GMAIL_MAX_ATTACHMENT_BYTES", 4)
    response = _post(
        client,
        files=[
            ("ok.txt", b"1", "text/plain"),
            ("big.txt", b"12345", "text/plain"),
            ("c.txt", b"1", "text/plain"),
        ],
    )
    assert (response.status_code, response.json()) == (
        413,
        {"detail": "Allegato troppo grande"},
    )
    assert _leftover_uploads() == []


def test_send_accepts_any_file_type_up_to_the_gmail_limit(client, sent):
    # Not media: an image above the media-kind limit but under Gmail's 25 MB is fine.
    blob = b"\x89PNG\r\n\x1a\n" + b"0" * (21 * 1024 * 1024)
    response = _post(client, files=[("big.png", blob, "image/png")])
    assert response.status_code == 200
    assert len(sent[0]["attachments"][0][1]) == len(blob)
    assert _leftover_uploads() == []


def test_send_operation_rejects_bad_slots(api, sent, tmp_path):
    with pytest.raises(ApiError) as missing:
        api.call(
            "gmail.messages.send",
            {"to": "a@example.com", "attachments": [{"path": str(tmp_path / "nope")}]},
        )
    assert (missing.value.code, missing.value.message) == (
        "invalid_request",
        "Richiesta non valida",
    )
    with pytest.raises(ApiError) as rejected:
        api.call(
            "gmail.messages.send",
            {"to": "a@example.com", "attachments": [{"rejected": "too_large"}]},
        )
    assert (rejected.value.code, rejected.value.message) == (
        "too_large",
        "Allegato troppo grande",
    )
    assert sent == []


def test_send_domain_error_texts_come_from_gmail(client, monkeypatch):
    monkeypatch.setattr(
        gmail,
        "load_settings",
        lambda: (_ for _ in ()).throw(gmail.GmailNotConfigured("x")),
    )
    response = _post(client)
    assert (response.status_code, response.json()) == (
        503,
        {"detail": "Gmail non configurato"},
    )
