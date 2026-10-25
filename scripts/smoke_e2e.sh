#!/usr/bin/env bash
# End-to-end smoke of the client/server split (serve, socket, attach-or-spawn,
# detach/quit). Run from anywhere; `make smoke` runs it.
#
# Fully isolated: temp HOME, lock file and socket under a short /tmp/mcXXXX
# dir (sun_path limit), no backends configured, free HTTP port. It never
# touches a real install or a running instance. Every process it starts is
# killed on exit, and the run fails if one survives a scenario or if a lock or
# socket remains. Needs `script` (util-linux) to give the TUI a pty.
#
#   PY=/path/to/python scripts/smoke_e2e.sh      # default: .venv/bin/python
#   make smoke                                   # uses $(PYTHON)
set -u
cd "$(dirname "$0")/.." || exit 2
ROOT=$PWD
PY=${PY:-.venv/bin/python}
case $PY in
  /*) ;;
  */*) PY=$ROOT/$PY ;;
  *) PY=$(command -v "$PY" || echo "$PY") ;;  # bare name (CI: `python`) via PATH
esac
[ -x "$PY" ] || { echo "SMOKE FAIL: python not found: $PY"; exit 2; }
command -v script >/dev/null || { echo "SMOKE FAIL: 'script' (util-linux) not found"; exit 2; }
command -v curl >/dev/null || { echo "SMOKE FAIL: curl not found"; exit 2; }

TMP=$(mktemp -d /tmp/mcXXXX)
export HOME=$TMP/home XDG_DATA_HOME=$TMP/home/share
export MEGACHAT_SOCKET=$TMP/s.sock MEGACHAT_LOCK_FILE=$TMP/l
export SIGNAL_ENABLED=0 WHATSAPP_API_PORT=1 TERM=${TERM:-xterm}
unset XDG_RUNTIME_DIR XDG_STATE_HOME
mkdir -p "$HOME"
LOCK=$MEGACHAT_LOCK_FILE SK=$MEGACHAT_SOCKET RT=$TMP/s.json
PORT=$("$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')

# Pids of the processes this run started (python, script, timeout): they all
# carry our MEGACHAT_LOCK_FILE in their environment. Excludes this shell.
strays() {
  local p
  for p in /proc/[0-9]*; do
    p=${p#/proc/}
    [ "$p" = "$$" ] && continue
    case $(cat "/proc/$p/comm" 2>/dev/null) in python*|script|timeout) ;; *) continue ;; esac
    grep -qaF "MEGACHAT_LOCK_FILE=$LOCK" "/proc/$p/environ" 2>/dev/null && echo "$p"
  done
}
cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  local p
  for p in $(strays); do kill -9 "$p" 2>/dev/null; done
  rm -rf "$TMP"
  exit $rc
}
trap cleanup EXIT INT TERM
fail() { echo "SMOKE FAIL: $*"; exit 1; }
wait_gone() { # pid seconds
  local i
  for i in $(seq 1 $(($2 * 4))); do kill -0 "$1" 2>/dev/null || return 0; sleep 0.25; done
  return 1
}
wait_for() { # seconds cmd...
  local n=$1 i; shift
  for i in $(seq 1 $((n * 4))); do "$@" && return 0; sleep 0.25; done
  return 1
}
lockpid() { cat "$LOCK" 2>/dev/null; }
servers() { # python processes running `megachat.py serve` (not shells, not script)
  local p
  for p in $(strays); do
    case $(cat "/proc/$p/comm") in python*) ;; *) continue ;; esac
    tr '\0' ' ' <"/proc/$p/cmdline" | grep -q 'megachat.py serve' && echo "$p"
  done
}
nothing_left() { # $1 = scenario name; no process, lock, socket or runtime file
  wait_for 10 test -z "$(strays)" || fail "$1: processes left: $(strays | tr '\n' ' ')"
  [ -e "$LOCK" ] && fail "$1: lock left behind"
  [ -e "$SK" ] && fail "$1: socket left behind"
  [ -e "$RT" ] && fail "$1: runtime file left behind"
  return 0
}
tui() { # out secs [args...]  -- megachat in a pty, SIGINT after secs
  local out=$1 secs=$2; shift 2
  script -qefc "timeout -s INT $secs $PY megachat.py $*" /dev/null </dev/null >"$out" 2>&1
}

echo "== serve (HTTP, /ws, /ws/v2, socket, SIGINT)"
"$PY" megachat.py serve --web-no-auth --web-host 127.0.0.1 --web-port "$PORT" >"$TMP/serve.out" 2>&1 &
SPID=$!
wait_for 30 curl -fs "http://127.0.0.1:$PORT/health" -o /dev/null || { cat "$TMP/serve.out"; fail "serve did not come up"; }
curl -fsS "http://127.0.0.1:$PORT/health" | grep -q '"status":"ok"' || fail "/health"
curl -fsS "http://127.0.0.1:$PORT/api/status" >/dev/null || fail "/api/status"
curl -fsS "http://127.0.0.1:$PORT/api/contacts" |
  "$PY" -c 'import json,sys; assert isinstance(json.load(sys.stdin), list)' || fail "/api/contacts"
"$PY" scripts/smoke_socket.py ws "$PORT" || fail "websockets"
[ "$(lockpid)" = "$SPID" ] || fail "serve does not hold the lock (lock=$(lockpid) pid=$SPID)"
wait_for 10 test -S "$SK" || fail "no socket"
"$PY" scripts/smoke_socket.py socket "$SK" || fail "socket adapter"
kill -INT "$SPID"
wait "$SPID"; RC=$?
[ $RC -eq 0 ] || { cat "$TMP/serve.out"; fail "serve exit code $RC on SIGINT"; }
nothing_left serve
echo "serve OK"

echo "== TUI spawn (no server running)"
tui "$TMP/tui1.out" 10
SRV=$(lockpid)
[ -n "$SRV" ] && kill -0 "$SRV" 2>/dev/null || { tail -5 "$TMP/tui1.out"; fail "no server holds the lock after the TUI spawned one"; }
tr '\0' ' ' <"/proc/$SRV/cmdline" | grep -q 'megachat.py serve' || fail "lock holder is not 'megachat serve'"
"$PY" scripts/smoke_socket.py socket "$SK" >/dev/null || fail "spawned server socket"
echo "spawned server pid $SRV survives the TUI's Ctrl+C (detach)"

echo "== TUI attach (server running)"
tui "$TMP/tui2.out" 8
[ "$(lockpid)" = "$SRV" ] || fail "attach changed the lock owner"
[ "$(servers | wc -l)" = 1 ] || fail "expected exactly one server: $(servers | tr '\n' ' ')"
echo "attached, still exactly one server"

echo "== --in-process refused while the server holds the lock"
tui "$TMP/tui3.out" 8 --in-process
grep -qi "already running\|lock" "$TMP/tui3.out" || fail "--in-process did not refuse: $(tail -3 "$TMP/tui3.out")"
[ "$(lockpid)" = "$SRV" ] || fail "--in-process disturbed the lock"
echo "refused"

echo "== server.shutdown over the socket (the Quit path)"
"$PY" scripts/smoke_socket.py shutdown || fail "server.shutdown call"
wait_gone "$SRV" 30 || fail "server did not stop after server.shutdown"
nothing_left shutdown
echo "shutdown OK"

echo "== --in-process TUI alone"
tui "$TMP/tui4.out" 8 --in-process
nothing_left in-process
echo "in-process OK"

echo "== SIGINT delivered to a non-main thread of the TUI"
for run in 1 2; do
  script -qefc "$PY megachat.py" /dev/null </dev/null >"$TMP/thr.out" 2>&1 &
  SCP=$!
  TPID=
  for _ in $(seq 1 60); do # wait until the TUI (not the server) runs with extra threads
    for p in $(strays); do
      case $(cat "/proc/$p/comm") in python*) ;; *) continue ;; esac
      tr '\0' ' ' <"/proc/$p/cmdline" | grep -q 'megachat.py *$' || continue
      [ "$(ls "/proc/$p/task" | wc -l)" -gt 2 ] && TPID=$p
    done
    [ -n "$TPID" ] && break
    sleep 0.5
  done
  [ -n "$TPID" ] || fail "thread-SIGINT run $run: TUI did not start"
  sleep 2
  TID=$(ls "/proc/$TPID/task" | grep -v "^$TPID\$" | tail -1)
  "$PY" -c 'import ctypes, signal, sys; r = ctypes.CDLL(None, use_errno=True).syscall(234, int(sys.argv[1]), int(sys.argv[2]), int(signal.SIGINT)); sys.exit(r != 0)' "$TPID" "$TID" || fail "tgkill failed"
  wait_gone "$TPID" 15 || fail "thread-SIGINT run $run: TUI (pid $TPID) ignored SIGINT sent to thread $TID"
  wait "$SCP" 2>/dev/null
  echo "run $run: TUI exited after SIGINT to thread $TID"
done
# The TUI spawned a server (it survives a detach): stop it like Quit does.
if [ -n "$(servers)" ]; then
  "$PY" scripts/smoke_socket.py shutdown >/dev/null || fail "final server.shutdown"
  wait_gone "$(lockpid)" 30 || true
fi
nothing_left thread-sigint
echo "thread-SIGINT OK"

echo "SMOKE PASS"
