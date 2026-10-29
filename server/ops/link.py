"""Device-link operations: the blocking linking flows run in the core.

``link.start`` and ``link.poll`` (and ``link.finalize`` / ``link.submit_2fa``)
block on ``signal-cli``, WAHA or Telegram: sync handlers, to be run off any UI
thread.  The service behind them is :mod:`server.link`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from server.api import ApiError, ChatAPI, operation
from server.link import LinkUnavailable, NoLinkInProgress
from server.models_api import (
    EmptyRequest,
    Link2faOut,
    Link2faRequest,
    LinkFinalizeOut,
    LinkFinalizeRequest,
    LinkPollOut,
    LinkProtocolRequest,
    LinkReconnectRequest,
    LinkRequiredOut,
    LinkStartOut,
    LinkStartRequest,
    LinkStatusOut,
    OkOut,
)


@contextmanager
def _link_errors() -> Iterator[None]:
    """Map the service's exceptions to API errors, keeping the user-facing text."""
    try:
        yield
    except ApiError:
        raise
    except NoLinkInProgress as exc:
        raise ApiError("conflict", str(exc)) from exc
    except LinkUnavailable as exc:
        raise ApiError("backend_down", str(exc)) from exc
    except Exception as exc:
        raise ApiError("backend_error", str(exc)) from exc


@operation("link.status", request=EmptyRequest, response=LinkStatusOut)
def link_status(api: ChatAPI, req: EmptyRequest) -> LinkStatusOut:
    """Which protocols can be linked, the linked Signal number (no I/O)."""
    return LinkStatusOut.model_validate({"protocols": api.link.status()})


@operation("link.required", request=EmptyRequest, response=LinkRequiredOut)
def link_required(api: ChatAPI, req: EmptyRequest) -> LinkRequiredOut:
    """Protocols whose session needs a new pairing QR (no I/O)."""
    return LinkRequiredOut(protocols=api.link_required())


@operation("link.start", request=LinkStartRequest, response=LinkStartOut)
def link_start(api: ChatAPI, req: LinkStartRequest) -> LinkStartOut:
    """Begin linking a protocol and return the QR payload to show.

    Blocks: starts ``signal-cli link`` / asks WAHA / starts the Telegram QR
    login.  ``backend_down`` when the protocol is not configured,
    ``backend_error`` (user-facing message) when the flow cannot start.
    """
    with _link_errors():
        qr = api.link.start(req.protocol, req.phone, req.device_name)
    return LinkStartOut.model_validate({"qr": qr})


@operation("link.poll", request=LinkProtocolRequest, response=LinkPollOut)
def link_poll(api: ChatAPI, req: LinkProtocolRequest) -> LinkPollOut:
    """Progress of a started link (call every ~2 s); may carry a renewed QR.

    ``conflict`` when no link is in progress for the protocol.
    """
    with _link_errors():
        return LinkPollOut.model_validate(api.link.poll(req.protocol))


@operation("link.finalize", request=LinkFinalizeRequest, response=LinkFinalizeOut)
def link_finalize(api: ChatAPI, req: LinkFinalizeRequest) -> LinkFinalizeOut:
    """Signal, after ``link.poll`` said ``finalize``: save and activate the new
    account.  Blocks (``signal-cli listAccounts``)."""
    with _link_errors():
        return LinkFinalizeOut(account=api.link.finalize_signal(req.phone))


@operation("link.submit_2fa", request=Link2faRequest, response=Link2faOut)
def link_submit_2fa(api: ChatAPI, req: Link2faRequest) -> Link2faOut:
    """Telegram: finish the login with the 2FA password (``ok`` false = wrong)."""
    with _link_errors():
        return Link2faOut(ok=api.link.submit_2fa(req.password))


@operation("link.cancel", request=LinkProtocolRequest, response=OkOut)
def link_cancel(api: ChatAPI, req: LinkProtocolRequest) -> OkOut:
    """Abandon a link in progress (kills the ``signal-cli link`` subprocess)."""
    api.link.cancel(req.protocol)
    return OkOut(ok=True)


@operation("link.reconnect", request=LinkReconnectRequest, response=OkOut)
def link_reconnect(api: ChatAPI, req: LinkReconnectRequest) -> OkOut:
    """Reconnect the backends whose link flow was started (non-blocking: the
    connect workers run on core threads)."""
    api.reconnect(set(req.protocols))
    return OkOut(ok=True)
