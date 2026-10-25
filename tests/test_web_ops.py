"""web.ops.call_op: threading, ApiError -> HTTP mapping, ChatAPI discovery."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from client_harness import api_for
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

import server.api as api_module
from server.api import ERROR_CODES, ApiError, ChatAPI, operation
from web.ops import HTTP_STATUS, call_op, get_api


class Req(BaseModel):
    code: str = ""


class Out(BaseModel):
    thread: str


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api_module, "_REGISTRY", {})

    @operation("t.sync", request=Req, response=Out)
    def sync_op(api, req):
        if req.code:
            raise ApiError(req.code, f"msg-{req.code}")
        return Out(thread=threading.current_thread().name)

    @operation("t.async", request=Req, response=Out)
    async def async_op(api, req):
        return Out(thread=threading.current_thread().name)

    app = FastAPI()
    app.state.api = api_for(SimpleNamespace())

    @app.get("/{name}")
    async def route(request: Request, name: str, code: str = ""):
        return await call_op(request, f"t.{name}", {"code": code})

    return TestClient(app)


def test_every_error_code_has_an_http_status():
    assert set(HTTP_STATUS) == set(ERROR_CODES)


@pytest.mark.parametrize("code", sorted(ERROR_CODES))
def test_error_codes_map_to_status_and_detail(client, code):
    response = client.get("/sync", params={"code": code})
    assert response.status_code == HTTP_STATUS[code]
    assert response.json() == {"detail": f"msg-{code}"}


def test_unknown_operation_is_404(client):
    assert client.get("/missing").status_code == 404


def test_sync_handler_runs_off_the_loop_thread_and_async_on_it(client):
    sync_thread = client.get("/sync").json()["thread"]
    async_thread = client.get("/async").json()["thread"]
    assert sync_thread != async_thread


def test_get_api_returns_the_api_the_app_was_given():
    app = FastAPI()
    app.state.api = api = api_for(SimpleNamespace())
    assert isinstance(api, ChatAPI) and get_api(app) is api


def test_web_adapter_has_no_manager_fallback():
    app = FastAPI()
    app.state.manager = SimpleNamespace()
    with pytest.raises(AttributeError):
        get_api(app)
