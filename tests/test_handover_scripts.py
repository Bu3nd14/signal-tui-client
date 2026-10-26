"""scripts/start_on_server.sh and scripts/tui_handover.sh: they start/stop
the detached ``megachat serve`` (never a tmux-hosted TUI).

The scripts run as subprocesses in a temporary project copy with stub
``ssh``/``tmux``/``docker``/``pkill``/``pgrep`` on PATH, ``MEGACHAT_LOCK_FILE``
pointing into tmp_path and HOME isolated.  Starting a real server is not
covered here (the scripts probe the fixed port 4242): the Python launcher
tests and ``scripts/smoke_e2e.sh`` cover that.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ("start_on_server.sh", "tui_handover.sh")


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def env(tmp_path):
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    for name in SCRIPTS:
        (project / "scripts" / name).write_text(
            (ROOT / "scripts" / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    # the local part of the handover needs a git checkout marker
    (project / ".git").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.log"
    for tool in ("tmux", "docker", "pkill"):
        _stub(bin_dir / tool, f'echo "{tool} $*" >> {str(calls)!r}\n')
    _stub(bin_dir / "pgrep", "exit 1\n")
    _stub(
        bin_dir / "ssh",
        f'echo "ssh $*" >> {str(calls)!r}\ncat > {str(tmp_path / "ssh.stdin")!r}\n'
        "echo OK_TUI_SERVER\n",
    )
    home = tmp_path / "home"
    home.mkdir()
    return {
        "project": project,
        "calls": calls,
        "lock": tmp_path / "signal-tui.lock",
        "ssh_stdin": tmp_path / "ssh.stdin",
        "env": {
            **os.environ,
            "HOME": str(home),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "MEGACHAT_LOCK_FILE": str(tmp_path / "signal-tui.lock"),
            "SIGNAL_TUI_HANDOVER_CONF": str(tmp_path / "none.conf"),
        },
    }


def _run(env, script, *args):
    return subprocess.run(
        ["bash", str(env["project"] / "scripts" / script), *args],
        capture_output=True,
        text=True,
        env=env["env"],
        timeout=60,
        check=False,
    )


def _spawn_fake_server(lock: Path) -> subprocess.Popen:
    """A process that holds the lock and exits cleanly (removing it) on SIGINT."""
    code = (
        "import os,signal,sys,time\n"
        "lock=sys.argv[1]\n"
        "def stop(*_):\n"
        "    os.remove(lock); sys.exit(0)\n"
        "signal.signal(signal.SIGINT, stop)\n"
        "open(lock,'w').write(str(os.getpid()))\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(lock)], stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"
    proc.stdout.close()  # the child prints nothing else
    # Reap it as soon as it exits: a zombie would still answer ``kill -0``,
    # unlike the real server (reparented to init).
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc


@pytest.mark.parametrize("name", SCRIPTS)
def test_syntax_and_no_tmux_new_session(name):
    path = ROOT / "scripts" / name
    assert subprocess.run(["bash", "-n", str(path)], check=False).returncode == 0
    text = path.read_text(encoding="utf-8")
    assert "tmux new-session" not in text
    assert "signal_tui --web" not in text
    assert "megachat.py serve --web-port 4242 --web-host 0.0.0.0" in text
    assert "set -euo pipefail" in text


def test_remote_heredoc_is_valid_bash():
    text = (ROOT / "scripts" / "tui_handover.sh").read_text(encoding="utf-8")
    parts = text.split("<<'REMOTE'\n")[1:]
    assert len(parts) == 3  # status, to-server, to-local
    for part in parts:
        body = part.split("\nREMOTE\n")[0]
        result = subprocess.run(
            ["bash", "-n"], input=body, text=True, capture_output=True, check=False
        )
        assert result.returncode == 0, result.stderr
        assert "tmux new-session" not in body
        rms = [ln.strip() for ln in body.splitlines() if "rm " in ln]
        assert all(r == 'rm -f "$LOCK"' for r in rms)


@pytest.mark.parametrize(
    "name,args", [("start_on_server.sh", ["status"]), ("tui_handover.sh", ["status"])]
)
def test_status_without_server(env, name, args):
    result = _run(env, name, *args)
    assert result.returncode == 0, result.stderr
    assert "Server: spento" in result.stdout
    assert "TUI:" not in result.stdout


def test_status_with_live_lock_holder_and_stale_lock(env):
    env["lock"].write_text(str(os.getpid()), encoding="utf-8")
    out = _run(env, "start_on_server.sh", "status").stdout
    assert f"Server: ATTIVO (pid {os.getpid()})" in out
    env["lock"].write_text("999999999", encoding="utf-8")  # dead pid
    assert "Server: spento" in _run(env, "start_on_server.sh", "status").stdout


def test_start_on_server_stop_sigints_waits_and_clears_lock(env):
    proc = _spawn_fake_server(env["lock"])
    try:
        result = _run(env, "start_on_server.sh", "stop")
        assert result.returncode == 0, result.stderr
        assert proc.wait(timeout=5) == 0  # got SIGINT and exited
        assert not env["lock"].exists()
        calls = env["calls"].read_text()
        assert "tmux kill-session -t tui" in calls  # legacy cleanup kept
        assert "tmux new-session" not in calls
        assert "docker compose" in calls
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGKILL)


def test_start_on_server_stop_without_server_removes_only_the_lock(env, tmp_path):
    bystander = tmp_path / "bystander"
    bystander.write_text("keep", encoding="utf-8")
    env["lock"].write_text("999999999", encoding="utf-8")
    assert _run(env, "start_on_server.sh", "stop").returncode == 0
    assert not env["lock"].exists()
    assert bystander.exists()


def test_to_server_stops_local_then_sends_serve_script_over_ssh(env):
    proc = _spawn_fake_server(env["lock"])
    try:
        result = _run(env, "tui_handover.sh", "to-server")
        assert result.returncode == 0, result.stdout + result.stderr
        assert proc.wait(timeout=5) == 0
        assert not env["lock"].exists()
        assert "Handover verso il server completato" in result.stdout
        assert "megachat.py" in result.stdout
        sent = env["ssh_stdin"].read_text()
        assert "megachat.py serve --web-port 4242 --web-host 0.0.0.0" in sent
        assert "tmux new-session" not in sent
        assert "OK_TUI_SERVER" in sent
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGKILL)


def test_to_server_fails_when_remote_does_not_report_ok(env, tmp_path):
    _stub(tmp_path / "bin" / "ssh", "cat >/dev/null\necho FAIL_TUI_SERVER\n")
    result = _run(env, "tui_handover.sh", "to-server")
    assert result.returncode != 0
    assert "Handover verso il server completato" not in result.stdout


def test_to_local_aborts_when_remote_stop_fails(env, tmp_path):
    _stub(tmp_path / "bin" / "ssh", "cat >/dev/null\nexit 3\n")
    result = _run(env, "tui_handover.sh", "to-local")
    assert result.returncode != 0
    assert "Handover verso il locale completato" not in result.stdout
    # nothing started locally: no WAHA start attempt reached docker/tmux
    assert "tmux new-session" not in (
        env["calls"].read_text() if env["calls"].exists() else ""
    )


def test_unknown_option_and_usage(env):
    assert _run(env, "start_on_server.sh", "status", "--bogus").returncode != 0
    assert _run(env, "tui_handover.sh", "bogus").returncode != 0
