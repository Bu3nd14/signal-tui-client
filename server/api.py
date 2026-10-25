"""Server API: the operation registry that every transport adapter translates.

An *operation* is declared once, with a name, a request model, a response type
and a handler::

    @operation("chats.archive", request=ChatsArchiveRequest, response=ArchiveOut)
    def chats_archive(api: ChatAPI, req: ChatsArchiveRequest) -> ArchiveOut: ...

Handlers are plain functions ``(api, req) -> response``; ``async def`` handlers
are supported and are awaited on the caller's event loop (see :meth:`ChatAPI.acall`).
Sync handlers may block: an async adapter must run them in a thread
(``web.ops.call_op`` does).

Adapters (HTTP, in-process, later the Unix socket) only parse their wire
format, call :meth:`ChatAPI.call` / :meth:`ChatAPI.call_json` and map
:class:`ApiError` codes to their own error form.  No domain logic lives there.

``ChatAPI`` needs a *host* exposing ``manager`` (the ``BackendManager``) and
``bus`` (an :class:`~server.bus.EventBus` or ``None``): a :class:`ChatCore` is
one, and so is any small stand-in (tests, the web adapter when started with
only a manager).  ``ChatCore`` creates its own ``core.api``.

This module must not import FastAPI, Starlette, Textual, ``tui`` or ``web``.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, TypeAdapter, ValidationError

from server.send_registry import SendRegistry

#: Neutral error codes; each adapter maps them to its own form (HTTP status,
#: JSON-RPC error, exception).  ``backend_error`` is a backend that answered
#: but failed (HTTP 502); ``backend_down`` is one that is missing/unavailable.
ERROR_CODES = (
    "invalid_request",
    "not_found",
    "unauthorized",
    "forbidden",
    "too_large",
    "conflict",
    "unprocessable",
    "not_supported",
    "backend_down",
    "backend_error",
    "internal",
)


class ApiError(Exception):
    """Neutral operation error: a ``code`` from :data:`ERROR_CODES` and the
    exact user-facing ``message`` (the HTTP adapter uses it as ``detail``)."""

    def __init__(self, code: str, message: str, details: Any = None) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown ApiError code {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def __repr__(self) -> str:
        return f"ApiError({self.code!r}, {self.message!r})"


class ApiHost(Protocol):
    """What :class:`ChatAPI` needs from the object it wraps."""

    manager: Any
    bus: Any


@dataclass(frozen=True)
class Operation:
    """One registered operation."""

    name: str
    request: type[BaseModel]
    response: Any  # pydantic model or any TypeAdapter-able type (list[Model], ...)
    handler: Callable[..., Any]
    doc: str = ""

    @property
    def is_async(self) -> bool:
        return inspect.iscoroutinefunction(self.handler)

    def response_adapter(self) -> TypeAdapter[Any]:
        return TypeAdapter(self.response)


_REGISTRY: dict[str, Operation] = {}
_REGISTRY_LOCK = threading.Lock()


def operation(
    name: str, *, request: type[BaseModel], response: Any, doc: str | None = None
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register the decorated handler as operation *name*."""

    def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
        with _REGISTRY_LOCK:
            if name in _REGISTRY:
                raise ValueError(f"operation {name!r} already registered")
            _REGISTRY[name] = Operation(
                name=name,
                request=request,
                response=response,
                handler=handler,
                doc=(doc if doc is not None else inspect.getdoc(handler) or ""),
            )
        return handler

    return decorator


def _load_operations() -> None:
    """Import the modules that register the built-in operations (idempotent)."""
    import server.ops  # noqa: F401


class ChatAPI:
    """Calls registered operations against a host (core or stand-in)."""

    def __init__(self, host: ApiHost) -> None:
        self._host = host
        #: Text-send idempotency claims; per-process state, one per ChatAPI
        #: (``ChatCore`` owns exactly one ChatAPI).
        self.send_registry = SendRegistry()
        #: Device-link sessions (per-process state, like ``send_registry``).
        from server.link import LinkService

        self.link = LinkService(lambda: self.manager)
        _load_operations()

    # -- host access (used by handlers) -------------------------------------

    @property
    def manager(self) -> Any:
        return self._host.manager

    @property
    def bus(self) -> Any:
        return self._host.bus

    def reconnect(self, protocols: set[str]) -> None:
        """Reconnect the given protocols' backends (the host's ``reconnect``)."""
        reconnect = getattr(self._host, "reconnect", None)
        if reconnect is None:
            raise ApiError("not_supported", "This server cannot reconnect backends")
        reconnect(protocols)

    def publish_web_push(self, event: dict[str, Any]) -> bool:
        """Publish a legacy ``/ws`` frame (until Phase 1 replaces them with
        real core events).  Same payload as ``web.bridge.push_event``."""
        bus = self.bus
        if bus is None:
            return False
        bus.publish("web_push", {"event": event})
        return True

    # -- registry views -----------------------------------------------------

    def operations(self) -> list[Operation]:
        """All registered operations, sorted by name."""
        with _REGISTRY_LOCK:
            return sorted(_REGISTRY.values(), key=lambda op: op.name)

    def operation(self, name: str) -> Operation:
        with _REGISTRY_LOCK:
            op = _REGISTRY.get(name)
        if op is None:
            raise ApiError("not_found", f"Unknown operation {name!r}")
        return op

    def json_schema(self) -> dict[str, dict[str, Any]]:
        """``{name: {"doc", "request", "response"}}`` JSON Schemas of every op."""
        return {
            op.name: {
                "doc": op.doc,
                "request": op.request.model_json_schema(),
                "response": op.response_adapter().json_schema(),
            }
            for op in self.operations()
        }

    # -- calls --------------------------------------------------------------

    def _prepare(self, name: str, request: BaseModel | dict[str, Any] | None):
        op = self.operation(name)
        try:
            if isinstance(request, op.request):
                model = request
            elif isinstance(request, BaseModel):
                model = op.request.model_validate(request.model_dump())
            else:
                model = op.request.model_validate(
                    request if request is not None else {}
                )
        except ValidationError as exc:
            raise ApiError(
                "invalid_request", "Invalid request", details=exc.errors()
            ) from exc
        return op, model

    def call(self, name: str, request: BaseModel | dict[str, Any] | None = None) -> Any:
        """Run operation *name*; returns the handler's python object.

        An ``async`` handler is run to completion on a private loop; call
        from a running event loop with :meth:`acall` instead.
        """
        op, model = self._prepare(name, request)
        result = op.handler(self, model)
        if inspect.isawaitable(result):
            result = _run_sync(result)
        return result

    async def acall(
        self, name: str, request: BaseModel | dict[str, Any] | None = None
    ) -> Any:
        """Like :meth:`call` but awaits ``async`` handlers on the running loop.
        A sync handler still runs inline (and may block): offload it."""
        op, model = self._prepare(name, request)
        result = op.handler(self, model)
        if inspect.isawaitable(result):
            result = await result
        return result

    def _serialize(self, op: Operation, result: Any) -> Any:
        adapter = op.response_adapter()
        return adapter.dump_python(adapter.validate_python(result), mode="json")

    def call_json(
        self, name: str, request: BaseModel | dict[str, Any] | None = None
    ) -> Any:
        """:meth:`call`, with the response validated and serialized to JSON-able
        python through the response type (what adapters send on the wire)."""
        result = self.call(name, request)
        return self._serialize(self.operation(name), result)

    async def acall_json(
        self, name: str, request: BaseModel | dict[str, Any] | None = None
    ) -> Any:
        result = await self.acall(name, request)
        return self._serialize(self.operation(name), result)


def _run_sync(awaitable: Any) -> Any:
    async def runner() -> Any:
        return await awaitable

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(runner())
    raise RuntimeError("async operation called from a running loop; use acall()")
