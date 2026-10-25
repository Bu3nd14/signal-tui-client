"""A real ``ChatClient`` over a fake core, for tests of frontend code paths."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from models import ChatContact
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


class StubClient:
    """Records ``call`` and answers from a script (value or exception)."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, dict]] = []
        #: Timeout passed with each call, parallel to ``calls``.
        self.timeouts: list[float | None] = []

    def call(
        self, name: str, data: dict | None = None, *, timeout: float | None = None
    ) -> Any:
        self.calls.append((name, data or {}))
        self.timeouts.append(timeout)
        answer = self.answers.pop(0) if self.answers else {"ok": True}
        if isinstance(answer, BaseException):
            raise answer
        return answer


def view_app(client: Any, protocol: str = "signal", cid: str = "+39123"):
    """A ``SignalTUI`` whose DOM-touching parts are stubbed, talking to *client*."""
    from signal_tui import SignalTUI

    app = SignalTUI()
    app.client = client  # type: ignore[assignment]
    contact = ChatContact(id=cid, display_name="Mario", protocol=protocol)
    app.contacts = [contact]
    app.selected_contact = contact
    app._is_completion_visible = MagicMock(return_value=False)
    app.query_one = MagicMock(side_effect=Exception("no DOM in test"))
    app._add_message = MagicMock()
    app._cancel_reply = MagicMock()
    app._status = MagicMock()
    app._update_message_widgets_status = MagicMock()
    app.call_from_thread = MagicMock(side_effect=lambda fn, *a, **k: fn(*a, **k))
    app.run_worker = MagicMock()
    app._reply_to = None
    app._cache = {}
    return app, contact


def run_workers(app) -> None:
    """Run every scheduled ``run_worker`` callable inline."""
    for call in app.run_worker.call_args_list:
        call.args[0]()


def api_for(manager: Any, bus: Any = None) -> ChatAPI:
    """A real ``ChatAPI`` over a fake core around *manager* (see ``client_for``).

    The web adapter only ever receives an API; tests that build a FastAPI app
    or start the web server give it this instead of a manager.  Pass an
    ``EventBus`` to observe the frames operations publish.
    """
    core = SimpleNamespace(manager=manager, bus=bus)
    core.api = ChatAPI(core)
    return core.api


def inline_client_calls(app: Any, *mixins: type) -> Any:
    """Give a stand-in app the thread helpers of ``ClientCallsMixin`` (inline).

    ``_offload``/``_ui`` run their callable at once, ``_call_op`` calls
    ``app.client`` with the configured timeout and transport errors are not
    reported.  Methods of *mixins* the app does not define are bound to it, so
    ``SimpleNamespace`` stand-ins can run code that hands work to a worker.
    """
    from types import MethodType

    from tui.client_timeouts import call_op

    defaults: dict[str, Any] = {
        "is_running": False,
        "_offload": lambda work: work(),
        "_ui": lambda fn, *a: fn(*a),
        "_call_op": lambda op, data=None: call_op(app.client, op, data),
        "_report_client_error": lambda exc, what="": False,
    }
    for name, value in defaults.items():
        if not hasattr(app, name):
            setattr(app, name, value)
    for mixin in mixins:
        for name, fn in vars(mixin).items():
            if callable(fn) and not hasattr(app, name):
                setattr(app, name, MethodType(fn, app))
    return app
