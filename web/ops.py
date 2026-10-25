"""HTTP adapter glue: run a server operation and map its errors to HTTP.

Routes parse the request, call :func:`call_op` and return the result; every
domain decision lives in the operation (``server/ops``).  The ``ChatAPI`` is
``app.state.api``: the one ``start_web_server`` was given (``core.api``).  The
adapter has no manager and no other way to reach the core.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel

from server.api import ApiError, ChatAPI

#: ``ApiError.code`` -> HTTP status.
HTTP_STATUS: dict[str, int] = {
    "invalid_request": 400,
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "conflict": 409,
    "too_large": 413,
    "unprocessable": 422,
    "not_supported": 501,
    "internal": 500,
    "backend_error": 502,
    "backend_down": 503,
}


def get_api(app: Any) -> ChatAPI:
    """The ``ChatAPI`` the server was started with (``app.state.api``)."""
    return app.state.api


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
