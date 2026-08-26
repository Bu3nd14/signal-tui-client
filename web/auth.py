"""Authentication helpers for the optional web reader."""

from __future__ import annotations

import hmac
from typing import Any


def is_authorized(authorization: str | None, token: str) -> bool:
    """Validate an Authorization Bearer header in constant time."""
    if not authorization or not token:
        return False
    scheme, separator, supplied = authorization.partition(" ")
    return (
        bool(separator)
        and scheme.lower() == "bearer"
        and bool(supplied)
        and hmac.compare_digest(supplied, token)
    )


def install_auth(app: Any, token: str) -> None:
    """Protect every REST and media endpoint mounted below ``/api``."""
    from fastapi.responses import JSONResponse

    @app.middleware("http")
    async def bearer_auth(request: Any, call_next: Any) -> Any:
        if request.url.path.startswith("/api/") and not is_authorized(
            request.headers.get("authorization"), token
        ):
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)
