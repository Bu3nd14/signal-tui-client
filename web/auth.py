"""Authentication helpers for the optional web reader."""

from __future__ import annotations

import hmac
import logging
from typing import Any

logger = logging.getLogger(__name__)


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


def install_auth(app: Any, token: str, *, required: bool = True) -> None:
    """Protect every REST and media endpoint mounted below ``/api``.

    When *required* is ``False`` (``--web-no-auth``), no middleware is
    installed at all: every request is let through unconditionally. This is
    an explicit opt-in the caller must request — anyone who can reach the
    port gets full read/send access with no credential.
    """
    if not required:
        logger.warning(
            "Web UI auth DISABLED (--web-no-auth): every request is accepted "
            "with no credential"
        )
        return

    from fastapi.responses import JSONResponse

    @app.middleware("http")
    async def bearer_auth(request: Any, call_next: Any) -> Any:
        if request.url.path.startswith("/api/") and not is_authorized(
            request.headers.get("authorization"), token
        ):
            logger.warning(
                "web auth 401 path=%s has_auth=%s",
                request.url.path,
                bool(request.headers.get("authorization")),
            )
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)
