"""Attach-or-spawn decision tree (``server/attach.py``) with fakes, and the
single-instance lock it relies on."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from server import attach, lock
from server.paths import default_socket_path, runtime_file_path
from server.socket_client import IncompatibleServer, ServerUnavailable


class FakeProc:
    def __init__(self, pid: int = 4242, exits_after: int | None = None, rc: int = 1):
        self.pid = pid
        self.returncode: int | None = None
        self._polls = 0
        self._exits_after = exits_after
        self._rc = rc
        self.signals: list[int] = []
        self.killed = False

    def poll(self):
        self._polls += 1
        if self._exits_after is not None and self._polls > self._exits_after:
            self.returncode = self._rc
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)
        self.returncode = -sig

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True


class Env:
    """Fakes for ``SocketClient.connect`` and the spawner."""

    def __init__(self, monkeypatch, tmp_path: Path):
        self.connects: list[Path] = []
        self.live_after: int | None = None  # connects before one succeeds
        self.error: Exception | None = None
        self.spawned: list[tuple[list[str], Path]] = []
        self.proc = FakeProc()
        self.log = tmp_path / "serve.log"
        self.log.write_bytes(b"old run\n")
        self.client = SimpleNamespace(name="client")
        monkeypatch.setattr(attach.SocketClient, "connect", self._connect)
        monkeypatch.setattr(attach.time, "sleep", lambda _s: None)

    def _connect(self, path, *, timeout=5.0, **_kw):
        self.connects.append(Path(path))
        if self.error is not None:
            raise self.error
        if self.live_after is not None and len(self.connects) > self.live_after:
            return self.client
        raise ServerUnavailable("nobody home")

    def spawn(self, serve_args, path, log_path):
        self.spawned.append((serve_args, path))
        return self.proc, self.log.stat().st_size

    def run(self, **kw):
        return attach.attach_or_spawn(
            log_path=self.log, spawn=self.spawn, spawn_timeout=5.0, **kw
        )


@pytest.fixture
def env(monkeypatch, tmp_path):
    return Env(monkeypatch, tmp_path)


def test_connect_path_is_used_and_never_spawns(env, tmp_path):
    env.live_after = 0
    res = env.run(connect_path=tmp_path / "x.sock")
    assert res.client is env.client and res.spawned is False
    assert env.connects == [tmp_path / "x.sock"]
    assert env.spawned == []


def test_connect_path_without_server_fails_without_spawning(env, tmp_path):
    with pytest.raises(attach.AttachError, match="x.sock"):
        env.run(connect_path=tmp_path / "x.sock")
    assert env.spawned == []


def test_attaches_to_a_live_server_on_the_default_path(env):
    env.live_after = 0
    res = env.run(serve_args=["--no-web"])
    assert res.spawned is False and res.path == default_socket_path()
    assert env.spawned == []


def test_spawns_when_absent_and_connects_when_the_socket_answers(env):
    env.live_after = 4
    res = env.run(serve_args=["--no-web"])
    assert res.spawned is True and res.client is env.client
    assert env.spawned == [(["--no-web"], default_socket_path())]
    assert env.proc.signals == []  # the server we started is left running


def test_spawn_failure_shows_the_log_tail_and_does_not_hide_the_exit(env):
    env.proc = FakeProc(exits_after=2, rc=3)

    def spawn(serve_args, path, log_path):
        offset = env.log.stat().st_size
        with open(env.log, "ab") as f:
            f.write(b"line1\nboom: cannot bind\n")
        return env.proc, offset

    with pytest.raises(attach.AttachError) as exc:
        attach.attach_or_spawn(log_path=env.log, spawn=spawn, spawn_timeout=5.0)
    msg = str(exc.value)
    assert "exited with code 3" in msg and "boom: cannot bind" in msg
    assert "old run" not in msg  # only this run's output


def test_incompatible_server_is_an_error_not_a_reason_to_spawn(env):
    env.error = IncompatibleServer("version 9")
    with pytest.raises(attach.AttachError, match="incompatible"):
        env.run()
    assert env.spawned == []


def test_timeout_stops_the_process_we_spawned(env, monkeypatch):
    clock = iter(x * 2.0 for x in range(1000))
    monkeypatch.setattr(attach.time, "monotonic", lambda: next(clock))
    with pytest.raises(attach.AttachError, match="did not answer"):
        env.run()
    assert env.proc.signals  # SIGTERM sent: no orphan server


def test_ctrl_c_while_waiting_stops_the_process_we_spawned(env, monkeypatch):
    def boom(_s):
        raise KeyboardInterrupt

    monkeypatch.setattr(attach.time, "sleep", boom)
    with pytest.raises(KeyboardInterrupt):
        env.run()
    assert env.proc.signals


def test_loser_of_the_spawn_race_attaches_to_the_winner(env, monkeypatch, tmp_path):
    """Our spawned serve exits (lock held by another TUI's server): keep
    waiting and attach, ``spawned=False``."""
    env.proc = FakeProc(exits_after=1, rc=1)
    lock_file = tmp_path / "held.lock"
    lock_file.write_text(str(os.getppid()))  # a live process holds it
    monkeypatch.setattr(lock, "LOCK_FILE", str(lock_file))
    env.live_after = 6
    res = env.run()
    assert res.spawned is False and res.client is env.client
    assert len(env.spawned) == 1


def test_answering_server_that_is_not_ours_is_not_spawned(env, tmp_path):
    env.live_after = 2
    path = default_socket_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    runtime_file_path(path).write_text('{"pid": 99999}')
    res = env.run()
    assert res.spawned is False


def test_serve_args_for_web():
    assert attach.serve_args_for_web(web=False, web_port=1) == ["--no-web"]
    assert attach.serve_args_for_web(
        web=True, web_port=4242, web_host="0.0.0.0", web_no_auth=True
    ) == ["--web-port", "4242", "--web-host", "0.0.0.0", "--web-no-auth"]
    assert attach.serve_args_for_web(web=True) == []


def test_spawn_server_command_and_detachment(monkeypatch, tmp_path):
    calls = {}

    class P:
        pid = 1

        def wait(self):
            return 0

    def popen(cmd, **kw):
        calls["cmd"], calls["kw"] = cmd, kw
        return P()

    monkeypatch.setattr(subprocess, "Popen", popen)
    log = tmp_path / "log"
    log.write_bytes(b"abc")
    _proc, offset = attach.spawn_server(["--no-web"], Path("/x/s.sock"), log)
    assert offset == 3
    assert calls["cmd"][0] == sys.executable
    assert calls["cmd"][1].endswith("megachat.py")
    assert calls["cmd"][2:] == ["serve", "--socket", "/x/s.sock", "--no-web"]
    assert calls["kw"]["start_new_session"] is True
    assert calls["kw"]["stdin"] == subprocess.DEVNULL


# -- the lock -------------------------------------------------------------------


_CONTENDER = """
import os, sys, time
from server import lock
lock.LOCK_FILE = sys.argv[1]
while not os.path.exists(sys.argv[2]):
    time.sleep(0.001)
print("WON" if lock._acquire_lock() else "LOST", flush=True)
while not os.path.exists(sys.argv[3]):  # stay alive: the winner's pid must count
    time.sleep(0.05)
"""


def test_lock_env_override(tmp_path):
    root = Path(__file__).resolve().parent.parent
    code = "from server.lock import LOCK_FILE; print(LOCK_FILE)"
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        env={**os.environ, "MEGACHAT_LOCK_FILE": str(tmp_path / "o.lock")},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out == str(tmp_path / "o.lock")


def test_simultaneous_acquire_has_exactly_one_winner(tmp_path):
    root = Path(__file__).resolve().parent.parent
    go, done = tmp_path / "go", tmp_path / "done"
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _CONTENDER,
                str(tmp_path / "r.lock"),
                str(go),
                str(done),
            ],
            cwd=root,
            stdout=subprocess.PIPE,
            text=True,
        )
        for _ in range(8)
    ]
    try:
        time.sleep(1.0)  # let every child reach the start line
        go.touch()
        results = [p.stdout.readline().strip() for p in procs]  # type: ignore[union-attr]
    finally:
        done.touch()
        for p in procs:
            p.wait(timeout=20)
    assert results.count("WON") == 1 and results.count("LOST") == 7


def test_lock_holder(monkeypatch, tmp_path):
    f = tmp_path / "l"
    monkeypatch.setattr(lock, "LOCK_FILE", str(f))
    assert lock.lock_holder() is None
    f.write_text("999999999")
    assert lock.lock_holder() is None
    f.write_text(str(os.getpid()))
    assert lock.lock_holder() == os.getpid()
