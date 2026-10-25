"""Helper of ``scripts/smoke_e2e.sh``: probe a running ``megachat serve``.

Usage (from the repository root, with the isolated env of the smoke script)::

    smoke_socket.py socket PATH   # JSON-RPC over the Unix socket
    smoke_socket.py ws PORT       # /ws heartbeat and /ws/v2 subscribe frame
    smoke_socket.py shutdown      # server.clients + server.shutdown (Quit path)

Exits non-zero (assertion / exception) on any mismatch.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def check_socket(path: str) -> None:
    mode = stat.S_IMODE(os.stat(path).st_mode)
    dmode = stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode)
    assert mode == 0o600, f"socket mode {oct(mode)}"
    # The directory is only ours to tighten when the server created it; the
    # smoke script puts the socket in its private 0700 temp dir.
    assert dmode == 0o700, f"socket dir mode {oct(dmode)}"
    runtime = os.path.splitext(path)[0] + ".json"  # server/paths.runtime_file_path
    with open(runtime) as fh:
        info = json.load(fh)
    assert info["socket"] == path and info["pid"] > 0, info

    s = socket.socket(socket.AF_UNIX)
    s.settimeout(15)
    s.connect(path)
    f = s.makefile("rwb")

    def call(i: int, method: str, params: dict | None = None) -> dict:
        msg: dict = {"jsonrpc": "2.0", "id": i, "method": method}
        if params is not None:
            msg["params"] = params
        f.write((json.dumps(msg) + "\n").encode())
        f.flush()
        while True:
            r = json.loads(f.readline())
            if r.get("id") == i:
                return r

    hello = call(1, "rpc.hello")["result"]
    assert "contacts.list" in hello["operations"], hello
    st = call(2, "server.status")
    assert "result" in st, st
    bad = call(3, "messages.page", {"protocol": "nope", "contact_id": "x"})
    assert bad["error"]["code"] == -32602, bad
    unk = call(4, "no.such.method")
    assert unk["error"]["code"] == -32601, unk
    sub = call(5, "events.subscribe", {})
    assert "result" in sub, sub
    t0 = time.time()
    got = json.loads(f.readline())
    assert got.get("method") in {"event", "heartbeat"}, got
    print(
        f"socket OK: ops={len(hello['operations'])} "
        f"first-notification={got['method']} after {time.time() - t0:.1f}s"
    )


async def _check_ws(port: str) -> None:
    import websockets

    async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
        msg = json.loads(await asyncio.wait_for(ws.recv(), 12))
        assert msg.get("type") == "heartbeat", msg
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws/v2") as ws:
        await ws.send(json.dumps({"type": "subscribe", "since": 0}))
        msg = json.loads(await asyncio.wait_for(ws.recv(), 12))
        assert msg.get("type") == "heartbeat" or "seq" in msg, msg
    print("ws OK: /ws heartbeat, /ws/v2 subscribe")


def do_shutdown() -> None:
    from server.socket_client import SocketClient

    c = SocketClient.connect(timeout=10)
    try:
        print("clients:", c.call("server.clients"))
        print("shutdown:", c.call("server.shutdown"))
    finally:
        c.close()


def main(argv: list[str]) -> None:
    if len(argv) == 2 and argv[0] == "socket":
        check_socket(argv[1])
    elif len(argv) == 2 and argv[0] == "ws":
        asyncio.run(_check_ws(argv[1]))
    elif argv == ["shutdown"]:
        do_shutdown()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
