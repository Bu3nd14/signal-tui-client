"""Contract: POST /api/send == the ``messages.send`` operation.

The route only translates: success bodies equal ``ChatAPI.call_json`` and every
failure has the status and ``detail`` of the matching ``ApiError`` (via
``web.ops.HTTP_STATUS``).  Published ``/ws`` frames are compared with the
literal legacy dicts, and the temp upload files are gone afterwards.
"""

from __future__ import annotations

import errno
from base64 import b64decode
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from event_harness import legacy_frames
from fastapi import FastAPI
from fastapi.testclient import TestClient

import protocols.db as backend_db
from models import ChatContact
from server.api import ApiError
from server.bus import EventBus
from server.models_api import AttachmentIn
from server.retry import SEND_RETRY_MAX_ATTEMPTS
from web.api import create_api_router
from web.auth import install_auth
from web.bridge import close_bridge, init_bridge
from web.ops import HTTP_STATUS, get_api

AUTH = {"Authorization": "Bearer contract-secret"}
_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
_MESSAGE_FRAME = {
    "type": "message",
    "payload": {"protocol": "signal", "contact_id": "alice", "is_mine": True},
}


class _Backend:
    def __init__(self, contacts):
        self.contacts = contacts

    def find_contact(self, contact_id):
        return next((c for c in self.contacts if c.id == contact_id), None)

    def register_contact(self, contact):
        pass


class _Manager:
    def __init__(self):
        contacts = [ChatContact("alice", "Alice", "signal")]
        self.backend = _Backend(contacts)
        self.missing: set[str] = set()
        self.outcomes: list[BaseException | None] = []
        self.send_calls: list[tuple] = []
        self.attachments_calls: list[dict] = []
        self.attachments_error: BaseException | None = None

    def get(self, protocol):
        return None if protocol in self.missing else self.backend

    def get_attachment_path(self, protocol, attachment_id):
        return None

    def send_message_sync(self, protocol, contact_id, text, **kwargs):
        self.send_calls.append((protocol, contact_id, text, kwargs))
        index = len(self.send_calls) - 1
        if index < len(self.outcomes) and self.outcomes[index] is not None:
            raise self.outcomes[index]
        return "sent-id"

    def send_attachments_sync(self, protocol, contact_id, file_paths, **kwargs):
        if self.attachments_error is not None:
            raise self.attachments_error
        self.attachments_calls.append(
            {
                "protocol": protocol,
                "contact_id": contact_id,
                "n": len(file_paths),
                "existed": [p.exists() for p in file_paths],
                **{k: kwargs[k] for k in sorted(kwargs)},
            }
        )
        return ["id"] * len(file_paths)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(backend_db, "CACHE_DIR", tmp_path)
    manager = _Manager()
    app = FastAPI()
    app.state.manager = manager
    install_auth(app, "contract-secret")
    app.include_router(create_api_router())
    bus = EventBus()
    sub = bus.subscribe()
    init_bridge(bus)
    uploads = tmp_path / "web-uploads"
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(
                client=client,
                api=get_api(app),
                manager=manager,
                sub=sub,
                uploads=uploads,
                tmp=tmp_path,
            )
    finally:
        close_bridge()


def _frames(env):
    return legacy_frames(env.sub.drain())


def _leftover_uploads(env):
    return list(env.uploads.iterdir()) if env.uploads.exists() else []


def _text(**over):
    body = {"protocol": "signal", "contact_id": "alice", "text": "Ciao"}
    body.update(over)
    return body


def _expect_error(env, response, exc):
    assert response.status_code == HTTP_STATUS[exc.value.code]
    assert response.json() == {"detail": exc.value.message}


# ── text sends ────────────────────────────────────────────────────────────────


def test_json_text_send_matches_operation_and_publishes_message_frame(env):
    response = env.client.post(
        "/api/send", json=_text(client_msg_id="route-1"), headers=AUTH
    )
    assert response.status_code == 200
    expected = env.api.call_json("messages.send", _text(client_msg_id="op-1"))
    assert response.json() == expected == {"ok": True}
    assert [call[:3] for call in env.manager.send_calls] == [
        ("signal", "alice", "Ciao")
    ] * 2
    # Exactly the legacy frame, once per send (route + direct op call).
    assert _frames(env) == [_MESSAGE_FRAME, _MESSAGE_FRAME]


def test_request_body_cannot_select_local_frontend_modes(env):
    """``optimistic`` / ``retry`` are operation knobs of local frontends: the
    route drops them (JSON and multipart), so the send is the plain web one."""
    body = _text(client_msg_id="knobs", optimistic={"timestamp": 5}, retry="nope")
    json_response = env.client.post("/api/send", json=body, headers=AUTH)
    form_response = env.client.post(
        "/api/send",
        data={**_text(client_msg_id="knobs2"), "optimistic": "{}", "retry": "0"},
        files={"file": ("a.png", _PNG, "image/png")},
        headers=AUTH,
    )

    assert json_response.status_code == 200
    assert json_response.json() == {"ok": True}
    assert form_response.status_code == 200
    assert form_response.json() == {"ok": True}
    # Plain web flavour: manager send (mirror path) + the web's own frame only.
    assert [call[:3] for call in env.manager.send_calls] == [
        ("signal", "alice", "Ciao")
    ]
    assert _frames(env) == [_MESSAGE_FRAME, _MESSAGE_FRAME]


def test_duplicate_client_msg_id_is_idempotent_like_the_operation(env):
    first = env.client.post("/api/send", json=_text(client_msg_id="dup"), headers=AUTH)
    second = env.client.post("/api/send", json=_text(client_msg_id="dup"), headers=AUTH)
    via_op = env.api.call_json("messages.send", _text(client_msg_id="dup"))
    assert first.json() == {"ok": True}
    assert second.status_code == 200
    assert second.json() == via_op == {"ok": True, "duplicate": True}
    assert len(env.manager.send_calls) == 1
    assert _frames(env) == [_MESSAGE_FRAME]


def test_client_msg_id_reused_with_other_payload_is_422_like_the_operation(env):
    env.client.post("/api/send", json=_text(client_msg_id="same"), headers=AUTH)
    response = env.client.post(
        "/api/send", json=_text(client_msg_id="same", text="Altro"), headers=AUTH
    )
    with pytest.raises(ApiError) as exc:
        env.api.call("messages.send", _text(client_msg_id="same", text="Altro"))
    assert exc.value.code == "unprocessable"
    assert response.status_code == 422
    _expect_error(env, response, exc)


def test_inflight_claim_is_409_like_the_operation(env):
    from server.send_registry import build_fingerprint

    fingerprint = build_fingerprint(
        protocol="signal",
        contact_id="alice",
        text="Ciao",
        quote_timestamp=None,
        quote_author=None,
        quote_message=None,
        reply_to_message_id=None,
        quote_content_type=None,
        quote_attachment_id=None,
    )
    outcome, _ = env.api.send_registry.claim_or_lookup(
        ("signal", "alice", "busy"), fingerprint
    )
    assert outcome == "claimed"
    response = env.client.post(
        "/api/send", json=_text(client_msg_id="busy"), headers=AUTH
    )
    with pytest.raises(ApiError) as exc:
        env.api.call("messages.send", _text(client_msg_id="busy"))
    assert exc.value.code == "conflict"
    _expect_error(env, response, exc)
    assert env.manager.send_calls == []
    assert _frames(env) == []


def test_backend_failure_retries_then_502_like_the_operation(env):
    refused = OSError(errno.ECONNREFUSED, "refused")
    env.manager.outcomes = [refused] * (2 * SEND_RETRY_MAX_ATTEMPTS)
    with (
        patch("server.ops.send.compute_delay", return_value=0.0),
        patch("server.ops.send.asyncio.sleep", new_callable=AsyncMock),
    ):
        response = env.client.post(
            "/api/send", json=_text(client_msg_id="retry-1"), headers=AUTH
        )
        frames = _frames(env)
        with pytest.raises(ApiError) as exc:
            env.api.call("messages.send", _text(client_msg_id="retry-2"))
    assert exc.value.code == "backend_error"
    assert response.status_code == 502
    _expect_error(env, response, exc)
    assert len(env.manager.send_calls) == 2 * SEND_RETRY_MAX_ATTEMPTS
    assert frames == [
        {
            "type": "send_retry",
            "payload": {
                "client_msg_id": "retry-1",
                "protocol": "signal",
                "contact_id": "alice",
                "attempt": attempt,
                "max_attempts": SEND_RETRY_MAX_ATTEMPTS,
                "error": "[Errno 111] refused",
            },
        }
        for attempt in (2, 3)
    ]
    # The failed claim was released: the same client_msg_id sends again.
    env.manager.outcomes = []
    again = env.client.post(
        "/api/send", json=_text(client_msg_id="retry-1"), headers=AUTH
    )
    assert again.json() == {"ok": True}


def test_terminal_backend_failure_is_502_without_retry_frames(env):
    env.manager.outcomes = [PermissionError("denied")] * 2
    response = env.client.post("/api/send", json=_text(), headers=AUTH)
    with pytest.raises(ApiError) as exc:
        env.api.call("messages.send", _text())
    assert response.status_code == 502
    _expect_error(env, response, exc)
    assert len(env.manager.send_calls) == 2
    assert _frames(env) == []


_INVALID_JSON = [
    pytest.param(_text(protocol="irc"), id="unknown-protocol"),
    pytest.param({"contact_id": "alice", "text": "x"}, id="missing-protocol"),
    pytest.param(_text(contact_id="  "), id="blank-contact"),
    pytest.param(_text(contact_id=5), id="non-string-contact"),
    pytest.param(_text(text=None), id="missing-text"),
    pytest.param(_text(text=5), id="non-string-text"),
    pytest.param(_text(text="   "), id="blank-text"),
    pytest.param(_text(text="x" * (64 * 1024 + 1)), id="text-too-long"),
    pytest.param(_text(quote_timestamp="12"), id="string-timestamp"),
    pytest.param(_text(quote_timestamp=True), id="bool-timestamp"),
    pytest.param(_text(quote_author=7, quote_timestamp=1), id="non-string-author"),
    pytest.param(_text(contact_id="nobody"), id="unknown-contact"),
]


@pytest.mark.parametrize("payload", _INVALID_JSON)
def test_json_validation_failures_match_operation_errors(env, payload):
    response = env.client.post("/api/send", json=payload, headers=AUTH)
    with pytest.raises(ApiError) as exc:
        env.api.call("messages.send", payload)
    assert response.status_code == HTTP_STATUS[exc.value.code]
    _expect_error(env, response, exc)
    assert env.manager.send_calls == []
    assert _frames(env) == []


def test_missing_backend_is_404_like_the_operation(env):
    env.manager.missing.add("signal")
    response = env.client.post("/api/send", json=_text(), headers=AUTH)
    with pytest.raises(ApiError) as exc:
        env.api.call("messages.send", _text())
    assert exc.value.code == "not_found"
    assert response.status_code == 404
    _expect_error(env, response, exc)


@pytest.mark.parametrize(
    "protocol,extra",
    [
        ("telegram", {"reply_to_message_id": "0"}),
        ("telegram", {"reply_to_message_id": "abc"}),
        ("whatsapp", {"quote_author": "x"}),
    ],
)
def test_reply_validation_matches_operation(env, protocol, extra):
    payload = _text(protocol=protocol, **extra)
    env.manager.backend.contacts.append(ChatContact("alice", "Alice", protocol))
    response = env.client.post("/api/send", json=payload, headers=AUTH)
    with pytest.raises(ApiError) as exc:
        env.api.call("messages.send", payload)
    assert response.status_code == 400
    _expect_error(env, response, exc)


def test_json_body_cannot_inject_attachment_paths(env):
    secret = env.tmp / "secret.png"
    secret.write_bytes(_PNG)
    response = env.client.post(
        "/api/send",
        json=_text(attachments=[{"path": str(secret), "filename": "secret.png"}]),
        headers=AUTH,
    )
    assert response.json() == {"ok": True}
    assert env.manager.attachments_calls == []
    assert len(env.manager.send_calls) == 1
    assert secret.exists()


# ── multipart / attachments ───────────────────────────────────────────────────


def _multipart(env, files, **fields):
    data = {"protocol": "signal", "contact_id": "alice", "text": ""}
    data.update(fields)
    return env.client.post(
        "/api/send",
        data=data,
        files=[("file", f) for f in files],
        headers=AUTH,
    )


def _op_attachment(env, name, data, content_type="image/png"):
    path = env.tmp / f"op-{name}"
    path.write_bytes(data)
    return AttachmentIn(path=str(path), filename=name, content_type=content_type)


def test_multipart_attachments_match_operation_and_temp_files_are_removed(env):
    response = _multipart(
        env,
        [("a.png", _PNG, "image/png"), ("b.png", _PNG, "image/png")],
        text="caption",
        batch_id="batch-1",
    )
    via_op = env.api.call_json(
        "messages.send",
        {
            "protocol": "signal",
            "contact_id": "alice",
            "text": "caption",
            "batch_id": "batch-1",
            "attachments": [
                _op_attachment(env, "a.png", _PNG),
                _op_attachment(env, "b.png", _PNG),
            ],
        },
    )
    assert response.status_code == 200
    assert response.json() == via_op == {"ok": True}
    route_call, op_call = env.manager.attachments_calls
    assert route_call == op_call
    assert route_call["existed"] == [True, True]
    assert route_call["captions"] == ["caption", None]
    assert route_call["filenames"] == ["a.png", "b.png"]
    assert route_call["mime_types"] == ["image/png", "image/png"]
    assert route_call["media_kinds"] == ["image", "image"]
    assert route_call["batch_id"] == "batch-1"
    assert _frames(env) == [_MESSAGE_FRAME, _MESSAGE_FRAME]
    assert _leftover_uploads(env) == []  # the adapter owns and deletes the temp files


def test_attachments_are_not_deduplicated_or_retried(env):
    for _ in range(2):
        response = _multipart(env, [("a.png", _PNG, "image/png")], client_msg_id="x")
        assert response.json() == {"ok": True}
    assert len(env.manager.attachments_calls) == 2


def test_multipart_unsupported_media_matches_operation(env):
    response = _multipart(env, [("notes.txt", b"plain text", "text/plain")])
    with pytest.raises(ApiError) as exc:
        env.api.call(
            "messages.send",
            {
                "protocol": "signal",
                "contact_id": "alice",
                "text": "",
                "attachments": [
                    _op_attachment(env, "notes.txt", b"plain text", "text/plain")
                ],
            },
        )
    assert (exc.value.code, exc.value.message) == (
        "invalid_request",
        "Unsupported media type",
    )
    assert response.status_code == 400
    _expect_error(env, response, exc)
    assert env.manager.attachments_calls == []
    assert _frames(env) == []
    assert _leftover_uploads(env) == []


def test_multipart_kind_limit_matches_operation(env):
    limits = {"image": 8, "video": 8, "audio": 8, "document": 8}
    with patch("server.uploads._MAX_BYTES_BY_KIND", limits):
        response = _multipart(env, [("a.png", _PNG, "image/png")])
        with pytest.raises(ApiError) as exc:
            env.api.call(
                "messages.send",
                {
                    "protocol": "signal",
                    "contact_id": "alice",
                    "text": "",
                    "attachments": [_op_attachment(env, "a.png", _PNG)],
                },
            )
    assert exc.value.code == "too_large"
    assert response.status_code == 413
    _expect_error(env, response, exc)
    assert _leftover_uploads(env) == []


def test_multipart_total_budget_is_reported_in_file_order(env):
    """Request-wide cap (transport) hits file 2; a bad file 1 still wins."""
    with patch("web.uploads._MAX_TOTAL_BYTES", len(_PNG) + 4):
        too_big = _multipart(
            env,
            [("a.png", _PNG, "image/png"), ("b.png", _PNG, "image/png")],
        )
        first_bad = _multipart(
            env,
            [("a.txt", b"plain text", "text/plain"), ("b.png", _PNG, "image/png")],
        )
    assert (too_big.status_code, too_big.json()) == (
        413,
        {"detail": "Upload too large"},
    )
    assert (first_bad.status_code, first_bad.json()) == (
        400,
        {"detail": "Unsupported media type"},
    )
    assert env.manager.attachments_calls == []
    assert _leftover_uploads(env) == []


def test_too_many_attachments_matches_operation(env):
    files = [(f"{i}.png", _PNG, "image/png") for i in range(11)]
    response = _multipart(env, files)
    with pytest.raises(ApiError) as exc:
        env.api.call(
            "messages.send",
            {
                "protocol": "signal",
                "contact_id": "alice",
                "text": "",
                "attachments": [
                    _op_attachment(env, f"{i}.png", _PNG) for i in range(11)
                ],
            },
        )
    assert (exc.value.code, exc.value.message) == (
        "invalid_request",
        "Too many attachments",
    )
    assert response.status_code == 400
    _expect_error(env, response, exc)
    assert _leftover_uploads(env) == []


def test_multipart_without_text_or_files_is_invalid(env):
    response = env.client.post(
        "/api/send",
        data={"protocol": "signal", "contact_id": "alice", "text": ""},
        files={"unrelated": ("x.txt", b"x", "text/plain")},
        headers=AUTH,
    )
    assert (response.status_code, response.json()) == (
        400,
        {"detail": "Invalid request"},
    )


@pytest.mark.parametrize(
    "error,code,status,detail",
    [
        (RuntimeError("down"), "backend_error", 502, "Message send failed"),
        (
            NotImplementedError(),
            "not_supported",
            501,
            "Attachment send not supported",
        ),
    ],
)
def test_attachment_backend_failures_match_operation(env, error, code, status, detail):
    env.manager.attachments_error = error
    response = _multipart(env, [("a.png", _PNG, "image/png")])
    with pytest.raises(ApiError) as exc:
        env.api.call(
            "messages.send",
            {
                "protocol": "signal",
                "contact_id": "alice",
                "text": "",
                "attachments": [_op_attachment(env, "a.png", _PNG)],
            },
        )
    assert (exc.value.code, exc.value.message) == (code, detail)
    assert response.status_code == status == HTTP_STATUS[code]
    _expect_error(env, response, exc)
    assert _frames(env) == []
    assert _leftover_uploads(env) == []


def test_multipart_quote_timestamp_form_conversion(env):
    ok = env.client.post(
        "/api/send",
        data={
            "protocol": "signal",
            "contact_id": "alice",
            "text": "re",
            "quote_timestamp": "123",
            "quote_author": "+39",
            "quote_message": "orig",
        },
        files={"unrelated": ("x.txt", b"x", "text/plain")},
        headers=AUTH,
    )
    empty = env.client.post(
        "/api/send",
        data={
            "protocol": "signal",
            "contact_id": "alice",
            "text": "re2",
            "quote_timestamp": "",
        },
        files={"unrelated": ("x.txt", b"x", "text/plain")},
        headers=AUTH,
    )
    bad = env.client.post(
        "/api/send",
        data={
            "protocol": "signal",
            "contact_id": "alice",
            "text": "re3",
            "quote_timestamp": "abc",
        },
        files={"unrelated": ("x.txt", b"x", "text/plain")},
        headers=AUTH,
    )
    assert (ok.json(), empty.json()) == ({"ok": True}, {"ok": True})
    assert env.manager.send_calls[0][3]["quote_timestamp"] == 123
    assert env.manager.send_calls[1][3]["quote_timestamp"] is None
    assert (bad.status_code, bad.json()) == (400, {"detail": "Invalid request"})
    assert len(env.manager.send_calls) == 2


def test_operation_registered_with_schema(env):
    schema = env.api.json_schema()["messages.send"]
    assert "attachments" in schema["request"]["properties"]


# ── validate before storing uploads (mc-mbm.10) ───────────────────────────────


@pytest.fixture
def store_spy(monkeypatch):
    """Count the uploads the route copies to temp files."""
    import web.api as web_api

    calls: list[object] = []
    real = web_api.store_temp_upload

    async def spy(upload_file, **kwargs):
        calls.append(upload_file)
        return await real(upload_file, **kwargs)

    monkeypatch.setattr(web_api, "store_temp_upload", spy)
    return calls


_PNG_FILE = ("a.png", _PNG, "image/png")
_ELEVEN = [(f"{i}.png", _PNG, "image/png") for i in range(11)]

# (fields, files, expected status, expected detail): each fails before any
# upload is stored; the order of simultaneous errors is the legacy one.
_PRESTORE_REJECTS = [
    pytest.param({"protocol": "irc"}, [_PNG_FILE], 400, "Invalid request", id="proto"),
    pytest.param({"contact_id": "  "}, [_PNG_FILE], 400, "Invalid request", id="blank"),
    pytest.param(
        {"text": "x" * (64 * 1024 + 1)}, [_PNG_FILE], 400, "Invalid request", id="long"
    ),
    pytest.param({}, _ELEVEN, 400, "Too many attachments", id="too-many"),
    pytest.param(
        {"quote_timestamp": "abc"}, [_PNG_FILE], 400, "Invalid request", id="ts"
    ),
    pytest.param(
        {"protocol": "whatsapp", "quote_author": "x"},
        [_PNG_FILE],
        400,
        "Invalid request",
        id="whatsapp-reply",
    ),
    pytest.param(
        {"protocol": "telegram", "quote_author": "x", "reply_to_message_id": "0"},
        [_PNG_FILE],
        400,
        "Invalid request",
        id="telegram-reply",
    ),
    pytest.param(
        {"quote_attachment_id": "nope"}, [_PNG_FILE], 400, "Invalid request", id="sig"
    ),
    pytest.param({"contact_id": "nobody"}, [_PNG_FILE], 404, "Not Found", id="nobody"),
    # Precedence between simultaneous errors (legacy order).
    pytest.param(
        {"protocol": "irc"}, _ELEVEN, 400, "Invalid request", id="proto>count"
    ),
    pytest.param(
        {"quote_timestamp": "abc"}, _ELEVEN, 400, "Too many attachments", id="count>ts"
    ),
    pytest.param(
        {"contact_id": "nobody"}, _ELEVEN, 400, "Too many attachments", id="count>404"
    ),
    pytest.param(
        {"contact_id": "nobody", "quote_timestamp": "abc"},
        [_PNG_FILE],
        400,
        "Invalid request",
        id="ts>404",
    ),
]


@pytest.mark.parametrize("fields,files,status,detail", _PRESTORE_REJECTS)
def test_rejected_multipart_never_stores_uploads(
    env, store_spy, fields, files, status, detail
):
    for protocol in ("telegram", "whatsapp"):
        env.manager.backend.contacts.append(ChatContact("alice", "Alice", protocol))
    response = _multipart(env, files, **fields)
    assert (response.status_code, response.json()) == (status, {"detail": detail})
    assert store_spy == []
    assert _leftover_uploads(env) == []
    assert env.manager.attachments_calls == []


def test_missing_backend_with_files_never_stores_uploads(env, store_spy):
    env.manager.missing.add("signal")
    response = _multipart(env, [_PNG_FILE])
    assert (response.status_code, response.json()) == (404, {"detail": "Not Found"})
    assert store_spy == []
    assert _leftover_uploads(env) == []


def test_non_file_field_named_file_never_stores_uploads(env, store_spy):
    response = env.client.post(
        "/api/send",
        data={"protocol": "signal", "contact_id": "alice", "text": "", "file": "x"},
        headers=AUTH,
    )
    assert response.status_code == 400
    assert store_spy == []


def test_form_field_named_attachments_cannot_break_the_check(env, store_spy):
    response = _multipart(env, [_PNG_FILE], attachments="junk", attachment_count="0")
    assert response.status_code == 200
    assert len(store_spy) == 1
    assert env.manager.attachments_calls[0]["n"] == 1


def test_duplicate_and_inflight_text_sends_store_nothing(env, store_spy):
    # Only text sends are idempotent, so there is nothing to store for a replay
    # or an in-flight duplicate; the route must not touch the upload dir.
    first = env.client.post("/api/send", json=_text(client_msg_id="k"), headers=AUTH)
    replay = env.client.post("/api/send", json=_text(client_msg_id="k"), headers=AUTH)
    assert (first.json(), replay.json()) == (
        {"ok": True},
        {"ok": True, "duplicate": True},
    )
    other = env.client.post(
        "/api/send", json=_text(client_msg_id="k", text="x"), headers=AUTH
    )
    assert other.status_code == 422
    assert store_spy == []
    assert not env.uploads.exists() or list(env.uploads.iterdir()) == []


def test_failed_upload_store_takes_no_claim_and_sends_nothing(env, monkeypatch):
    import web.api as web_api

    async def boom(upload_file, **kwargs):
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(web_api, "store_temp_upload", boom)
    response = _multipart(env, [_PNG_FILE], client_msg_id="same")
    assert (response.status_code, response.json()) == (
        502,
        {"detail": "Message send failed"},
    )
    assert env.manager.attachments_calls == []
    # No registry entry was left behind: the same id is free for a text send.
    retry = env.client.post("/api/send", json=_text(client_msg_id="same"), headers=AUTH)
    assert retry.json() == {"ok": True}


def test_check_operation_is_registered_and_sends_nothing(env):
    assert env.api.call_json(
        "messages.send.check", {**_text(), "attachment_count": 1}
    ) == {"ok": True}
    with pytest.raises(ApiError) as exc:
        env.api.call("messages.send.check", {**_text(), "attachment_count": 11})
    assert exc.value.message == "Too many attachments"
    assert env.manager.send_calls == [] and env.manager.attachments_calls == []
    assert _frames(env) == []
