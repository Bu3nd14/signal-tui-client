"""The TUI over a real ``SocketClient`` against a real adapter and core pieces.

Real Signal backend + ``BackendManager`` + temp SQLite behind the Unix-socket
adapter (network send stubbed); the app runs headless with ``run_test``.  The
server is stopped and restarted mid-test.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import protocols.db as backend_db
import server.socket_adapter as sa
from models import PROTOCOL_SIGNAL, ChatContact
from protocols import BackendManager, SignalBackend
from server.api import ChatAPI
from server.bus import EventBus
from server.socket_client import SocketClient
from tui.client_calls import MSG_RESTORED, MSG_RESYNCED, MSG_UNAVAILABLE

MARIO = "+391234567890"
LUIGI = "+390987654321"


class Server:
    """Core pieces (backend, manager, API, bus) behind a socket adapter."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.backend = SignalBackend()
        self.backend._use_daemon = True  # "connected"
        for cid, name in ((MARIO, "Mario"), (LUIGI, "Luigi")):
            self.backend.register_contact(
                ChatContact(id=cid, display_name=name, protocol=PROTOCOL_SIGNAL)
            )
        self.backend.cache[MARIO] = [
            {
                "id": "m1",
                "text": "ciao",
                "timestamp": 1_000,
                "is_mine": False,
                "read": False,
                "sender": MARIO,
                "status": "delivered",
            }
        ]
        self.sent: list[tuple[str, str]] = []
        self.backend.send_message_sync = self._send
        self.manager = BackendManager()
        self.manager.register(self.backend)
        self.bus = EventBus()
        self.core = SimpleNamespace(manager=self.manager, bus=self.bus)
        self.core.api = ChatAPI(self.core)
        self.handle = sa.start(self.core.api, self.bus, path)

    def _send(self, contact_id, text, **_kwargs):
        self.sent.append((contact_id, text))
        return f"srv-{len(self.sent)}"

    def stop(self) -> None:
        sa.stop(self.handle)

    def start(self) -> None:
        self.bus = EventBus()  # a restarted server: new sequence numbers
        self.core.bus = self.bus
        self.handle = sa.start(self.core.api, self.bus, self.path)


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(backend_db, "DB_FILE", tmp_path / "messages.db")
    monkeypatch.setattr(backend_db, "CACHE_DIR", tmp_path)
    short = Path(tempfile.mkdtemp(prefix="mc"))
    path = short / "s.sock"
    monkeypatch.setenv("MEGACHAT_SOCKET", str(path))
    srv = Server(path)
    yield srv
    srv.stop()
    shutil.rmtree(short, ignore_errors=True)


@pytest.fixture
def app(server):
    from tui.app import SignalTUI

    client = SocketClient.connect(
        server.path, backoff=(0.05, 0.2), heartbeat_timeout=3, ping_interval=1
    )
    with (
        patch("server.core.BackendManager"),
        patch("server.core.SignalBackend"),
        patch("server.core.whatsapp_enabled", return_value=False),
        patch("server.core.telegram_enabled", return_value=False),
    ):
        tui = SignalTUI(client=client)
    statuses: list[str] = []
    original = tui._status

    def recording(text, duration=3.0):
        statuses.append(text)
        original(text, duration)

    tui._status = recording
    tui.statuses = statuses
    yield tui
    client.close()


async def _until(pilot, predicate, seconds=8.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        await pilot.pause(0.05)
    return predicate()


def _bubbles(app):
    return [
        getattr(child, "_msg_text", None)
        for child in app.chat_log.children
        if getattr(child, "_msg_text", None) is not None
    ]


@pytest.mark.integration
async def test_tui_over_the_socket_loads_sends_and_survives_a_restart(server, app):
    async with app.run_test() as pilot:
        # Attach: no backend_ready event will come, so the TUI syncs itself.
        assert await _until(pilot, lambda: len(app.contacts) == 2)
        assert {c.id for c in app.contacts} == {MARIO, LUIGI}

        # Open a chat: its message is shown once.
        mario = next(c for c in app.contacts if c.id == MARIO)
        app._select_contact(mario)
        assert await _until(pilot, lambda: "ciao" in _bubbles(app))
        assert _bubbles(app).count("ciao") == 1

        # Send a message through the socket.
        input_box = app.query_one("#message-input")
        input_box.focus()
        await pilot.press(*"hello", "enter")
        assert await _until(pilot, lambda: "hello" in _bubbles(app))
        assert await _until(pilot, lambda: server.sent == [(MARIO, "hello")])
        assert _bubbles(app).count("hello") == 1

        # The server goes away: status message, no crash, no call spam.
        server.stop()
        assert await _until(pilot, lambda: MSG_UNAVAILABLE in app.statuses)
        assert app._server_lost is True
        # A message arrives while the TUI is cut off; the server restarts.
        server.backend.cache[MARIO].append(
            {
                "id": "m2",
                "text": "sei li?",
                "timestamp": 2_000_000_000_000,
                "is_mine": False,
                "read": False,
                "sender": MARIO,
                "status": "delivered",
            }
        )
        server.start()

        # Reconnected and resynced: the missed message appears, nothing doubles.
        assert await _until(pilot, lambda: MSG_RESTORED in app.statuses)
        assert await _until(pilot, lambda: MSG_RESYNCED in app.statuses)
        assert await _until(pilot, lambda: "sei li?" in _bubbles(app))
        assert app._server_lost is False
        assert len(app.contacts) == 2
        bubbles = _bubbles(app)
        assert bubbles.count("ciao") == 1
        assert bubbles.count("hello") == 1
        assert bubbles.count("sei li?") == 1
        assert app._unread_counts.get(mario.cache_key, 0) == 0  # the open chat

        # Still usable after the restart.
        input_box.focus()
        await pilot.press(*"again", "enter")
        assert await _until(pilot, lambda: server.sent[-1:] == [(MARIO, "again")])
        # Leave cleanly: the poll worker ends when the client closes.
        app._polling_active = False
        app.client.close()
