"""Operation registry: validation, errors, schema export, JSON serialization."""

from __future__ import annotations

import pydantic
import pytest
from pydantic import BaseModel

import server.api as api_module
from server.api import ApiError, ChatAPI, operation
from server.bus import EventBus


class Echo(BaseModel):
    text: str
    count: int = 1


class EchoOut(BaseModel):
    text: str
    count: int


class Host:
    def __init__(self, bus=None):
        self.manager = object()
        self.bus = bus


@pytest.fixture
def registry(monkeypatch):
    """Isolated registry so test operations never leak into the real one."""
    reg: dict = {}
    monkeypatch.setattr(api_module, "_REGISTRY", reg)
    return reg


@pytest.fixture
def api(registry):
    @operation("t.echo", request=Echo, response=EchoOut)
    def echo(api, req):
        """Echo back."""
        return EchoOut(text=req.text, count=req.count)

    @operation("t.many", request=Echo, response=list[EchoOut])
    def many(api, req):
        return [{"text": req.text, "count": i} for i in range(req.count)]

    @operation("t.boom", request=Echo, response=EchoOut)
    def boom(api, req):
        raise ApiError("conflict", "nope")

    @operation("t.async", request=Echo, response=EchoOut)
    async def aecho(api, req):
        return EchoOut(text=req.text, count=req.count)

    return ChatAPI(Host())


def test_call_validates_dict_into_request_model(api):
    result = api.call("t.echo", {"text": "hi"})
    assert result == EchoOut(text="hi", count=1)


def test_call_accepts_request_model_instance(api):
    assert api.call("t.echo", Echo(text="a", count=3)).count == 3


def test_extra_request_keys_are_ignored(api):
    assert api.call("t.echo", {"text": "hi", "other": 1}).text == "hi"


def test_validation_error_becomes_invalid_request(api):
    with pytest.raises(ApiError) as info:
        api.call("t.echo", {"count": "x"})
    assert info.value.code == "invalid_request"
    assert info.value.message == "Invalid request"
    assert info.value.details


def test_unknown_operation_is_not_found(api):
    with pytest.raises(ApiError) as info:
        api.call("nope", {})
    assert info.value.code == "not_found"


def test_handler_api_error_propagates(api):
    with pytest.raises(ApiError) as info:
        api.call("t.boom", {"text": "x"})
    assert (info.value.code, info.value.message) == ("conflict", "nope")


def test_unknown_error_code_rejected():
    with pytest.raises(ValueError):
        ApiError("bogus", "x")


def test_duplicate_registration_rejected(api):
    with pytest.raises(ValueError):
        operation("t.echo", request=Echo, response=EchoOut)(lambda a, r: None)


def test_call_json_serializes_through_response_type(api):
    assert api.call_json("t.echo", {"text": "hi"}) == {"text": "hi", "count": 1}
    assert api.call_json("t.many", {"text": "x", "count": 2}) == [
        {"text": "x", "count": 0},
        {"text": "x", "count": 1},
    ]


def test_call_json_rejects_response_violating_the_type(registry):
    @operation("t.bad", request=Echo, response=EchoOut)
    def bad(api, req):
        return {"text": 1}

    with pytest.raises(pydantic.ValidationError):
        ChatAPI(Host()).call_json("t.bad", {"text": "x"})


def test_async_handler_runs_in_sync_call_and_acall(api):
    import asyncio

    assert api.call("t.async", {"text": "a"}).text == "a"
    assert asyncio.run(api.acall_json("t.async", {"text": "b"})) == {
        "text": "b",
        "count": 1,
    }


def test_operations_listing_and_schema_export(api):
    names = [op.name for op in api.operations()]
    assert names == sorted(names) == ["t.async", "t.boom", "t.echo", "t.many"]
    schema = api.json_schema()
    assert schema["t.echo"]["doc"] == "Echo back."
    assert schema["t.echo"]["request"]["properties"]["text"]["type"] == "string"
    assert schema["t.many"]["response"]["type"] == "array"


def test_publish_web_push_uses_bus_and_tolerates_none(registry):
    bus = EventBus()
    assert ChatAPI(Host(bus)).publish_web_push({"type": "x"}) is True
    assert ChatAPI(Host(None)).publish_web_push({"type": "x"}) is False


def test_builtin_operations_registered_and_schema_exportable():
    real = ChatAPI(Host())
    real.json_schema()  # every registered operation must export cleanly
