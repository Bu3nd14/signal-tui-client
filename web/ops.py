"""HTTP adapter glue: run a server operation and map its errors to HTTP.

Routes parse the request, call :func:`call_op` and return the result; every
domain decision lives in the operation (``server/ops``).  The ``ChatAPI`` is
``app.state.api`` when the server was started with one (``megachat serve``);
otherwise it is built lazily around ``app.state.manager`` (tests, legacy
``start_web_server(manager)``), publishing on the bridge's active bus.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel

from server.api import ApiError, ChatAPI
from web.bridge import get_bus

#: ``ApiError.code`` -> HTTP status.
HTTP_STATUS: dict[str, int] = {
    "invalid_request": 400,
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "conflict": 409,
    "too_large": 413,
    "internal": 500,
    "backend_error": 502,
    "backend_down": 503,
}


class _ManagerHost:
    """Minimal ``ApiHost`` around a bare manager; the bus is the bridge's."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager

    @property
    def bus(self) -> Any:
        return get_bus()


def get_api(app: Any) -> ChatAPI:
    """The ``ChatAPI`` for *app* (see module docstring)."""
    state = app.state
    api = getattr(state, "api", None)
    manager = state.manager
    if api is None or api.manager is not manager:
        api = ChatAPI(_ManagerHost(manager))
        state.api = api
    return api


async def call_op(
    request: Any, name: str, data: BaseModel | dict[str, Any] | None = None
) -> Any:
    """Run operation *name* for an HTTP *request*; return the JSON-able result.

    Sync handlers run in a worker thread (they may block); ``async`` handlers
    are awaited on the server loop.  :class:`ApiError` becomes an
    ``HTTPException`` with the error message as ``detail``.
    """
    from fastapi import HTTPException

    api = get_api(request.app)
    try:
        if api.operation(name).is_async:
            return await api.acall_json(name, data)
        return await asyncio.to_thread(api.call_json, name, data)
    except ApiError as exc:
        raise HTTPException(
            status_code=HTTP_STATUS.get(exc.code, 500), detail=exc.message
        ) from None
