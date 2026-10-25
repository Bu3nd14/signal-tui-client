"""Client side of the server API: what a frontend (the TUI) talks to.

A :class:`ChatClient` offers the two things every transport carries:

- *operations*: ``call(name, data)`` / ``acall(name, data)`` take and return
  plain JSON data (the registered request/response models validate both
  ends); failures raise :class:`~server.api.ApiError`;
- *events*: ``subscribe(since)`` returns a :class:`ClientSubscription` whose
  events are :class:`~server.bus.Event` envelopes ``(seq, type, payload, ts)``
  with wire-form payloads (:mod:`server.events_api`).  A ``gap`` event
  (:data:`~server.bus.GAP`) means events were lost and the client must
  resync.

:class:`InProcessClient` is the transport for tests and the embedded TUI; the
Unix-socket client (Phase 2) implements the same two interfaces.  Nothing
transport specific lives outside the concrete client classes.

This module must not import Textual, FastAPI, ``tui`` or ``web``.
"""

from __future__ import annotations

import abc
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from server.api import ApiError
from server.bus import GAP, Event, Subscription

if TYPE_CHECKING:
    from server.core import ChatCore

__all__ = [
    "GAP",
    "ApiError",
    "ChatClient",
    "ClientSubscription",
    "Event",
    "InProcessClient",
]


class ClientSubscription(abc.ABC):
    """A stream of events; same consumer API for every transport."""

    @abc.abstractmethod
    def get(self, timeout: float | None = None) -> Event | None:
        """Block up to *timeout* for the next event; ``None`` on timeout or
        when the subscription is closed."""

    @abc.abstractmethod
    def drain(self) -> list[Event]:
        """Return the events already queued, without blocking."""

    @abc.abstractmethod
    async def aget(self, timeout: float | None = None) -> Event | None:
        """Asyncio flavour of :meth:`get`."""

    @abc.abstractmethod
    def close(self) -> None:
        """Stop the stream and wake blocked getters.  Idempotent."""

    @property
    @abc.abstractmethod
    def closed(self) -> bool: ...

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class ChatClient(abc.ABC):
    """Operations + events of a chat server, over some transport."""

    @abc.abstractmethod
    def call(self, name: str, data: dict[str, Any] | None = None) -> Any:
        """Run operation *name* with JSON request *data*; returns JSON data.

        Raises :class:`ApiError` on a neutral API error.  May block.
        """

    @abc.abstractmethod
    async def acall(self, name: str, data: dict[str, Any] | None = None) -> Any:
        """Like :meth:`call` from a running event loop."""

    @abc.abstractmethod
    def subscribe(self, since: int | None = None) -> ClientSubscription:
        """Start receiving events; with *since*, replay what was missed after
        that ``seq`` (or get a ``gap`` event when it is no longer available)."""

    @abc.abstractmethod
    def close(self) -> None:
        """Release the transport (subscriptions are closed too)."""

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class InProcessSubscription(ClientSubscription):
    """Subscription backed by an :class:`~server.bus.EventBus` subscription."""

    def __init__(self, sub: Subscription) -> None:
        self._sub = sub

    def get(self, timeout: float | None = None) -> Event | None:
        return self._sub.get(timeout)

    def drain(self) -> list[Event]:
        return self._sub.drain()

    async def aget(self, timeout: float | None = None) -> Event | None:
        return await self._sub.aget(timeout)

    def close(self) -> None:
        self._sub.close()

    @property
    def closed(self) -> bool:
        return self._sub.closed


class InProcessClient(ChatClient):
    """Client calling a :class:`~server.core.ChatCore` in the same process.

    Requests and responses go through the operation models (``call_json`` /
    ``acall_json``), so what a socket client would send or receive is what
    this one sees: a non-serializable value fails here too.
    """

    def __init__(self, core: ChatCore) -> None:
        self._core = core
        self._subs: list[InProcessSubscription] = []

    def call(self, name: str, data: dict[str, Any] | None = None) -> Any:
        return self._core.api.call_json(name, data)

    async def acall(self, name: str, data: dict[str, Any] | None = None) -> Any:
        return await self._core.api.acall_json(name, data)

    def subscribe(self, since: int | None = None) -> InProcessSubscription:
        sub = InProcessSubscription(self._core.bus.subscribe(since))
        self._subs = [s for s in self._subs if not s.closed]
        self._subs.append(sub)
        return sub

    def close(self) -> None:
        subs, self._subs = self._subs, []
        for sub in subs:
            sub.close()
