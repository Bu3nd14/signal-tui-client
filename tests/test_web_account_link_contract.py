"""Contract: the ``/api/link/*`` routes equal the ``link.*`` operations.

The routes only add the QR -> PNG normalisation; no real WAHA / signal-cli /
Telegram is involved (a fake manager behind the real ``LinkService``).
"""

from __future__ import annotations

import base64
import io
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from server.api import ChatAPI
from server.link import qr_as_png
from web.api import create_api_router
from web.auth import install_auth

TOKEN = "link-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 8).decode()


class FakeLink:
    """Stands in for ``ChatAPI.link`` (the service has its own tests)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.start_qr = {"kind": "png", "data": PNG_B64}
        self.poll_result: dict = {"state": "waiting", "needs_2fa": False, "qr": None}
        self.active = False

    def status(self):
        return {"whatsapp": {"available": True, "account": "", "linked": None}}

    def start(self, protocol, phone="", device_name=""):
        self.calls.append(("start", protocol, phone, device_name))
        self.active = True
        return self.start_qr

    def poll(self, protocol):
        from server.link import NoLinkInProgress

        self.calls.append(("poll", protocol))
        if not self.active:
            raise NoLinkInProgress("No link in progress")
        return self.poll_result

    def cancel(self, protocol):
        self.calls.append(("cancel", protocol))
        self.active = False


def make(host=None):
    app = FastAPI()
    host = host or SimpleNamespace(
        manager=SimpleNamespace(),
        bus=None,
        reconnect=lambda protocols: host.reconnects.append(set(protocols)),
        reconnects=[],
        wa_session_needs_pairing=lambda: True,
    )
    api = ChatAPI(host)
    api.link = FakeLink()  # type: ignore[assignment]
    app.state.api = api
    install_auth(app, TOKEN)
    app.include_router(create_api_router())
    return TestClient(app), api, host


def test_auth_required():
    client, _, _ = make()
    assert client.get("/api/link/status").status_code == 401
    assert client.get("/api/link/required").status_code == 401
    for path in ("start", "poll", "cancel", "reconnect"):
        response = client.post(f"/api/link/{path}", json={"protocol": "whatsapp"})
        assert response.status_code == 401


def test_status_and_required_equal_operations():
    client, api, _ = make()
    response = client.get("/api/link/status", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == api.call_json("link.status", {})
    response = client.get("/api/link/required", headers=AUTH)
    assert response.json() == api.call_json("link.required", {})
    assert response.json() == {"protocols": {"whatsapp": True}}


def test_start_png_passthrough():
    client, api, _ = make()
    response = client.post(
        "/api/link/start", json={"protocol": "whatsapp"}, headers=AUTH
    )
    assert response.status_code == 200
    assert response.json() == {"qr": {"kind": "png", "data": PNG_B64}}
    assert api.link.calls[0][:2] == ("start", "whatsapp")  # type: ignore[attr-defined]


@pytest.mark.parametrize("kind", ["url", "text"])
def test_start_converts_raw_qr_to_png(kind):
    client, api, _ = make()
    api.link.start_qr = {"kind": kind, "data": "2@abcdef,ghij,klmn"}  # type: ignore[attr-defined]
    response = client.post(
        "/api/link/start", json={"protocol": "whatsapp"}, headers=AUTH
    )
    qr = response.json()["qr"]
    assert qr == qr_as_png({"kind": kind, "data": "2@abcdef,ghij,klmn"})
    assert Image.open(io.BytesIO(base64.b64decode(qr["data"]))).format == "PNG"


@pytest.mark.parametrize("kind", ["info", "error"])
def test_start_message_kinds_unchanged(kind):
    client, api, _ = make()
    api.link.start_qr = {"kind": kind, "data": "hello"}  # type: ignore[attr-defined]
    response = client.post(
        "/api/link/start", json={"protocol": "whatsapp"}, headers=AUTH
    )
    assert response.json() == {"qr": {"kind": kind, "data": "hello"}}


def test_poll_without_and_with_qr():
    client, api, _ = make()
    client.post("/api/link/start", json={"protocol": "whatsapp"}, headers=AUTH)
    response = client.post(
        "/api/link/poll", json={"protocol": "whatsapp"}, headers=AUTH
    )
    assert response.json() == api.call_json("link.poll", {"protocol": "whatsapp"})
    assert response.json()["qr"] is None
    api.link.poll_result = {  # type: ignore[attr-defined]
        "state": "waiting",
        "needs_2fa": False,
        "qr": {"kind": "url", "data": "2@fresh"},
    }
    response = client.post(
        "/api/link/poll", json={"protocol": "whatsapp"}, headers=AUTH
    )
    qr = response.json()["qr"]
    assert qr["kind"] == "png"
    assert Image.open(io.BytesIO(base64.b64decode(qr["data"]))).size[0] > 20


def test_poll_without_link_is_409():
    client, _, _ = make()
    response = client.post(
        "/api/link/poll", json={"protocol": "whatsapp"}, headers=AUTH
    )
    assert response.status_code == 409


def test_invalid_protocol_is_rejected():
    client, _, _ = make()
    response = client.post("/api/link/poll", json={"protocol": "icq"}, headers=AUTH)
    assert response.status_code == 400


def test_cancel_and_reconnect():
    client, api, host = make()
    client.post("/api/link/start", json={"protocol": "whatsapp"}, headers=AUTH)
    response = client.post(
        "/api/link/cancel", json={"protocol": "whatsapp"}, headers=AUTH
    )
    assert response.json() == {"ok": True}
    assert ("cancel", "whatsapp") in api.link.calls  # type: ignore[attr-defined]
    response = client.post(
        "/api/link/reconnect", json={"protocols": ["whatsapp"]}, headers=AUTH
    )
    assert response.json() == {"ok": True}
    assert host.reconnects == [{"whatsapp"}]


def test_reconnect_without_host_support_is_501():
    host = SimpleNamespace(manager=SimpleNamespace(), bus=None)
    client, _, _ = make(host)
    response = client.post(
        "/api/link/reconnect", json={"protocols": ["whatsapp"]}, headers=AUTH
    )
    assert response.status_code == 501


# -- qr_as_png / link.required --------------------------------------------


@pytest.mark.parametrize("kind", ["url", "text"])
def test_qr_as_png_renders_image(kind):
    out = qr_as_png({"kind": kind, "data": "https://example.com/x"})
    assert out["kind"] == "png"
    image = Image.open(io.BytesIO(base64.b64decode(out["data"])))
    assert image.format == "PNG"
    assert image.size[0] == image.size[1] > 40


@pytest.mark.parametrize(
    "qr",
    [
        {"kind": "png", "data": PNG_B64},
        {"kind": "info", "data": "already linked"},
        {"kind": "error", "data": "boom"},
    ],
)
def test_qr_as_png_passthrough(qr):
    assert qr_as_png(qr) == qr


def test_link_required_reflects_host():
    needs = {"v": False}
    host = SimpleNamespace(
        manager=None, bus=None, wa_session_needs_pairing=lambda: needs["v"]
    )
    api = ChatAPI(host)
    assert api.link_required() == {"whatsapp": False}
    needs["v"] = True
    assert api.call_json("link.required", {}) == {"protocols": {"whatsapp": True}}


def test_link_required_host_without_method_or_failing():
    assert ChatAPI(SimpleNamespace(manager=None, bus=None)).link_required() == {
        "whatsapp": False
    }

    def boom():
        raise RuntimeError

    host = SimpleNamespace(manager=None, bus=None, wa_session_needs_pairing=boom)
    assert ChatAPI(host).link_required() == {"whatsapp": False}


def test_finalize_equals_operation_and_requires_auth():
    client, api, _ = make()

    def finalize_signal(phone=""):
        api.link.calls.append(("finalize", phone))  # type: ignore[attr-defined]
        return "+391234567890"

    api.link.finalize_signal = finalize_signal  # type: ignore[attr-defined]
    assert client.post("/api/link/finalize", json={}).status_code == 401
    response = client.post("/api/link/finalize", json={"phone": "+39123"}, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"account": "+391234567890"}
    assert response.json() == api.call_json(
        "link.finalize", {"protocol": "signal", "phone": "+39123"}
    )
    # no phone: empty string, as the TUI does
    client.post("/api/link/finalize", json={}, headers=AUTH)
    client.post("/api/link/finalize", json={"phone": None}, headers=AUTH)
    assert api.link.calls[-2:] == [("finalize", ""), ("finalize", "")]  # type: ignore[attr-defined]


def test_finalize_error_maps_to_502_with_message():
    client, api, _ = make()

    def boom(phone=""):
        raise RuntimeError("signal-cli: nessun account")

    api.link.finalize_signal = boom  # type: ignore[attr-defined]
    response = client.post("/api/link/finalize", json={}, headers=AUTH)
    assert response.status_code == 502
    assert "signal-cli: nessun account" in response.json()["detail"]
