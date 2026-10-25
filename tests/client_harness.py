"""A real ``ChatClient`` over a fake core, for tests of frontend code paths."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from server.api import ChatAPI
from server.client import InProcessClient


def client_for(manager: Any, bus: Any = None) -> InProcessClient:
    """``InProcessClient`` whose operations run against *manager*.

    The core is a stand-in with just ``api``, ``manager`` and *bus* (none by
    default): enough for operations that only read the manager (blobs,
    lookups); pass an ``EventBus`` to observe the frames operations publish.
    """
    core = SimpleNamespace(manager=manager, bus=bus)
    core.api = ChatAPI(core)
    return InProcessClient(core)  # type: ignore[arg-type]
